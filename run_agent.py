# -*- coding: utf-8 -*-
"""
05_NL规划Agent：自然语言 → Ontology规划 → SQL → 执行 → 回答（端到端）
架构：LangGraph 风格状态机（节点为纯函数，可1:1映射到 langgraph.StateGraph 生产版）
  intent → plan(★Ontology规划) → gen_sql(确定性翻译) → execute → answer
  执行失败时自动带错误信息回退重新规划（最多2次）

依赖：pyyaml、oracledb（本地 libs/）+ 标准库 urllib（DeepSeek OpenAI 兼容接口，无需 openai SDK）
密钥：环境变量 DEEPSEEK_API_KEY，或本地文件 local/deepseek_key.local（已 gitignore）

用法：
  在项目目录下直接运行（自动加载 libs/，无需设 PYTHONPATH）：
  py run_agent.py --question "2026年3月销售额最高的5家门店？"
"""
import importlib.util
import json
import os
import re
import sys
import urllib.request

# 本地依赖引导：确保 libs 目录在 sys.path（无需手动设 PYTHONPATH）
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

KEY_FILE = "local/deepseek_key.local"
MAX_RETRY = 2
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|exec)\b|手机号|身份证|删掉|清空|修改|更新|插入|重置|作废|篡改", re.I)
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"


# ---------- 复用引擎 ----------

def _load_engine():
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "query_engine.py")
    spec = importlib.util.spec_from_file_location("query_engine", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


engine = _load_engine()
engine.DIALECT = "oracle"
CONFIG_PATH = "config/ontology_models.yaml"


# ---------- DeepSeek（urllib 直连，零依赖） ----------

def _api_key():
    k = os.environ.get("DEEPSEEK_API_KEY")
    if k:
        return k.strip()
    if os.path.exists(KEY_FILE):
        return open(KEY_FILE, encoding="utf-8").read().strip()
    sys.exit("缺少 DEEPSEEK_API_KEY：设置环境变量，或写本地文件 deepseek_key.local")


def llm(system: str, user: str, temperature: float = 0.1, max_tokens: int = 1500) -> str:
    body = json.dumps({
        "model": "deepseek-chat",
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }).encode("utf-8")
    last_err = None
    for attempt in range(5):  # 沙箱网络间歇抖动，重试5次·指数退避
        try:
            req = urllib.request.Request(
                DEEPSEEK_URL, data=body,
                headers={"Content-Type": "application/json", "Authorization": f"Bearer {_api_key()}"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            return data["choices"][0]["message"]["content"]
        except Exception as e:
            last_err = e
            import time
            time.sleep(3 * (attempt + 1))
    raise last_err


# ---------- 对象模型摘要（喂给规划Agent） ----------

def build_model_summary(cfg: dict) -> str:
    lines = ["可用对象（只能从这里选，不许发明新对象/新字段）："]
    for name, obj in cfg["objects"].items():
        props = []
        for p, v in obj["props"].items():
            aliases = "/".join(v.get("alias", []) or ["-"])
            note = f"，{v['note']}" if v.get("note") else ""
            vals = f"，取值:{'/'.join(v['values'])}" if v.get("values") else ""
            props.append(f"{p}(别名:{aliases}{vals}{note})")
        links = ", ".join(f"{k}→{v['to']}" for k, v in obj["links"].items())
        lines.append(f"- {name}: 属性[{'; '.join(props)}] 关联[{links}]")
    lines.append("\n派生属性（aggregate 里用 derived 引用）：")
    for obj, ds in cfg["derived"].items():
        for n in ds:
            lines.append(f"- {obj}.{n}")
    lines.append("\n业务口径规则：")
    for r in cfg["rules"]:
        lines.append(f"- {r}")
    return "\n".join(lines)


PLAN_SCHEMA = """输出规划JSON，字段说明：
{
  "object": "对象名",                                  // 必填
  "aggregate": {"func": "COUNT|SUM|AVG|MAX|MIN", "property": "属性名"}   // 需要聚合时
           或  {"derived": "派生属性名"}                                 // 派生属性自带聚合
  ,"filters": [{"property": "属性名", "op": "=|!=|>|<|>=|<=|between|in", "value": 值或[起,止]}],
  "group_by": [{"property": "属性名"} 或 {"link": "关联名", "property": "目标对象属性名"}],
  "having": [{"func": "SUM", "property": "属性名", "op": ">", "value": 数}],
  "order_by": {"property": "agg_result 或 属性名", "dir": "ASC|DESC"},
  "limit": 10
}
要点：
- 涉及关联对象的属性/过滤用 {"link": "关联名", "property": "..."}（如门店名 → {"link":"store","property":"store_name"}）
- 当前对象没有的属性必须走关联：如 OrderItem 没有订单日期/单类型，要用 {"link": "order", "property": "order_date"} / {"link": "order", "property": "type"}
- 按月/按周统计（月度趋势）→ group_by 用 {"property": "order_date", "bucket": "month"}
- 日期一律用字符串 "YYYY-MM-DD"（如 "2026-03-01"），不写"今天/上月"等相对词
- 非法/越权（删数据、改数据、查敏感字段如手机号/身份证/银行账号）输出 {"reject": "原因"}
- 区分『问口径』与『执行操作』：询问计算规则（如"退货算不算销售额""删除的订单怎么算"）是合法查询，应正常规划（可查对应口径的单据金额）；只有要求删改数据/查敏感字段才拒绝
- 只输出JSON，不要任何解释"""

FEW_SHOT = """示例1：问题"2026年3月销售额最高的5家门店？"
{"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
 "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
             {"property": "type", "op": "=", "value": 0}],
 "group_by": [{"link": "store", "property": "store_name"}],
 "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 5}

示例2：问题"2026年3月有多少笔销售单？"
{"object": "Order", "aggregate": {"func": "COUNT"},
 "filters": [{"property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]}]}

示例3：问题"2026年3月销量前10的商品？"（OrderItem 的时间/类型通过 link: order 访问）
{"object": "OrderItem", "aggregate": {"func": "SUM", "property": "qty"},
 "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
             {"link": "order", "property": "type", "op": "=", "value": 0}],
 "group_by": [{"link": "product", "property": "product_name"}],
 "order_by": {"property": "agg_result", "dir": "DESC"}, "limit": 10}

示例4：问题"按月份统计2025年12月到2026年3月的销售额"（按月用 bucket）
{"object": "Order", "aggregate": {"func": "SUM", "property": "pay_amt"},
 "filters": [{"property": "order_date", "op": "between", "value": ["2025-12-01", "2026-03-31"]}],
 "group_by": [{"property": "order_date", "bucket": "month"}]}

示例5：问题"库存最少的10个商品？"（点查+排序，无聚合）
{"object": "Inventory", "properties": ["qty"],
 "order_by": {"property": "qty", "dir": "ASC"}, "limit": 10}

示例6：问题"3月品类A商品卖了多少？"（品类名是枚举值，直接过滤）
{"object": "OrderItem", "aggregate": {"func": "SUM", "property": "qty"},
 "filters": [{"link": "order", "property": "order_date", "op": "between", "value": ["2026-03-01", "2026-03-31"]},
             {"link": "order", "property": "type", "op": "=", "value": 0},
             {"link": "product", "property": "category_name", "op": "=", "value": "品类A"}]}

示例7：问题"客单价是多少？"（派生属性用 derived）
{"object": "Order", "aggregate": {"derived": "客单价"},
 "filters": [{"property": "type", "op": "=", "value": 0}]}

示例8：问题"把订单表删掉"
{"reject": "只允许查询，禁止写操作"}"""


# ---------- 状态机节点 ----------

def node_intent(state: dict) -> dict:
    if FORBIDDEN.search(state["question"]):
        return {**state, "reject": "检测到禁止的操作或敏感字段请求，已拒绝", "intent": "reject"}
    return {**state, "intent": "query"}


def node_plan(state: dict, fix_error: str = None) -> dict:
    cfg = engine.load_config(CONFIG_PATH)
    system = f"你是企业数据查询规划器。\n{build_model_summary(cfg)}\n\n{PLAN_SCHEMA}\n\n{FEW_SHOT}"
    user = f"问题：{state['question']}"
    if fix_error:
        prev = json.dumps(state.get("plan"), ensure_ascii=False)
        user += (f"\n\n上一次规划（有误）：{prev}\n执行错误：{fix_error}\n"
                 f"请修正规划后重新输出JSON。注意：若报错是『对象 X 没有属性 Y』，"
                 f"说明 Y 属于关联对象，请改用 {{\"link\": \"关联名\", \"property\": \"Y\"}}。")
    out = llm(system, user)
    try:
        plan = json.loads(out.strip().strip("`"))
    except json.JSONDecodeError:
        return {**state, "reject": f"规划解析失败：{out[:200]}", "intent": "reject"}
    if isinstance(plan, dict) and "reject" in plan:
        return {**state, "reject": plan["reject"], "intent": "reject"}
    return {**state, "plan": plan, "intent": "query", "plan_raw": out}


def node_gen_sql(state: dict) -> dict:
    try:
        cfg = engine.load_config(CONFIG_PATH)
        sql = engine.translate(cfg, state["plan"])
        engine.check_safety(sql)
        return {**state, "sql": sql, "error": ""}
    except Exception as e:
        return {**state, "sql": "", "error": str(e)}


def node_execute(state: dict) -> dict:
    try:
        ocfg = engine._load_oracle_cfg(None)
        rows = engine.execute("oracle", state["sql"], oracle_cfg=ocfg)
        return {**state, "rows": rows, "error": ""}
    except Exception as e:
        return {**state, "rows": [], "error": str(e)}


def node_answer(state: dict) -> dict:
    if state.get("reject"):
        return {**state, "answer": f"❌ 已拒绝：{state['reject']}"}
    if not state.get("sql") or state.get("error"):
        return {**state, "answer": f"⚠️ 查询失败：{state['error']}"}
    rows = state["rows"]
    if not rows:
        return {**state, "answer": "✅ 查询成功，结果为空（0行）"}
    if len(rows) == 1 and len(rows[0]) == 1:
        return {**state, "answer": f"✅ 结果：{list(rows[0].values())[0]}"}
    head = "\n".join(str(list(r.values())) for r in rows[:20])
    return {**state, "answer": f"✅ 结果（{len(rows)}行，前20）：\n{head}"}


def run(question: str, verbose: bool = True) -> dict:
    state = {"question": question, "dialect": "oracle", "retry": 0,
             "intent": "", "plan": {}, "sql": "", "error": "", "rows": [], "answer": ""}
    state = node_intent(state)
    if state.get("reject"):
        return node_answer(state)
    state = node_plan(state)
    if state.get("reject"):
        return node_answer(state)
    while True:
        state = node_gen_sql(state)
        if state.get("error"):
            if state["retry"] >= MAX_RETRY:
                break
            state["retry"] += 1
            state = node_plan(state, fix_error=state["error"])
            if state.get("reject"):
                break
            continue
        state = node_execute(state)
        if not state.get("error") or state["retry"] >= MAX_RETRY:
            break
        state["retry"] += 1
        state = node_plan(state, fix_error=state["error"])
        if state.get("reject"):
            break
    return node_answer(state)


def main():
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--question", required=True, help="自然语言问题")
    ap.add_argument("--verbose", action="store_true", default=True, help="打印规划与SQL")
    args = ap.parse_args()

    state = run(args.question)
    if args.verbose and state.get("plan"):
        print("规划:", json.dumps(state.get("plan"), ensure_ascii=False))
    if args.verbose and state.get("sql"):
        print("SQL :", state["sql"][:300])
    print(state.get("answer"))


if __name__ == "__main__":
    main()
