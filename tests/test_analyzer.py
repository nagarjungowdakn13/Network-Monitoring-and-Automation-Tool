import unittest

from netmon.analyzer import Analyzer, Severity
from netmon.config import ThresholdCfg
from netmon.monitor import Sample


def sample(
    *,
    ts=1.0,
    interval=2.0,
    mbps_in=0.0,
    mbps_out=0.0,
    conns=0,
    packets=0,
    errors=0,
):
    return Sample(
        ts=ts,
        interval=interval,
        total_bytes_recv_per_s=mbps_in * 1_000_000 / 8,
        total_bytes_sent_per_s=mbps_out * 1_000_000 / 8,
        total_packets_window=packets,
        total_err_drop=errors,
        connections_total=conns,
    )


class AnalyzerTests(unittest.TestCase):
    def test_hard_thresholds_emit_expected_anomalies(self):
        analyzer = Analyzer(
            ThresholdCfg(
                bandwidth_mbps_in=10,
                bandwidth_mbps_out=20,
                connections_total=50,
                min_samples_for_stats=99,
                smoothing_window=1,
                persistence_intervals=1,
            ),
            window_size=30,
        )

        anomalies = analyzer.analyze(sample(mbps_in=11, mbps_out=21, conns=51))
        keys = {a.key for a in anomalies}

        self.assertEqual(
            keys,
            {"threshold:mbps_in", "threshold:mbps_out", "threshold:connections"},
        )
        self.assertTrue(all(a.severity is Severity.WARNING for a in anomalies))

    def test_packet_error_rate_is_critical(self):
        analyzer = Analyzer(
            ThresholdCfg(
                packet_error_rate=0.10,
                min_samples_for_stats=99,
                smoothing_window=1,
                persistence_intervals=1,
            ),
            window_size=30,
        )

        anomalies = analyzer.analyze(sample(packets=100, errors=11))

        self.assertEqual(len(anomalies), 1)
        self.assertEqual(anomalies[0].key, "threshold:packet_err_rate")
        self.assertIs(anomalies[0].severity, Severity.CRITICAL)

    def test_zscore_compares_spike_against_prior_window(self):
        analyzer = Analyzer(
            ThresholdCfg(
                bandwidth_mbps_in=1_000,
                bandwidth_mbps_out=1_000,
                connections_total=10_000,
                zscore_threshold=3.0,
                min_samples_for_stats=4,
                smoothing_window=1,
                persistence_intervals=1,
            ),
            window_size=10,
        )

        for i, value in enumerate([10, 12, 11], start=1):
            self.assertEqual(analyzer.analyze(sample(ts=i, mbps_in=value)), [])

        anomalies = analyzer.analyze(sample(ts=4, mbps_in=40))

        self.assertIn("zscore:mbps_in", {a.key for a in anomalies})

    def test_warmup_sample_is_ignored(self):
        analyzer = Analyzer(
            ThresholdCfg(bandwidth_mbps_in=1, smoothing_window=1, persistence_intervals=1),
            window_size=10,
        )

        anomalies = analyzer.analyze(sample(interval=0, mbps_in=99))

        self.assertEqual(anomalies, [])


if __name__ == "__main__":
    unittest.main()
