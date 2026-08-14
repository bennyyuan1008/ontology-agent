# -*- coding: utf-8 -*-
"""
06_评测运行器：让 NL 规划 Agent 跑完评测集（20条），统计：
  - 规划正确率（生成 plan 与黄金 plan 归一化一致）
  - 执行成功率（生成的 SQL 能跑通）
  - 结果正确率（执行结果与黄金结果一致）
  - 拒绝正确率（非法/越权用例正确拒绝）

用法：
  $env:PYTHONPATH = "D:\简历\_pylibs"
  py 06_评测运行器.py
"""
import importlib.util
import json
import os
import sys

# 本地依赖引导：确保 libs 目录在 sys.path（无需手动设 PYTHONPATH）
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

spec = importlib.util.spec_from_file_location(
    "agent", os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_agent.py"))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

EVAL_FILE = "eval/eval_set.json"


def norm(x):
    """归一化用于对比：数字转 float，键排序，递归。"""
    if isinstance(x, dict):
        return {k: norm(v) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return [norm(v) for v in x]
    if isinstance(x, (int, float)):
        return round(float(x), 2)
    return x


def plan_equivalent(gold, got):
    """规划语义等价：忽略 order_by 差异；filters 顺序无关；其余结构一致。"""
    if not gold or not got:
        return gold == got
    g, o = json.loads(json.dumps(gold)), json.loads(json.dumps(got))
    g.pop("order_by", None)
    o.pop("order_by", None)
    for side in (g, o):
        if "filters" in side:
            side["filters"] = sorted(side["filters"], key=lambda f: json.dumps(f, ensure_ascii=False))
    return norm(g) == norm(o)


def result_equivalent(gold, got):
    """结果等价：行序无关（按 repr 排序）、数字保留2位小数。"""
    if not gold and not got:
        return True
    if isinstance(gold, list) and isinstance(got, list):
        if len(gold) != len(got):
            return False
        g = sorted((norm(r) for r in gold), key=repr)
        o = sorted((norm(r) for r in got), key=repr)
        return g == o
    return norm(gold) == norm(got)


def main():
    data = json.load(open(EVAL_FILE, encoding="utf-8"))
    entries = data["entries"]

    stats = {"total": 0, "plan_ok": 0, "exec_ok": 0, "result_ok": 0,
             "reject_ok": 0, "reject_total": 0, "boundary": []}

    for e in entries:
        q = e["question"]
        kind = e.get("kind", "query")
        stats["total"] += 1

        if kind == "boundary":
            stats["boundary"].append(e["id"])
            print(f"[{e['id']:>2}] ·  边界用例(应澄清/拒绝) | {q}")
            continue

        if kind == "reject":
            state = agent.run(q, verbose=False)
            refused = bool(state.get("reject")) or "拒绝" in str(state.get("answer", ""))
            stats["reject_total"] += 1
            if refused:
                stats["reject_ok"] += 1
                print(f"[{e['id']:>2}] ✅ 拒绝正确 | {q}")
            else:
                print(f"[{e['id']:>2}] ❌ 拒绝失败 | {q} -> {state.get('answer','')[:60]}")
            continue

        state = agent.run(q, verbose=False)
        plan = state.get("plan")
        expected = e["expected_plan"]
        if plan and plan_equivalent(expected, plan):
            stats["plan_ok"] += 1
            plan_mark = "✅"
        else:
            plan_mark = "❌"
        sql = state.get("sql")
        if sql:
            stats["exec_ok"] += 1
            exec_mark = "✅"
        else:
            exec_mark = "❌"
        rows = state.get("rows", [])
        if result_equivalent(e["expected_result"], rows):
            stats["result_ok"] += 1
            res_mark = "✅"
        else:
            res_mark = "❌"
        print(f"[{e['id']:>2}] {plan_mark}{exec_mark}{res_mark} | {q}")

    n = stats["total"]
    reject_n = stats["reject_total"]
    query_n = n - reject_n - len(stats["boundary"])
    print("\n" + "=" * 46)
    print(f"评测集：{n} 条（查询 {query_n} + 拒绝 {reject_n} + 边界 {len(stats['boundary'])}）")
    if query_n:
        print(f"规划正确率(语义等价) : {stats['plan_ok']}/{query_n} = {stats['plan_ok']/query_n:.0%}")
        print(f"执行成功率            : {stats['exec_ok']}/{query_n} = {stats['exec_ok']/query_n:.0%}")
        print(f"结果正确率(语义等价) : {stats['result_ok']}/{query_n} = {stats['result_ok']/query_n:.0%}")
    if reject_n:
        print(f"拒绝正确率            : {stats['reject_ok']}/{reject_n} = {stats['reject_ok']/reject_n:.0%}")
    print("=" * 46)


if __name__ == "__main__":
    main()
