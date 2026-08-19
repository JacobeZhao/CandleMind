# 后端命令

所有命令都应从仓库根目录以 Python 模块方式运行。生成数据和报告必须写入
外部数据根目录，禁止写入仓库。

## 数据维护

```powershell
python -m backend.scripts.data.sync_klines --root G:\CandleMind\CandleMind_data
python -m backend.scripts.data.sync_derivatives --root G:\CandleMind\CandleMind_data --release-id <id> --start <date> --through <date>
python -m backend.scripts.data.build_ema_data_release --help
```

- `sync_klines` 下载并校验 Binance Vision K 线，发布标准化 Parquet 和清单。
- `sync_derivatives` 发布不可变、满足因果时间约束的衍生品 release。
- EMA release builder 仅用于复现与验证历史 V2 数据，不训练或激活模型。

同步命令要求可写的数据根目录；应用容器中的 `/app/market-data` 为只读挂载，
不能用于执行同步。

## SAR + ADX 评估

```powershell
python -m backend.scripts.evaluation.run_sar_pyramid_backtest --help
python -m backend.scripts.evaluation.run_backtrader_sar_pyramid --help
python -m backend.scripts.evaluation.sweep_sol_adx_sar --help
python -m backend.scripts.evaluation.sweep_sol_adx_sar_v2 --help
python -m backend.scripts.evaluation.sweep_sol_adx_sar_v3 --help
python -m backend.scripts.evaluation.sweep_multi_symbol_sar_market_gate --help
python -m backend.scripts.evaluation.sweep_multi_symbol_sar_staged_risk --help
```

确定性账本与 Backtrader runner 使用已验证 OHLCV 和实际观测资金费率。参数扫描
仅适用于 SAR+ADX 研究；多币种 runner 使用月度 PIT 资格和固定等权子账户。
扫描结果不能直接作为生产准入证据。每次运行必须使用
新的 `--output`，并记录时间范围、成本、参数、输入 release 和代码 revision。

## 安全规则

执行写入型命令前先查看 `--help`。不得将输出指向仓库、覆盖不可变 release，
或把参数扫描中的最优结果当作未见样本结论。
