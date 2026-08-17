# 评测集说明

- `eval_set.json`（设计集 20 条）与 `blind_pool.json`（盲测池 8 条）**不随公开仓库分发**：
  内含基于内部脱敏测试库的真实执行结果（业务金额），由 `gen_eval.py` / `gen_blind.py`
  在内部环境生成，生成后请勿提交（已在 .gitignore 排除）。
- 公开仓库保留评测**方法论**与**运行器**：
  - `gen_eval.py` / `gen_blind.py`：按内部库生成黄金基准（expected_sql / expected_result）
  - `run_eval.py`：跑评测，输出 规划/执行/结果/拒绝 四项指标；`--blind` 跑盲测池；`--coverage` 输出覆盖矩阵
- 评测口径：语义等价判定（忽略 order_by、filters 顺序无关、结果行序无关、数字保留2位小数）。
- 复现路径：内部环境连接脱敏测试库 → `py gen_eval.py && py gen_blind.py` → `py run_eval.py --both`。
