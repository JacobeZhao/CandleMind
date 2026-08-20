# CandleMind G 盘存储规范

权威实时数据根目录：

```text
G:\CandleMind\CandleMind_data
```

备份必须与实时数据并列，禁止嵌套到实时目录：

```text
G:\CandleMind\
|-- CandleMind_data\
`-- CandleMind_backups\
```

实时目录的标准分组为 `raw`、`normalized`、`processed`、`experiments`、
`runtime` 和 `manifests`。当前应用读取：

- `normalized/ohlcv_parquet`
- `normalized/ema/releases`
- `normalized/derivatives/releases`

运行数据库、加密密钥、`strategies/execution_<network>_<symbol>.json` 和
`analytics/strategy_analytics.sqlite3` 必须共同保存在 `runtime/app`。分析账本仅
统计 CandleMind 策略归属的成交，并记录覆盖缺口；它不参与订单恢复。回测输出放入 `experiments/backtests` 或
`experiments/reports`。`models` 仅可作为历史模型归档，当前 SAR+ADX 策略不依赖
其中内容。

Docker Compose 对 `CandleMind_data` 使用只读挂载，对 `runtime/app` 使用可写
挂载。数据同步应在宿主机通过 [`../backend/scripts/README.md`](../backend/scripts/README.md)
中的命令执行。

禁止手工编辑不可变 release、把生成数据放入 Git 仓库，或仅凭文件名相同删除
raw 数据。`pytest_*` 和其他临时目录不是标准数据资产，清理前仍需确认无进程占用、
无唯一日志且不在有效清单中。备份 runtime 时必须生成一致性快照，包含 SQLite
WAL、密钥、执行日志和分析账本，不能分散复制。
