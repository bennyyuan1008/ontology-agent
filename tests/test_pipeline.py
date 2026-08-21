# -*- coding: utf-8 -*-
import json
import os
import tempfile
import unittest

from agent_pipeline import MVPOrchestrator
from diagnosis_agent import DiagnosisAgent
from metric_service import MetricService
from monitor_service import AnomalyCandidate, MonitorService, SQLiteMonitorStore


class FakeMetricService:
    def evaluate_breakdown(self, metric_id, dimension, **kwargs):
        return {"items": [{"key": "SKU-A", "value": 10, "baseline_value": 20,
                            "delta": -10, "delta_ratio": -0.5}],
                "sql": "SELECT 1"}


class PipelineTests(unittest.TestCase):
    def test_process_and_confirm(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = SQLiteMonitorStore(os.path.join(tmp, "control.sqlite3"))
            candidate = AnomalyCandidate(
                anomaly_id="a1", rule_id="r1", metric_id="inventory_quantity",
                severity="P1", trigger="value <= 100", value=50,
                baseline_value=200, delta=-150, delta_ratio=-0.75,
                current_window={"start": "2026-03-01", "end": "2026-03-31"},
                baseline_window=None, dimension="store.store_name",
                dimension_key="S1", status="open", dedupe_key="d1", evidence={
                    "metric_name": "库存量", "metric_version": "1.0", "unit": "件",
                },
            )
            def llm(_system, user):
                catalog = json.loads(user)["evidence_catalog"]
                return json.dumps({
                    "summary": "库存偏低，需核查。",
                    "hypotheses": [{"reason": "库存偏离阈值", "confidence": "medium",
                                    "evidence_ids": ["metric_value", "breakdown_sku_quantity"],
                                    "missing_data": []}],
                    "next_checks": ["核查营业状态"], "data_sufficiency": "medium",
                }, ensure_ascii=False)
            metric = FakeMetricService()
            monitor = MonitorService({"metrics": {}, "monitor_rules": {}}, metric, store)
            orchestrator = MVPOrchestrator(
                {}, metric, monitor, DiagnosisAgent({}, llm_func=llm), store=store
            )
            result = orchestrator.process_candidate(candidate)
            self.assertEqual(result["recommendation"]["status"], "pending_confirmation")
            saved = store.list_recommendations()
            self.assertEqual(len(saved), 1)
            confirmed = orchestrator.confirm_recommendation(
                saved[0]["recommendation_id"], "tester", True, "确认", "ops"
            )
            self.assertEqual(confirmed["recommendation"]["status"], "accepted")
            self.assertEqual(confirmed["task"]["assignee"], "ops")
            self.assertEqual(len(store.list_tasks()), 1)


if __name__ == "__main__":
    unittest.main()
