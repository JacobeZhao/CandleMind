import React, { useEffect, useMemo, useState } from "react";
import { AlertCircle, Check, LoaderCircle, Save, ShieldCheck, TrendingDown, TrendingUp } from "lucide-react";
import clsx from "clsx";
import { getStrategyCatalog, saveStrategyConfig } from "../api/client";
import { useApp } from "../context/AppContext";
import {
  STRATEGIES,
  defaultParameters,
  normalizeConfiguration,
  strategyDefinition,
} from "../strategies/catalog";

const ICONS = {
  sar_adx_trend: ShieldCheck,
  sar_martingale: TrendingDown,
  sar_anti_martingale: TrendingUp,
};

const INPUT_CLASS = "w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-white outline-none transition-colors focus:border-accent disabled:cursor-not-allowed disabled:opacity-60";

function apiError(error, fallback) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join("；");
  return fallback;
}

function normalizeCatalog(payload) {
  const rows = payload?.strategies || payload?.catalog || payload;
  if (!Array.isArray(rows)) return STRATEGIES;
  return STRATEGIES.map((fallback) => {
    const remote = rows.find((item) => item.strategy_type === fallback.strategy_type);
    if (!remote) return fallback;
    return {
      ...fallback,
      config_version: remote.config_version || fallback.config_version,
      parameters: fallback.parameters.map((field) => ({
        ...field,
        default: remote.default_parameters?.[field.key] ?? field.default,
      })),
    };
  });
}

function editableValue(field, value) {
  if (field.type === "percent" && value !== "") return Number(value) * 100;
  return value;
}

function storedValue(field, value) {
  if (field.type === "select") return value;
  if (value === "") return "";
  const numeric = Number(value);
  return field.type === "percent" ? numeric / 100 : numeric;
}

function validate(definition, values) {
  const errors = {};
  definition.parameters.forEach((field) => {
    const value = values[field.key];
    if (field.type === "select") {
      if (!value || (field.options?.length && !field.options.includes(value))) errors[field.key] = "请选择有效值";
      return;
    }
    const numeric = Number(value);
    if (value === "" || !Number.isFinite(numeric)) errors[field.key] = "请输入有效数字";
    else if (field.min != null && numeric < field.min) errors[field.key] = `不能小于 ${field.min}`;
    else if (field.max != null && numeric > field.max) errors[field.key] = `不能大于 ${field.max}`;
    else if (field.type === "integer" && !Number.isInteger(numeric)) errors[field.key] = "必须为整数";
  });
  if (Number(values.sar_step) > Number(values.sar_max)) {
    errors.sar_max = "必须大于或等于 SAR 加速因子";
  }
  return errors;
}

export default function Strategies() {
  const {
    botStatus,
    strategyConfiguration,
    strategyConfigurationError,
    loadStrategyConfiguration,
    setStrategyConfiguration,
  } = useApp();
  const [catalog, setCatalog] = useState(STRATEGIES);
  const [selectedType, setSelectedType] = useState(strategyConfiguration?.strategy_type || STRATEGIES[0].strategy_type);
  const [forms, setForms] = useState(() => Object.fromEntries(
    STRATEGIES.map((item) => [item.strategy_type, defaultParameters(item)]),
  ));
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    let active = true;
    Promise.allSettled([getStrategyCatalog(), loadStrategyConfiguration()])
      .then(([catalogResult]) => {
        if (!active) return;
        if (catalogResult.status === "fulfilled") {
          setCatalog(normalizeCatalog(catalogResult.value.data));
        }
      })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [loadStrategyConfiguration]);

  useEffect(() => {
    if (!strategyConfiguration) return;
    setSelectedType(strategyConfiguration.strategy_type);
    setForms((current) => ({
      ...current,
      [strategyConfiguration.strategy_type]: {
        ...defaultParameters(strategyDefinition(strategyConfiguration.strategy_type, catalog)),
        ...strategyConfiguration.parameters,
      },
    }));
  }, [catalog, strategyConfiguration]);

  const definition = strategyDefinition(selectedType, catalog);
  const values = forms[selectedType] || defaultParameters(definition);
  const errors = useMemo(() => validate(definition, values), [definition, values]);
  const hasErrors = Object.keys(errors).length > 0;
  const running = Boolean(botStatus?.running);

  const selectStrategy = (strategyType) => {
    setSelectedType(strategyType);
    setMessage("");
    setError("");
    setForms((current) => current[strategyType]
      ? current
      : { ...current, [strategyType]: defaultParameters(strategyDefinition(strategyType, catalog)) });
  };

  const update = (field, rawValue) => {
    const value = storedValue(field, rawValue);
    setForms((current) => ({
      ...current,
      [selectedType]: { ...current[selectedType], [field.key]: value },
    }));
    setMessage("");
    setError("");
  };

  const save = async () => {
    if (hasErrors || running || saving) return;
    setSaving(true);
    setMessage("");
    setError("");
    try {
      const { data } = await saveStrategyConfig({
        strategy_type: definition.strategy_type,
        parameters: values,
        ...(strategyConfiguration?.config_hash
          ? { expected_config_hash: strategyConfiguration.config_hash }
          : {}),
      });
      const configuration = normalizeConfiguration(data);
      if (!configuration) throw new Error("Invalid strategy configuration response");
      setStrategyConfiguration(configuration);
      setMessage("策略配置已保存，全局启动将使用这份配置。 ");
    } catch (requestError) {
      setError(apiError(requestError, "策略配置保存失败，请稍后重试。"));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="mx-auto w-full max-w-6xl space-y-4">
      <header className="flex flex-wrap items-end justify-between gap-3 border-b border-border pb-4">
        <div>
          <h1 className="text-xl font-semibold text-white">自动化交易策略</h1>
          <p className="mt-1 text-sm text-muted">选择并保存一套策略配置，全局启动按钮将应用到当前品种。</p>
        </div>
        {strategyConfiguration?.config_hash && (
          <div className="text-right text-xs text-muted">
            当前配置 <span className="font-mono text-white">{strategyConfiguration.config_hash.slice(0, 10)}</span>
          </div>
        )}
      </header>

      {loading ? (
        <div className="flex min-h-48 items-center justify-center text-muted" role="status">
          <LoaderCircle size={20} className="mr-2 animate-spin" />正在加载策略配置
        </div>
      ) : (
        <>
          <section aria-label="策略选择" className="grid gap-3 md:grid-cols-3">
            {catalog.map((item) => {
              const Icon = ICONS[item.strategy_type] || ShieldCheck;
              const selected = selectedType === item.strategy_type;
              const saved = strategyConfiguration?.strategy_type === item.strategy_type;
              return (
                <button
                  key={item.strategy_type}
                  type="button"
                  aria-pressed={selected}
                  onClick={() => selectStrategy(item.strategy_type)}
                  disabled={running}
                  className={clsx(
                    "min-h-36 rounded-md border p-4 text-left transition-colors focus:outline-none focus:ring-2 focus:ring-accent disabled:cursor-not-allowed disabled:opacity-60",
                    selected ? "border-accent bg-accent/10" : "border-border bg-card hover:border-muted",
                  )}
                >
                  <div className="flex items-start justify-between gap-3">
                    <Icon size={20} className={selected ? "text-accent" : "text-muted"} />
                    {saved && <span className="flex items-center gap-1 text-xs text-green"><Check size={13} />已保存</span>}
                  </div>
                  <div className="mt-4 font-semibold text-white">{item.name}</div>
                  <p className="mt-1 text-xs leading-5 text-muted">{item.description}</p>
                </button>
              );
            })}
          </section>

          <section className="border-t border-border pt-4" aria-labelledby="strategy-parameters-title">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 id="strategy-parameters-title" className="text-base font-semibold text-white">{definition.name}参数</h2>
                <p className="mt-1 text-xs text-muted">执行周期固定为已验证的 5 分钟，资金上限仍在顶部启动流程中设置。</p>
              </div>
              {running && <span className="text-xs text-accent">策略运行期间不可修改配置</span>}
            </div>

            <div className="grid gap-x-5 gap-y-4 sm:grid-cols-2 lg:grid-cols-3">
              {definition.parameters.map((field) => (
                <label key={field.key} htmlFor={`strategy-${field.key}`} className="block min-w-0 text-sm text-muted">
                  <span>{field.label}</span>
                  {field.type === "select" ? (
                    <select
                      id={`strategy-${field.key}`}
                      aria-label={field.label}
                      className={`mt-1.5 ${INPUT_CLASS}`}
                      value={values[field.key] ?? field.default}
                      onChange={(event) => update(field, event.target.value)}
                      disabled={running || field.readOnly}
                    >
                      {field.options.map((option) => <option key={option} value={option}>{option}</option>)}
                    </select>
                  ) : (
                    <div className="relative mt-1.5">
                      <input
                        id={`strategy-${field.key}`}
                        aria-label={field.label}
                        className={clsx(INPUT_CLASS, errors[field.key] && "border-red focus:border-red", field.type === "percent" && "pr-9")}
                        type="number"
                        inputMode="decimal"
                        value={editableValue(field, values[field.key] ?? field.default)}
                        min={field.type === "percent" ? field.min * 100 : field.min}
                        max={field.type === "percent" ? field.max * 100 : field.max}
                        step={field.type === "percent" ? field.step * 100 : field.step}
                        onChange={(event) => update(field, event.target.value)}
                        disabled={running || field.readOnly}
                        aria-invalid={Boolean(errors[field.key])}
                        aria-describedby={errors[field.key] ? `${field.key}-error` : undefined}
                      />
                      {field.type === "percent" && <span className="pointer-events-none absolute right-3 top-2 text-sm text-muted">%</span>}
                    </div>
                  )}
                  {errors[field.key] && <span id={`${field.key}-error`} className="mt-1 block text-xs text-red">{errors[field.key]}</span>}
                </label>
              ))}
            </div>

            {(error || strategyConfigurationError) && (
              <div role="alert" className="mt-4 flex items-start gap-2 rounded-md border border-red/30 bg-red/10 px-3 py-2 text-sm text-red">
                <AlertCircle size={16} className="mt-0.5 shrink-0" />{error || strategyConfigurationError}
              </div>
            )}
            {message && <div role="status" className="mt-4 text-sm text-green">{message}</div>}

            <div className="mt-5 flex justify-end">
              <button
                type="button"
                onClick={save}
                disabled={hasErrors || running || saving}
                className="flex min-h-9 items-center gap-2 rounded-md bg-accent px-4 py-2 text-sm font-semibold text-black transition-colors hover:bg-yellow-400 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {saving ? <LoaderCircle size={16} className="animate-spin" /> : <Save size={16} />}
                保存策略配置
              </button>
            </div>
          </section>
        </>
      )}
    </div>
  );
}
