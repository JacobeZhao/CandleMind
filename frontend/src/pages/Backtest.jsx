import React, { useEffect, useMemo, useState } from "react";
import {
  Activity,
  AlertCircle,
  BarChart3,
  CheckCircle2,
  ChevronDown,
  ChevronUp,
  Database,
  Play,
  ShieldAlert,
} from "lucide-react";
import {
  Area,
  CartesianGrid,
  ComposedChart,
  Line,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import { getSarAdxBacktestCapabilities, runSarAdxBacktest } from "../api/client";
import { useApp } from "../context/AppContext";

const FALLBACK_SYMBOLS = [
  "AAVEUSDT", "ADAUSDT", "APTUSDT", "ARBUSDT", "ATOMUSDT", "AVAXUSDT", "BCHUSDT", "BNBUSDT",
  "BTCUSDT", "DOGEUSDT", "DOTUSDT", "ETCUSDT", "ETHUSDT", "FILUSDT", "GALAUSDT", "INJUSDT",
  "LDOUSDT", "LINKUSDT", "LTCUSDT", "NEARUSDT", "OPUSDT", "RUNEUSDT", "SEIUSDT", "SOLUSDT",
  "SUIUSDT", "TIAUSDT", "TRXUSDT", "UNIUSDT", "XLMUSDT", "XRPUSDT",
];

function capabilitySymbols(data) {
  const rows = data?.symbols ?? data?.available_symbols ?? data?.eligible_symbols ?? [];
  if (!Array.isArray(rows)) return [];
  return [...new Set(rows.map((row) => typeof row === "string" ? row : row?.symbol).filter(Boolean))].sort();
}

const INPUT_CLASS =
  "w-full rounded-md border border-border bg-surface px-3 py-2 text-sm text-white outline-none transition-colors focus:border-accent disabled:cursor-not-allowed disabled:opacity-60";

const FROZEN_PARAMETERS = [
  ["执行周期", "5m"],
  ["SAR", "0.02 / 0.20"],
  ["趋势过滤", "1h ADX(14) >= 45"],
  ["ADX 动量", "连续上升 2 周期"],
  ["入场确认", "6 根 K 线"],
  ["仓位结构", "5 层 x 20%"],
  ["回踩再突破", "0.24%"],
  ["Regime 开仓上限", "2 次"],
];

function money(value, digits = 2) {
  if (value == null || !Number.isFinite(Number(value))) return "--";
  return new Intl.NumberFormat("en-US", {
    style: "currency",
    currency: "USD",
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  }).format(Number(value));
}

function number(value, digits = 2) {
  if (value == null || !Number.isFinite(Number(value))) return "--";
  return Number(value).toLocaleString("en-US", {
    minimumFractionDigits: digits,
    maximumFractionDigits: digits,
  });
}

function percent(value, digits = 2) {
  if (value == null || !Number.isFinite(Number(value))) return "--";
  return `${Number(value) >= 0 ? "+" : ""}${number(Number(value) * 100, digits)}%`;
}

function dateTime(value) {
  if (!value) return "--";
  return String(value).replace("T", " ").replace(/\+00:00$/, " UTC").slice(0, 20);
}

function direction(value) {
  if (Number(value) === 1 || String(value).toLowerCase() === "long") return "多";
  if (Number(value) === -1 || String(value).toLowerCase() === "short") return "空";
  return value ?? "--";
}

function apiError(error) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) return detail.map((item) => item.msg).filter(Boolean).join("；");
  return error?.message || "回测执行失败，请稍后重试。";
}

function Metric({ label, value, tone = "text-white", note }) {
  return (
    <div className="min-w-0 rounded-md border border-border bg-card p-3">
      <div className="mb-1 text-xs text-muted">{label}</div>
      <div className={`truncate font-mono text-lg font-semibold ${tone}`}>{value}</div>
      {note && <div className="mt-1 text-[11px] leading-4 text-muted">{note}</div>}
    </div>
  );
}

function StatusBanner({ status, message }) {
  if (status === "idle") return null;
  const running = status === "running";
  const failed = status === "error";
  const Icon = failed ? AlertCircle : running ? Activity : CheckCircle2;
  return (
    <div
      className={`flex items-center gap-2 rounded-md border px-3 py-2 text-sm ${
        failed
          ? "border-red/30 bg-red/10 text-red"
          : running
            ? "border-accent/30 bg-accent/10 text-accent"
            : "border-green/30 bg-green/10 text-green"
      }`}
      role={failed ? "alert" : "status"}
    >
      <Icon size={16} className={running ? "animate-pulse" : ""} />
      <span>{failed ? message : running ? "正在读取受验证数据并执行 Backtrader 回测..." : "回测完成"}</span>
    </div>
  );
}

function DetailTable({ type, rows }) {
  const configs = {
    fills: {
      columns: [
        ["time", "时间", dateTime],
        ["action", "动作"],
        ["direction", "方向", direction],
        ["price", "成交价", (v) => number(v, 4)],
        ["size", "数量", (v) => number(v, 5)],
        ["commission", "手续费", money],
      ],
      empty: "该区间没有成交记录。",
    },
    funding: {
      columns: [
        ["time", "时间", dateTime],
        ["notional", "名义金额", money],
        ["rate", "资金费率", (v) => percent(v, 4)],
        ["payment", "资金费现金流", money],
      ],
      empty: "该区间没有资金费记录。",
    },
  };
  const config = configs[type];
  if (!rows.length) return <div className="py-10 text-center text-sm text-muted">{config.empty}</div>;
  return (
    <div className="max-h-80 overflow-auto">
      <table className="w-full min-w-[680px] text-left text-xs">
        <thead className="sticky top-0 bg-card text-muted">
          <tr className="border-b border-border">
            {config.columns.map(([, label]) => <th key={label} className="px-3 py-2 font-medium">{label}</th>)}
          </tr>
        </thead>
        <tbody>
          {rows.map((row, index) => (
            <tr key={`${row.time || "row"}-${index}`} className="border-b border-border/60 hover:bg-surface/50">
              {config.columns.map(([key, , format]) => (
                <td key={key} className="whitespace-nowrap px-3 py-2 font-mono text-white">
                  {format ? format(row[key]) : row[key] ?? "--"}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

export default function Backtest() {
  const { symbol: headerSymbol } = useApp();
  const [form, setForm] = useState({
    symbol: headerSymbol || "SOLUSDT",
    start_date: "2025-01-01",
    end_date: "2026-01-01",
    initial_capital: 10000,
    fee_rate: 0.001,
    slippage_bps: 2,
  });
  const [status, setStatus] = useState("idle");
  const [error, setError] = useState("");
  const [result, setResult] = useState(null);
  const [activeDetail, setActiveDetail] = useState("fills");
  const [detailsOpen, setDetailsOpen] = useState(false);
  const [symbols, setSymbols] = useState(FALLBACK_SYMBOLS);
  const [coverageBySymbol, setCoverageBySymbol] = useState({});
  const [capabilityWarning, setCapabilityWarning] = useState("");

  useEffect(() => {
    let active = true;
    getSarAdxBacktestCapabilities()
      .then(({ data }) => {
        if (!active) return;
        const available = capabilitySymbols(data);
        if (available.length) setSymbols(available);
        setCoverageBySymbol(Object.fromEntries(
          (data?.coverage || []).map((row) => [row.symbol, row]),
        ));
        setCapabilityWarning("");
      })
      .catch(() => {
        if (active) setCapabilityWarning("无法读取数据能力清单，当前显示已验证发布的 30 个品种；运行时仍以后端校验为准。");
      });
    return () => { active = false; };
  }, []);

  useEffect(() => {
    if (headerSymbol && symbols.includes(headerSymbol)) update("symbol", headerSymbol);
  }, [headerSymbol, symbols]);

  const update = (key, value) => setForm((current) => ({ ...current, [key]: value }));
  const selectedCoverage = coverageBySymbol[form.symbol];
  const coverageStart = selectedCoverage?.start?.slice(0, 10);
  const coverageEnd = selectedCoverage?.end?.slice(0, 10);

  useEffect(() => {
    if (!coverageStart || !coverageEnd) return;
    setForm((current) => ({
      ...current,
      start_date: current.start_date < coverageStart ? coverageStart : current.start_date,
      end_date: current.end_date > coverageEnd ? coverageEnd : current.end_date,
    }));
  }, [coverageStart, coverageEnd]);

  const validationError = useMemo(() => {
    if (!form.start_date || !form.end_date) return "请选择完整的回测日期。";
    if (form.start_date >= form.end_date) return "结束日期必须晚于开始日期。";
    if (!(Number(form.initial_capital) > 0)) return "初始资金必须大于 0。";
    if (Number(form.fee_rate) < 0 || Number(form.fee_rate) > 0.01) return "手续费率必须在 0 到 1% 之间。";
    if (Number(form.slippage_bps) < 0 || Number(form.slippage_bps) > 100) return "滑点必须在 0 到 100 bps 之间。";
    return "";
  }, [form]);

  const run = async () => {
    if (validationError) {
      setError(validationError);
      setStatus("error");
      return;
    }
    setStatus("running");
    setError("");
    setResult(null);
    try {
      const { data } = await runSarAdxBacktest({
        symbol: form.symbol,
        start_date: form.start_date,
        end_date: form.end_date,
        initial_capital: Number(form.initial_capital),
        fee_rate: Number(form.fee_rate),
        slippage_bps: Number(form.slippage_bps),
      });
      setResult(data);
      setStatus("done");
    } catch (requestError) {
      setError(apiError(requestError));
      setStatus("error");
    }
  };

  const metrics = result?.metrics;
  const trades = result?.trades ?? [];
  const chartData = useMemo(() => {
    if (!result) return [];
    const drawdowns = new Map((result.drawdown_curve ?? []).map((point) => [point.time, point.drawdown]));
    return (result.equity_curve ?? []).map((point) => ({
      time: point.time,
      label: dateTime(point.time),
      equity: point.equity,
      drawdown: drawdowns.get(point.time),
    }));
  }, [result]);
  const profitable = Number(metrics?.total_return) >= 0;

  return (
    <div className="mx-auto min-w-0 max-w-[1480px] space-y-4 pb-8">
      <header className="flex flex-col gap-3 border-b border-border pb-4 lg:flex-row lg:items-center lg:justify-between">
        <div className="min-w-0">
          <div className="mb-1 flex min-w-0 flex-col items-start gap-2 sm:flex-row sm:flex-wrap sm:items-center">
            <h1 className="text-xl font-semibold text-white">SAR + ADX 回测</h1>
            <span className="max-w-full whitespace-normal rounded border border-red/40 bg-red/10 px-2 py-0.5 text-xs font-medium text-red">
              研究回测 / 未通过生产准入
            </span>
          </div>
          <p className="break-all text-sm text-muted">{form.symbol} · 5 分钟执行 · 1 小时趋势过滤 · Backtrader 专业回测</p>
        </div>
        <div className="flex items-center gap-2 text-xs text-muted">
          <ShieldAlert size={15} className="text-accent" />
          结果用于诊断，不代表未来收益或实盘可用性
        </div>
      </header>

      <section className="rounded-md border border-border bg-card p-4">
        <div className="mb-4 flex items-center gap-2">
          <BarChart3 size={16} className="text-accent" />
          <h2 className="text-sm font-semibold text-white">回测设置</h2>
        </div>
        <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-5">
          <div>
            <label className="mb-1 block text-xs text-muted">品种</label>
            <select value={form.symbol} onChange={(e) => update("symbol", e.target.value)} disabled={status === "running"} className={INPUT_CLASS}>
              {symbols.map((item) => <option key={item} value={item}>{item}</option>)}
            </select>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">开始日期</label>
            <input type="date" min={coverageStart} max={coverageEnd} value={form.start_date} onChange={(e) => update("start_date", e.target.value)} className={INPUT_CLASS} />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">结束日期（不含）</label>
            <input type="date" min={coverageStart} max={coverageEnd} value={form.end_date} onChange={(e) => update("end_date", e.target.value)} className={INPUT_CLASS} />
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">初始资金（USD）</label>
            <input type="number" min="1" step="100" value={form.initial_capital} onChange={(e) => update("initial_capital", e.target.value)} className={INPUT_CLASS} />
          </div>
          <div className="flex items-end">
            <button
              type="button"
              onClick={run}
              disabled={status === "running"}
              className="flex h-[38px] w-full items-center justify-center gap-2 rounded-md bg-accent px-4 text-sm font-semibold text-black transition-colors hover:bg-accent/90 disabled:cursor-not-allowed disabled:opacity-60"
            >
              <Play size={15} fill="currentColor" />
              {status === "running" ? "运行中" : "运行回测"}
            </button>
          </div>
        </div>
        <div className="mt-3 grid gap-3 sm:grid-cols-2 lg:w-2/5">
          <div>
            <label className="mb-1 block text-xs text-muted">手续费率（单边）</label>
            <input type="number" min="0" max="0.01" step="0.0001" value={form.fee_rate} onChange={(e) => update("fee_rate", e.target.value)} className={INPUT_CLASS} />
            <div className="mt-1 text-[11px] text-muted">当前 {percent(form.fee_rate, 3)}</div>
          </div>
          <div>
            <label className="mb-1 block text-xs text-muted">滑点（bps / 单边）</label>
            <input type="number" min="0" max="100" step="1" value={form.slippage_bps} onChange={(e) => update("slippage_bps", e.target.value)} className={INPUT_CLASS} />
            <div className="mt-1 text-[11px] text-muted">滑点已计入成交价，不单独估算成本</div>
          </div>
        </div>
        <div className="mt-3 border border-accent/25 bg-accent/5 px-3 py-2 text-xs leading-5 text-muted">
          当前 V3 参数基于 SOLUSDT 调优。其他品种使用相同参数仅用于跨币诊断，不代表参数已适配或具备生产准入条件。
          {coverageStart && coverageEnd && <span className="ml-1">数据范围 {coverageStart} 至 {coverageEnd}，结束日期不含。</span>}
          {capabilityWarning && <span className="ml-1 text-accent">{capabilityWarning}</span>}
        </div>
      </section>

      <section className="border-y border-border py-3">
        <div className="mb-2 flex items-center justify-between gap-3">
          <h2 className="text-xs font-semibold uppercase text-muted">冻结策略参数</h2>
          <span className="text-[11px] text-muted">页面不可修改</span>
        </div>
        <dl className="grid grid-cols-2 gap-x-5 gap-y-2 text-xs md:grid-cols-4 xl:grid-cols-8">
          {FROZEN_PARAMETERS.map(([label, value]) => (
            <div key={label} className="min-w-0">
              <dt className="truncate text-muted">{label}</dt>
              <dd className="mt-0.5 break-words font-mono text-white">{value}</dd>
            </div>
          ))}
        </dl>
      </section>

      <StatusBanner status={status} message={error} />

      {metrics && (
        <>
          <section>
            <div className="mb-2 flex flex-wrap items-end justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-white">账户结果</h2>
                <p className="text-xs text-muted">最终权益和总收益已包含 observed funding；滑点已体现在成交价格中。</p>
              </div>
              <span className="text-xs text-muted">{result.window?.start?.slice(0, 10)} 至 {result.window?.end?.slice(0, 10)} · {result.window?.semantics}</span>
            </div>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-4 xl:grid-cols-6">
              <Metric label="总收益" value={percent(metrics.total_return)} tone={profitable ? "text-green" : "text-red"} />
              <Metric label="最终权益" value={money(metrics.final_equity)} />
              <Metric label="最大回撤" value={percent(metrics.max_drawdown)} tone="text-red" />
              <Metric label="手续费" value={money(metrics.commission)} tone="text-red" />
              <Metric label="资金费现金流" value={money(metrics.funding_pnl)} tone={Number(metrics.funding_pnl) >= 0 ? "text-green" : "text-red"} />
              <Metric label="成交额" value={money(metrics.turnover, 0)} />
              <Metric label="交易 / 成交" value={`${metrics.trade_count} / ${metrics.fill_count}`} />
              <Metric label="拒绝加仓" value={number(metrics.rejected_add_count, 0)} />
            </div>
          </section>

          <section>
            <div className="mb-2">
              <h2 className="text-sm font-semibold text-white">交易质量</h2>
              <p className="text-xs text-muted">以下指标均为资金费分摊前口径，已扣成交手续费，不应与最终权益混为同一统计口径。</p>
            </div>
            <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
              <Metric label="胜率（资金费前）" value={percent(metrics.win_rate_before_funding)} />
              <Metric label="Profit Factor（资金费前）" value={number(metrics.profit_factor_before_funding)} />
              <Metric label="盈亏比（资金费前）" value={number(metrics.payoff_ratio_before_funding)} />
              <Metric label="单笔期望（资金费前）" value={money(metrics.expectancy_before_funding)} />
              <Metric label="平均盈利（资金费前）" value={money(metrics.average_win_before_funding)} tone="text-green" />
              <Metric label="平均亏损（资金费前）" value={money(metrics.average_loss_before_funding)} tone="text-red" />
            </div>
          </section>

          <section className="rounded-md border border-border bg-card p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-white">权益与回撤</h2>
                <p className="text-xs text-muted">左轴为账户权益，右轴为回撤。</p>
              </div>
              <span className="text-xs text-muted">{result.execution?.curve_points_total ?? chartData.length} 个原始点</span>
            </div>
            {chartData.length ? (
              <div className="h-[320px] w-full">
                <ResponsiveContainer width="100%" height="100%">
                  <ComposedChart data={chartData} margin={{ top: 8, right: 12, left: 4, bottom: 0 }}>
                    <defs>
                      <linearGradient id="equityFill" x1="0" y1="0" x2="0" y2="1">
                        <stop offset="0%" stopColor="#0ecb81" stopOpacity={0.24} />
                        <stop offset="100%" stopColor="#0ecb81" stopOpacity={0} />
                      </linearGradient>
                    </defs>
                    <CartesianGrid stroke="#1e2436" strokeDasharray="3 3" vertical={false} />
                    <XAxis dataKey="label" minTickGap={70} tick={{ fill: "#8b94b2", fontSize: 10 }} tickLine={false} axisLine={false} />
                    <YAxis yAxisId="equity" width={76} tickFormatter={(v) => `$${number(v, 0)}`} tick={{ fill: "#8b94b2", fontSize: 10 }} tickLine={false} axisLine={false} />
                    <YAxis yAxisId="drawdown" orientation="right" width={55} tickFormatter={(v) => `${number(v * 100, 1)}%`} tick={{ fill: "#8b94b2", fontSize: 10 }} tickLine={false} axisLine={false} />
                    <Tooltip
                      contentStyle={{ background: "#141823", border: "1px solid #1e2436", borderRadius: 6, fontSize: 12 }}
                      labelStyle={{ color: "#8b94b2" }}
                      formatter={(value, name) => name === "权益" ? [money(value), name] : [percent(value), name]}
                    />
                    <Area yAxisId="equity" type="monotone" dataKey="equity" name="权益" stroke="#0ecb81" strokeWidth={1.5} fill="url(#equityFill)" dot={false} />
                    <Line yAxisId="drawdown" type="monotone" dataKey="drawdown" name="回撤" stroke="#f6465d" strokeWidth={1.25} dot={false} />
                  </ComposedChart>
                </ResponsiveContainer>
              </div>
            ) : <div className="py-16 text-center text-sm text-muted">没有可绘制的权益数据。</div>}
          </section>

          <section className="rounded-md border border-border bg-card p-4">
            <div className="mb-3 flex flex-wrap items-center justify-between gap-2">
              <div>
                <h2 className="text-sm font-semibold text-white">交易记录</h2>
                <p className="text-xs text-muted">单笔净盈亏为资金费分摊前口径。</p>
              </div>
              <span className="text-xs text-muted">{trades.length} 笔</span>
            </div>
            {trades.length ? (
              <div className="max-h-96 overflow-auto">
                <table className="w-full min-w-[860px] text-left text-xs">
                  <thead className="sticky top-0 bg-card text-muted">
                    <tr className="border-b border-border">
                      {["方向", "最大层数", "入场时间", "退出时间", "净盈亏（资金费前）", "退出原因"].map((label) => <th key={label} className="px-3 py-2 font-medium">{label}</th>)}
                    </tr>
                  </thead>
                  <tbody>
                    {trades.map((trade, index) => (
                      <tr key={`${trade.entry_time || "trade"}-${index}`} className="border-b border-border/60 hover:bg-surface/50">
                        <td className={`px-3 py-2 font-semibold ${Number(trade.direction) === 1 || String(trade.direction).toLowerCase() === "long" ? "text-green" : "text-red"}`}>{direction(trade.direction)}</td>
                        <td className="px-3 py-2 font-mono text-white">{trade.max_layers ?? "--"}</td>
                        <td className="whitespace-nowrap px-3 py-2 font-mono text-muted">{dateTime(trade.entry_time)}</td>
                        <td className="whitespace-nowrap px-3 py-2 font-mono text-muted">{dateTime(trade.exit_time)}</td>
                        <td className={`px-3 py-2 font-mono font-semibold ${Number(trade.net_pnl_before_funding) >= 0 ? "text-green" : "text-red"}`}>{money(trade.net_pnl_before_funding)}</td>
                        <td className="px-3 py-2 text-white">{trade.exit_reason ?? "--"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : <div className="py-10 text-center text-sm text-muted">当前区间没有产生交易。账户指标仍按完整回测过程计算。</div>}
          </section>

          <section className="rounded-md border border-border bg-card">
            <button type="button" onClick={() => setDetailsOpen((open) => !open)} aria-expanded={detailsOpen} aria-controls="execution-details" className="flex w-full items-center justify-between gap-3 px-4 py-3 text-left">
              <span>
                <span className="block text-sm font-semibold text-white">执行明细</span>
                <span className="text-xs text-muted">成交 {result.fills?.length ?? 0} 条 · 资金费 {result.funding?.length ?? 0} 条</span>
              </span>
              {detailsOpen ? <ChevronUp size={17} className="text-muted" /> : <ChevronDown size={17} className="text-muted" />}
            </button>
            {detailsOpen && (
              <div id="execution-details" className="border-t border-border">
                <div className="flex gap-1 border-b border-border px-3 pt-2">
                  {[["fills", "成交"], ["funding", "资金费"]].map(([key, label]) => (
                    <button key={key} type="button" onClick={() => setActiveDetail(key)} className={`border-b-2 px-3 py-2 text-xs ${activeDetail === key ? "border-accent text-white" : "border-transparent text-muted hover:text-white"}`}>{label}</button>
                  ))}
                </div>
                <DetailTable type={activeDetail} rows={result[activeDetail] ?? []} />
              </div>
            )}
          </section>

          <footer className="flex flex-col gap-2 border-t border-border pt-3 text-[11px] text-muted md:flex-row md:items-center md:justify-between">
            <span className="flex items-center gap-1.5"><Database size={13} />{result.data_lineage?.ohlcv_release_id} · {result.data_lineage?.funding_release_id}</span>
            <span>{result.execution?.engine} {result.execution?.engine_version} · {result.execution?.bar_count?.toLocaleString()} bars · {result.execution?.signal_timing}</span>
          </footer>
        </>
      )}
    </div>
  );
}
