import asyncio
import unittest

from netmon import alerts
from netmon.alerts import AlertManager
from netmon.analyzer import Anomaly, Severity
from netmon.config import AlertCfg, HandlerCfg


def anomaly(key="threshold:mbps_in"):
    return Anomaly(
        key=key,
        severity=Severity.WARNING,
        metric="mbps_in",
        value=101.0,
        threshold=100.0,
        message="inbound threshold exceeded",
        sample_ts=1.0,
    )


class AlertManagerTests(unittest.TestCase):
    def test_cooldown_suppresses_repeated_anomaly_key(self):
        calls = []

        async def capture(a):
            calls.append(a.key)

        original = dict(alerts._BUILTIN_FACTORIES)
        alerts._BUILTIN_FACTORIES["capture"] = lambda opts: capture
        try:
            manager = AlertManager(
                AlertCfg(
                    cooldown_seconds=60,
                    handlers=[HandlerCfg(type="capture")],
                )
            )

            asyncio.run(manager.dispatch([anomaly()]))
            asyncio.run(manager.dispatch([anomaly()]))

            self.assertEqual(calls, ["threshold:mbps_in"])
            self.assertEqual(
                manager.stats(),
                {"fired_total": 1, "suppressed_total": 1, "cooldown_keys": 1},
            )
        finally:
            alerts._BUILTIN_FACTORIES.clear()
            alerts._BUILTIN_FACTORIES.update(original)

    def test_different_keys_fire_independently_during_cooldown(self):
        calls = []

        async def capture(a):
            calls.append(a.key)

        original = dict(alerts._BUILTIN_FACTORIES)
        alerts._BUILTIN_FACTORIES["capture"] = lambda opts: capture
        try:
            manager = AlertManager(
                AlertCfg(
                    cooldown_seconds=60,
                    handlers=[HandlerCfg(type="capture")],
                )
            )

            asyncio.run(manager.dispatch([anomaly("threshold:mbps_in")]))
            asyncio.run(manager.dispatch([anomaly("threshold:connections")]))

            self.assertEqual(calls, ["threshold:mbps_in", "threshold:connections"])
            self.assertEqual(manager.stats()["fired_total"], 2)
            self.assertEqual(manager.stats()["suppressed_total"], 0)
        finally:
            alerts._BUILTIN_FACTORIES.clear()
            alerts._BUILTIN_FACTORIES.update(original)


if __name__ == "__main__":
    unittest.main()
