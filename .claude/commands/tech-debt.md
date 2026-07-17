# /tech-debt — CandleMind 技术债清理系统

## 调用方式

| 命令 | 行为 |
|------|------|
| `/tech-debt` | 全量流程（扫描→汇总→确认→修复→验证） |
| `/tech-debt audit` | 仅扫描汇总，不修改任何文件 |
| `/tech-debt ml` | 仅运行 ML 审计 Agent |
| `/tech-debt strategy` | 仅运行策略审计 Agent |
| `/tech-debt scripts` | 仅运行脚本/工具审计 Agent |
| `/tech-debt quality` | 仅运行代码质量审计 Agent |
| `/tech-debt fix` | 跳过扫描，直接用 `.claude/reports/latest.json` 修复 |
| `/tech-debt verify` | 仅运行 fix-verifier 验证上次修复 |
| `/tech-debt p3` | 仅运行 p3-auto-fixer 处理机械性清理 |

---

## Phase 0 — 加载历史报告

读取 `.claude/reports/latest.json`（如存在）。用于：
- 对比本次 vs 上次，标记 `new / recurring / resolved`
- `recurring`（连续 2+ 次出现）自动升优先级
- 输出 `run_id = YYYYMMDD-HHMM`

若文件不存在则跳过历史对比，全量作为 `new`。

---

## Phase 1 — 并发运行 4 个审计 Agent

同时启动，各自独立扫描，互不依赖：

1. **ml-auditor** → `ML-*` findings（数据泄漏、特征漂移、模型问题）
2. **strategy-auditor** → `ST-*` findings（回测/实盘不一致、门控逻辑缺失）
3. **scripts-auditor** → `SC-*` findings（脚本杂乱、路径硬编码、.gitignore）
4. **quality-auditor** → `QA-*` findings（异常捕获、超长函数、死代码）

每个 Agent 输出 JSON findings 数组。

---

## Phase 1.5 — Consolidator 汇总

运行 **consolidator** Agent：
- 合并 4 份 findings，去重（ID 精确 + 语义兜底）
- 统一优先级：P0（数据/安全风险）→ P1（行为正确性/架构）→ P2（代码质量）→ P3（整洁）
- 历史对比标记
- 保存到 `.claude/reports/YYYYMMDD-HHMM.json` 和 `latest.json`

输出：
```
找到 N 个问题  P0:X  P1:Y  P2:Z  P3:W
Sprint 工作量：P0+P1 约 Xh，P2 约 Yh，P3 机械处理
```

---

## Phase 2 — 分拣确认（等待用户）

按优先级展示 findings，格式：

```
[P0] ML-trend_predictor-insample-scoring
     回测在训练期用全量模型打分 → 指标虚高
     文件: backend/app/services/ml_strategy.py:180
     修复: 区分训练/OOS 打分路径，或在回测时用 CPCV OOS 分数
     工作量: 1h

[P1] ST-bot_engine-backtest-live-divergence
     ...
```

等待用户确认：
- `y` / `all` → 全部修复
- 输入 ID 列表 → 仅修复指定项
- `skip P2` → 跳过 P2
- `audit only` → 仅保存报告，不修复

---

## Phase 3 — 按优先级修复（每批一个 commit）

```
Sprint P0: 修复 X 个 P0 问题 → git commit "fix(p0): ..."
Sprint P1: 修复 Y 个 P1 问题 → git commit "fix(p1): ..."
Sprint P2: 修复 Z 个 P2 问题 → git commit "fix(p2): ..."
```

每个 fix 前先 Read 文件确认当前状态，再 Edit。

---

## Phase 4 — Fix Verifier 验证

运行 **fix-verifier** Agent，逐条核实：
- ✅ 已修复（文件改动方向正确）
- ⚠️ 部分修复（改动不完整）
- ❌ 未处理

输出验证表 + 未处理项列表。

---

## Phase 5 — P3 Auto-Fixer

运行 **p3-auto-fixer** Agent 处理机械性问题：
- 添加 .gitignore 条目
- 删除临时文件
- 修正错误项目路径引用

不自行 commit，提示用户 `git add -p`。

---

## Phase 6 — 收尾

- 更新 `latest.json` 为本次结果
- 输出总结：修复了 N 个，跳过 M 个，剩余 K 个
- 提示运行 `python -m backend.scripts.training.retrain_multi_horizon --help` 验证训练入口

---

## 项目关键约定（审计时使用）

### 目录结构
```
backend/
├── app/
│   ├── services/          # 核心逻辑
│   │   ├── ml_strategy.py     # 回测引擎（simulate_ml_trend）
│   │   ├── bot_engine.py      # 实盘/纸盘引擎（_ml_trend_cycle）
│   │   ├── trend_predictor.py # 模型训练
│   │   ├── feature_builder.py # 特征工程
│   │   └── ml_signal.py       # 实时信号生成
│   ├── routes/            # FastAPI 路由
│   └── datastore.py       # MARKET_ROOT 定义
├── scripts/               # 分析/诊断脚本（非生产）
└── scripts/{data,training,evaluation,artifacts}/
```

### 关键不变量
1. **回测/实盘对等**：`simulate_ml_trend` 中的所有门控逻辑必须同步到 `_ml_trend_cycle` 和 `_ml_trend_paper_step`
2. **路径规范**：所有脚本从 `app.datastore` 导入用途对应的目录常量；训练仅写 supervised candidate，推理通过 active release 解析器读取；禁止硬编码盘符路径
3. **特征排除**：`train_symbol()` 的 `exclude` set 必须包含 `'index'`（PSI=12.43 时序泄漏）
4. **阈值来源**：入场阈值从 `thresholds.json` 读取，不在代码中硬编码数值
5. **脚本归位**：命令必须归入 `backend/scripts/{data,training,evaluation,artifacts}/`，`backend/` 根目录不保留 Python 命令

### 数据路径
- `MARKET_ROOT` 默认是 `G:\CandleMind\CandleMind_data`（由 `app/datastore.py` 定义，可用 `MARKET_DATA_DIR` 覆盖）
- 当前监督模型 release：由 `<data-root>/models/current/ACTIVE` 指向 `models/releases/<release_id>`
- 候选模型与阈值：写入 `models/candidates/supervised/<release_id>`，封存并验证后整体晋升
- ML 特征：`FEATURES_ML_DIR / f'{symbol}_features.parquet'`，实际目录为 `processed/features_ml`
- 标注：`LABELS_DIR / ...`，实际目录为 `processed/labels`
- 回测与报告：分别使用 `BACKTEST_DIR`、`REPORTS_DIR`，实际目录位于 `experiments/`
