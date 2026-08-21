# -*- coding: utf-8 -*-
"""受控经营指标服务。

指标定义来自 Ontology 配置，Agent 不能传入 SQL 表达式；查询统一经过
query_engine 的计划校验和参数绑定，并返回可回放的 MetricSnapshot。
"""
from dataclasses import asdict, dataclass
from typing import Any, Optional

import query_engine as engine


@dataclass
class MetricSnapshot:
    metric_id: str
    metric_name: str
    metric_version: str
    current_window: Optional[dict]
    baseline_window: Optional[dict]
    value: Any
    baseline_value: Any
    delta: Any
    delta_ratio: Optional[float]
    rows: list
    sql: str
    bind_params: dict
    evidence: dict

    def to_dict(self) -> dict:
        return asdict(self)


class MetricService:
    def __init__(self, cfg: dict, dialect: str = "oracle", db_path: str = None,
                 oracle_cfg: str = None):
        self.cfg = cfg
        self.dialect = dialect
        self.db_path = db_path
        self.oracle_cfg = oracle_cfg

    def metric(self, metric_id: str) -> dict:
        metric = self.cfg.get("metrics", {}).get(metric_id)
        if not metric:
            raise ValueError(f"未知指标：{metric_id}")
        if metric.get("status", "implemented") != "implemented":
            raise ValueError(f"指标 {metric_id} 尚未实现：{metric.get('note', '')}")
        return metric

    @staticmethod
    def _window_filter(time_cfg: dict, window: Optional[dict]) -> Optional[dict]:
        if not window:
            return None
        if not isinstance(window, dict) or "start" not in window or "end" not in window:
            raise ValueError("时间窗口必须包含 start 和 end")
        entry = {"property": time_cfg["property"], "op": "between",
                 "value": [window["start"], window["end"]]}
        if time_cfg.get("link"):
            entry["link"] = time_cfg["link"]
        return entry

    @staticmethod
    def _dimension_entry(dimension):
        if dimension is None:
            return []
        if isinstance(dimension, dict):
            return [dimension]
        if not isinstance(dimension, str):
            raise ValueError("dimension 必须是字符串或对象")
        parts = dimension.split(".")
        if len(parts) == 1:
            return [{"property": parts[0]}]
        if len(parts) == 2:
            return [{"link": parts[0], "property": parts[1]}]
        raise ValueError("首期指标维度只支持对象属性或一跳关联属性")

    def build_plan(self, metric_id: str, current_window: Optional[dict] = None,
                   scope_filters: Optional[list] = None, dimension=None,
                   limit: int = 200) -> dict:
        metric = self.metric(metric_id)
        plan = {
            "object": metric["object"],
            "aggregate": metric["aggregate"],
            "filters": list(metric.get("filters", [])),
            "group_by": self._dimension_entry(dimension),
            "limit": limit,
        }
        if metric.get("time"):
            time_filter = self._window_filter(metric["time"], current_window)
            if time_filter:
                plan["filters"].append(time_filter)
        if scope_filters:
            if not isinstance(scope_filters, list):
                raise ValueError("scope_filters 必须是数组")
            plan["filters"].extend(scope_filters)
        return plan

    def compile(self, metric_id: str, current_window: Optional[dict] = None,
                scope_filters: Optional[list] = None, dimension=None,
                limit: int = 200):
        plan = self.build_plan(metric_id, current_window, scope_filters, dimension, limit)
        engine.DIALECT = self.dialect
        sql, bind_params = engine.translate_bound(self.cfg, plan)
        engine.check_safety(sql)
        return plan, sql, bind_params

    @staticmethod
    def _value(rows: list):
        if not rows:
            return None
        row = rows[0]
        for key, value in row.items():
            if key.lower() == "agg_result":
                return value
        return next(iter(row.values()), None)

    def _execute(self, sql: str, bind_params: dict):
        return engine.execute(self.dialect, sql, db_path=self.db_path,
                              oracle_cfg=self.oracle_cfg, bind_params=bind_params)

    def evaluate(self, metric_id: str, current_window: Optional[dict] = None,
                 baseline_window: Optional[dict] = None,
                 scope_filters: Optional[list] = None, dimension=None,
                 limit: int = 200) -> MetricSnapshot:
        metric = self.metric(metric_id)
        plan, sql, bind_params = self.compile(metric_id, current_window, scope_filters,
                                               dimension, limit)
        rows = self._execute(sql, bind_params)
        value = self._value(rows)
        baseline_value = None
        if baseline_window:
            baseline_plan, baseline_sql, baseline_params = self.compile(
                metric_id, baseline_window, scope_filters, dimension, limit)
            baseline_rows = self._execute(baseline_sql, baseline_params)
            baseline_value = self._value(baseline_rows)
        delta = None if value is None or baseline_value is None else value - baseline_value
        delta_ratio = None
        if baseline_value not in (None, 0) and delta is not None:
            delta_ratio = delta / baseline_value
        evidence = {
            "metric_id": metric_id,
            "metric_name": metric["name"],
            "metric_version": metric.get("version", "1.0"),
            "object": metric["object"],
            "unit": metric.get("unit"),
            "grain": metric.get("grain"),
            "freshness": metric.get("freshness"),
            "current_window": current_window,
            "baseline_window": baseline_window,
            "plan": plan,
            "sql": sql,
        }
        return MetricSnapshot(
            metric_id=metric_id,
            metric_name=metric["name"],
            metric_version=metric.get("version", "1.0"),
            current_window=current_window,
            baseline_window=baseline_window,
            value=value,
            baseline_value=baseline_value,
            delta=delta,
            delta_ratio=delta_ratio,
            rows=rows,
            sql=sql,
            bind_params=bind_params,
            evidence=evidence,
        )

    @staticmethod
    def _column(row: dict, name: str):
        for key, value in row.items():
            if key.lower() == name.lower():
                return value
        return None

    def evaluate_breakdown(self, metric_id: str, dimension: str,
                           current_window: Optional[dict] = None,
                           baseline_window: Optional[dict] = None,
                           scope_filters: Optional[list] = None,
                           limit: int = 200) -> dict:
        """按一个受控维度返回当前/基线对比，供门店或 SKU 级监测使用。"""
        if not dimension:
            raise ValueError("breakdown 必须指定 dimension")
        metric = self.metric(metric_id)
        plan, sql, bind_params = self.compile(
            metric_id, current_window, scope_filters, dimension, limit)
        rows = self._execute(sql, bind_params)
        baseline_rows = []
        baseline_sql = None
        baseline_params = {}
        if baseline_window:
            _, baseline_sql, baseline_params = self.compile(
                metric_id, baseline_window, scope_filters, dimension, limit)
            baseline_rows = self._execute(baseline_sql, baseline_params)

        def as_map(source):
            result = {}
            for row in source:
                key = self._column(row, "group_0")
                if key is None:
                    key = "<NULL>"
                result[str(key)] = self._column(row, "agg_result")
            return result

        current_map = as_map(rows)
        baseline_map = as_map(baseline_rows)
        items = []
        for key in current_map.keys() | baseline_map.keys():
            value = current_map.get(key)
            baseline = baseline_map.get(key)
            delta = None if value is None or baseline is None else value - baseline
            ratio = None
            if baseline not in (None, 0) and delta is not None:
                ratio = delta / baseline
            items.append({
                "dimension": dimension,
                "key": key,
                "value": value,
                "baseline_value": baseline,
                "delta": delta,
                "delta_ratio": ratio,
            })
        items.sort(key=lambda x: x["key"])
        return {
            "metric_id": metric_id,
            "metric_name": metric["name"],
            "metric_version": metric.get("version", "1.0"),
            "dimension": dimension,
            "current_window": current_window,
            "baseline_window": baseline_window,
            "items": items,
            "rows": rows,
            "sql": sql,
            "bind_params": bind_params,
            "evidence": {
                "metric_id": metric_id,
                "metric_name": metric["name"],
                "metric_version": metric.get("version", "1.0"),
                "object": metric["object"],
                "unit": metric.get("unit"),
                "grain": metric.get("grain"),
                "dimension": dimension,
                "current_window": current_window,
                "baseline_window": baseline_window,
                "plan": plan,
                "sql": sql,
                "baseline_sql": baseline_sql,
            },
        }
