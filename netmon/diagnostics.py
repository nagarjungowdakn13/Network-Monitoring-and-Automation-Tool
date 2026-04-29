"""One-shot system diagnostics.

These are the checks an operator wants when something feels off but the
monitor isn't loud yet: are the interfaces up, can we resolve DNS, is the
default gateway reachable, what's the per-process network footprint.

Each check returns a Result with status + detail so the CLI can print a
unified report. We never raise out of a check — a failed check is data,
not an error.
"""

from __future__ import annotations

import asyncio
import socket
import subprocess
import sys
from dataclasses import asdict, dataclass
from typing import Awaitable, Callable

import psutil


@dataclass
class Result:
    name: str
    ok: bool
    detail: str
    data: dict | None = None


CheckFn = Callable[[], Awaitable[Result]]


async def check_interfaces() -> Result:
    addrs = psutil.net_if_addrs()
    stats = psutil.net_if_stats()
    rows = []
    up_count = 0
    for name, addr_list in addrs.items():
        st = stats.get(name)
        is_up = bool(st and st.isup)
        if is_up:
            up_count += 1
        ipv4 = next((a.address for a in addr_list if a.family == socket.AF_INET), None)
        rows.append({
            "name": name, "up": is_up,
            "ipv4": ipv4,
            "speed_mbps": st.speed if st else None,
            "mtu": st.mtu if st else None,
        })
    return Result(
        name="interfaces",
        ok=up_count > 0,
        detail=f"{up_count} up of {len(rows)} total",
        data={"interfaces": rows},
    )


async def check_dns(host: str = "example.com") -> Result:
    loop = asyncio.get_running_loop()
    try:
        # getaddrinfo blocks; offload it.
        infos = await asyncio.wait_for(
            loop.getaddrinfo(host, None, family=socket.AF_UNSPEC, type=socket.SOCK_STREAM),
            timeout=3.0,
        )
        addrs = sorted({info[4][0] for info in infos})
        return Result("dns", True, f"resolved {host} -> {', '.join(addrs)}", {"host": host, "addrs": addrs})
    except asyncio.TimeoutError:
        return Result("dns", False, f"DNS resolution timed out for {host}")
    except socket.gaierror as e:
        return Result("dns", False, f"DNS failure for {host}: {e}")


async def check_gateway() -> Result:
    """Ping the default gateway. Cross-platform: uses the system `ping` binary."""
    # Pick a target — the gateway varies, so we use a globally-routable anycast
    # (1.1.1.1) by default. It's a reachability proxy, not strictly the gateway.
    target = "1.1.1.1"
    is_windows = sys.platform.startswith("win")
    args = ["ping", "-n", "1", "-w", "2000", target] if is_windows else ["ping", "-c", "1", "-W", "2", target]

    try:
        proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5.0)
    except (FileNotFoundError, asyncio.TimeoutError) as e:
        return Result("gateway_reachability", False, f"ping unavailable or timed out: {e}")

    ok = proc.returncode == 0
    return Result(
        name="gateway_reachability",
        ok=ok,
        detail=f"ping {target}: {'reachable' if ok else 'unreachable'}",
        data={"target": target, "exit_code": proc.returncode},
    )


async def check_listening_ports() -> Result:
    try:
        conns = await asyncio.to_thread(psutil.net_connections, "inet")
    except (psutil.AccessDenied, PermissionError):
        return Result("listening_ports", False, "access denied (try elevated)")
    listening = [c for c in conns if c.status == psutil.CONN_LISTEN]
    rows = sorted({(c.laddr.ip, c.laddr.port) for c in listening if c.laddr})
    return Result(
        "listening_ports", True,
        f"{len(rows)} listening sockets",
        {"ports": [{"ip": ip, "port": p} for ip, p in rows]},
    )


async def check_top_talkers(n: int = 5) -> Result:
    """Per-process connection counts. May be partial without admin."""
    try:
        conns = await asyncio.to_thread(psutil.net_connections, "inet")
    except (psutil.AccessDenied, PermissionError):
        return Result("top_talkers", False, "access denied (try elevated)")
    by_pid: dict[int, int] = {}
    for c in conns:
        if c.pid is None:
            continue
        by_pid[c.pid] = by_pid.get(c.pid, 0) + 1
    top = sorted(by_pid.items(), key=lambda kv: kv[1], reverse=True)[:n]
    rows = []
    for pid, count in top:
        try:
            name = psutil.Process(pid).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            name = "?"
        rows.append({"pid": pid, "name": name, "connections": count})
    return Result("top_talkers", True, f"top {len(rows)} processes by connection count", {"processes": rows})


async def run_all() -> list[Result]:
    """Execute the standard diagnostics suite concurrently."""
    checks: list[CheckFn] = [
        check_interfaces, check_dns, check_gateway,
        check_listening_ports, check_top_talkers,
    ]
    results = await asyncio.gather(*[c() for c in checks], return_exceptions=True)
    out: list[Result] = []
    for c, r in zip(checks, results):
        if isinstance(r, Exception):
            out.append(Result(c.__name__.replace("check_", ""), False, f"check crashed: {r}"))
        else:
            out.append(r)
    return out


def format_report(results: list[Result]) -> str:
    lines = ["Diagnostics report", "=" * 60]
    for r in results:
        marker = "OK " if r.ok else "FAIL"
        lines.append(f"[{marker}] {r.name:<22} - {r.detail}")
    return "\n".join(lines)


def to_dict(results: list[Result]) -> dict:
    return {"checks": [asdict(r) for r in results]}
