# -*- coding: utf-8 -*-
"""异常 -> 证据 -> 诊断 -> 建议 -> 人工确认的最小编排器。"""
from __future__ import annotations

from dataclasses import asdict

from decision_service import DecisionService
from diagnosis_agent import DiagnosisAgent
from evidence_service import EvidenceService
from monitor_service import MonitorService, SQLiteMonitorStore


class MVPOrchestrator:
    def __init__(self, cfg, metric_service, monitor_service: MonitorService,
                 diagnosis_agent: DiagnosisAgent | None = None,
                 evidence_service: EvidenceService | None = None,
                 decision_service: DecisionService | None = None,
                 store: SQLiteMonitorStore | None = None):
        self.cfg = cfg
        self.metric_service = metric_service
        self.monitor_service = monitor_service
        self.store = store or monitor_service.store
        self.evidence_service = evidence_service or EvidenceService(metric_service)
        self.diagnosis_agent = diagnosis_agent or DiagnosisAgent(cfg)
        self.decision_service = decision_service or DecisionService()

    @staticmethod
    def _candidate_dict(candidate):
        return asdict(candidate) if hasattr(candidate, "__dataclass_fields__") else dict(candidate)

    def process_candidate(self, candidate) -> dict:
        payload = self._candidate_dict(candidate)
        bundle = self.evidence_service.collect(payload)
        payload["evidence"] = dict(payload.get("evidence") or {})
        payload["evidence"]["facts"] = bundle.get("facts", {})
        payload["evidence"]["warnings"] = bundle.get("warnings", [])
        report = self.diagnosis_agent.diagnose(payload)
        recommendation = self.decision_service.generate(payload, report, bundle)
        if self.store:
            self.store.save_recommendation(recommendation)
            self.store.audit("diagnosis.completed", "agent", {
                "anomaly_id": payload.get("anomaly_id"),
                "recommendation_id": recommendation.get("recommendation_id"),
            })
        return {
            "anomaly": payload,
            "evidence": bundle,
            "diagnosis": report.to_dict(),
            "recommendation": recommendation,
        }

    def run_rule(self, rule_id, current_window=None, baseline_window=None,
                 scope_filters=None, dimension=None) -> list:
        result = self.monitor_service.run_rule(
            rule_id, current_window=current_window,
            baseline_window=baseline_window, scope_filters=scope_filters,
            dimension=dimension,
        )
        candidates = result if isinstance(result, list) else ([result] if result else [])
        return [self.process_candidate(candidate) for candidate in candidates]

    def confirm_recommendation(self, recommendation_id: str, actor: str,
                               accepted: bool, note: str = "", assignee: str = "") -> dict:
        if not actor or not isinstance(actor, str):
            raise ValueError("actor 必须是非空字符串")
        status = "accepted" if accepted else "rejected"
        recommendation = self.store.update_recommendation(
            recommendation_id, status, actor, note
        )
        task = None
        if accepted:
            task = self.store.create_task(recommendation, assignee=assignee)
        self.store.audit("recommendation.confirmed", actor, {
            "recommendation_id": recommendation_id, "status": status,
            "task_id": task.get("task_id") if task else None,
        })
        return {"recommendation": recommendation, "task": task}
