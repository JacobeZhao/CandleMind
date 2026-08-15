# 数据与产物布局

## 归属边界

Git 仓库只保存应用源码、测试、配置模板、文档和受支持的命令。行情数据、报告、
日志、数据库、密钥、缓存、前端构建和模型产物都必须位于仓库外。

Windows 默认行情数据根目录为 `G:/CandleMind/CandleMind_data`：

- 直接运行后端时，`MARKET_DATA_DIR` 指定行情数据根目录，`DATA_DIR` 指定运行状态目录。
- Docker Compose 使用 `CANDLEMIND_DATA_ROOT` 和 `CANDLEMIND_RUNTIME_ROOT` 指定宿主机路径。
- Compose 将行情数据挂载到只读 `/app/market-data`，将 runtime 挂载到可写 `/app/runtime`。

非 Windows 环境必须显式配置 `MARKET_DATA_DIR` 和 `DATA_DIR`。

## 标准外部结构

```text
CandleMind_data/
|-- raw/
|   |-- klines_archive/
|   |-- funding/
|   `-- derivatives_archive/
|-- normalized/
|   |-- ohlcv_parquet/
|   |-- ema/releases/
|   `-- derivatives/releases/
|-- processed/
|   `-- features_app/
|-- experiments/
|   |-- backtests/
|   `-- reports/
|-- runtime/
|   `-- app/
`-- manifests/
```

`models/` 可以保留历史模型归档，但当前 SAR+ADX 运行时不读取模型文件。临时
pytest 目录、缓存和一次性 staging 目录不属于标准结构，确认无进程占用且不含
唯一证据后应清理。

## 应用读写规则

`backend/app/datastore.py` 负责选择权威行情数据根目录。仓库内不存在备用
`data/`；行情目录必须预先存在并通过 `backend/app/data_layout.py` 校验，应用
导入过程不会创建这些目录。

数据同步脚本要求根目录可写，并以原子方式发布输出。在线应用只需读取行情
release。`DATA_DIR` 独占运行状态，包括：

- `trader.db`
- `secret.key`
- `strategies/sar_adx_paper_<symbol>.json`

数据库和密钥是不可拆分的加密配置对。paper 状态空仓且因停机落后时，V3
运行时允许无历史成交地重新对齐；若恢复状态仍持仓，则拒绝跳过历史执行并要求
人工处理。

## Release 与备份规则

K 线和衍生品命令必须原子发布已验证 release，禁止原地修改不可变 release。
SAR+ADX 回测必须绑定 OHLCV、资金费率、参数、成本和代码 revision。跨品种回测
仍使用 SOL 调优参数，只能作为诊断结果。

移动或删除外部数据前，必须先清点路径与校验和、验证目标包含关系、保留 raw
数据，并成对备份 runtime 数据库和密钥。备份只保留经过验证且可恢复的版本。
