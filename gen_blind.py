# -*- coding: utf-8 -*-
"""盲测池生成器：8 条【全新问法】的黄金基准（开发过程从未见过）。
问法刻意多样化：口语化/模糊/跨对象组合/派生分组/库存Top/边界降级。
用引擎真实执行生成 expected_sql/expected_result。
"""
import importlib.util
import json
import os
import sys

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
    (1, "多对象JOIN", "easy", "3月哪个店卖得最火？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}],
      "group_by": [{"link": "store", "property": "store_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 1}),
    (2, "时间范围", "easy", "2月份一共收了多少钱？",
     {"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-02-01", "2026-02-28"]},
                  {"property": "type", "op": "=", "value": 0}]}),
    (3, "聚合分组", "easy", "3月一共卖了多少件商品？",
     {"object": "OrderItem", "aggregate": {"func": "SUM", "property": "qty"},
      "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"link": "order", "property": "type", "op": "=", "value": 0}]}),
    (4, "多对象JOIN", "medium", "3月退货最多的商品是哪个？",
     {"object": "OrderItem", "aggregate": {"func": "SUM", "property": "qty"},
      "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"link": "order", "property": "type", "op": "=", "value": 1}],
      "group_by": [{"link": "product", "property": "product_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 1}),
    (5, "条件组合", "medium", "3月吐司类商品卖了多少？",
     {"object": "OrderItem", "aggregate": {"func": "SUM", "property": "qty"},
      "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"link": "order", "property": "type", "op": "=", "value": 0},
                  {"link": "product", "property": "category_name", "op": "=", "value": "吐司类"}]}),
    (6, "TopN排序", "easy", "目前库存最多的商品是什么？",
     {"object": "Inventory", "properties": ["qty"],
      "order_by": {"property": "qty", "dir": "DESC"}, "limit": 1}),
    (7, "聚合分组", "hard", "3月各门店的客单价排名",
     {"object": "Order", "aggregate": {"derived": "客单价"},
      "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
                  {"property": "type", "op": "=", "value": 0}],
      "group_by": [{"link": "store", "property": "store_name"}],
      "order_by": {"property": "agg_result", "dir": "DESC"}}),
    (8, "边界用例", "hard", "3月和2月比，销售额是涨了还是跌了？",
     {"boundary": "跨时间段对比需两步查询+逻辑判断，v1 不支持；应转澄清或优雅降级（如分别给出两月销售额）"}),
]

entries = []
for eid, cat, diff, q, plan in PLANS:
    entry = {"id": eid, "category": cat, "difficulty": diff, "question": q}
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
    "_说明": "盲测池：开发过程从未见过的全新问法（口语化/模糊/跨对象/派生分组/边界），"
            "用于衡量样本外泛化。跑 run_eval.py --blind 评估。",
    "entries": entries,
}
out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "eval", "blind_pool.json")
with open(out, "w", encoding="utf-8") as f:
    json.dump(doc, f, ensure_ascii=False, indent=2)
print("written:", out, "entries:", len(entries))
