# -*- coding: utf-8 -*-
"""轻量监测调度器：支持单次运行和本地进程内 interval 循环。"""
from __future__ import annotations

import argparse
import json
import os
import time

import query_engine
from agent_pipeline import MVPOrchestrator
from diagnosis_agent import DiagnosisAgent
from metric_service import MetricService
from monitor_service import MonitorService, SQLiteMonitorStore


def _window(value):
    return json.loads(value) if value else None


def _oracle_connection_value(value):
    """将 local/oracle_conn.local.json 或连接串统一转换为执行器格式。"""
    if not value:
        return query_engine._load_oracle_cfg(None)
    if isinstance(value, str) and os.path.isfile(value):
        with open(value, encoding="utf-8") as handle:
            payload = json.load(handle)
        return " ".join(f"{key}={item}" for key, item in payload.items())
    return value


def run_once(config_path="config/ontology_models.yaml", oracle_cfg=None,
             state_path="local/monitor_control.sqlite3", rule_id=None,
             current_window=None, baseline_window=None, dimension=None):
    cfg = query_engine.load_config(config_path)
    store = SQLiteMonitorStore(state_path)
    service = MetricService(cfg, dialect="oracle",
                            oracle_cfg=_oracle_connection_value(oracle_cfg))
    monitor = MonitorService(cfg, service, store=store)
    for configured_rule in cfg.get("monitor_rules", {}).values():
        store.save_rule(configured_rule)
    # 默认离线诊断，避免把 Oracle 事实发送到外部模型；如需接入 LLM，
    # 由上层显式注入 DiagnosisAgent(llm_func=...)。
    def offline_llm(_system, user):
        request = json.loads(user)
        catalog = request.get("evidence_catalog", {})
        ids = list(catalog)[:2] or ["metric_value"]
        return json.dumps({
            "summary": "异常已由规则触发，当前仅生成待确认的事实假设。",
            "hypotheses": [{
                "reason": "指标偏离监测阈值，需结合补查证据确认业务根因。",
                "confidence": "low",
                "evidence_ids": ids,
                "missing_data": ["门店营业状态", "渠道/履约明细"],
            }],
            "next_checks": ["核查门店营业状态", "核查渠道与履约数据"],
            "data_sufficiency": "low",
        }, ensure_ascii=False)
    orchestrator = MVPOrchestrator(
        cfg, service, monitor,
        diagnosis_agent=DiagnosisAgent(cfg, llm_func=offline_llm),
        store=store,
    )
    rule_ids = [rule_id] if rule_id else [
        rid for rid, rule in cfg.get("monitor_rules", {}).items()
        if rule.get("enabled")
    ]
    results = []
    for rid in rule_ids:
        results.extend(orchestrator.run_rule(
            rid, current_window=current_window, baseline_window=baseline_window,
            dimension=dimension,
        ))
    return results


def main():
    ap = argparse.ArgumentParser(description="运行零售异常监测与诊断 MVP")
    ap.add_argument("--config", default="config/ontology_models.yaml")
    ap.add_argument("--oracle-config", default="local/oracle_conn.local.json")
    ap.add_argument("--state", default="local/monitor_control.sqlite3")
    ap.add_argument("--rule")
    ap.add_argument("--current-window", help='JSON，例如 {"start":"2026-03-01","end":"2026-03-31"}')
    ap.add_argument("--baseline-window")
    ap.add_argument("--dimension", help="可选一跳维度，例如 store.store_name")
    ap.add_argument("--interval", type=int, default=0, help="秒；0 表示单次运行")
    args = ap.parse_args()
    while True:
        results = run_once(args.config, args.oracle_config, args.state, args.rule,
                           _window(args.current_window), _window(args.baseline_window),
                           args.dimension)
        print(json.dumps(results, ensure_ascii=False, indent=2, default=str))
        if args.interval <= 0:
            break
        time.sleep(args.interval)


if __name__ == "__main__":
    main()
