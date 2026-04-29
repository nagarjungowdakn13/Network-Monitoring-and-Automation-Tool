"""Network sampler.

psutil's per-NIC counters are *cumulative* since boot — to get a rate you must
diff against the previous sample and divide by elapsed wall-time. We compute
that here, plus connection counts and per-interface deltas, and emit a Sample
that the analyzer can consume.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import asdict, dataclass, field
from typing import Iterable

import psutil


@dataclass
class IfaceDelta:
    name: str
    bytes_sent_per_s: float
    bytes_recv_per_s: float
    packets_sent_per_s: float
    packets_recv_per_s: float
    errin: int
    errout: int
    dropin: int
    dropout: int


@dataclass
class Sample:
    ts: float                                  # epoch seconds
    interval: float                            # seconds since previous sample
    interfaces: list[IfaceDelta] = field(default_factory=list)
    total_bytes_sent_per_s: float = 0.0
    total_bytes_recv_per_s: float = 0.0
    total_packets_sent_per_s: float = 0.0
    total_packets_recv_per_s: float = 0.0
    total_err_drop: int = 0
    total_packets_window: int = 0              # for error-rate denominator
    connections_total: int = 0
    connections_by_state: dict[str, int] = field(default_factory=dict)

    @property
    def mbps_in(self) -> float:
        return self.total_bytes_recv_per_s * 8 / 1_000_000

    @property
    def mbps_out(self) -> float:
        return self.total_bytes_sent_per_s * 8 / 1_000_000

    def to_dict(self) -> dict:
        d = asdict(self)
        d["mbps_in"] = self.mbps_in
        d["mbps_out"] = self.mbps_out
        return d


class NetworkMonitor:
    """Samples network state on each tick.

    The first call to `sample()` returns a zero-rate sample because we have no
    baseline yet. Callers should treat the first sample as a warm-up.
    """

    def __init__(self, interfaces: Iterable[str] | None = None):
        self._wanted = set(interfaces) if interfaces else None
        self._prev_counters: dict[str, psutil._common.snetio] | None = None
        self._prev_ts: float | None = None

    def _select_ifaces(self, counters: dict[str, psutil._common.snetio]) -> dict[str, psutil._common.snetio]:
        if self._wanted:
            return {k: v for k, v in counters.items() if k in self._wanted}
        # Skip loopback by default — it's noisy and rarely useful.
        return {k: v for k, v in counters.items() if not k.lower().startswith(("lo", "loopback"))}

    async def sample(self) -> Sample:
        # psutil calls are sync but cheap (microseconds). Run in thread anyway
        # so a slow OS call (e.g. net_connections on Windows) can't stall the loop.
        return await asyncio.to_thread(self._sample_sync)

    def _sample_sync(self) -> Sample:
        now = time.time()
        counters_all = psutil.net_io_counters(pernic=True)
        counters = self._select_ifaces(counters_all)

        if self._prev_counters is None or self._prev_ts is None:
            # Warm-up — store baseline, return zeroes.
            self._prev_counters = counters
            self._prev_ts = now
            return Sample(ts=now, interval=0.0)

        interval = max(now - self._prev_ts, 1e-6)
        ifaces: list[IfaceDelta] = []
        tot_bs = tot_br = tot_ps = tot_pr = 0.0
        tot_err = 0
        tot_pkts = 0

        for name, cur in counters.items():
            prev = self._prev_counters.get(name)
            if prev is None:
                continue
            # Counter rollover guard: psutil exposes uint64 on most OSes but a
            # NIC reset can drop the counter. If we see a negative delta, treat
            # it as zero rather than emit a giant spike.
            d_bs = max(cur.bytes_sent - prev.bytes_sent, 0) / interval
            d_br = max(cur.bytes_recv - prev.bytes_recv, 0) / interval
            d_ps = max(cur.packets_sent - prev.packets_sent, 0) / interval
            d_pr = max(cur.packets_recv - prev.packets_recv, 0) / interval

            ifaces.append(IfaceDelta(
                name=name,
                bytes_sent_per_s=d_bs, bytes_recv_per_s=d_br,
                packets_sent_per_s=d_ps, packets_recv_per_s=d_pr,
                errin=cur.errin, errout=cur.errout,
                dropin=cur.dropin, dropout=cur.dropout,
            ))
            tot_bs += d_bs; tot_br += d_br
            tot_ps += d_ps; tot_pr += d_pr

            tot_err += (
                max(cur.errin - prev.errin, 0)
                + max(cur.errout - prev.errout, 0)
                + max(cur.dropin - prev.dropin, 0)
                + max(cur.dropout - prev.dropout, 0)
            )
            tot_pkts += (
                max(cur.packets_sent - prev.packets_sent, 0)
                + max(cur.packets_recv - prev.packets_recv, 0)
            )

        # Connection counts — `kind="inet"` covers TCP+UDP v4/v6.
        # Can require elevated privileges on Windows; we degrade gracefully.
        try:
            conns = psutil.net_connections(kind="inet")
        except (psutil.AccessDenied, PermissionError):
            conns = []

        by_state: dict[str, int] = {}
        for c in conns:
            by_state[c.status] = by_state.get(c.status, 0) + 1

        self._prev_counters = counters
        self._prev_ts = now

        return Sample(
            ts=now, interval=interval, interfaces=ifaces,
            total_bytes_sent_per_s=tot_bs, total_bytes_recv_per_s=tot_br,
            total_packets_sent_per_s=tot_ps, total_packets_recv_per_s=tot_pr,
            total_err_drop=tot_err, total_packets_window=tot_pkts,
            connections_total=len(conns), connections_by_state=by_state,
        )
