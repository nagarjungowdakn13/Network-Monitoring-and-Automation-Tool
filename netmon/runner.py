"""The async monitoring loop.

A single coroutine pulls samples on an interval, hands them to the analyzer,
fires alerts, updates the state snapshot, and publishes events to the hub
for live subscribers (the dashboard). The loop is cancellation-aware:
SIGINT / SIGTERM cancels the task; the finally-block flushes one last snapshot.

Why a fixed interval instead of an event-driven design? psutil exposes counters,
not events. A short polling interval (1-5 s) is the standard pattern. If you
need sub-second precision you'd hook into pcap/eBPF, which is out of scope.
"""

from __future__ import annotations

import asyncio
import dataclasses
import logging
import signal
import time

from netmon.alerts import AlertManager
from netmon.analyzer import Analyzer, Anomaly, Severity
from netmon.config import Config, load_config
from netmon.hub import Hub
from netmon.monitor import NetworkMonitor
from netmon.state import StateStore

log = logging.getLogger("netmon.runner")


class Runner:
    def __init__(self, cfg: Config, hub: Hub | None = None):
        self.cfg = cfg
        self.monitor = NetworkMonitor(cfg.monitor.interfaces or None)
        self.analyzer = Analyzer(cfg.thresholds, cfg.monitor.window_size)
        self.alerts = AlertManager(cfg.alerts)
        self.state = StateStore(cfg.state.file, cfg.state.flush_every_seconds)
        self.hub = hub or Hub()
        self._stop = asyncio.Event()
        self._config_mtime = self._read_config_mtime()
        self._tick_failures = 0
        # Anomalies queued via inject_anomaly() are merged into the next tick's
        # analysis output so the dashboard's Simulate Spike button behaves
        # exactly like a real detection (cooldown, fan-out to handlers, etc).
        self._injected: list[Anomaly] = []

    def request_stop(self) -> None:
        log.info("stop requested")
        self._stop.set()

    def inject_anomaly(self, anomaly: Anomaly) -> None:
        """Queue a synthetic anomaly to be processed on the next tick.

        Used by the dashboard's Simulate Spike button. We deliberately route
        through the same path real anomalies take so the demo exercises the
        full pipeline (alerts, cooldown, snapshot, hub broadcast).
        """
        self._injected.append(anomaly)

    async def run(self) -> None:
        log.info("netmon starting (interval=%ss, window=%s, interfaces=%s)",
                 self.cfg.monitor.interval_seconds,
                 self.cfg.monitor.window_size,
                 self.cfg.monitor.interfaces or "all")

        loop = asyncio.get_running_loop()
        for sig in (getattr(signal, "SIGINT", None), getattr(signal, "SIGTERM", None)):
            if sig is None:
                continue
            try:
                loop.add_signal_handler(sig, self.request_stop)
            except NotImplementedError:
                pass  # Windows: KeyboardInterrupt path handles it instead

        try:
            while not self._stop.is_set():
                tick_started = time.monotonic()
                try:
                    self._maybe_reload_config()
                    sample = await self.monitor.sample()
                    anomalies = self.analyzer.analyze(sample)
                    if self._injected:
                        anomalies = anomalies + self._injected
                        self._injected = []
                    if anomalies:
                        log.debug("detected %d anomalies", len(anomalies))
                    await self.alerts.dispatch(anomalies)
                    self._tick_failures = 0
                    self.state.update(sample, anomalies, self.alerts.stats(), self._health())
                    self.state.maybe_flush()
                    self._publish(sample, anomalies)
                except Exception:  # noqa: BLE001
                    self._tick_failures += 1
                    log.exception("tick failed")

                # Sleep the *remainder* so we don't drift when a tick runs long.
                elapsed = time.monotonic() - tick_started
                wait = max(self.cfg.monitor.interval_seconds - elapsed, 0)
                if wait > 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), timeout=wait)
                    except asyncio.TimeoutError:
                        pass
        finally:
            try:
                self.state.maybe_flush(force=True)
            except Exception:
                log.exception("final state flush failed")
            log.info("netmon stopped (samples=%d, alerts_fired=%s, alerts_suppressed=%s)",
                     self.state._snap.samples_processed,
                     self.alerts.stats()["fired_total"],
                     self.alerts.stats()["suppressed_total"])

    def _publish(self, sample, anomalies: list[Anomaly]) -> None:
        if self.hub.subscriber_count == 0:
            return
        anomaly_dicts = []
        for a in anomalies:
            d = dataclasses.asdict(a)
            d["severity"] = a.severity.value
            anomaly_dicts.append(d)
        self.hub.publish({
            "type": "tick",
            "sample": sample.to_dict(),
            "anomalies": anomaly_dicts,
            "alert_stats": self.alerts.stats(),
            "system_state": self.state._snap.system_state,
            "health": self._health(),
            "samples_processed": self.state._snap.samples_processed,
        })

    def _read_config_mtime(self) -> float | None:
        if self.cfg.source_path is None:
            return None
        try:
            return self.cfg.source_path.stat().st_mtime
        except OSError:
            return None

    def _maybe_reload_config(self) -> None:
        if self.cfg.source_path is None:
            return
        mtime = self._read_config_mtime()
        if mtime is None or mtime == self._config_mtime:
            return
        old = self.cfg
        self.cfg = load_config(self.cfg.source_path)
        self._config_mtime = mtime
        if dataclasses.asdict(old.monitor) != dataclasses.asdict(self.cfg.monitor):
            self.monitor = NetworkMonitor(self.cfg.monitor.interfaces or None)
        if (
            dataclasses.asdict(old.thresholds) != dataclasses.asdict(self.cfg.thresholds)
            or old.monitor.window_size != self.cfg.monitor.window_size
        ):
            self.analyzer = Analyzer(self.cfg.thresholds, self.cfg.monitor.window_size)
        if dataclasses.asdict(old.alerts) != dataclasses.asdict(self.cfg.alerts):
            self.alerts = AlertManager(self.cfg.alerts)
        if old.state.file != self.cfg.state.file:
            self.state = StateStore(self.cfg.state.file, self.cfg.state.flush_every_seconds)
        else:
            self.state.flush_every = self.cfg.state.flush_every_seconds
        log.info("configuration reloaded from %s", self.cfg.source_path)

    def _health(self) -> dict:
        return {
            "state": self.state._snap.system_state,
            "tick_failures": self._tick_failures,
            "subscribers": self.hub.subscriber_count,
            "config_path": str(self.cfg.source_path) if self.cfg.source_path else None,
        }


def make_simulated_anomaly() -> Anomaly:
    """Used by the dashboard's Simulate Spike button."""
    return Anomaly(
        key="zscore:simulated",
        severity=Severity.WARNING,
        metric="mbps_in",
        value=999.99,
        threshold=None,
        message="Simulated spike (dashboard test) - inbound bandwidth far above baseline",
        sample_ts=time.time(),
    )
