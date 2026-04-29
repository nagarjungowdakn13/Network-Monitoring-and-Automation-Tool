"""Command-line interface.

Subcommands:

  netmon start-monitor [--dashboard --port N]
                              run the monitoring loop, optionally with
                              an embedded web dashboard
  netmon show-status          read the live state snapshot and print it
  netmon run-diagnostics      one-shot system diagnostics
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from netmon import __version__
from netmon.config import load_config
from netmon.diagnostics import format_report, run_all, to_dict
from netmon.logger import setup_logging
from netmon.runner import Runner
from netmon.state import read_snapshot

DEFAULT_CONFIG = "config.yaml"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="netmon",
        description="Async network monitoring & automation tool.",
    )
    p.add_argument("--version", action="version", version=f"netmon {__version__}")
    p.add_argument("-c", "--config", default=DEFAULT_CONFIG, help="path to YAML config (default: %(default)s)")
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("start-monitor", help="run the monitoring loop")
    m.add_argument("--dashboard", action="store_true", help="also serve the web dashboard")
    m.add_argument("--host", default="127.0.0.1", help="dashboard bind host (default: %(default)s)")
    m.add_argument("--port", type=int, default=8765, help="dashboard port (default: %(default)s)")
    m.add_argument("--open", action="store_true", help="open the dashboard in a browser on start")

    s = sub.add_parser("show-status", help="show the latest snapshot from a running monitor")
    s.add_argument("--json", action="store_true", help="emit raw JSON instead of a table")
    s.add_argument("--watch", type=float, metavar="SECONDS",
                   help="re-poll every SECONDS (Ctrl+C to exit)")

    d = sub.add_parser("run-diagnostics", help="run one-shot diagnostics and exit")
    d.add_argument("--json", action="store_true", help="emit JSON")

    return p


# ---------- Subcommand handlers ----------

def _cmd_start_monitor(args: argparse.Namespace) -> int:
    cfg_path = args.config if Path(args.config).exists() else None
    cfg = load_config(cfg_path)
    setup_logging(cfg.logging)
    log = logging.getLogger("netmon.cli")
    if cfg_path is None:
        log.warning("config file %s not found - using defaults", args.config)

    runner = Runner(cfg)

    async def _serve() -> None:
        app_runner = None
        if args.dashboard:
            # Lazy import - aiohttp is only needed when --dashboard is used.
            try:
                from netmon.dashboard.server import serve as serve_dashboard
                app_runner = await serve_dashboard(runner, args.host, args.port)
            except Exception:
                log.exception("failed to start dashboard")
                return
            if args.open:
                import webbrowser
                webbrowser.open(f"http://{args.host}:{args.port}/")

        try:
            await runner.run()
        finally:
            if app_runner is not None:
                await app_runner.cleanup()

    try:
        asyncio.run(_serve())
    except KeyboardInterrupt:
        runner.request_stop()
    return 0


def _cmd_show_status(args: argparse.Namespace) -> int:
    cfg = load_config(args.config if Path(args.config).exists() else None)

    def render_once() -> int:
        snap = read_snapshot(cfg.state.file)
        if snap is None:
            print(f"no snapshot at {cfg.state.file} - is the monitor running?", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(snap, indent=2, default=str))
            return 0
        _print_status(snap)
        return 0

    if not args.watch:
        return render_once()

    try:
        while True:
            # Clear screen between refreshes for a poor-man's `top` UX.
            sys.stdout.write("\033[2J\033[H")
            sys.stdout.flush()
            render_once()
            time.sleep(args.watch)
    except KeyboardInterrupt:
        return 0


def _print_status(snap: dict) -> None:
    sample = snap.get("last_sample") or {}
    started = datetime.fromtimestamp(snap["started_at"]).isoformat(timespec="seconds")
    updated = datetime.fromtimestamp(snap["updated_at"]).isoformat(timespec="seconds")
    age = max(time.time() - snap["updated_at"], 0)
    stats = snap.get("alert_stats") or {}

    print(f"netmon status (pid {snap['pid']})")
    print(f"  started   : {started}")
    print(f"  updated   : {updated}  ({age:.1f}s ago)")
    print(f"  samples   : {snap.get('samples_processed', 0)}")
    print(f"  alerts    : fired={stats.get('fired_total', 0)} "
          f"suppressed={stats.get('suppressed_total', 0)} "
          f"keys={stats.get('cooldown_keys', 0)}")

    if sample:
        print()
        print("  -- last sample --")
        print(f"  in/out    : {sample.get('mbps_in', 0):.2f} / {sample.get('mbps_out', 0):.2f} Mbps")
        pkts = (sample.get('total_packets_sent_per_s', 0)
                + sample.get('total_packets_recv_per_s', 0))
        print(f"  packets/s : {pkts:.0f}  (errors+drops in window: {sample.get('total_err_drop', 0)})")
        print(f"  conns     : {sample.get('connections_total', 0)}")
        states = sample.get("connections_by_state") or {}
        if states:
            top = sorted(states.items(), key=lambda kv: kv[1], reverse=True)[:5]
            print("  by state  : " + ", ".join(f"{s}={n}" for s, n in top))

    recent = snap.get("recent_anomalies") or []
    if recent:
        print()
        print(f"  -- recent anomalies (last {min(5, len(recent))} of {len(recent)}) --")
        for a in recent[-5:]:
            ts = datetime.fromtimestamp(a["sample_ts"]).strftime("%H:%M:%S")
            print(f"  [{ts}] {a['severity'].upper():<8} {a['message']}")


def _cmd_run_diagnostics(args: argparse.Namespace) -> int:
    cfg = load_config(args.config if Path(args.config).exists() else None)
    # Minimal logging during diagnostics — output goes to stdout instead.
    setup_logging(cfg.logging)
    results = asyncio.run(run_all())
    if args.json:
        print(json.dumps(to_dict(results), indent=2))
    else:
        print(format_report(results))
    return 0 if all(r.ok for r in results) else 1


# ---------- Entrypoint ----------

def main(argv: list[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.cmd == "start-monitor":
        return _cmd_start_monitor(args)
    if args.cmd == "show-status":
        return _cmd_show_status(args)
    if args.cmd == "run-diagnostics":
        return _cmd_run_diagnostics(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
