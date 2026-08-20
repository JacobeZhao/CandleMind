import React, { useEffect, useRef, useState } from "react";
import { AlertCircle, Loader } from "lucide-react";
import clsx from "clsx";
import { getStrategyAnalytics } from "../api/client";
import { useApp } from "../context/AppContext";

const UNAVAILABLE = "暂不可用";
const PARTIAL_STATUSES = new Set(["partial", "estimated"]);
const UNAVAILABLE_STATUSES = new Set(["unavailable", "insufficient", "incomplete"]);

function metricValue(value, formatter, status) {
  const normalizedStatus = String(status || "").toLowerCase();
  if (
    value === null
    || value === undefined
    || !Number.isFinite(Number(value))
    || UNAVAILABLE_STATUSES.has(normalizedStatus)
  ) return UNAVAILABLE;
  return formatter(Number(value));
}

const percent = (value, status) => metricValue(value, (number) => `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`, status);
const plainPercent = (value, status) => metricValue(value, (number) => `${number.toFixed(2)}%`, status);
const money = (value, status) => metricValue(value, (number) => `${number >= 0 ? "+" : ""}${number.toFixed(2)} USDT`, status);
const count = (value, status) => metricValue(value, (number) => number.toLocaleString("zh-CN"), status);
const ratio = (value, status) => metricValue(value, (number) => number.toFixed(2), status);

function MetricCard({ label, status, value }) {
  const partial = PARTIAL_STATUSES.has(String(status || "").toLowerCase());
  return (
    <article className="min-w-0 rounded-md border border-border bg-surface/40 p-3 sm:p-4">
      <div className="flex min-h-5 items-start justify-between gap-2">
        <p className="text-xs text-muted">{label}</p>
        {partial && <span className="shrink-0 text-[11px] text-accent">部分数据</span>}
      </div>
      <p className={clsx("mt-2 break-words font-mono text-lg font-semibold", value === UNAVAILABLE ? "text-muted" : "text-white")}>{value}</p>
    </article>
  );
}

function displayDate(value) {
  if (!value) return UNAVAILABLE;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? UNAVAILABLE : date.toLocaleString("zh-CN");
}

export default function StrategyAnalyticsPanel() {
  const {
    networkTab,
    refreshRevision,
    strategyCapitalLimit,
    setStrategyCapitalLimit,
    symbol,
  } = useApp();
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const requestId = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const currentRequest = ++requestId.current;
    setState({ loading: true, error: null, data: null });
    getStrategyAnalytics(controller.signal)
      .then(({ data }) => {
        if (!controller.signal.aborted && requestId.current === currentRequest) {
          setState({ loading: false, error: null, data });
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted && requestId.current === currentRequest) {
          const detail = error?.response?.data?.detail;
          setState({ loading: false, data: null, error: typeof detail === "string" ? detail : "策略分析加载失败" });
        }
      });
    return () => controller.abort();
  }, [networkTab, refreshRevision, symbol]);

  const analytics = state.data;
  const overall = analytics?.overall || {};
  const coverageStatus = String(analytics?.coverage?.status || "").toLowerCase();
  const coveragePartial = PARTIAL_STATUSES.has(coverageStatus) || UNAVAILABLE_STATUSES.has(coverageStatus);
  const scopeNetwork = ["main", "mainnet", "production"].includes(String(analytics?.scope?.network).toLowerCase()) ? "真实网" : "测试网";
  const weekReturnStatus = analytics?.week?.return_status || analytics?.week?.status;
  const monthReturnStatus = analytics?.month?.return_status || analytics?.month?.status;

  return (
    <section className="mb-4 overflow-hidden rounded-md border border-border bg-card" aria-label="策略分析">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h1 className="text-sm font-semibold text-white">策略分析</h1>
          <p className="mt-1 text-xs text-muted">{symbol} · {networkTab === "main" ? "真实网" : "测试网"}</p>
        </div>
        <label className="flex items-center gap-2 text-xs text-muted">
          资金上限
          <input
            aria-label="资金上限"
            type="number"
            min="1"
            step="100"
            value={strategyCapitalLimit}
            onChange={(event) => setStrategyCapitalLimit(event.target.value)}
            className="w-24 rounded-md border border-border bg-surface px-2 py-1.5 text-right font-mono text-white outline-none focus:border-accent"
          />
          <span>USDT</span>
        </label>
      </div>
      {state.loading && <div role="status" className="flex h-48 items-center justify-center gap-2 text-sm text-muted"><Loader size={16} className="animate-spin" />正在加载策略分析</div>}
      {state.error && <div role="alert" className="flex h-48 items-center justify-center gap-2 px-4 text-sm text-red"><AlertCircle size={16} />{state.error}</div>}
      {!state.loading && !state.error && analytics && <>
        <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="本周收益" status={analytics.week?.status} value={money(analytics.week?.net_pnl_usdt, analytics.week?.status)} />
          <MetricCard label="本周收益率" status={weekReturnStatus} value={percent(analytics.week?.net_return_pct, weekReturnStatus)} />
          <MetricCard label="本月收益" status={analytics.month?.status} value={money(analytics.month?.net_pnl_usdt, analytics.month?.status)} />
          <MetricCard label="本月收益率" status={monthReturnStatus} value={percent(analytics.month?.net_return_pct, monthReturnStatus)} />
          <MetricCard label="多头交易" status={analytics.counts?.status} value={count(analytics.counts?.long, analytics.counts?.status)} />
          <MetricCard label="空头交易" status={analytics.counts?.status} value={count(analytics.counts?.short, analytics.counts?.status)} />
          <MetricCard label="胜率" status={overall.status} value={plainPercent(overall.win_rate_pct, overall.status)} />
          <MetricCard label="盈亏比" status={overall.status} value={ratio(overall.payoff_ratio, overall.status)} />
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 border-t border-border bg-surface/40 px-4 py-2 text-xs text-muted">
          <span>范围 <strong className="font-medium text-white">{analytics.scope?.symbol || symbol} · {scopeNetwork}</strong></span>
          <span>截至 <strong className="font-medium text-white">{displayDate(analytics.as_of)}</strong></span>
          <span>覆盖 <strong className={coveragePartial ? "font-medium text-accent" : "font-medium text-white"}>{coveragePartial ? "部分数据" : `${displayDate(analytics.coverage?.from)} - ${displayDate(analytics.coverage?.through)}`}</strong></span>
          {analytics.coverage?.sync_state && <span>同步 <strong className="font-medium text-white">{analytics.coverage.sync_state}</strong></span>}
        </div>
      </>}
      {!state.loading && !state.error && !analytics && <div className="flex h-48 items-center justify-center text-sm text-muted">{UNAVAILABLE}</div>}
    </section>
  );
}
