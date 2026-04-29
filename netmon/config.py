"""Configuration loading.

YAML on disk → typed dataclasses in memory. Env-var overrides for the handful
of fields you actually want to flip in a container without rewriting the file.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml


@dataclass
class MonitorCfg:
    interval_seconds: float = 2.0
    window_size: int = 30
    interfaces: list[str] = field(default_factory=list)
    per_process: bool = False


@dataclass
class ThresholdCfg:
    bandwidth_mbps_in: float = 100.0
    bandwidth_mbps_out: float = 100.0
    connections_total: int = 5000
    packet_error_rate: float = 0.02
    zscore_threshold: float = 3.0
    min_samples_for_stats: int = 10
    smoothing_window: int = 3
    persistence_intervals: int = 2


@dataclass
class HandlerCfg:
    type: str
    options: dict[str, Any] = field(default_factory=dict)


@dataclass
class AlertCfg:
    cooldown_seconds: float = 60.0
    handlers: list[HandlerCfg] = field(default_factory=list)


@dataclass
class LogCfg:
    level: str = "INFO"
    file: str = "logs/netmon.log"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5
    console: bool = True
    structured: bool = False


@dataclass
class StateCfg:
    file: str = ".netmon-state.json"
    flush_every_seconds: float = 2.0


@dataclass
class Config:
    monitor: MonitorCfg = field(default_factory=MonitorCfg)
    thresholds: ThresholdCfg = field(default_factory=ThresholdCfg)
    alerts: AlertCfg = field(default_factory=AlertCfg)
    logging: LogCfg = field(default_factory=LogCfg)
    state: StateCfg = field(default_factory=StateCfg)
    source_path: Path | None = None


def _split_known(d: dict[str, Any], keys: set[str]) -> tuple[dict, dict]:
    known = {k: v for k, v in d.items() if k in keys}
    extra = {k: v for k, v in d.items() if k not in keys}
    return known, extra


def load_config(path: str | Path | None) -> Config:
    """Load YAML config; missing path → defaults. Unknown keys raise."""
    cfg = Config()

    if path:
        p = Path(path)
        if not p.exists():
            raise FileNotFoundError(f"config not found: {p}")
        with p.open("r", encoding="utf-8") as f:
            raw = yaml.safe_load(f) or {}
        cfg.source_path = p

        if "monitor" in raw:
            cfg.monitor = MonitorCfg(**raw["monitor"])
        if "thresholds" in raw:
            cfg.thresholds = ThresholdCfg(**raw["thresholds"])
        if "alerts" in raw:
            a = raw["alerts"]
            handlers_raw = a.get("handlers", []) or []
            handlers = []
            for h in handlers_raw:
                htype = h.pop("type")
                handlers.append(HandlerCfg(type=htype, options=h))
            cfg.alerts = AlertCfg(
                cooldown_seconds=a.get("cooldown_seconds", 60.0),
                handlers=handlers,
            )
        if "logging" in raw:
            cfg.logging = LogCfg(**raw["logging"])
        if "state" in raw:
            cfg.state = StateCfg(**raw["state"])

    # Env overrides for the few fields most useful in deployment.
    if v := os.getenv("NETMON_LOG_LEVEL"):
        cfg.logging.level = v
    if v := os.getenv("NETMON_INTERVAL"):
        cfg.monitor.interval_seconds = float(v)
    if v := os.getenv("NETMON_STATE_FILE"):
        cfg.state.file = v

    return cfg
