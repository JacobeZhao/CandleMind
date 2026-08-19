# 文档索引

## 当前运行文档

- [`DATA_LAYOUT.md`](DATA_LAYOUT.md)：仓库、行情数据和运行状态的归属边界。
- [`G_DRIVE_README.md`](G_DRIVE_README.md)：G 盘权威目录结构与备份规则。
- [`DERIVATIVES_DATA_V1.md`](DERIVATIVES_DATA_V1.md)：衍生品数据来源、因果性和验收证据。
- [`AI_CONFIGURATION.md`](AI_CONFIGURATION.md)：AI Provider、Base URL、代理和密钥安全边界。
- [`../backend/scripts/README.md`](../backend/scripts/README.md)：受支持的数据维护与评估命令。
- [`../ops/README.md`](../ops/README.md)：Docker Compose 启动和隔离验证流程。

## 冻结研究证据

- [`research/RL_RESEARCH_STATUS.md`](research/RL_RESEARCH_STATUS.md)：保留的 EMA/RL
  研究基础设施、当前运行边界与未来接入门槛。
- [`research/SAR_ADX_PYRAMID_BASELINE_V1.md`](research/SAR_ADX_PYRAMID_BASELINE_V1.md)
- [`research/SAR_ADX_SOL_OPTIMIZATION_V1.md`](research/SAR_ADX_SOL_OPTIMIZATION_V1.md)
- [`research/SAR_ADX_BACKTRADER_VALIDATION_V1.md`](research/SAR_ADX_BACKTRADER_VALIDATION_V1.md)

这些研究文档保留实验时的参数、指标和清单哈希，不随当前页面文案改写。它们
共同表明现有 SAR+ADX V3 仍是诊断与 paper trading 策略，不是盈利或生产准入
证明。

强化学习相关代码当前只用于研究兼容和数据契约。在线决策仍由 SAR+ADX V3
paper trading 链路承担，尚未接入 RL 推理。

生成报告必须写入
`G:\CandleMind\CandleMind_data\experiments\reports`，不得提交到 `docs/`。
