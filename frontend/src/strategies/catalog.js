export const STRATEGIES = [
  {
    strategy_type: "sar_adx_trend",
    config_version: "sar_adx_trend_v1",
    name: "CandleMind趋势策略",
    description: "在趋势条件满足后分批建立仓位，并在方向失效时退出。",
    parameters: [
      { key: "execution_interval", label: "执行周期", type: "select", default: "5m", options: ["5m"], readOnly: true },
      { key: "sar_step", label: "SAR 加速因子", type: "number", default: 0.02, min: 0.001, max: 0.1, step: 0.001 },
      { key: "sar_max", label: "SAR 最大加速因子", type: "number", default: 0.2, min: 0.01, max: 0.5, step: 0.01 },
      { key: "max_layers", label: "最大仓位层数", type: "integer", default: 5, min: 1, max: 6, step: 1 },
      { key: "adx_timeframe", label: "趋势过滤周期", type: "select", default: "1h", options: ["1h"], readOnly: true },
      { key: "adx_period", label: "趋势强度周期", type: "integer", default: 14, min: 2, max: 100, step: 1 },
      { key: "adx_threshold", label: "趋势强度阈值", type: "number", default: 45, min: 1, max: 99, step: 1 },
      { key: "adx_rising_periods", label: "趋势增强确认 K 线数", type: "integer", default: 2, min: 0, max: 20, step: 1 },
      { key: "entry_confirmation_bars", label: "入场确认 K 线数", type: "integer", default: 6, min: 1, max: 50, step: 1 },
      { key: "recapture_buffer_fraction", label: "回撤再突破幅度", type: "percent", default: 0.0024, min: 0, max: 0.05, step: 0.0001 },
      { key: "max_entries_per_adx_regime", label: "单趋势最多入场次数", type: "integer", default: 2, min: 1, max: 20, step: 1 },
    ],
  },
  {
    strategy_type: "sar_martingale",
    config_version: "sar_martingale_v1",
    name: "SAR马丁",
    description: "SAR 确定方向，价格逆向移动达到阈值后按层增加仓位。",
    parameters: [
      { key: "execution_interval", label: "执行周期", type: "select", default: "5m", options: ["5m"], readOnly: true },
      { key: "sar_step", label: "SAR 加速因子", type: "number", default: 0.02, min: 0.001, max: 0.1, step: 0.001 },
      { key: "sar_max", label: "SAR 最大加速因子", type: "number", default: 0.2, min: 0.01, max: 0.5, step: 0.01 },
      { key: "max_layers", label: "最大仓位层数", type: "integer", default: 4, min: 1, max: 6, step: 1 },
      { key: "layer_multiplier", label: "仓位倍率", type: "number", default: 1.5, min: 1, max: 1.8, step: 0.1 },
      { key: "add_trigger_fraction", label: "逆向加仓幅度", type: "percent", default: 0.005, min: 0.001, max: 0.05, step: 0.001 },
    ],
  },
  {
    strategy_type: "sar_anti_martingale",
    config_version: "sar_anti_martingale_v1",
    name: "SAR反马丁",
    description: "SAR 确定方向，价格顺向移动达到阈值后逐层增加仓位。",
    parameters: [
      { key: "execution_interval", label: "执行周期", type: "select", default: "5m", options: ["5m"], readOnly: true },
      { key: "sar_step", label: "SAR 加速因子", type: "number", default: 0.02, min: 0.001, max: 0.1, step: 0.001 },
      { key: "sar_max", label: "SAR 最大加速因子", type: "number", default: 0.2, min: 0.01, max: 0.5, step: 0.01 },
      { key: "max_layers", label: "最大仓位层数", type: "integer", default: 4, min: 1, max: 6, step: 1 },
      { key: "layer_multiplier", label: "仓位倍率", type: "number", default: 1.5, min: 1, max: 1.8, step: 0.1 },
      { key: "add_trigger_fraction", label: "顺向加仓幅度", type: "percent", default: 0.005, min: 0.001, max: 0.05, step: 0.001 },
    ],
  },
];

export function strategyDefinition(strategyType, catalog = STRATEGIES) {
  return catalog.find((item) => item.strategy_type === strategyType) || STRATEGIES[0];
}

export function defaultParameters(definition) {
  return Object.fromEntries(definition.parameters.map((field) => [field.key, field.default]));
}

export function normalizeConfiguration(payload) {
  const source = payload?.configuration || payload?.config || payload;
  if (!source?.strategy_type || !source?.config_version || !source?.config_hash) return null;
  return {
    strategy_type: source.strategy_type,
    config_version: source.config_version,
    config_hash: source.config_hash,
    parameters: source.parameters || source.params || {},
    name: source.name,
  };
}

export function strategySummary(configuration, catalog = STRATEGIES) {
  const definition = strategyDefinition(configuration?.strategy_type, catalog);
  const parameters = configuration?.parameters || {};
  const keys = definition.strategy_type === "sar_adx_trend"
    ? ["execution_interval", "sar_step", "sar_max", "adx_threshold", "max_layers"]
    : ["execution_interval", "sar_step", "sar_max", "add_trigger_fraction", "max_layers", "layer_multiplier"];
  return keys.flatMap((key) => {
    const field = definition.parameters.find((item) => item.key === key);
    const value = parameters[key] ?? field?.default;
    if (!field || value == null) return [];
    const display = field.type === "percent" ? `${(Number(value) * 100).toFixed(1)}%` : String(value);
    return [`${field.label} ${display}`];
  }).join(" · ");
}
