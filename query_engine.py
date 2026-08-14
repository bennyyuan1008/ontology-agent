# -*- coding: utf-8 -*-
"""
03_语义查询引擎：把"规划JSON"确定性翻译成 SQL 并只读执行。
LLM 只负责出规划（见 05 号文件），这里全部是确定性代码，保证可靠与安全。

支持方言：sqlite（本地快照）/ oracle（测试库直连）
Oracle 特殊处理：
  - NCHAR 字符集（id 871）thin 模式不可读 → NVARCHAR2 列自动 CAST(... AS VARCHAR2(4000))
  - 字符串日期（ptype=string_date，如订单日期 YYYY-MM-DD）→ 按字符串比较
  - 月份分组：DATE 列用 TO_CHAR，字符串日期用 SUBSTR
  - 行数限制：FETCH FIRST n ROWS ONLY
属性配置支持：
  - column: 物理列名
  - expr:   自定义 SQL 表达式（{t} 会被替换为对象表别名，如 "Order"）
  - db_type: nvarchar2/number/date/varchar2 —— nvarchar2 自动 CAST
  - ptype:   date(真日期)/string_date(字符串日期)/缺省(普通值)
用法：
  # Oracle
  python 03_语义查询引擎_query_engine.py --oracle-file 本地凭证 --dialect oracle --plan '{...}'
依赖：pyyaml（pip install pyyaml）；Oracle 需 oracledb（+ cryptography + cffi）
"""
import argparse
import json
import os
import re
import sqlite3
import sys

# 本地依赖引导：确保 libs 目录在 sys.path（无需手动设 PYTHONPATH）
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

import yaml

CONFIG_PATH = "config/ontology_models.yaml"
ORACLE_LOCAL_FILE = "local/oracle_conn.local.json"  # 本地凭证文件（已被 .gitignore 排除）
MAX_LIMIT = 200
FORBIDDEN = re.compile(r"\b(insert|update|delete|drop|alter|truncate|create|grant|"
                       r"attach|detach|pragma|exec|execute|merge|call)\b", re.I)


def _load_oracle_cfg(arg: str):
    if arg:
        return arg
    if os.path.exists(ORACLE_LOCAL_FILE):
        with open(ORACLE_LOCAL_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return " ".join(f"{k}={v}" for k, v in d.items())
    return None


# ---------- 加载对象模型 ----------

def load_config(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        cfg = yaml.safe_load(f)
    objs = {}
    for o in cfg["objects"]:
        name = o["name"]
        props = {k: v for k, v in o["properties"].items()}
        alias_idx = {}
        for pname, p in props.items():
            for a in p.get("alias", []):
                alias_idx[a] = pname
        objs[name] = {
            "table": o["source"],
            "pk": o.get("pk"),
            "props": props,
            "links": o.get("links", {}),
            "alias_idx": alias_idx,
        }
    derived = {}
    for d in cfg.get("derived_properties", []):
        derived.setdefault(d["object"], {})[d["name"]] = d["formula"]
    return {"objects": objs, "derived": derived, "rules": cfg.get("business_rules", [])}


# ---------- 方言抽象 ----------

DIALECT = "sqlite"  # sqlite | oracle，由 CLI --dialect 覆盖


def date_literal(value: str) -> str:
    if DIALECT == "oracle":
        return f"TO_DATE('{value}','YYYY-MM-DD')"
    return f"'{value}'"


def month_bucket(expr: str, ptype: str) -> str:
    if DIALECT == "oracle":
        if ptype == "string_date":
            return f"SUBSTR({expr},1,7)"
        return f"TO_CHAR({expr},'YYYY-MM')"
    return f"strftime('%Y-%m', {expr})"


def limit_clause(n: int) -> str:
    if DIALECT == "oracle":
        return f" FETCH FIRST {n} ROWS ONLY"
    return f" LIMIT {n}"


# ---------- 翻译（plan → SQL） ----------

def _col(alias: str, col: str) -> str:
    return f'"{alias}"."{col}"'


def _cast(expr: str, db_type: str) -> str:
    """NVARCHAR2 列自动加 CAST，绕过 thin 模式 NCHAR 限制。"""
    if db_type in ("nvarchar2", "nchar"):
        return f"CAST({expr} AS VARCHAR2(4000))"
    return expr


def _resolve_prop(cfg: dict, obj_name: str, prop: str):
    """返回 (所属表别名, SQL表达式, ptype, db_type)。支持 link.property 形式。"""
    obj = cfg["objects"][obj_name]
    if "." in prop:
        link_name, pname = prop.split(".", 1)
        link = obj["links"].get(link_name)
        if not link:
            raise ValueError(f"对象 {obj_name} 没有关联 {link_name}")
        target = cfg["objects"][link["to"]]
        if pname not in target["props"]:
            raise ValueError(f"{link['to']}.{pname} 不存在")
        return link_name, _prop_expr(target, link_name, pname), \
            target["props"][pname].get("ptype"), target["props"][pname].get("db_type")
    if prop in obj["props"]:
        return obj_name, _prop_expr(obj, obj_name, prop), \
            obj["props"][prop].get("ptype"), obj["props"][prop].get("db_type")
    d = cfg["derived"].get(obj_name, {}).get(prop)
    if d:
        return obj_name, d.replace("{t}", f'"{obj_name}"'), None, None
    raise ValueError(f"对象 {obj_name} 没有属性 {prop}")


def _prop_expr(obj: dict, alias: str, pname: str) -> str:
    p = obj["props"][pname]
    if "expr" in p:
        return p["expr"].replace("{t}", f'"{alias}"')
    return _col(alias, p["column"])


def _op_sql(op: str) -> str:
    return {"=": "=", "!=": "!=", ">": ">", "<": "<", ">=": ">=",
            "<=": "<=", "like": "LIKE"}.get(op, op)


def _value_sql(v, ptype: str) -> str:
    if ptype == "date":
        return date_literal(v)
    return f"'{v}'"


def _filter_sql(cfg: dict, obj_name: str, f: dict) -> str:
    owner, expr, ptype, db_type = _resolve_prop(cfg, obj_name, _prop_key(f))
    lhs = _cast(expr, db_type)
    op = _op_sql(f["op"])
    v = f["value"]
    if f["op"] == "between":
        a, b = v
        return f"{lhs} BETWEEN {_value_sql(a, ptype)} AND {_value_sql(b, ptype)}"
    if f["op"] == "in":
        vals = ", ".join(_value_sql(x, ptype) for x in v)
        return f"{lhs} IN ({vals})"
    return f"{lhs} {op} {_value_sql(v, ptype)}"


def _prop_key(entry) -> str:
    """规划条目支持 {"property": "x"} 或 {"link": "a", "property": "b"} → a.b"""
    if "link" in entry:
        return f"{entry['link']}.{entry['property']}"
    return entry["property"]


def _from(table: str, alias: str) -> str:
    """schema.table → "schema"."table"；table → "table"（Oracle 大小写敏感，必须分引）"""
    if "." in table:
        schema, tbl = table.split(".", 1)
        return f'"{schema}"."{tbl}" "{alias}"'
    return f'"{table}" "{alias}"'


def _find_prop_cfg(cfg: dict, obj_name: str, prop: str) -> dict:
    """按属性名（支持 link.prop）取属性配置，用于 group_key 等元信息。"""
    obj = cfg["objects"][obj_name]
    if "." in prop:
        link_name, pname = prop.split(".", 1)
        link = obj["links"][link_name]
        return cfg["objects"][link["to"]]["props"].get(pname, {})
    return obj["props"].get(prop, {})


def translate(cfg: dict, plan: dict) -> str:
    obj_name = plan["object"]
    obj = cfg["objects"][obj_name]
    alias = obj_name
    table = obj["table"]
    joins = []

    used_links = set()
    for f in plan.get("filters", []):
        if "." in _prop_key(f):
            used_links.add(_prop_key(f).split(".", 1)[0])
    for g in plan.get("group_by", []):
        if "." in _prop_key(g):
            used_links.add(_prop_key(g).split(".", 1)[0])
    for lnk in used_links:
        link = obj["links"][lnk]
        tgt = cfg["objects"][link["to"]]
        joins.append(
            f'JOIN {_from(tgt["table"], lnk)} ON {_col(alias, link["via"])}'
            f' = {_col(lnk, _pk_of(tgt))}'
        )

    sel_cols = []
    if plan.get("aggregate"):
        agg = plan["aggregate"]
        if agg.get("derived"):
            # 派生属性公式自带聚合（如 SUM(..)/COUNT(..)），原样使用，不再包 func
            owner, expr, _, _ = _resolve_prop(cfg, obj_name, agg["derived"])
            sel_cols.append(f'{expr} AS agg_result')
        else:
            func = agg["func"]
            if "property" in agg:
                owner, expr, _, db_type = _resolve_prop(cfg, obj_name, agg["property"])
                sel_cols.append(f'{func}({_cast(expr, db_type)}) AS agg_result')
            else:
                sel_cols.append(f'{func}(*) AS agg_result')  # COUNT 可无属性
    else:
        for p in plan.get("properties", ["*"]):
            if p == "*":
                sel_cols.append("*")
            else:
                owner, expr, _, db_type = _resolve_prop(cfg, obj_name, p)
                sel_cols.append(_cast(expr, db_type))
    select = ", ".join(sel_cols) if sel_cols else "*"

    group_by_exprs = []   # GROUP BY 子句用的表达式
    select_additions = []  # 需要追加到 SELECT 的展示表达式
    group_exprs = {}      # prop_key -> GROUP BY 表达式（ORDER BY 复用）
    for g in plan.get("group_by", []):
        key = _prop_key(g)
        owner, expr, ptype, db_type = _resolve_prop(cfg, obj_name, key)
        disp = _cast(expr, db_type)
        if g.get("bucket") == "month":
            disp = month_bucket(disp, ptype)
        if "SELECT" in disp.upper():
            # 标量子查询不能出现在 GROUP BY（ORA-22818）→ 按 group_key 或主键分组，名称子查询只进 SELECT
            tgt_name = owner if owner == obj_name else obj["links"][owner]["to"]
            tgt = cfg["objects"][tgt_name]
            gexpr = _col(owner, _find_prop_cfg(cfg, obj_name, key).get("group_key") or _pk_of(tgt))
            group_exprs[key] = gexpr
            select_additions.append(disp)
        else:
            gexpr = disp
            group_exprs[key] = gexpr
            select_additions.append(gexpr)
        group_by_exprs.append(gexpr)
    group_sql = group_by_exprs
    for g in select_additions:
        if g not in sel_cols:
            select = select + ", " + g

    having_sql = []
    for h in plan.get("having", []):
        owner, expr, ptype, db_type = _resolve_prop(cfg, obj_name, _prop_key(h))
        having_sql.append(f'{h["func"]}({_cast(expr, db_type)}) {_op_sql(h["op"])} {h["value"]}')

    order_sql = ""
    if plan.get("order_by"):
        ob = plan["order_by"]
        obkey = _prop_key(ob)
        if ob.get("property") == "agg_result" or obkey == "agg_result":
            order_sql = " ORDER BY agg_result " + ob.get("dir", "DESC")
        elif obkey in group_exprs:
            order_sql = f" ORDER BY {group_exprs[obkey]} " + ob.get("dir", "ASC")
        elif plan.get("aggregate"):
            order_sql = " ORDER BY agg_result " + ob.get("dir", "ASC")  # 聚合查询非分组列：兜底按聚合值排
        elif group_sql:
            order_sql = ""  # 分组查询排非分组列会 ORA-00979：跳过排序（规划层应避免）
        else:
            owner, expr, ptype, db_type = _resolve_prop(cfg, obj_name, obkey)
            order_sql = f" ORDER BY {_cast(expr, db_type)} " + ob.get("dir", "ASC")
    limit = min(int(plan.get("limit", 50)), MAX_LIMIT)

    sql = f"SELECT {select} FROM {_from(table, alias)}"
    if joins:
        sql += " " + " ".join(joins)
    where = [_filter_sql(cfg, obj_name, f) for f in plan.get("filters", [])]
    if obj.get("filter"):  # 对象级默认过滤（如门店 org_form='2'）
        where.append(obj["filter"].replace("{t}", f'"{alias}"'))
    if where:
        sql += " WHERE " + " AND ".join(where)
    if group_sql:
        sql += " GROUP BY " + ", ".join(group_sql)
    if having_sql:
        sql += " HAVING " + " AND ".join(having_sql)
    sql += order_sql + limit_clause(limit)
    return sql


def _pk_of(obj: dict) -> str:
    if obj.get("pk"):
        return obj["pk"]
    for pname, p in obj["props"].items():
        if p.get("type") == "int" and pname.endswith("_id"):
            return p["column"]
    return list(obj["props"].values())[0]["column"]


# ---------- 安全与执行 ----------

def check_safety(sql: str):
    if not sql.lstrip().upper().startswith("SELECT"):
        raise ValueError("只允许 SELECT 查询")
    if FORBIDDEN.search(sql):
        raise ValueError("检测到禁止的 SQL 操作")


def execute_sqlite(db_path: str, sql: str):
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    rows = con.execute(sql).fetchall()
    con.close()
    return [dict(r) for r in rows]


def execute_oracle(cfg: str, sql: str):
    try:
        import oracledb
    except ImportError:
        sys.exit("缺少 oracledb，请先执行：pip install oracledb")
    params = {}
    for kv in cfg.split():
        k, _, v = kv.partition("=")
        params[k] = v
    user = params.pop("user")
    password = params.pop("password")
    dsn = params.pop("dsn", None)
    if not dsn:
        dsn = f"{params.pop('host')}:{params.pop('port', '1521')}/{params.pop('service')}"
    con = oracledb.connect(user=user, password=password, dsn=dsn)
    con.autocommit = False
    cur = con.cursor()
    try:
        cur.execute("ALTER SESSION SET TRANSACTION READ ONLY")
    except Exception:
        pass
    cur.execute(sql)
    cols = [d[0] for d in cur.description] if cur.description else []
    rows = [dict(zip(cols, r)) for r in cur.fetchall()]
    cur.close()
    con.close()
    return rows


def execute(dialect: str, sql: str, db_path: str = None, oracle_cfg: str = None):
    if dialect == "oracle":
        return execute_oracle(oracle_cfg, sql)
    return execute_sqlite(db_path, sql)


def main():
    global DIALECT
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", help="SQLite 快照路径")
    ap.add_argument("--oracle", help='Oracle 连接串（缺省读 oracle_conn.local.json）')
    ap.add_argument("--dialect", choices=["sqlite", "oracle"], default="sqlite")
    ap.add_argument("--plan", required=True, help="规划JSON字符串")
    args = ap.parse_args()
    DIALECT = args.dialect

    cfg = load_config(CONFIG_PATH)
    plan = json.loads(args.plan)
    sql = translate(cfg, plan)
    check_safety(sql)
    print("SQL:", sql)
    oracle_cfg = _load_oracle_cfg(args.oracle)
    if args.dialect == "oracle" and not oracle_cfg:
        sys.exit("Oracle 模式需要 --oracle 连接串或本地 oracle_conn.local.json")
    rows = execute(args.dialect, sql, db_path=args.db, oracle_cfg=oracle_cfg)
    print(f"行数: {len(rows)}")
    for r in rows[:20]:
        print(r)


if __name__ == "__main__":
    main()
