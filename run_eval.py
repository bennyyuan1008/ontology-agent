# -*- coding: utf-8 -*-
"""
评测运行器：让 NL 规划 Agent 跑评测集，统计四项指标：
  - 规划正确率（生成 plan 与黄金 plan 语义等价）
  - 执行成功率（生成的 SQL 能跑通）
  - 结果正确率（执行结果与黄金结果语义等价）
  - 拒绝正确率（非法/越权用例正确拒绝）

支持两套评测：
  - 设计集  eval/eval_set.json    （开发迭代用的 20 条）
  - 盲测池  eval/blind_pool.json  （开发过程从未见过的全新问法，衡量样本外泛化）

用法：
  py run_eval.py                 # 跑设计集
  py run_eval.py --blind         # 跑盲测池
  py run_eval.py --both          # 两套都跑，分别报指标
  py run_eval.py --coverage      # 打印覆盖矩阵（对象×查询类型×算子），找覆盖空洞
"""
import argparse
import importlib.util
import json
import os
import sys
from collections import Counter, defaultdict

# 本地依赖引导：确保 libs 目录在 sys.path
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

spec = importlib.util.spec_from_file_location(
    "agent", os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_agent.py"))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

EVAL_FILE = "eval/eval_set.json"
BLIND_FILE = "eval/blind_pool.json"


# ---------- 语义等价比较 ----------

def norm(x):
    if isinstance(x, dict):
        return {k: norm(v) for k, v in sorted(x.items())}
    if isinstance(x, list):
        return [norm(v) for v in x]
    if isinstance(x, (int, float)):
        return round(float(x), 2)
    return x


def plan_equivalent(gold, got):
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
    if not gold and not got:
        return True
    if isinstance(gold, list) and isinstance(got, list):
        if len(gold) != len(got):
            return False
        g = sorted((norm(r) for r in gold), key=repr)
        o = sorted((norm(r) for r in got), key=repr)
        return g == o
    return norm(gold) == norm(got)


# ---------- 覆盖标签（从黄金规划推导） ----------

def tags_of(entry) -> dict:
    plan = entry.get("expected_plan") or {}
    kind = entry.get("kind", "query")
    objs = set()
    links = set()
    agg = ""
    if plan:
        objs.add(plan.get("object"))
        for g in plan.get("group_by", []):
            if "link" in g:
                links.add(g["link"])
        for f in plan.get("filters", []):
            if "link" in f:
                links.add(f["link"])
        a = plan.get("aggregate") or {}
        if a.get("derived"):
            agg = f"derived:{a['derived']}"
        elif a.get("func"):
            agg = a["func"]
    return {
        "kind": kind,
        "objects": sorted(objs),
        "links": sorted(links),
        "aggregate": agg or "-",
        "group_by": bool(plan and plan.get("group_by")),
        "having": bool(plan and plan.get("having")),
        "bucket": any(g.get("bucket") for g in (plan.get("group_by") or [])),
        "category": entry.get("category", "-"),
    }


def print_coverage(entries, label):
    print(f"\n===== 覆盖矩阵：{label} =====")
    objs = Counter()
    cat_obj = defaultdict(Counter)      # category -> object -> count
    agg = Counter()
    has_group = Counter()
    for e in entries:
        t = tags_of(e)
        for o in t["objects"]:
            objs[o] += 1
            cat_obj[t["category"]][o] += 1
        agg[t["aggregate"]] += 1
        has_group["group_by" if t["group_by"] else "point"] += 1
    print("对象使用频次:", dict(objs))
    print("聚合算子:", dict(agg))
    print("点查 vs 分组:", dict(has_group))
    print("类别 × 对象：")
    for cat in sorted(cat_obj):
        print(f"  {cat}: {dict(cat_obj[cat])}")
    print("提示：某类别下某对象为空 = 覆盖空洞，可针对性补题")


# ---------- 评估 ----------

def evaluate(entries, label, verbose=True):
    stats = {"total": 0, "plan_ok": 0, "exec_ok": 0, "result_ok": 0,
             "reject_ok": 0, "reject_total": 0, "boundary": [], "errors": 0}
    for e in entries:
        q = e["question"]
        kind = e.get("kind", "query")
        stats["total"] += 1

        if kind == "boundary":
            stats["boundary"].append(e["id"])
            if verbose:
                print(f"[{e['id']:>2}] ·  边界(应澄清/降级) | {q}")
            continue

        try:
            state = agent.run(q, verbose=False)
        except Exception as ex:
            # 单条异常（如网络故障）跳过继续，不计为成功
            stats["errors"] += 1
            if verbose:
                print(f"[{e['id']:>2}] ⚠️ 异常跳过 | {q} ({type(ex).__name__})")
            continue

        if kind == "reject":
            refused = bool(state.get("reject")) or "拒绝" in str(state.get("answer", ""))
            stats["reject_total"] += 1
            if refused:
                stats["reject_ok"] += 1
                if verbose:
                    print(f"[{e['id']:>2}] ✅ 拒绝正确 | {q}")
            else:
                if verbose:
                    print(f"[{e['id']:>2}] ❌ 拒绝失败 | {q}")
            continue

        plan, expected = state.get("plan"), e["expected_plan"]
        ok_plan = bool(plan and plan_equivalent(expected, plan))
        if ok_plan:
            stats["plan_ok"] += 1
        sql = state.get("sql")
        if sql:
            stats["exec_ok"] += 1
        ok_res = result_equivalent(e["expected_result"], state.get("rows", []))
        if ok_res:
            stats["result_ok"] += 1
        if verbose:
            marks = "✅" if ok_plan else "❌", "✅" if sql else "❌", "✅" if ok_res else "❌"
            print(f"[{e['id']:>2}] {''.join(marks)} | {q}")

    n = stats["total"]
    reject_n = stats["reject_total"]
    query_n = n - reject_n - len(stats["boundary"]) - stats["errors"]
    print("\n" + "=" * 50)
    print(f"【{label}】评测集：{n} 条（查询 {query_n} + 拒绝 {reject_n} + 边界 {len(stats['boundary'])} + 异常 {stats['errors']}）")
    if query_n:
        print(f"规划正确率(语义等价) : {stats['plan_ok']}/{query_n} = {stats['plan_ok']/query_n:.0%}")
        print(f"执行成功率            : {stats['exec_ok']}/{query_n} = {stats['exec_ok']/query_n:.0%}")
        print(f"结果正确率(语义等价) : {stats['result_ok']}/{query_n} = {stats['result_ok']/query_n:.0%}")
    if reject_n:
        print(f"拒绝正确率            : {stats['reject_ok']}/{reject_n} = {stats['reject_ok']/reject_n:.0%}")
    print("=" * 50)
    stats["query_n"] = query_n
    return stats


def load(file):
    return json.load(open(file, encoding="utf-8"))["entries"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--blind", action="store_true", help="只跑盲测池")
    ap.add_argument("--both", action="store_true", help="设计集+盲测池都跑")
    ap.add_argument("--coverage", action="store_true", help="打印覆盖矩阵（不跑Agent）")
    args = ap.parse_args()

    if args.coverage:
        print_coverage(load(EVAL_FILE), "设计集")
        print_coverage(load(BLIND_FILE), "盲测池")
        return

    if args.blind or args.both:
        evaluate(load(BLIND_FILE), "盲测池(样本外)")
    if not args.blind or args.both:
        evaluate(load(EVAL_FILE), "设计集")


if __name__ == "__main__":
    main()
