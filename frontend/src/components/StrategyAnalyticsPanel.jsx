import React, { useEffect, useRef, useState } from "react";
import { AlertCircle, Loader } from "lucide-react";
import clsx from "clsx";
import { getAccountTradingAnalytics } from "../api/client";
import { useApp } from "../context/AppContext";

const UNAVAILABLE = "暂无样本";
const PARTIAL_STATUSES = new Set(["partial", "estimated"]);
const UNAVAILABLE_STATUSES = new Set(["unavailable", "insufficient", "incomplete"]);

function metricValue(value, formatter) {
  if (
    value === null
    || value === undefined
    || !Number.isFinite(Number(value))
  ) return UNAVAILABLE;
  return formatter(Number(value));
}

const percent = (value) => metricValue(value, (number) => `${number >= 0 ? "+" : ""}${number.toFixed(2)}%`);
const plainPercent = (value) => metricValue(value, (number) => `${number.toFixed(2)}%`);
const money = (value) => metricValue(value, (number) => `${number >= 0 ? "+" : ""}${number.toFixed(2)} USDT`);
const count = (value) => metricValue(value, (number) => number.toLocaleString("zh-CN"));
const ratio = (value) => metricValue(value, (number) => number.toFixed(2));

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
  const { networkTab, refreshRevision, symbol } = useApp();
  const [state, setState] = useState({ loading: true, error: null, data: null });
  const requestId = useRef(0);

  useEffect(() => {
    const controller = new AbortController();
    const currentRequest = ++requestId.current;
    setState({ loading: true, error: null, data: null });
    getAccountTradingAnalytics(symbol, controller.signal)
      .then(({ data }) => {
        const responseNetwork = String(data?.scope?.network || "").toLowerCase();
        const expectedNetwork = networkTab === "main" ? "mainnet" : "testnet";
        const normalizedNetwork = responseNetwork === "main" ? "mainnet" : responseNetwork === "test" ? "testnet" : responseNetwork;
        const responseScope = `${normalizedNetwork}:${data?.scope?.symbol || ""}`;
        if (
          !controller.signal.aborted
          && requestId.current === currentRequest
          && responseScope === `${expectedNetwork}:${symbol}`
        ) {
          setState({ loading: false, error: null, data });
        } else if (!controller.signal.aborted && requestId.current === currentRequest) {
          setState({ loading: false, data: null, error: "账户交易统计范围不一致，请重试" });
        }
      })
      .catch((error) => {
        if (!controller.signal.aborted && requestId.current === currentRequest) {
          const detail = error?.response?.data?.detail;
          setState({ loading: false, data: null, error: typeof detail === "string" ? detail : "账户交易统计加载失败" });
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
    <section className="mb-4 overflow-hidden rounded-md border border-border bg-card" aria-label="账户交易统计">
      <div className="flex flex-wrap items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <h1 className="text-sm font-semibold text-white">账户交易统计</h1>
          <p className="mt-1 text-xs text-muted">{symbol} · {networkTab === "main" ? "真实网" : "测试网"}</p>
        </div>
      </div>
      {state.loading && <div role="status" className="flex h-48 items-center justify-center gap-2 text-sm text-muted"><Loader size={16} className="animate-spin" />正在加载账户交易统计</div>}
      {state.error && <div role="alert" className="flex h-48 items-center justify-center gap-2 px-4 text-sm text-red"><AlertCircle size={16} />{state.error}</div>}
      {!state.loading && !state.error && analytics && <>
        <div className="grid grid-cols-1 gap-3 p-4 sm:grid-cols-2 xl:grid-cols-4">
          <MetricCard label="本周收益" status={analytics.week?.status} value={money(analytics.week?.net_pnl_usdt)} />
          <MetricCard label="本周收益率" status={weekReturnStatus} value={percent(analytics.week?.net_return_pct)} />
          <MetricCard label="本月收益" status={analytics.month?.status} value={money(analytics.month?.net_pnl_usdt)} />
          <MetricCard label="本月收益率" status={monthReturnStatus} value={percent(analytics.month?.net_return_pct)} />
          <MetricCard label="多头交易" status={analytics.counts?.status} value={count(analytics.counts?.long)} />
          <MetricCard label="空头交易" status={analytics.counts?.status} value={count(analytics.counts?.short)} />
          <MetricCard label="胜率" status={overall.status} value={plainPercent(overall.win_rate_pct)} />
          <MetricCard label="盈亏比" status={overall.status} value={ratio(overall.payoff_ratio)} />
        </div>
        <div className="flex flex-wrap gap-x-5 gap-y-1 border-t border-border bg-surface/40 px-4 py-2 text-xs text-muted">
          <span>范围 <strong className="font-medium text-white">{analytics.scope?.symbol || symbol} · {scopeNetwork}</strong></span>
          <span>截至 <strong className="font-medium text-white">{displayDate(analytics.as_of)}</strong></span>
          <span>覆盖 <strong className={coveragePartial ? "font-medium text-accent" : "font-medium text-white"}>{coveragePartial ? "数据覆盖不足" : `${displayDate(analytics.coverage?.from)} - ${displayDate(analytics.coverage?.through)}`}</strong></span>
          {analytics.coverage?.sync_state && <span>同步 <strong className="font-medium text-white">{analytics.coverage.sync_state}</strong></span>}
        </div>
      </>}
      {!state.loading && !state.error && !analytics && <div className="flex h-48 items-center justify-center text-sm text-muted">{UNAVAILABLE}</div>}
    </section>
  );
}
