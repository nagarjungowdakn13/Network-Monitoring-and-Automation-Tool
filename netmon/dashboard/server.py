"""aiohttp dashboard server.

Endpoints
---------
GET  /                        index.html
GET  /static/*                static assets (JS, CSS)
GET  /api/status              latest snapshot (single JSON object)
GET  /api/history             ring buffer of recent samples for charts
GET  /api/alerts              recent anomalies
GET  /api/config              public config view (thresholds, interval...)
POST /api/diagnostics         run diagnostics suite, return result
POST /api/simulate            inject a synthetic anomaly into the next tick
WS   /ws                      live event stream (one message per tick)

The server is mounted into the same event loop as the monitor, so there's
no IPC. State is read directly from the Runner's StateStore. WebSocket
clients subscribe to the Runner's Hub.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path

from aiohttp import WSMsgType, web

from netmon import diagnostics
from netmon.runner import Runner, make_simulated_anomaly

log = logging.getLogger("netmon.dashboard")

STATIC_DIR = Path(__file__).parent / "static"


def _public_config(runner: Runner) -> dict:
    cfg = runner.cfg
    return {
        "interval_seconds": cfg.monitor.interval_seconds,
        "window_size": cfg.monitor.window_size,
        "interfaces": cfg.monitor.interfaces or [],
        "thresholds": {
            "bandwidth_mbps_in": cfg.thresholds.bandwidth_mbps_in,
            "bandwidth_mbps_out": cfg.thresholds.bandwidth_mbps_out,
            "connections_total": cfg.thresholds.connections_total,
            "packet_error_rate": cfg.thresholds.packet_error_rate,
            "zscore_threshold": cfg.thresholds.zscore_threshold,
            "min_samples_for_stats": cfg.thresholds.min_samples_for_stats,
        },
        "alerts": {
            "cooldown_seconds": cfg.alerts.cooldown_seconds,
            "handler_types": [h.type for h in cfg.alerts.handlers],
        },
    }


# ---------- Handlers ----------

async def index(request: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC_DIR / "index.html")


async def api_status(request: web.Request) -> web.Response:
    runner: Runner = request.app["runner"]
    return web.json_response(runner.state.snapshot_dict(), dumps=_dumps)


async def api_history(request: web.Request) -> web.Response:
    runner: Runner = request.app["runner"]
    return web.json_response({"samples": runner.state.history()}, dumps=_dumps)


async def api_alerts(request: web.Request) -> web.Response:
    runner: Runner = request.app["runner"]
    snap = runner.state.snapshot_dict()
    return web.json_response({"alerts": snap.get("recent_anomalies", [])}, dumps=_dumps)


async def api_config(request: web.Request) -> web.Response:
    runner: Runner = request.app["runner"]
    return web.json_response(_public_config(runner))


async def api_diagnostics(request: web.Request) -> web.Response:
    results = await diagnostics.run_all()
    return web.json_response(diagnostics.to_dict(results))


async def api_simulate(request: web.Request) -> web.Response:
    runner: Runner = request.app["runner"]
    runner.inject_anomaly(make_simulated_anomaly())
    return web.json_response({"ok": True, "scheduled_for_next_tick": True})


async def websocket(request: web.Request) -> web.WebSocketResponse:
    runner: Runner = request.app["runner"]
    ws = web.WebSocketResponse(heartbeat=20.0)
    await ws.prepare(request)

    queue = await runner.hub.subscribe()
    log.info("ws client connected (%d total)", runner.hub.subscriber_count)

    # Send a hello payload so the client can backfill charts immediately.
    await ws.send_json({
        "type": "hello",
        "config": _public_config(runner),
        "history": runner.state.history(),
        "snapshot": runner.state.snapshot_dict(),
    }, dumps=_dumps)

    async def pump_outbound() -> None:
        while not ws.closed:
            event = await queue.get()
            if ws.closed:
                break
            await ws.send_json(event, dumps=_dumps)

    pump_task = asyncio.create_task(pump_outbound())
    try:
        async for msg in ws:
            # We don't expect inbound messages; ignore text, log unexpected.
            if msg.type == WSMsgType.ERROR:
                log.warning("ws error: %s", ws.exception())
                break
    finally:
        pump_task.cancel()
        await runner.hub.unsubscribe(queue)
        try:
            await pump_task
        except (asyncio.CancelledError, Exception):
            pass
        log.info("ws client disconnected (%d remaining)", runner.hub.subscriber_count)

    return ws


def _dumps(obj) -> str:
    # Tolerant JSON: non-serialisable values become strings rather than
    # 500ing. Numbers that are NaN/inf would crash json.dumps by default;
    # keep it simple and let the frontend handle the strings.
    return json.dumps(obj, default=str, allow_nan=False)


# ---------- App factory + lifecycle ----------

def build_app(runner: Runner) -> web.Application:
    app = web.Application()
    app["runner"] = runner

    app.router.add_get("/", index)
    app.router.add_get("/api/status", api_status)
    app.router.add_get("/api/history", api_history)
    app.router.add_get("/api/alerts", api_alerts)
    app.router.add_get("/api/config", api_config)
    app.router.add_post("/api/diagnostics", api_diagnostics)
    app.router.add_post("/api/simulate", api_simulate)
    app.router.add_get("/ws", websocket)
    app.router.add_static("/static/", STATIC_DIR, show_index=False)

    return app


async def serve(runner: Runner, host: str, port: int) -> web.AppRunner:
    """Start the dashboard alongside the runner. Returns the AppRunner so
    the caller can `cleanup()` on shutdown."""
    app = build_app(runner)
    app_runner = web.AppRunner(app, access_log=None)
    await app_runner.setup()
    site = web.TCPSite(app_runner, host=host, port=port)
    await site.start()
    log.info("dashboard listening on http://%s:%d", host, port)
    return app_runner
