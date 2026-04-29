"""Alert system.

Pluggable handlers receive an Anomaly. Cooldown is per-key so a sustained
condition doesn't spam — but a *different* anomaly (different key) still
fires immediately. Handlers run concurrently; one slow handler (e.g. a
webhook) does not delay the monitor loop.
"""

from __future__ import annotations

import asyncio
import dataclasses
import importlib
import json
import logging
import shlex
import sys
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from netmon.analyzer import Anomaly, Severity
from netmon.config import AlertCfg, HandlerCfg

log = logging.getLogger("netmon.alerts")

HandlerFn = Callable[[Anomaly], Awaitable[None]]

# ---------- Built-in handlers ----------

_LEVEL_MAP = {
    Severity.INFO: logging.INFO,
    Severity.WARNING: logging.WARNING,
    Severity.CRITICAL: logging.CRITICAL,
}


def _make_log_handler(opts: dict) -> HandlerFn:
    override = opts.get("level")

    async def _h(a: Anomaly) -> None:
        lvl = _LEVEL_MAP[a.severity] if override is None else getattr(logging, override.upper(), logging.WARNING)
        log.log(lvl, "ALERT [%s] %s | metric=%s value=%.4f threshold=%s",
                a.severity.value, a.message, a.metric, a.value,
                f"{a.threshold:.4f}" if a.threshold is not None else "n/a")
    return _h


def _make_console_handler(opts: dict) -> HandlerFn:
    use_colour = opts.get("colour", True) and sys.stderr.isatty()
    palette = {
        Severity.INFO: "\033[36m",
        Severity.WARNING: "\033[33m",
        Severity.CRITICAL: "\033[31m",
    }
    reset = "\033[0m"

    async def _h(a: Anomaly) -> None:
        prefix = f"{palette[a.severity]}[{a.severity.value.upper()}]{reset}" if use_colour else f"[{a.severity.value.upper()}]"
        print(f"{prefix} {a.message}", file=sys.stderr, flush=True)
    return _h


def _make_script_handler(opts: dict) -> HandlerFn:
    cmd = opts.get("command")
    if not cmd:
        raise ValueError("script handler requires 'command'")
    if isinstance(cmd, str):
        cmd = shlex.split(cmd)

    async def _h(a: Anomaly) -> None:
        payload = json.dumps(_anomaly_to_dict(a))
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            # Pipe payload to script via stdin — simpler and safer than argv.
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(payload.encode("utf-8")), timeout=opts.get("timeout_seconds", 10)
            )
            if proc.returncode != 0:
                log.warning("alert script exited %s: %s", proc.returncode, stderr.decode(errors="replace")[:500])
        except asyncio.TimeoutError:
            log.warning("alert script timed out: %s", cmd)
        except FileNotFoundError:
            log.error("alert script not found: %s", cmd)
        except Exception:  # noqa: BLE001 — handler must never propagate
            log.exception("alert script handler crashed")
    return _h


def _make_webhook_handler(opts: dict) -> HandlerFn:
    import urllib.request
    url = opts["url"]
    timeout = opts.get("timeout_seconds", 5)

    async def _h(a: Anomaly) -> None:
        body = json.dumps(_anomaly_to_dict(a)).encode("utf-8")

        def _post():
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                r.read()

        try:
            await asyncio.to_thread(_post)
        except Exception:  # noqa: BLE001
            log.exception("webhook handler failed: %s", url)
    return _h


_BUILTIN_FACTORIES: dict[str, Callable[[dict], HandlerFn]] = {
    "log": _make_log_handler,
    "console": _make_console_handler,
    "script": _make_script_handler,
    "webhook": _make_webhook_handler,
}


def register_handler(name: str, factory: Callable[[dict], HandlerFn]) -> None:
    """Register an alert handler factory.

    External packages can call this during import, or configs can reference a
    factory directly as ``package.module:function``.
    """
    _BUILTIN_FACTORIES[name] = factory


def _load_factory(name: str) -> Callable[[dict], HandlerFn] | None:
    factory = _BUILTIN_FACTORIES.get(name)
    if factory is not None:
        return factory
    if ":" not in name:
        return None
    module_name, attr = name.split(":", 1)
    module = importlib.import_module(module_name)
    candidate = getattr(module, attr)
    if not callable(candidate):
        raise TypeError(f"alert handler factory is not callable: {name}")
    return candidate


def _anomaly_to_dict(a: Anomaly) -> dict:
    d = dataclasses.asdict(a)
    d["severity"] = a.severity.value
    return d


# ---------- Manager ----------

@dataclass
class _CooldownEntry:
    last_fired: float
    count: int  # number of suppressed events


class AlertManager:
    def __init__(self, cfg: AlertCfg):
        self.cfg = cfg
        self._handlers: list[HandlerFn] = self._build_handlers(cfg.handlers)
        self._cooldown: dict[str, _CooldownEntry] = {}
        self._fired_total = 0
        self._suppressed_total = 0

    def _build_handlers(self, specs: list[HandlerCfg]) -> list[HandlerFn]:
        out: list[HandlerFn] = []
        for h in specs:
            try:
                factory = _load_factory(h.type)
                if factory is None:
                    log.warning("unknown alert handler type: %s - skipping", h.type)
                    continue
                out.append(factory(h.options))
            except Exception:
                log.exception("failed to build handler %s", h.type)
        return out

    async def dispatch(self, anomalies: list[Anomaly]) -> None:
        if not anomalies or not self._handlers:
            return
        now = time.time()
        to_fire: list[Anomaly] = []
        for a in anomalies:
            entry = self._cooldown.get(a.key)
            if entry and (now - entry.last_fired) < self.cfg.cooldown_seconds:
                entry.count += 1
                self._suppressed_total += 1
                continue
            self._cooldown[a.key] = _CooldownEntry(last_fired=now, count=0)
            to_fire.append(a)

        if not to_fire:
            return

        self._fired_total += len(to_fire)
        # Run every handler against every alert concurrently. Errors inside
        # handlers are logged, never raised — one bad handler must not bring
        # the loop down.
        coros = [h(a) for a in to_fire for h in self._handlers]
        results = await asyncio.gather(*coros, return_exceptions=True)
        for r in results:
            if isinstance(r, Exception):
                log.exception("alert handler raised", exc_info=r)

    def stats(self) -> dict:
        return {
            "fired_total": self._fired_total,
            "suppressed_total": self._suppressed_total,
            "cooldown_keys": len(self._cooldown),
        }
