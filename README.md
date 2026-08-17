# 零售数据语义层（Ontology）与自然语言查询 Agent

> 预研项目（基于公司测试库·脱敏数据）｜受 Palantir Ontology 启发，用对象模型把企业级复杂库（2.2万张表/14个schema）抽象为业务概念，让 AI Agent 用自然语言直查真实业务数据。

## 核心架构：LLM 只做语义理解，SQL 由确定性代码生成

```
用户提问 → ①意图识别(规则) → ②Ontology规划(LLM，输出JSON) → ③确定性翻译(引擎→SQL)
        → ④安全校验 → ⑤只读执行(Oracle) → ⑥结果回答
        执行失败 → 带错误信息回退重规划(≤2次)
```

- **搜索空间**：LLM 只在 6 个业务对象（Order/OrderItem/Product/Store/Inventory/Member）里选，不接触裸表
- **口径锁死**：销售额/毛利/退货等口径写在对象定义层（`type=0`、退货取负），翻译代码无发挥空间
- **天然安全**：对象模型不暴露手机号/身份证等敏感字段——物理表有，语义层没有，LLM 无从查询

## 目录结构

```
ontology-agent/
├── README.md                  # 本文件
├── requirements.txt           # 依赖清单（离线依赖见 libs/）
├── .gitignore
├── run_agent.py               # ★ 自然语言查询入口（NL → 规划 → SQL → 结果）
├── run_eval.py                # ★ 评测入口（跑20条评测集 → 指标）
├── query_engine.py            # 语义查询引擎（规划JSON → SQL 确定性翻译 + 只读执行）
├── gen_eval.py                # 评测集生成器（真实执行生成黄金基准）
├── schema_inventory.py        # 数据库盘点工具（表清单/字段/行数）
├── config/
│   └── ontology_models.yaml   # 对象模型定义（表/字段/别名/口径/关联，可配置核心）
├── eval/
│   └── eval_set.json          # 评测集（20条：17查询+2拒绝+1边界，含黄金SQL与真实结果）
├── local/                     # 本地敏感文件（gitignore，不入库）
│   ├── oracle_conn.local.json # Oracle 只读账号凭证
│   └── deepseek_key.local     # DeepSeek API Key
├── libs/                      # 离线依赖（pyyaml/oracledb/cryptography/cffi）
└── docs/
    ├── W1盘点报告.md           # 盘点过程/口径来源/评测结果
    ├── 执行计划.md             # 项目执行计划（Ontology 一体方案）
    └── pos_schema_tables.md   # POS schema 表清单
```

## 快速开始

```powershell
# 1. 确认 local/ 下有 oracle_conn.local.json 和 deepseek_key.local
# 2. 在 ontology-agent 目录下直接运行（无需设 PYTHONPATH，自动加载 libs/）

py run_agent.py --question "2026年3月销售额最高的5家门店？"
# 规划: {...}   SQL: SELECT ...   ✅ 结果：直营店1-16: 847043.83 ...

py run_eval.py        # 跑完整评测集，输出四项指标

py schema_inventory.py --oracle ""   # 盘点（凭证从 local/ 读取，可加 --owner POS）
py gen_eval.py                       # 重新生成黄金评测集（需数据库连通）
```

## 当前评测指标（R1 迭代后，2026-08-15）

| 指标 | 设计集(17查询) | 盲测池(7查询·样本外) |
|---|---|---|
| 规划正确率（语义等价） | **88%** | **86%**（严格） |
| 执行成功率 | **100%** | **100%** |
| 结果正确率（语义等价） | **88%** | **86%**（严格）/ 实质 100% |
| 拒绝正确率（非法/越权） | **100%** | —（无拒绝用例） |

> 迭代史：29%（首轮严格对比）→ 76%（口径规范化）→ **88%**（few-shot+领域知识）。
> 盲测与设计差距仅 2pt = **样本外泛化证据**；盲测第6条为"答案正确但实现路径不同"（agent 用 MAX 聚合 vs 黄金点查），严格对比记差异。
> 迭代过程详见 `docs/迭代日志.md`（R0/R1 记录）。

## 评测方法论（样本外评估）

- **设计集/盲测集分离**：`run_eval.py`（设计集）与 `--blind`（盲测池）分别报指标
- **覆盖矩阵**：`run_eval.py --coverage` 输出 对象×类别×算子 使用频次，找覆盖空洞补题
- **盲测池生长**：真实用户/demo 中出现的新问题，随时加入 `eval/blind_pool.json`（先定义期望答案），下次迭代后跑一遍
- **语义等价判定**：忽略 order_by、filters 顺序无关、结果行序无关、数字保留2位小数

## 口径来源（权威）

`D:\简历\DCP` 帆软报表模板库（公司资产，不在本仓库）中的报表 SQL：
- 销售额 = 明细 `AMT` 汇总，退货单（type=1/2/4）取负相抵
- 单类型：0=正常 / 1=退货 / 2=冲销 / 16=特殊单（排除）
- 门店 `org_form='2'`；店名/品类名取多语言表（zh_CN）

## 安全与合规

- 只读连接 + 只读事务 + SQL 黑名单 + 强制 LIMIT
- 对象模型不暴露 PII；非法/越权问题一律拒绝（评测集含真实用例）
- 开源仓库只含代码/配置/评测集；`local/` 凭证与 `DCP/` 报表资产不纳入

## 演进路线

- [ ] v1.1：评测驱动迭代（few-shot/温度调优，目标 90%+）
- [ ] v1.2：与 REP_* 报表表交叉对账（黄金结果外部背书）
- [ ] v2：导出脱敏快照到 SQLite（离线演示/开源数据）
- [ ] v3：语义查询能力封装为 MCP Server；Actions 动作层（带校验的写操作）
