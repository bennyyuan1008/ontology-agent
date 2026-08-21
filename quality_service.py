# -*- coding: utf-8 -*-
"""阶段 0：Ontology、指标和监测配置质量检查。

该检查器不连接业务库、不写业务数据，先发现配置断链和不可执行定义，
作为后续指标对账、数据新鲜度检查和异常标注的入口。
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

import query_engine as engine


@dataclass
class QualityIssue:
    level: str
    code: str
    message: str
    subject: str = ""


@dataclass
class QualityReport:
    ok: bool
    errors: list[dict]
    warnings: list[dict]
    stats: dict[str, Any]

    def to_dict(self):
        return asdict(self)


def _issue(level, code, message, subject=""):
    return asdict(QualityIssue(level, code, message, subject))


def check_config(cfg: dict) -> QualityReport:
    errors = []
    warnings = []
    objects = cfg.get("objects", {})
    metrics = cfg.get("metrics", {})
    rules = cfg.get("monitor_rules", {})
    derived = cfg.get("derived", {})

    if not isinstance(objects, dict) or not objects:
        errors.append(_issue("error", "objects.empty", "Ontology 未定义对象", "objects"))
        return QualityReport(False, errors, warnings, {})

    property_count = 0
    link_count = 0
    for object_name, obj in objects.items():
        props = obj.get("props", {})
        property_count += len(props)
        if not obj.get("table"):
            errors.append(_issue("error", "object.source_missing",
                                 "对象缺少 source 表映射", object_name))
        if not props:
            errors.append(_issue("error", "object.properties_empty",
                                 "对象没有属性定义", object_name))
        for prop_name, prop in props.items():
            if not prop.get("column") and not prop.get("expr"):
                errors.append(_issue("error", "property.mapping_missing",
                                     "属性必须配置 column 或 expr", f"{object_name}.{prop_name}"))
        for link_name, link in obj.get("links", {}).items():
            link_count += 1
            target_name = link.get("to")
            if target_name not in objects:
                errors.append(_issue("error", "link.target_missing",
                                     f"关联目标对象不存在：{target_name}",
                                     f"{object_name}.{link_name}"))
                continue
            via = link.get("via")
            if not isinstance(via, str) or not via.strip():
                errors.append(_issue("error", "link.via_missing",
                                     "关联键不能为空", f"{object_name}.{link_name}"))
            elif not any(definition.get("column") == via for definition in props.values()):
                # 关联键可以是未暴露给用户的物理 FK，这是安全且常见的配置。
                warnings.append(_issue("warning", "link.via_hidden",
                                       "关联键未作为可查询属性暴露", 
                                       f"{object_name}.{link_name}:{via}"))

    for object_name, definitions in derived.items():
        if object_name not in objects:
            errors.append(_issue("error", "derived.object_missing",
                                 "派生属性所属对象不存在", object_name))
        if not isinstance(definitions, dict):
            errors.append(_issue("error", "derived.invalid",
                                 "派生属性定义必须是对象", object_name))

    implemented_metrics = 0
    blocked_metrics = 0
    for metric_id, metric in metrics.items():
        object_name = metric.get("object")
        status = metric.get("status", "implemented")
        subject = f"metric:{metric_id}"
        if object_name not in objects:
            errors.append(_issue("error", "metric.object_missing",
                                 f"指标对象不存在：{object_name}", subject))
            continue
        if status == "implemented":
            implemented_metrics += 1
        elif status in {"planned", "blocked"}:
            blocked_metrics += 1
            warnings.append(_issue("warning", f"metric.{status}",
                                   f"指标当前不可执行：{metric.get('note', '')}", subject))
        else:
            errors.append(_issue("error", "metric.status_invalid",
                                 f"未知指标状态：{status}", subject))
        # planned/blocked 指标只记录未实现状态，不要求当前就具备可执行聚合。
        if status != "implemented":
            continue
        aggregate = metric.get("aggregate") or {}
        if aggregate.get("derived"):
            if aggregate["derived"] not in derived.get(object_name, {}):
                errors.append(_issue("error", "metric.derived_missing",
                                     f"派生属性不存在：{aggregate['derived']}", subject))
        else:
            func = aggregate.get("func")
            if func not in engine.ALLOWED_FUNCS:
                errors.append(_issue("error", "metric.aggregate_invalid",
                                     f"不支持的聚合函数：{func}", subject))
            if func != "COUNT" and not aggregate.get("property"):
                errors.append(_issue("error", "metric.property_missing",
                                     "非 COUNT 指标缺少聚合属性", subject))
            if aggregate.get("property"):
                try:
                    engine._resolve_prop(cfg, object_name, aggregate["property"])
                except Exception as exc:
                    errors.append(_issue("error", "metric.property_invalid",
                                         str(exc), subject))
        time_cfg = metric.get("time")
        if time_cfg:
            time_prop = ".".join(x for x in [time_cfg.get("link"), time_cfg.get("property")] if x)
            try:
                engine._resolve_prop(cfg, object_name, time_prop)
            except Exception as exc:
                errors.append(_issue("error", "metric.time_invalid",
                                     str(exc), subject))
        for dimension in metric.get("dimensions", []) or []:
            if not isinstance(dimension, str):
                errors.append(_issue("error", "metric.dimension_invalid",
                                     "维度必须是字符串", subject))
                continue
            if dimension.count(".") > 1:
                warnings.append(_issue("warning", "metric.dimension_multi_hop",
                                       "当前首期引擎只支持一跳关联，需拆分或扩展维度树", 
                                       f"{subject}:{dimension}"))

    for rule_id, rule in rules.items():
        metric_id = rule.get("metric_id")
        subject = f"rule:{rule_id}"
        if metric_id not in metrics:
            errors.append(_issue("error", "rule.metric_missing",
                                 f"监测规则指标不存在：{metric_id}", subject))
        elif metrics[metric_id].get("status", "implemented") != "implemented":
            warnings.append(_issue("warning", "rule.metric_not_executable",
                                   "规则引用了尚未实现的指标", subject))
        if rule.get("rule_type") not in {"fixed_threshold", "relative_change"}:
            errors.append(_issue("error", "rule.type_invalid",
                                 f"不支持的规则类型：{rule.get('rule_type')}", subject))

    stats = {
        "object_count": len(objects),
        "property_count": property_count,
        "link_count": link_count,
        "metric_count": len(metrics),
        "implemented_metric_count": implemented_metrics,
        "blocked_or_planned_metric_count": blocked_metrics,
        "monitor_rule_count": len(rules),
        "enabled_monitor_rule_count": sum(1 for rule in rules.values() if rule.get("enabled")),
    }
    return QualityReport(not errors, errors, warnings, stats)


def main():
    import argparse

    ap = argparse.ArgumentParser(description="检查 Ontology、指标和监测规则配置")
    ap.add_argument("--config", default="config/ontology_models.yaml")
    args = ap.parse_args()
    cfg = engine.load_config(args.config)
    report = check_config(cfg)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    raise SystemExit(0 if report.ok else 1)


if __name__ == "__main__":
    main()
