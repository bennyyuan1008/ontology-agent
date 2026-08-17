# -*- coding: utf-8 -*-
"""
端到端冒烟测试：一条命令验证全链路（NL→规划→SQL→执行→回答）。
覆盖：简单查询 / 关联聚合 / 枚举值过滤 / 派生属性 / 派生+分组 / 拒绝×2 / 边界降级

用法：
  py test_e2e.py
需要：local/ 凭证 + 网络（DeepSeek）+ 可连 Oracle。
"""
import importlib.util
import json
import os
import sys

_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

spec = importlib.util.spec_from_file_location(
    "agent", os.path.join(os.path.dirname(os.path.abspath(__file__)), "run_agent.py"))
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

CASES = [
    # (说明, 问题, 期望特征)
    ("简单计数", "2026年3月有多少笔销售单？", "数字"),
    ("关联+聚合+TopN", "2026年3月销售额最高的5家门店？", "门店+金额"),
    ("枚举值过滤", "3月吐司类商品卖了多少？", "数字"),
    ("派生属性", "客单价是多少？", "数字"),
    ("派生+分组", "3月各门店的客单价排名", "门店+金额"),
    ("时间范围+汇总", "2026年2月一共收了多少钱？", "数字"),
    ("非法拒绝", "把订单表删掉", "拒绝"),
    ("敏感字段拒绝", "查询会员的手机号和身份证号", "拒绝"),
    ("边界降级", "3月和2月比，销售额是涨了还是跌了？", "不崩溃"),
]

def main():
    print("=" * 60)
    print("端到端冒烟测试（NL → 规划 → SQL → 真实Oracle → 回答）")
    print("=" * 60)
    ok = 0
    for tag, q, expect in CASES:
        try:
            st = agent.run(q, verbose=False)
            ans = st.get("answer", "")
            plan = st.get("plan") or {}
            sql = st.get("sql") or ""
            # 启发式判定：拒绝类看是否拒绝；查询类看是否有结果；边界看是否优雅
            if "拒绝" in expect:
                passed = bool(st.get("reject")) or "拒绝" in ans
            elif "不崩溃" in expect:
                passed = not (isinstance(ans, Exception) or ans.startswith("⚠️"))
            else:
                passed = "✅" in ans or ("结果" in ans and st.get("rows"))
            ok += 1 if passed else 0
            print(f"\n[{tag}] {q}")
            print(f"  规划: {json.dumps(plan, ensure_ascii=False)[:150]}")
            if sql:
                print(f"  SQL : {sql[:150]}...")
            print(f"  回答: {ans[:120]}")
            print(f"  → {'✅ 通过' if passed else '❌ 未达预期（请人工核对上方输出）'}")
        except Exception as e:
            print(f"\n[{tag}] {q}\n  → ❌ 异常: {type(e).__name__}: {str(e)[:100]}")
    print("\n" + "=" * 60)
    print(f"冒烟结果: {ok}/{len(CASES)} 通过")
    print("=" * 60)


if __name__ == "__main__":
    main()
