"""Lightweight state snapshot for cross-process status reads.

The monitor process writes a JSON file atomically every N seconds; the CLI
`show-status` command reads it. This is intentionally a file (not a socket
or HTTP server) — fewer moving parts, no port allocation, easy to inspect
with `cat`. Atomic write via temp + rename means a partial read is impossible.

For multi-host or production deployments you'd push these metrics to a real
TSDB instead. This is a single-host operator tool.
"""

from __future__ import annotations

import json
import os
import tempfile
import time
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Deque

from netmon.analyzer import Anomaly
from netmon.monitor import Sample


@dataclass
class Snapshot:
    pid: int
    started_at: float
    updated_at: float
    last_sample: dict | None = None
    recent_anomalies: list[dict] = field(default_factory=list)
    alert_stats: dict[str, Any] = field(default_factory=dict)
    samples_processed: int = 0


class StateStore:
    """Holds the live snapshot in memory; flushes to disk periodically.

    Also keeps a bounded ring buffer of recent samples (lightweight series:
    timestamp + a handful of scalars) for the dashboard's charts. We don't
    persist this — on restart, charts simply start empty.
    """

    MAX_RECENT_ANOMALIES = 50
    HISTORY_LEN = 300  # 10 min at a 2s interval; tune if interval changes

    def __init__(self, path: str | Path, flush_every: float = 2.0):
        self.path = Path(path)
        self.flush_every = flush_every
        self._last_flush = 0.0
        self._snap = Snapshot(
            pid=os.getpid(),
            started_at=time.time(),
            updated_at=time.time(),
        )
        self._history: Deque[dict] = deque(maxlen=self.HISTORY_LEN)

    def update(self, sample: Sample, anomalies: list[Anomaly], alert_stats: dict) -> None:
        self._snap.updated_at = time.time()
        self._snap.last_sample = sample.to_dict()
        self._snap.samples_processed += 1
        for a in anomalies:
            d = asdict(a)
            d["severity"] = a.severity.value
            self._snap.recent_anomalies.append(d)
        if len(self._snap.recent_anomalies) > self.MAX_RECENT_ANOMALIES:
            self._snap.recent_anomalies = self._snap.recent_anomalies[-self.MAX_RECENT_ANOMALIES:]
        self._snap.alert_stats = alert_stats

        # Append a small projection to history. Keep the payload tiny — this
        # is what we ship over the wire many times a second to dashboards.
        if sample.interval > 0:
            self._history.append({
                "ts": sample.ts,
                "mbps_in": sample.mbps_in,
                "mbps_out": sample.mbps_out,
                "connections": sample.connections_total,
                "packets_per_s": sample.total_packets_sent_per_s + sample.total_packets_recv_per_s,
                "anomaly_count": len(anomalies),
            })

    def history(self) -> list[dict]:
        return list(self._history)

    def snapshot_dict(self) -> dict:
        return asdict(self._snap)

    def maybe_flush(self, force: bool = False) -> None:
        now = time.time()
        if not force and (now - self._last_flush) < self.flush_every:
            return
        self._last_flush = now
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write: stage to a sibling temp file then rename. On Windows,
        # os.replace is atomic for files on the same volume.
        fd, tmp = tempfile.mkstemp(prefix=".netmon-state-", dir=str(self.path.parent))
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(asdict(self._snap), f, indent=2, default=str)
            os.replace(tmp, self.path)
        except Exception:
            # Best-effort cleanup; failing to flush state is not fatal.
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise


def read_snapshot(path: str | Path) -> dict | None:
    p = Path(path)
    if not p.exists():
        return None
    try:
        with p.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return None
