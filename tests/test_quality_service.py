# -*- coding: utf-8 -*-
import copy
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import query_engine as engine
from quality_service import check_config


class QualityServiceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.cfg = engine.load_config(
            os.path.join(ROOT, "config", "ontology_models.example.yaml")
        )

    def test_example_config_reports_stats_and_known_warnings(self):
        report = check_config(self.cfg)
        self.assertTrue(report.ok)
        self.assertEqual(report.stats["object_count"], 6)
        self.assertGreaterEqual(report.stats["implemented_metric_count"], 5)
        self.assertTrue(any(x["code"] == "metric.dimension_multi_hop"
                            for x in report.warnings))

    def test_broken_metric_and_rule_are_errors(self):
        cfg = copy.deepcopy(self.cfg)
        cfg["metrics"]["broken"] = {
            "id": "broken", "name": "broken", "status": "implemented",
            "object": "Missing", "aggregate": {"func": "SUM", "property": "qty"},
        }
        cfg["monitor_rules"]["bad_rule"] = {
            "id": "bad_rule", "metric_id": "missing_metric",
            "rule_type": "unknown", "enabled": True,
        }
        report = check_config(cfg)
        self.assertFalse(report.ok)
        codes = {item["code"] for item in report.errors}
        self.assertIn("metric.object_missing", codes)
        self.assertIn("rule.metric_missing", codes)
        self.assertIn("rule.type_invalid", codes)


if __name__ == "__main__":
    unittest.main()
