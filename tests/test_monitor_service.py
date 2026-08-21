import os
import tempfile
import unittest

from metric_service import MetricSnapshot
from monitor_service import MonitorService, SQLiteMonitorStore


class FakeMetricService:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    def evaluate(self, *args, **kwargs):
        return self.snapshot


class FakeBreakdownMetricService(FakeMetricService):
    def evaluate_breakdown(self, *args, **kwargs):
        return {
            "evidence": {"metric_id": "inventory_quantity"},
            "items": [
                {"dimension": "store.store_name", "key": "门店A", "value": 5,
                 "baseline_value": 10, "delta": -5, "delta_ratio": -0.5},
                {"dimension": "store.store_name", "key": "门店B", "value": 20,
                 "baseline_value": 10, "delta": 10, "delta_ratio": 1.0},
            ],
        }


def snapshot(value, baseline=None):
    delta = None if baseline is None else value - baseline
    ratio = None if baseline in (None, 0) else delta / baseline
    return MetricSnapshot(
        metric_id="inventory_quantity",
        metric_name="库存量",
        metric_version="1.0",
        current_window=None,
        baseline_window=None,
        value=value,
        baseline_value=baseline,
        delta=delta,
        delta_ratio=ratio,
        rows=[{"AGG_RESULT": value}],
        sql="SELECT :p0",
        bind_params={"p0": value},
        evidence={"metric_id": "inventory_quantity"},
    )


class MonitorServiceTests(unittest.TestCase):
    def test_fixed_threshold_and_deduplication(self):
        cfg = {
            "metrics": {"inventory_quantity": {}},
            "monitor_rules": {
                "stock_low": {
                    "id": "stock_low",
                    "metric_id": "inventory_quantity",
                    "rule_type": "fixed_threshold",
                    "direction": "below",
                    "threshold": 10,
                    "severity": "P1",
                    "enabled": True,
                }
            },
        }
        with tempfile.TemporaryDirectory() as td:
            store = SQLiteMonitorStore(os.path.join(td, "control.sqlite3"))
            service = MonitorService(cfg, FakeMetricService(snapshot(9)), store)
            first = service.run_rule("stock_low")
            second = service.run_rule("stock_low")
            self.assertIsNotNone(first)
            self.assertEqual(first.severity, "P1")
            self.assertEqual(second.anomaly_id, first.anomaly_id)
            self.assertEqual(store.count_anomalies(), 1)

    def test_relative_down_rule(self):
        cfg = {
            "metrics": {"inventory_quantity": {}},
            "monitor_rules": {
                "stock_drop": {
                    "id": "stock_drop",
                    "metric_id": "inventory_quantity",
                    "rule_type": "relative_change",
                    "direction": "down",
                    "threshold": 0.2,
                    "severity": "P2",
                    "enabled": True,
                }
            },
        }
        service = MonitorService(cfg, FakeMetricService(snapshot(80, 100)))
        candidate = service.run_rule(
            "stock_drop",
            current_window={"start": "2026-08-13", "end": "2026-08-13"},
            baseline_window={"start": "2026-08-12", "end": "2026-08-12"},
        )
        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.delta_ratio, -0.2)

    def test_disabled_rule_is_rejected(self):
        cfg = {
            "metrics": {"inventory_quantity": {}},
            "monitor_rules": {
                "off": {
                    "id": "off",
                    "metric_id": "inventory_quantity",
                    "rule_type": "fixed_threshold",
                    "direction": "below",
                    "threshold": 10,
                    "severity": "P2",
                    "enabled": False,
                }
            },
        }
        service = MonitorService(cfg, FakeMetricService(snapshot(9)))
        with self.assertRaises(ValueError):
            service.run_rule("off")

    def test_dimension_rule_returns_only_triggered_scopes(self):
        cfg = {
            "metrics": {"inventory_quantity": {}},
            "monitor_rules": {
                "stock_drop": {
                    "id": "stock_drop",
                    "metric_id": "inventory_quantity",
                    "rule_type": "relative_change",
                    "direction": "down",
                    "threshold": 0.2,
                    "severity": "P1",
                    "enabled": True,
                }
            },
        }
        service = MonitorService(cfg, FakeBreakdownMetricService(snapshot(1)))
        candidates = service.run_dimension_rule("stock_drop", "store.store_name")
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].dimension_key, "门店A")


if __name__ == "__main__":
    unittest.main()
