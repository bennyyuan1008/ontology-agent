# -*- coding: utf-8 -*-
"""极简异常诊断 Agent：异常证据 -> 结构化原因假设。

LLM 只负责组织假设和表达，不负责查库、生成 SQL 或执行经营动作。
"""
import argparse
import json
import os
import re
from dataclasses import asdict, dataclass
from typing import Callable, Optional


MAX_HYPOTHESES = 3
CONFIDENCE = {"high", "medium", "low"}
TOP_KEYS = {"summary", "hypotheses", "next_checks", "data_sufficiency"}
HYPOTHESIS_KEYS = {"reason", "confidence", "evidence_ids", "missing_data"}


class DiagnosisError(ValueError):
    pass


@dataclass
class DiagnosisReport:
    summary: str
    hypotheses: list
    next_checks: list
    data_sufficiency: str
    evidence_catalog: dict
    anomaly_id: Optional[str] = None

    def to_dict(self):
        return asdict(self)


def _default_llm(system: str, user: str) -> str:
    # 延迟导入，离线测试和服务启动不会因为缺少 API Key 失败。
    from run_agent import llm
    return llm(system, user, temperature=0.1, max_tokens=1000)


def _candidate_dict(candidate) -> dict:
    if isinstance(candidate, dict):
        return candidate
    try:
        return asdict(candidate)
    except TypeError as exc:
        raise DiagnosisError("诊断输入必须是字典或 AnomalyCandidate") from exc


def _evidence_catalog(candidate: dict) -> dict:
    evidence = candidate.get("evidence") or {}
    item = evidence.get("dimension_item") or {}
    catalog = {
        "metric_value": {
            "label": "当前指标值",
            "value": candidate.get("value"),
        },
        "baseline_value": {
            "label": "基线指标值",
            "value": candidate.get("baseline_value"),
        },
        "delta": {
            "label": "指标差值",
            "value": candidate.get("delta"),
        },
        "delta_ratio": {
            "label": "相对偏差",
            "value": candidate.get("delta_ratio"),
        },
        "metric_definition": {
            "label": "指标定义",
            "value": {
                "metric_id": candidate.get("metric_id"),
                "metric_name": evidence.get("metric_name"),
                "metric_version": evidence.get("metric_version"),
                "unit": evidence.get("unit"),
                "grain": evidence.get("grain"),
            },
        },
    }
    if candidate.get("dimension") or candidate.get("dimension_key"):
        catalog["dimension_scope"] = {
            "label": "异常维度范围",
            "value": {
                "dimension": candidate.get("dimension"),
                "key": candidate.get("dimension_key"),
            },
        }
    if item:
        catalog["dimension_item"] = {
            "label": "维度对比明细",
            "value": item,
        }
    # EvidenceService 产生的补查事实沿用白名单 ID，模型只能引用这些事实。
    facts = evidence.get("facts")
    if isinstance(facts, dict):
        for fact_id, fact in facts.items():
            if not isinstance(fact_id, str) or not isinstance(fact, dict):
                continue
            catalog[fact_id] = {
                "label": fact.get("label", fact_id),
                "value": fact.get("value"),
            }
    return catalog


def _clean_json(text: str) -> dict:
    raw = (text or "").strip()
    fence = chr(96) * 3
    if raw.startswith(fence):
        raw = re.sub(r"^" + fence + r"(?:json)?\s*", "", raw, flags=re.I)
        raw = re.sub(r"\s*" + fence + r"$", "", raw)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        start, end = raw.find("{"), raw.rfind("}")
        if start < 0 or end <= start:
            raise DiagnosisError("诊断模型未返回 JSON")
        try:
            return json.loads(raw[start:end + 1])
        except json.JSONDecodeError as exc:
            raise DiagnosisError("诊断模型返回的 JSON 无法解析") from exc


def _text(value, field: str, max_len: int = 500) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DiagnosisError(f"{field} 必须是非空字符串")
    value = value.strip()
    if len(value) > max_len:
        raise DiagnosisError(f"{field} 超过长度限制")
    return value


class DiagnosisAgent:
    def __init__(self, cfg: Optional[dict] = None,
                 llm_func: Optional[Callable[[str, str], str]] = None):
        self.cfg = cfg or {}
        self.llm_func = llm_func or _default_llm

    def _prompt(self, candidate: dict, catalog: dict):
        rules = self.cfg.get("rules", [])
        system = (
            "你是零售经营异常诊断助手。只能根据给定证据提出原因假设，"
            "不能查询数据库、生成SQL、虚构数据或直接下发经营动作。"
            "必须区分事实和推断；证据不足时降低置信度并列出缺失数据。"
            "只输出 JSON，不要 Markdown。输出最多 3 条 hypotheses。"
            "evidence_ids 只能引用给定证据目录中的 ID。"
            "JSON 字段为 summary、hypotheses、next_checks、data_sufficiency；"
            "hypotheses 每项字段为 reason、confidence、evidence_ids、missing_data。"
            f"业务规则：{json.dumps(rules, ensure_ascii=False)}"
        )
        user = json.dumps(
            {
                "anomaly": {
                    "anomaly_id": candidate.get("anomaly_id"),
                    "rule_id": candidate.get("rule_id"),
                    "metric_id": candidate.get("metric_id"),
                    "severity": candidate.get("severity"),
                    "trigger": candidate.get("trigger"),
                    "current_window": candidate.get("current_window"),
                    "baseline_window": candidate.get("baseline_window"),
                },
                "evidence_catalog": catalog,
            },
            ensure_ascii=False,
        )
        return system, user

    def _validate(self, output: dict, catalog: dict, candidate: dict) -> DiagnosisReport:
        if not isinstance(output, dict):
            raise DiagnosisError("诊断结果必须是 JSON 对象")
        unknown = set(output) - TOP_KEYS
        if unknown:
            raise DiagnosisError(f"诊断结果包含未知字段：{sorted(unknown)}")
        summary = _text(output.get("summary"), "summary")
        hypotheses = output.get("hypotheses")
        if not isinstance(hypotheses, list) or not hypotheses or len(hypotheses) > MAX_HYPOTHESES:
            raise DiagnosisError("hypotheses 必须是 1-3 条")
        validated = []
        for index, hypothesis in enumerate(hypotheses):
            if not isinstance(hypothesis, dict):
                raise DiagnosisError(f"hypotheses[{index}] 必须是对象")
            unknown_h = set(hypothesis) - HYPOTHESIS_KEYS
            if unknown_h:
                raise DiagnosisError(f"hypotheses[{index}] 包含未知字段：{sorted(unknown_h)}")
            reason = _text(hypothesis.get("reason"), f"hypotheses[{index}].reason")
            confidence = hypothesis.get("confidence")
            if confidence not in CONFIDENCE:
                raise DiagnosisError(f"hypotheses[{index}].confidence 不合法")
            ids = hypothesis.get("evidence_ids")
            if not isinstance(ids, list) or not ids or any(i not in catalog for i in ids):
                raise DiagnosisError(f"hypotheses[{index}].evidence_ids 引用了未知证据")
            missing = hypothesis.get("missing_data", [])
            if not isinstance(missing, list) or any(not isinstance(x, str) for x in missing):
                raise DiagnosisError(f"hypotheses[{index}].missing_data 必须是字符串数组")
            validated.append({
                "reason": reason,
                "confidence": confidence,
                "evidence_ids": ids,
                "missing_data": [x[:200] for x in missing[:5]],
            })
        next_checks = output.get("next_checks", [])
        if not isinstance(next_checks, list) or any(not isinstance(x, str) for x in next_checks):
            raise DiagnosisError("next_checks 必须是字符串数组")
        sufficiency = output.get("data_sufficiency", "medium")
        if sufficiency not in CONFIDENCE:
            raise DiagnosisError("data_sufficiency 必须是 high/medium/low")
        return DiagnosisReport(
            summary=summary,
            hypotheses=validated,
            next_checks=[x[:200] for x in next_checks[:5]],
            data_sufficiency=sufficiency,
            evidence_catalog=catalog,
            anomaly_id=candidate.get("anomaly_id"),
        )

    def diagnose(self, candidate) -> DiagnosisReport:
        candidate_data = _candidate_dict(candidate)
        catalog = _evidence_catalog(candidate_data)
        system, user = self._prompt(candidate_data, catalog)
        output = _clean_json(self.llm_func(system, user))
        return self._validate(output, catalog, candidate_data)


def main():
    ap = argparse.ArgumentParser(description="对异常事件生成结构化原因假设")
    ap.add_argument("--candidate-json", required=True, help="AnomalyCandidate JSON 文件路径")
    ap.add_argument("--config", default="config/ontology_models.yaml")
    args = ap.parse_args()

    import query_engine
    cfg = query_engine.load_config(args.config)
    with open(args.candidate_json, encoding="utf-8") as f:
        candidate = json.load(f)
    report = DiagnosisAgent(cfg).diagnose(candidate)
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
