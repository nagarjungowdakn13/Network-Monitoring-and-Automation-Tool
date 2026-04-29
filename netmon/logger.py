"""Logging setup. Rotating file handler + optional console."""

from __future__ import annotations

import json
import logging
import logging.handlers
from pathlib import Path

from netmon.config import LogCfg

_FMT = "%(asctime)s %(levelname)-7s %(name)s: %(message)s"


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, self.datefmt),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, default=str)


def setup_logging(cfg: LogCfg) -> logging.Logger:
    root = logging.getLogger("netmon")
    root.setLevel(getattr(logging, cfg.level.upper(), logging.INFO))
    # Idempotent — repeated setup (tests, reconfigure) doesn't multiply handlers.
    for h in list(root.handlers):
        root.removeHandler(h)

    fmt = JsonFormatter() if cfg.structured else logging.Formatter(_FMT)

    log_path = Path(cfg.file)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    fh = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=cfg.max_bytes, backupCount=cfg.backup_count, encoding="utf-8"
    )
    fh.setFormatter(fmt)
    root.addHandler(fh)

    if cfg.console:
        ch = logging.StreamHandler()
        ch.setFormatter(fmt)
        root.addHandler(ch)

    root.propagate = False
    return root
