# -*- coding: utf-8 -*-
"""确定性经营异常监测服务。

MVP 规则：
- fixed_threshold：当前指标高于/低于固定阈值；
- relative_change：相对基线同比/环比偏差达到阈值；
- anomaly 以规则+范围+窗口生成稳定 ID，SQLite 控制库负责去重和留痕。
"""
import hashlib
import json
import os
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Optional

from metric_service import MetricService, MetricSnapshot


RULE_TYPES = {"fixed_threshold", "relative_change"}
SEVERITIES = {"P0", "P1", "P2", "P3"}


@dataclass
class AnomalyCandidate:
    anomaly_id: str
    rule_id: str
    metric_id: str
    severity: str
    trigger: str
    value: float
    baseline_value: Optional[float]
    delta: Optional[float]
    delta_ratio: Optional[float]
    current_window: Optional[dict]
    baseline_window: Optional[dict]
    dimension: Optional[str]
    dimension_key: Optional[str]
    status: str
    dedupe_key: str
    evidence: dict


class SQLiteMonitorStore:
    """只保存 Agent 控制面数据，不写 Oracle 业务库。"""

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(os.path.abspath(path))
        os.makedirs(parent, exist_ok=True)
        self._init_schema()

    def _connect(self):
        return sqlite3.connect(self.path)

    def _init_schema(self):
        con = self._connect()
        try:
            con.executescript(
                """
                CREATE TABLE IF NOT EXISTS monitor_rules (
                    rule_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS anomaly_events (
                    anomaly_id TEXT PRIMARY KEY,
                    rule_id TEXT NOT NULL,
                    dedupe_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_anomaly_rule
                    ON anomaly_events(rule_id, created_at);
                CREATE TABLE IF NOT EXISTS recommendations (
                    recommendation_id TEXT PRIMARY KEY,
                    anomaly_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS tasks (
                    task_id TEXT PRIMARY KEY,
                    recommendation_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    assignee TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS feedback (
                    feedback_id TEXT PRIMARY KEY,
                    anomaly_id TEXT NOT NULL,
                    actor TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    event_id TEXT PRIMARY KEY,
                    event_type TEXT NOT NULL,
                    actor TEXT,
                    payload TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                """
            )
            con.commit()
        finally:
            con.close()

    def save_rule(self, rule: dict):
        now = datetime.now(timezone.utc).isoformat()
        con = self._connect()
        try:
            con.execute(
                "INSERT INTO monitor_rules(rule_id,payload,updated_at) VALUES(?,?,?) "
                "ON CONFLICT(rule_id) DO UPDATE SET payload=excluded.payload, updated_at=excluded.updated_at",
                (rule["id"], json.dumps(rule, ensure_ascii=False), now),
            )
            con.commit()
        finally:
            con.close()

    def save_anomaly(self, candidate: AnomalyCandidate) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        con = self._connect()
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO anomaly_events "
                "(anomaly_id,rule_id,dedupe_key,status,severity,payload,created_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (
                    candidate.anomaly_id,
                    candidate.rule_id,
                    candidate.dedupe_key,
                    candidate.status,
                    candidate.severity,
                    json.dumps(asdict(candidate), ensure_ascii=False),
                    now,
                ),
            )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()

    def count_anomalies(self) -> int:
        con = self._connect()
        try:
            return con.execute("SELECT COUNT(*) FROM anomaly_events").fetchone()[0]
        finally:
            con.close()

    def list_anomalies(self, limit: int = 100) -> list:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT payload FROM anomaly_events ORDER BY created_at DESC LIMIT ?",
                (min(max(int(limit), 1), 500),),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            con.close()

    def save_recommendation(self, recommendation: dict) -> bool:
        now = datetime.now(timezone.utc).isoformat()
        con = self._connect()
        try:
            cur = con.execute(
                "INSERT OR IGNORE INTO recommendations "
                "(recommendation_id,anomaly_id,status,payload,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (recommendation["recommendation_id"], recommendation["anomaly_id"],
                 recommendation["status"], json.dumps(recommendation, ensure_ascii=False), now, now),
            )
            con.commit()
            return cur.rowcount == 1
        finally:
            con.close()

    def list_recommendations(self, limit: int = 100) -> list:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT payload FROM recommendations ORDER BY created_at DESC LIMIT ?",
                (min(max(int(limit), 1), 500),),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            con.close()

    def get_recommendation(self, recommendation_id: str) -> dict:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT payload FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"未知建议：{recommendation_id}")
            return json.loads(row[0])
        finally:
            con.close()

    def update_recommendation(self, recommendation_id: str, status: str,
                              actor: str, note: str = "") -> dict:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT payload FROM recommendations WHERE recommendation_id = ?",
                (recommendation_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"未知建议：{recommendation_id}")
            payload = json.loads(row[0])
            if payload["status"] not in {"pending_confirmation", "accepted", "rejected", "edited"}:
                raise ValueError("建议状态不允许变更")
            if status not in {"accepted", "rejected", "edited"}:
                raise ValueError("建议目标状态不合法")
            payload.update({"status": status, "decision_actor": actor, "decision_note": note[:500]})
            now = datetime.now(timezone.utc).isoformat()
            con.execute(
                "UPDATE recommendations SET status=?,payload=?,updated_at=? WHERE recommendation_id=?",
                (status, json.dumps(payload, ensure_ascii=False), now, recommendation_id),
            )
            con.commit()
            self.audit("recommendation." + status, actor, payload)
            return payload
        finally:
            con.close()

    def create_task(self, recommendation: dict, assignee: str = "") -> dict:
        if recommendation.get("status") != "accepted":
            raise ValueError("只有已采纳建议才能创建任务")
        task_id = hashlib.sha256(
            (recommendation["recommendation_id"] + assignee).encode("utf-8")
        ).hexdigest()[:24]
        task = {
            "task_id": task_id,
            "recommendation_id": recommendation["recommendation_id"],
            "anomaly_id": recommendation["anomaly_id"],
            "status": "created",
            "assignee": assignee,
            "title": recommendation["action"],
            "note": recommendation.get("rationale", ""),
        }
        now = datetime.now(timezone.utc).isoformat()
        con = self._connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO tasks "
                "(task_id,recommendation_id,status,assignee,payload,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (task_id, task["recommendation_id"], task["status"], assignee,
                 json.dumps(task, ensure_ascii=False), now, now),
            )
            con.commit()
            self.audit("task.created", assignee, task)
            return task
        finally:
            con.close()

    def list_tasks(self, limit: int = 100) -> list:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT payload FROM tasks ORDER BY created_at DESC LIMIT ?",
                (min(max(int(limit), 1), 500),),
            ).fetchall()
            return [json.loads(row[0]) for row in rows]
        finally:
            con.close()

    def add_feedback(self, anomaly_id: str, actor: str, feedback: dict) -> dict:
        feedback_id = hashlib.sha256(
            (anomaly_id + actor + json.dumps(feedback, ensure_ascii=False, sort_keys=True)).encode("utf-8")
        ).hexdigest()[:24]
        payload = {"feedback_id": feedback_id, "anomaly_id": anomaly_id, **feedback}
        now = datetime.now(timezone.utc).isoformat()
        con = self._connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO feedback(feedback_id,anomaly_id,actor,payload,created_at) "
                "VALUES(?,?,?,?,?)",
                (feedback_id, anomaly_id, actor, json.dumps(payload, ensure_ascii=False), now),
            )
            con.commit()
            self.audit("feedback.created", actor, payload)
            return payload
        finally:
            con.close()

    def audit(self, event_type: str, actor: str, payload: dict) -> str:
        event_id = hashlib.sha256(
            (event_type + actor + json.dumps(payload, ensure_ascii=False, sort_keys=True)).encode("utf-8")
        ).hexdigest()[:24]
        now = datetime.now(timezone.utc).isoformat()
        con = self._connect()
        try:
            con.execute(
                "INSERT OR IGNORE INTO audit_events(event_id,event_type,actor,payload,created_at) "
                "VALUES(?,?,?,?,?)",
                (event_id, event_type, actor, json.dumps(payload, ensure_ascii=False), now),
            )
            con.commit()
            return event_id
        finally:
            con.close()


class MonitorService:
    def __init__(self, cfg: dict, metric_service: MetricService,
                 store: Optional[SQLiteMonitorStore] = None):
        self.cfg = cfg
        self.metric_service = metric_service
        self.store = store

    @staticmethod
    def _validate_rule(rule: dict):
        if not isinstance(rule, dict) or not rule.get("id"):
            raise ValueError("监测规则必须包含 id")
        if rule.get("metric_id") not in rule.get("_metric_ids", {rule.get("metric_id")}):
            raise ValueError("监测规则 metric_id 不合法")
        if rule.get("rule_type") not in RULE_TYPES:
            raise ValueError("不支持的监测规则类型")
        if rule.get("severity", "P2") not in SEVERITIES:
            raise ValueError("severity 必须是 P0-P3")
        if rule.get("enabled") is False:
            raise ValueError("监测规则未启用")
        threshold = rule.get("threshold")
        if isinstance(threshold, bool) or not isinstance(threshold, (int, float)) or threshold < 0:
            raise ValueError("threshold 必须是非负数字")
        direction = rule.get("direction")
        allowed = {"fixed_threshold": {"below", "above"},
                   "relative_change": {"down", "up", "abs"}}
        if direction not in allowed[rule["rule_type"]]:
            raise ValueError("监测规则 direction 不合法")

    @staticmethod
    def _trigger_values(rule: dict, value, delta_ratio):
        if not isinstance(value, (int, float)):
            return None
        threshold = rule["threshold"]
        if rule["rule_type"] == "fixed_threshold":
            if rule["direction"] == "below" and value <= threshold:
                return f"value <= {threshold}"
            if rule["direction"] == "above" and value >= threshold:
                return f"value >= {threshold}"
            return None
        ratio = delta_ratio
        if ratio is None:
            return None
        if rule["direction"] == "down" and ratio <= -threshold:
            return f"delta_ratio <= {-threshold}"
        if rule["direction"] == "up" and ratio >= threshold:
            return f"delta_ratio >= {threshold}"
        if rule["direction"] == "abs" and abs(ratio) >= threshold:
            return f"abs(delta_ratio) >= {threshold}"
        return None

    @staticmethod
    def _trigger(rule: dict, snapshot: MetricSnapshot):
        return MonitorService._trigger_values(rule, snapshot.value, snapshot.delta_ratio)

    @staticmethod
    def _dedupe_key(rule_id: str, current_window: Optional[dict],
                    baseline_window: Optional[dict], scope_filters, dimension,
                    dimension_key=None):
        raw = json.dumps(
            {
                "rule_id": rule_id,
                "current_window": current_window,
                "baseline_window": baseline_window,
                "scope_filters": scope_filters or [],
                "dimension": dimension,
                "dimension_key": dimension_key,
            },
            ensure_ascii=False,
            sort_keys=True,
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def run_rule(self, rule_id: str, current_window: Optional[dict] = None,
                 baseline_window: Optional[dict] = None,
                 scope_filters: Optional[list] = None, dimension=None):
        if dimension is not None:
            return self.run_dimension_rule(
                rule_id, dimension, current_window, baseline_window, scope_filters
            )
        rule = self.cfg.get("monitor_rules", {}).get(rule_id)
        if not rule:
            raise ValueError(f"未知监测规则：{rule_id}")
        rule = dict(rule)
        rule["_metric_ids"] = set(self.cfg.get("metrics", {}))
        self._validate_rule(rule)

        snapshot = self.metric_service.evaluate(
            rule["metric_id"],
            current_window=current_window,
            baseline_window=baseline_window,
            scope_filters=scope_filters,
            dimension=dimension,
        )
        trigger = self._trigger(rule, snapshot)
        if trigger is None:
            return None

        dedupe_key = self._dedupe_key(
            rule_id, current_window, baseline_window, scope_filters, dimension
        )
        anomaly_id = hashlib.sha256(dedupe_key.encode("ascii")).hexdigest()[:24]
        candidate = AnomalyCandidate(
            anomaly_id=anomaly_id,
            rule_id=rule_id,
            metric_id=rule["metric_id"],
            severity=rule.get("severity", "P2"),
            trigger=trigger,
            value=snapshot.value,
            baseline_value=snapshot.baseline_value,
            delta=snapshot.delta,
            delta_ratio=snapshot.delta_ratio,
            current_window=current_window,
            baseline_window=baseline_window,
            dimension=None,
            dimension_key=None,
            status="open",
            dedupe_key=dedupe_key,
            evidence=snapshot.evidence,
        )
        if self.store:
            self.store.save_anomaly(candidate)
        return candidate

    def run_dimension_rule(self, rule_id: str, dimension: str,
                           current_window: Optional[dict] = None,
                           baseline_window: Optional[dict] = None,
                           scope_filters: Optional[list] = None):
        """按门店/SKU 等一跳维度扫描，返回所有触发项。"""
        rule = self.cfg.get("monitor_rules", {}).get(rule_id)
        if not rule:
            raise ValueError(f"未知监测规则：{rule_id}")
        rule = dict(rule)
        rule["_metric_ids"] = set(self.cfg.get("metrics", {}))
        self._validate_rule(rule)
        breakdown = self.metric_service.evaluate_breakdown(
            rule["metric_id"], dimension, current_window, baseline_window,
            scope_filters=scope_filters,
        )
        candidates = []
        for item in breakdown["items"]:
            trigger = self._trigger_values(rule, item["value"], item["delta_ratio"])
            if trigger is None:
                continue
            dimension_key = item["key"]
            dedupe_key = self._dedupe_key(
                rule_id, current_window, baseline_window, scope_filters,
                dimension, dimension_key,
            )
            anomaly_id = hashlib.sha256(dedupe_key.encode("ascii")).hexdigest()[:24]
            evidence = dict(breakdown["evidence"])
            evidence["dimension_item"] = item
            candidate = AnomalyCandidate(
                anomaly_id=anomaly_id,
                rule_id=rule_id,
                metric_id=rule["metric_id"],
                severity=rule.get("severity", "P2"),
                trigger=trigger,
                value=item["value"],
                baseline_value=item["baseline_value"],
                delta=item["delta"],
                delta_ratio=item["delta_ratio"],
                current_window=current_window,
                baseline_window=baseline_window,
                dimension=dimension,
                dimension_key=dimension_key,
                status="open",
                dedupe_key=dedupe_key,
                evidence=evidence,
            )
            if self.store:
                self.store.save_anomaly(candidate)
            candidates.append(candidate)
        return candidates

    def scan(self, current_window: Optional[dict] = None,
              baseline_window: Optional[dict] = None):
        candidates = []
        for rule_id, rule in self.cfg.get("monitor_rules", {}).items():
            if rule.get("enabled"):
                candidate = self.run_rule(rule_id, current_window, baseline_window)
                if candidate:
                    candidates.append(candidate)
        return candidates
