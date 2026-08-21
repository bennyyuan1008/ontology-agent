import unittest

from diagnosis_agent import DiagnosisAgent, DiagnosisError


CANDIDATE = {
    "anomaly_id": "abc123",
    "rule_id": "inventory_store_below_100k",
    "metric_id": "inventory_quantity",
    "severity": "P1",
    "trigger": "value <= 100000",
    "value": 4380.253,
    "baseline_value": None,
    "delta": None,
    "delta_ratio": None,
    "dimension": "store.store_name",
    "dimension_key": "门店A",
    "current_window": None,
    "baseline_window": None,
    "status": "open",
    "evidence": {
        "metric_name": "库存量",
        "metric_version": "1.0",
        "unit": "quantity",
        "grain": "snapshot",
        "sql": "SECRET_SQL_SHOULD_NOT_REACH_PROMPT",
    },
}


class DiagnosisAgentTests(unittest.TestCase):
    def test_valid_output_is_normalized(self):
        seen = {}

        def fake_llm(system, user):
            seen["system"] = system
            seen["user"] = user
            return """{
              "summary": "库存异常集中在门店A",
              "hypotheses": [{
                "reason": "该门店当前库存低于阈值",
                "confidence": "high",
                "evidence_ids": ["metric_value", "dimension_scope"],
                "missing_data": ["近7日销售速度"]
              }],
              "next_checks": ["检查门店近7日销量"],
              "data_sufficiency": "medium"
            }"""

        report = DiagnosisAgent({"rules": ["库存异常需人工确认"]}, fake_llm).diagnose(CANDIDATE)
        self.assertEqual(report.anomaly_id, "abc123")
        self.assertEqual(len(report.hypotheses), 1)
        self.assertEqual(report.hypotheses[0]["confidence"], "high")
        self.assertNotIn("SECRET_SQL_SHOULD_NOT_REACH_PROMPT", seen["user"])

    def test_unknown_evidence_is_rejected(self):
        def fake_llm(system, user):
            return '{"summary":"x","hypotheses":[{"reason":"x","confidence":"low","evidence_ids":["not_allowed"],"missing_data":[]}]}'

        with self.assertRaises(DiagnosisError):
            DiagnosisAgent(llm_func=fake_llm).diagnose(CANDIDATE)

    def test_more_than_three_hypotheses_is_rejected(self):
        def fake_llm(system, user):
            hs = [{
                "reason": str(i),
                "confidence": "low",
                "evidence_ids": ["metric_value"],
                "missing_data": [],
            } for i in range(4)]
            return '{"summary":"x","hypotheses":' + str(hs).replace("'", '"') + '}'

        with self.assertRaises(DiagnosisError):
            DiagnosisAgent(llm_func=fake_llm).diagnose(CANDIDATE)


if __name__ == "__main__":
    unittest.main()

