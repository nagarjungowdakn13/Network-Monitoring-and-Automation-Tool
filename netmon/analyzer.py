"""Anomaly detection.

Two complementary strategies, layered:

1. **Hard thresholds** — fast, predictable, configured by the operator.
   "Don't tell me about clever stats; if connections > 5000, page me."

2. **Statistical (z-score over a rolling window)** — catches *unusual-for-this-host*
   spikes that a static threshold would miss. We keep the last N samples per
   metric in a deque and flag the current value if it sits more than `zscore_threshold`
   stdevs above the rolling mean. We don't fire below `min_samples_for_stats`
   because the mean/stdev are unstable while the window is filling.

Tradeoffs to be aware of:
- Z-score assumes roughly normal-ish values. Real network traffic is bursty
  and lognormal-ish, so we measure on the raw rate but only alert on
  *upward* deviations (outbound DDoS-like behaviour, runaway connections).
- A long window is more stable but slower to adapt; short windows are jumpy.
  30 samples × 2 s = 1 minute of context is a reasonable starting point.
- We deliberately do not learn across restarts. Persistent baselines belong
  in a real TSDB (Prometheus/InfluxDB), not in this tool.
"""

from __future__ import annotations

import math
import statistics
from collections import deque
from dataclasses import dataclass
from enum import Enum
from typing import Deque

from netmon.config import ThresholdCfg
from netmon.monitor import Sample


class Severity(str, Enum):
    INFO = "info"
    WARNING = "warning"
    CRITICAL = "critical"


@dataclass
class Anomaly:
    key: str            # stable identifier; used for cooldown deduping
    severity: Severity
    metric: str
    value: float
    threshold: float | None
    message: str
    sample_ts: float


class Analyzer:
    def __init__(self, cfg: ThresholdCfg, window_size: int):
        self.cfg = cfg
        self._window: dict[str, Deque[float]] = {
            "mbps_in":  deque(maxlen=window_size),
            "mbps_out": deque(maxlen=window_size),
            "conns":    deque(maxlen=window_size),
        }

    def analyze(self, s: Sample) -> list[Anomaly]:
        anomalies: list[Anomaly] = []

        # Skip the warm-up sample (interval=0 → rates are zero by construction).
        if s.interval <= 0:
            return anomalies

        self._window["mbps_in"].append(s.mbps_in)
        self._window["mbps_out"].append(s.mbps_out)
        self._window["conns"].append(float(s.connections_total))

        # --- Hard thresholds ---
        if s.mbps_in > self.cfg.bandwidth_mbps_in:
            anomalies.append(Anomaly(
                key="threshold:mbps_in", severity=Severity.WARNING,
                metric="mbps_in", value=s.mbps_in,
                threshold=self.cfg.bandwidth_mbps_in,
                message=f"Inbound bandwidth {s.mbps_in:.2f} Mbps exceeds threshold "
                        f"{self.cfg.bandwidth_mbps_in:.2f} Mbps",
                sample_ts=s.ts,
            ))
        if s.mbps_out > self.cfg.bandwidth_mbps_out:
            anomalies.append(Anomaly(
                key="threshold:mbps_out", severity=Severity.WARNING,
                metric="mbps_out", value=s.mbps_out,
                threshold=self.cfg.bandwidth_mbps_out,
                message=f"Outbound bandwidth {s.mbps_out:.2f} Mbps exceeds threshold "
                        f"{self.cfg.bandwidth_mbps_out:.2f} Mbps",
                sample_ts=s.ts,
            ))
        if s.connections_total > self.cfg.connections_total:
            anomalies.append(Anomaly(
                key="threshold:connections", severity=Severity.WARNING,
                metric="connections_total", value=float(s.connections_total),
                threshold=float(self.cfg.connections_total),
                message=f"Open connections {s.connections_total} exceeds threshold "
                        f"{self.cfg.connections_total}",
                sample_ts=s.ts,
            ))

        # --- Packet error/drop rate ---
        if s.total_packets_window > 0:
            err_rate = s.total_err_drop / s.total_packets_window
            if err_rate > self.cfg.packet_error_rate:
                anomalies.append(Anomaly(
                    key="threshold:packet_err_rate", severity=Severity.CRITICAL,
                    metric="packet_error_rate", value=err_rate,
                    threshold=self.cfg.packet_error_rate,
                    message=f"Packet error/drop rate {err_rate:.2%} exceeds "
                            f"{self.cfg.packet_error_rate:.2%} - possible link issue",
                    sample_ts=s.ts,
                ))

        # --- Statistical spikes (one-sided, upward) ---
        for label, metric_name, value in (
            ("mbps_in",  "mbps_in",            s.mbps_in),
            ("mbps_out", "mbps_out",           s.mbps_out),
            ("conns",    "connections_total",  float(s.connections_total)),
        ):
            z = self._zscore(label, value)
            if z is not None and z > self.cfg.zscore_threshold:
                anomalies.append(Anomaly(
                    key=f"zscore:{label}", severity=Severity.WARNING,
                    metric=metric_name, value=value, threshold=None,
                    message=f"Spike on {metric_name}: value={value:.2f} "
                            f"is {z:.1f} stdevs above rolling mean",
                    sample_ts=s.ts,
                ))

        return anomalies

    def _zscore(self, label: str, value: float) -> float | None:
        w = self._window[label]
        if len(w) < self.cfg.min_samples_for_stats:
            return None
        # Compare current value against the *prior* window so the spike itself
        # doesn't inflate its own baseline.
        prior = list(w)[:-1]
        if len(prior) < 2:
            return None
        mean = statistics.fmean(prior)
        try:
            stdev = statistics.stdev(prior)
        except statistics.StatisticsError:
            return None
        if stdev <= 0 or math.isnan(stdev):
            return None
        return (value - mean) / stdev
