# -*- coding: utf-8 -*-
"""建议生成与红线控制。

MVP 采用可解释的规则模板生成建议；建议默认处于待人工确认状态，
不会自动修改 Oracle 或下发外部动作。
"""
from __future__ import annotations

import hashlib
import json


class DecisionService:
    _ACTION_TEMPLATES = {
        "inventory_quantity": (
            "核查异常范围内的缺货门店与近7日销量，人工确认后从库存充足门店评估调拨。",
            "库存指标触发阈值，且商品销量补查可用于判断是否为需求驱动的缺货。",
            "调拨前确认门店营业状态、在途库存和商品保质期。",
        ),
        "sales_amount": (
            "核查异常门店、商品销量与营业状态，人工确认后制定促销或补货方案。",
            "销售额偏离基线，商品维度销量和库存证据可帮助区分需求与供给因素。",
            "促销或调拨需先确认毛利、库存和区域政策。",
        ),
        "order_count": (
            "核查订单来源、门店营业状态和异常商品，人工确认后处理渠道或履约问题。",
            "订单数偏离基线，需先排除渠道、营业日和数据延迟因素。",
            "不要仅凭订单数直接调整价格或库存。",
        ),
    }

    def generate(self, candidate, diagnosis_report, evidence_bundle=None) -> dict:
        data = candidate if isinstance(candidate, dict) else candidate.__dict__
        report = (diagnosis_report.to_dict() if hasattr(diagnosis_report, "to_dict")
                  else diagnosis_report)
        metric_id = data.get("metric_id")
        action, rationale, risk = self._ACTION_TEMPLATES.get(
            metric_id,
            ("补充缺失数据并由业务负责人人工确认后再处理。",
             "当前异常已触发监测规则，但没有足够证据自动决定经营动作。",
             "禁止在证据不足时自动执行经营动作。"),
        )
        evidence_ids = []
        catalog = report.get("evidence_catalog", {}) if isinstance(report, dict) else {}
        for hypothesis in report.get("hypotheses", []) if isinstance(report, dict) else []:
            for evidence_id in hypothesis.get("evidence_ids", []):
                if evidence_id in catalog and evidence_id not in evidence_ids:
                    evidence_ids.append(evidence_id)
        if not evidence_ids:
            evidence_ids = ["metric_value"]
        raw = json.dumps({"anomaly_id": data.get("anomaly_id"), "action": action},
                         ensure_ascii=False, sort_keys=True)
        recommendation_id = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:24]
        return {
            "recommendation_id": recommendation_id,
            "anomaly_id": data.get("anomaly_id"),
            "status": "pending_confirmation",
            "action": action,
            "rationale": rationale,
            "diagnosis_summary": report.get("summary", "") if isinstance(report, dict) else "",
            "expected_impact": "降低异常持续时间并验证根因（需业务确认后评估）。",
            "applicable_conditions": ["异常事件仍处于 open 状态", "补查数据未过期"],
            "risks": [risk],
            "suggested_owner": "门店运营/商品运营",
            "required_confirmation": True,
            "evidence_ids": evidence_ids,
            "next_checks": report.get("next_checks", []) if isinstance(report, dict) else [],
            "evidence_warnings": (evidence_bundle or {}).get("warnings", []),
        }
