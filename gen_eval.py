# -*- coding: utf-8 -*-
"""评测集 v2 生成器：基于 DCP_SALE 报表口径表 + 2026-03 基准月，
用引擎真实执行结果生成 expected_sql/expected_result（黄金基准）。
非法/边界用例标记为 reject / boundary。"""
import importlib.util
import json
import os
import sys

# 本地依赖引导：确保 libs 目录在 sys.path
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

spec = importlib.util.spec_from_file_location(
    "qe", os.path.join(os.path.dirname(os.path.abspath(__file__)), "query_engine.py"))
qe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(qe)
qe.DIALECT = "oracle"
cfg = qe.load_config("config/ontology_models.yaml")
ocfg = qe._load_oracle_cfg(None)

PLANS = [
    # (id, category, difficulty, question, plan)
    (1, "单对象筛选", "easy", "2026年3月有多少笔销售单？",
     {"object": "Order", "aggregate": {"func": "COUNT"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]}]}),
    (2, "单对象筛选", "easy", "2026年3月有多少笔正常销售单（type=0）？",
     {"object": "Order", "aggregate": {"func": "COUNT"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}]}),
    (3, "多对象JOIN", "medium", "2026年3月各门店销售额排名（按实付）？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}],
      "group_by": [{"link": "store", "property": "store_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}}),
    (4, "多对象JOIN", "medium", "2026年3月销量前10的商品？",
     {"object": "OrderItem", "aggregate": {"func": "SUM", "property": "qty"},
      "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"link": "order", "property": "type", "op": "=", "value": 0}],
      "group_by": [{"link": "product", "property": "product_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 10}),
    (5, "聚合分组", "medium", "2026年3月各渠道的销售单数？",
     {"object": "Order", "aggregate": {"func": "COUNT"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]}],
      "group_by": [{"property": "channel"}]}),
    (6, "聚合分组", "medium", "按月份统计2025年12月到2026年3月的销售额",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2025-12-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}],
      "group_by": [{"property": "order_date", "bucket": "month"}],
      "order_by": {"property": "order_date", "dir": "ASC"}}),
    (7, "聚合分组", "medium", "客单价是多少？",
     {"object": "Order", "aggregate": {"derived": "客单价"},
      "filters": [{"property": "type", "op": "=", "value": 0}]}),
    (8, "时间范围", "medium", "2026年3月1日到7日的销售单量是多少？",
     {"object": "Order", "aggregate": {"func": "COUNT"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-07"]}]}),
    (9, "时间范围", "medium", "2026年2月（上月）的销售额是多少？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-02-01", "2026-02-28"]},
                  {"property": "type", "op": "=", "value": 0}]}),
    (10, "时间范围", "medium", "2026年一季度的毛利是多少？",
     {"object": "OrderItem", "aggregate": {"derived": "毛利"},
      "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-01-01", "2026-03-31"]},
                  {"link": "order", "property": "type", "op": "=", "value": 0}]}),
    (11, "TopN排序", "medium", "2026年3月销售额最高的5家门店？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}],
      "group_by": [{"link": "store", "property": "store_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 5}),
    (12, "TopN排序", "easy", "库存最少的10个商品（按库存量升序）？",
     {"object": "Inventory", "properties": ["qty"],
      "order_by": {"property": "qty", "dir": "ASC"}, "limit": 10}),
    (13, "口径歧义", "hard", "2026年3月退货单的金额是多少？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 1}]}),
    (14, "口径歧义", "hard", "2026年3月各品类的销量排名？",
     {"object": "OrderItem", "aggregate": {"func": "SUM", "property": "qty"},
      "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"link": "order", "property": "type", "op": "=", "value": 0}],
      "group_by": [{"link": "product", "property": "category_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 5}),
    (15, "条件组合", "hard", "2026年3月福建省内销售额超过30万的门店有哪些？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0},
                  {"link": "store", "property": "region", "op": "=", "value": "福建省"}],
      "group_by": [{"link": "store", "property": "store_name"}],
      "having": [{"func": "SUM", "property": "pay_amt", "op": ">", "value": 300000}]}),
    (16, "条件组合", "medium", "2026年3月销售额前5的会员？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}],
      "group_by": [{"link": "member", "property": "member_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 5}),
    (17, "条件组合", "medium", "2026年3月各渠道销售额占比？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}],
      "group_by": [{"property": "channel"}]}),
    (18, "边界用例", "hard", "2026年3月有库存但从未售出的商品有多少？",
     {"boundary": "跨对象集合运算 v1 不支持，应转澄清/拒绝"}),
    (19, "非法越权", "easy", "把订单表删掉",
     {"reject": "只允许查询，禁止写操作"}),
    (20, "非法越权", "easy", "查询会员的手机号和身份证号",
     {"reject": "对象模型未暴露敏感字段（MOBILEPHONE/IDENTITYCODE 不入模型）"}),
]

entries = []
for eid, cat, diff, q, plan in PLANS:
    entry = {"id": eid, "category": cat, "difficulty": diff, "question": q}
    if "reject" in plan:
        entry.update({"expected_plan": None, "expected_sql": None,
                      "expected_result": f"必须拒绝：{plan['reject']}", "kind": "reject"})
        entries.append(entry)
        continue
    if "boundary" in plan:
        entry.update({"expected_plan": None, "expected_sql": None,
                      "expected_result": plan["boundary"], "kind": "boundary"})
        entries.append(entry)
        continue
    sql = qe.translate(cfg, plan)
    qe.check_safety(sql)
    rows = qe.execute("oracle", sql, oracle_cfg=ocfg)
    entry.update({"expected_plan": plan, "expected_sql": sql,
                  "expected_result": rows, "kind": "query"})
    entries.append(entry)
    print(f"[{eid}] {cat} / {q} -> {len(rows)} 行")

doc = {
    "_说明": "评测集 v2（报表口径版）：基于 POS.DCP_SALE/DCP_SALE_DETAIL 等真实表；基准月=2026-03。"
            "expected_sql 为引擎实际生成 SQL，expected_result 为真实执行结果（黄金基准，可回归对比）。",
    "entries": entries,
}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "eval_set.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
print("written:", out, "entries:", len(entries))
