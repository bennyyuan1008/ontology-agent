# -*- coding: utf-8 -*-
"""确定性诊断证据补查。

该模块只调用受控 MetricService，不接受模型生成的 SQL，也不把查询结果
直接发送到外部服务；它把补查结果整理成带 ID 的事实目录，供诊断层引用。
"""
from __future__ import annotations

from typing import Optional

from metric_service import MetricService


class EvidenceService:
    def __init__(self, metric_service: MetricService):
        self.metric_service = metric_service

    @staticmethod
    def _scope_filter(candidate: dict) -> Optional[list]:
        dimension = candidate.get("dimension")
        key = candidate.get("dimension_key")
        if not dimension or key is None or "." not in str(dimension):
            return None
        link, prop = str(dimension).split(".", 1)
        return [{"link": link, "property": prop, "op": "eq", "value": key}]

    @staticmethod
    def _fact(label, value, source="metric"):
        return {"label": label, "value": value, "source": source}

    def collect(self, candidate) -> dict:
        data = candidate if isinstance(candidate, dict) else candidate.__dict__
        facts = {}
        warnings = []
        queries = []

        # 事件本身就是第一组事实；不重复查库。
        facts["metric_value"] = self._fact("当前指标值", data.get("value"), "anomaly")
        facts["baseline_value"] = self._fact("基线指标值", data.get("baseline_value"), "anomaly")
        facts["delta"] = self._fact("指标差值", data.get("delta"), "anomaly")
        facts["delta_ratio"] = self._fact("相对偏差", data.get("delta_ratio"), "anomaly")

        metric_id = data.get("metric_id")
        current = data.get("current_window")
        baseline = data.get("baseline_window")
        scope = self._scope_filter(data)
        dimension = data.get("dimension")

        # 用业务上最有价值、且已在配置中实现的指标做一跳补查。
        followups = []
        if metric_id == "inventory_quantity":
            followups.append(("sku_quantity", "product.product_name", "异常范围内商品销量"))
        elif metric_id in {"sales_amount", "order_count", "average_order_value"}:
            followups.append(("sku_quantity", "product.product_name", "异常范围内商品销量"))
            followups.append(("inventory_quantity", "product.product_name", "异常范围内商品库存"))
        elif metric_id == "sku_quantity":
            followups.append(("inventory_quantity", "product.product_name", "异常范围内商品库存"))
        # 无维度事件按全局商品维度补查；有维度事件使用该维度作为范围。
        for follow_metric, follow_dimension, label in followups:
            try:
                result = self.metric_service.evaluate_breakdown(
                    follow_metric,
                    follow_dimension,
                    current_window=current,
                    baseline_window=baseline,
                    scope_filters=scope,
                    limit=10,
                )
                queries.append({"metric_id": follow_metric, "dimension": follow_dimension,
                                "sql": result.get("sql")})
                top = sorted(result.get("items", []),
                             key=lambda x: abs(x.get("value") or 0), reverse=True)[:10]
                fact_id = "breakdown_" + follow_metric
                facts[fact_id] = self._fact(label, top, "metric_breakdown")
            except Exception as exc:  # 补查失败应降级为缺失数据，而不是阻断异常处理。
                warnings.append(f"{follow_metric} 补查失败：{type(exc).__name__}: {exc}")

        if dimension and data.get("dimension_key") is not None:
            facts["dimension_scope"] = self._fact(
                "异常维度范围", {"dimension": dimension, "key": data.get("dimension_key")}, "anomaly"
            )
        return {"facts": facts, "evidence_ids": list(facts), "queries": queries,
                "warnings": warnings}
