# -*- coding: utf-8 -*-
"""
01_盘点脚本：导出测试库全部表清单（表名/行数/字段），按前缀分组，供 W1 盘点使用。

用法（三选一）：
  # SQLite 本地快照（无依赖，可直接跑）
  python 01_盘点脚本_schema_inventory.py --sqlite 快照.db

  # MySQL（需先安装驱动：pip install pymysql）
  python 01_盘点脚本_schema_inventory.py --mysql "host=127.0.0.1 port=3306 user=xxx password=xxx db=xxx charset=utf8mb4"

  # Oracle（推荐，需先安装驱动：pip install oracledb；thin 模式免装 Oracle 客户端）
  #   dsn 写法：host:port/service_name（如 192.168.1.10:1521/ORCLPDB1），缺省用 --dsn 组合
  python 01_盘点脚本_schema_inventory.py --oracle "user=xxx password=xxx dsn=192.168.1.10:1521/ORCLPDB1"

输出：
  inventory_report.md   —— 表清单+行数+按前缀分组（人看）
  tables_detail.json    —— 每张表的字段明细（程序用）

注意：
  - 只做只读操作。
  - Oracle 的行数来自 ALL_TABLES.NUM_ROWS（统计信息，近似值，速度快）；精确行数对重点表再单独 COUNT。
  - 连接信息只会出现在命令行，不会写入任何输出文件。
"""
import argparse
import json
import os
import re
import sqlite3
import sys
from collections import defaultdict

# 本地依赖引导：确保 libs 目录在 sys.path（无需手动设 PYTHONPATH）
_LIB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "libs")
if os.path.isdir(_LIB) and _LIB not in sys.path:
    sys.path.insert(0, _LIB)

ORACLE_LOCAL_FILE = "local/oracle_conn.local.json"  # 本地凭证文件（已被 .gitignore 排除）


def _load_oracle_cfg(arg: str):
    """优先用命令行参数；否则读本地凭证文件 oracle_conn.local.json（不入库、不入仓库）。"""
    if arg:
        return arg
    if os.path.exists(ORACLE_LOCAL_FILE):
        with open(ORACLE_LOCAL_FILE, encoding="utf-8") as f:
            d = json.load(f)
        return " ".join(f"{k}={v}" for k, v in d.items())
    return None


# ---------- 数据源 ----------

def dump_sqlite(path: str) -> dict:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # 只读打开
    cur = con.cursor()
    tables = [
        r[0]
        for r in cur.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    ]
    result = {}
    for t in sorted(tables):
        cols = [c[1] for c in cur.execute(f'PRAGMA table_info("{t}")').fetchall()]
        try:
            n = cur.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
        except Exception:
            n = -1
        result[t] = {"rows": n, "columns": cols, "rows_note": "exact"}
    con.close()
    return result


def dump_mysql(cfg: str) -> dict:
    try:
        import pymysql
    except ImportError:
        sys.exit("缺少 pymysql，请先执行：pip install pymysql")
    params = _parse_kv(cfg)
    con = pymysql.connect(**params)
    cur = con.cursor()
    cur.execute("SHOW TABLES")
    tables = [r[0] for r in cur.fetchall()]
    result = {}
    for t in sorted(tables):
        cur.execute(f"SHOW COLUMNS FROM `{t}`")
        cols = [r[0] for r in cur.fetchall()]
        try:
            cur.execute(f"SELECT COUNT(*) FROM `{t}`")
            n = cur.fetchone()[0]
        except Exception:
            n = -1
        result[t] = {"rows": n, "columns": cols, "rows_note": "exact"}
    con.close()
    return result


def dump_oracle(cfg: str, owner: str = None) -> dict:
    try:
        import oracledb
    except ImportError:
        sys.exit("缺少 oracledb，请先执行：pip install oracledb")
    params = _parse_kv(cfg)
    user = params.pop("user")
    password = params.pop("password")
    dsn = params.pop("dsn", None)
    if not dsn:  # 用 host/port/service 组装
        dsn = f"{params.pop('host')}:{params.pop('port', '1521')}/{params.pop('service')}"
    con = oracledb.connect(user=user, password=password, dsn=dsn)
    cur = con.cursor()

    if not owner:
        # 未指定 owner：若当前用户自有表则盘它，否则列出可访问的 schema
        cur.execute("SELECT COUNT(*) FROM all_tables WHERE owner = USER")
        if cur.fetchone()[0] > 0:
            owner = user
        else:
            cur.execute("""
                SELECT owner, COUNT(*) FROM all_tables
                WHERE owner NOT IN ('SYS','SYSTEM','XDB','CTXSYS','MDSYS','DBSNMP','WMSYS',
                                    'OUTLN','APPQOSSYS','GSMADMIN_INTERNAL','AUDSYS','ORACLE_OCM')
                GROUP BY owner ORDER BY COUNT(*) DESC
            """)
            print("当前用户无自有表，可访问的 schema：")
            for o, cnt in cur.fetchall():
                print(f"  {o}: {cnt} 张表")
            sys.exit("请用 --owner 指定要盘点的 schema，如 --owner POS")

    cur.execute("""
        SELECT table_name, num_rows FROM all_tables
        WHERE owner = :1 AND table_name NOT LIKE 'BIN$%'
        ORDER BY table_name
    """, [owner])
    rows = cur.fetchall()
    result = {}
    for t, num_rows in rows:
        cur.execute("""
            SELECT column_name FROM all_tab_columns
            WHERE owner = :1 AND table_name = :2
            ORDER BY column_id
        """, [owner, t])
        cols = [r[0] for r in cur.fetchall()]
        result[f"{owner}.{t}"] = {"rows": num_rows, "columns": cols,
                                  "rows_note": "approx(stats) —— 精确行数请对重点表单独 COUNT(*)"}
    con.close()
    return result


def _parse_kv(cfg: str) -> dict:
    params = {}
    for kv in cfg.split():
        k, _, v = kv.partition("=")
        params[k] = v
    return params


# ---------- 报告 ----------

def prefix_group(tables: list) -> dict:
    """按表名第一个下划线前的词分组，如 T_ORD_ORDER → T_ORD。"""
    groups = defaultdict(list)
    for t in tables:
        m = re.match(r"^([a-zA-Z_]+?)(?:_|$)", t)
        groups[m.group(1) if m else t].append(t)
    return dict(sorted(groups.items()))


def write_report(result: dict, md_path: str, json_path: str):
    lines = ["# 测试库盘点报告", ""]
    for prefix, tables in prefix_group(list(result)).items():
        lines.append(f"## 分组：{prefix}")
        lines.append("")
        lines.append("| 表名 | 行数 | 字段数 | 行数口径 |")
        lines.append("|---|---|---|---|")
        for t in tables:
            info = result[t]
            lines.append(f"| {t} | {info['rows']} | {len(info['columns'])} | {info['rows_note']} |")
        lines.append("")
    lines.append(f"**表总数：{len(result)}**")
    lines.append("")
    lines.append("> 下一步：按计划 §2.2 选 6~10 张核心表（订单/明细/商品/门店/库存/会员），"
                 "把每张表的字段语义填进『字段语义盘点表』。")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"✅ 已生成：{md_path} 与 {json_path}（共 {len(result)} 张表）")


def main():
    ap = argparse.ArgumentParser(description="测试库盘点")
    ap.add_argument("--sqlite", help="SQLite 快照文件路径")
    ap.add_argument("--mysql", help='MySQL 连接串，如 "host=... user=... password=... db=..."')
    ap.add_argument("--oracle", help='Oracle 连接串，如 "user=... password=... dsn=host:port/service"')
    ap.add_argument("--owner", help="Oracle 要盘点的 schema（默认：当前用户自有表；无则提示）")
    args = ap.parse_args()

    if args.sqlite:
        result = dump_sqlite(args.sqlite)
    elif args.mysql:
        result = dump_mysql(args.mysql)
    elif _load_oracle_cfg(args.oracle):
        result = dump_oracle(_load_oracle_cfg(args.oracle), owner=args.owner)
    else:
        ap.print_help()
        sys.exit(1)

    write_report(result, "inventory_report.md", "tables_detail.json")

    print("\n行数 TOP 15（候选业务表，行数大的优先）：")
    for t, info in sorted(result.items(), key=lambda kv: -(kv[1]["rows"] or 0))[:15]:
        print(f"  {info['rows']:>10,}  {t}   [{info['rows_note']}]")


if __name__ == "__main__":
    main()
