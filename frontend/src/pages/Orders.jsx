import React, { useState, useEffect, useCallback } from "react";
import { useApp } from "../context/AppContext";
import {
  getOrderHistory, getRecentTrades,
  startEngine, stopEngine, getEngineStatus,
} from "../api/client";
import {
  RefreshCw, Play, Square, Loader,
  AlertTriangle, CheckCircle, AlertCircle,
  Zap,
} from "lucide-react";
import clsx from "clsx";

const TABS = ["挂单", "交易所成交记录", "历史订单"];
const ENGINE_STATE_VIEWS = {
  loading: { label: "加载中", tone: "text-muted" },
  stopped: { label: "未运行", tone: "text-muted" },
  running: { label: "运行中", tone: "text-green" },
  retrying: { label: "网络重试中", tone: "text-accent" },
  network_halted: { label: "网络故障", tone: "text-red" },
  halted: { label: "已安全停止", tone: "text-red" },
  recovery_required: { label: "需要恢复", tone: "text-red" },
};
const NO_ACTION_LABELS = {
  baseline: "等待下一根完整 K 线",
  bar_already_processed: "当前 K 线已经处理",
  no_strategy_action: "当前条件未形成有效交易信号",
};

// ── 订单表格辅助 ──────────────────────────────────────────────────────────────

function Badge({ type }) {
  const map = {
    LIMIT: "bg-blue-500/10 text-blue-400",
    MARKET: "bg-purple-500/10 text-purple-400",
    STOP_MARKET: "bg-orange-500/10 text-orange-400",
    TAKE_PROFIT_MARKET: "bg-green/10 text-green",
  };
  return (
    <span className={clsx("text-xs px-1.5 py-0.5 rounded", map[type] || "bg-surface text-muted")}>
      {type?.replace("_", " ")}
    </span>
  );
}

function StatusBadge({ status }) {
  const map = {
    FILLED: "text-green", NEW: "text-accent", CANCELED: "text-muted",
    PARTIALLY_FILLED: "text-blue-400", EXPIRED: "text-muted",
  };
  return <span className={clsx("text-xs font-medium", map[status] || "text-muted")}>{status}</span>;
}

// ── 引擎控制面板 ──────────────────────────────────────────────────────────────

function EnginePanel() {
  const { networkTab, symbol } = useApp();
  const [engine, setEngine] = useState({ running: false, circuit_open: false, engine_state: "loading" });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);
  const [capitalLimit, setCapitalLimit] = useState("1000");
  const [showMainnetConfirm, setShowMainnetConfirm] = useState(false);
  const [confirmation, setConfirmation] = useState("");

  const loadStatus = useCallback(() => {
    getEngineStatus().then(({ data }) => setEngine(data)).catch(() => {});
  }, []);

  useEffect(() => {
    loadStatus();
    const id = setInterval(loadStatus, 5000);
    return () => clearInterval(id);
  }, [loadStatus]);

  const toggleEngine = async (confirmed = false) => {
    if (!engine.running && networkTab === "main" && !confirmed) {
      setConfirmation("");
      setShowMainnetConfirm(true);
      return;
    }
    if (!engine.running && !(Number(capitalLimit) > 0)) {
      setMsg({ ok: false, text: "资金上限必须大于 0" });
      return;
    }
    setBusy(true); setMsg(null);
    try {
      if (engine.running) {
        await stopEngine();
        setMsg({ ok: true, text: "策略已停止" });
      } else {
        const request = {
          strategy_type: "sar_adx_pyramid",
          config_version: "sar_adx_v3",
          symbol,
          capital_limit: Number(capitalLimit),
        };
        if (networkTab === "main") request.mainnet_confirmation = confirmation;
        const { data } = await startEngine(request);
        setMsg({ ok: true, text: data.message || "已启动" });
        setShowMainnetConfirm(false);
      }
      const { data } = await getEngineStatus();
      setEngine(data);
    } catch (e) {
      setMsg({ ok: false, text: e.response?.data?.detail || "操作失败" });
    } finally {
      setBusy(false);
    }
  };

  const boundNetwork = engine.running && engine.network
    ? String(engine.network).toLowerCase()
    : networkTab;
  const isTestnet = !["main", "mainnet", "production"].includes(boundNetwork);
  const boundSymbol = engine.symbol || engine.strategy_symbol || symbol;
  const engineState = engine.engine_state || (engine.running ? "running" : "stopped");
  const engineView = ENGINE_STATE_VIEWS[engineState] || ENGINE_STATE_VIEWS.halted;
  const stats = [
    ["决策", engine.decision_count],
    ["已提交", engine.submitted_order_count],
    ["已成交", engine.filled_order_count],
    ["已拒绝", engine.rejected_order_count],
    ["待恢复", engine.unknown_order_count],
  ];
  const noActionReason = engine.no_action_reason
    ? NO_ACTION_LABELS[engine.no_action_reason]
      || String(engine.no_action_reason).replace(/SAR|ADX|V3/gi, "策略")
    : null;

  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden mb-4">

      {/* ── 顶栏 ─────────────────────────────────────────────── */}
      <div className="flex flex-wrap items-center justify-between gap-3 px-4 py-3 border-b border-border">
        <div className="flex min-w-0 flex-wrap items-center gap-3">
          <Zap size={15} className={engine.running ? "text-green" : "text-muted"} />
          <div className="flex min-w-0 flex-wrap items-center gap-2">
            <span className={clsx("flex items-center gap-1.5 text-xs font-medium", engineView.tone)}>
              {engine.running && <span className="w-1.5 h-1.5 rounded-full bg-current animate-pulse" />}
              {engineView.label}
            </span>
            <span className={clsx("text-xs px-1.5 py-0.5 rounded border font-mono",
              isTestnet
                ? "border-accent/40 text-accent bg-accent/5"
                : "border-red/40 text-red bg-red/5")}>
              {isTestnet ? "测试网" : "真实网"}
            </span>
            <span className="text-xs text-muted font-mono">{engine.running ? `绑定 ${boundSymbol}` : `待启动 ${symbol}`}</span>
          </div>
        </div>

        <div className="flex items-center gap-2">
          {msg && (
            <span className={clsx("text-xs flex items-center gap-1", msg.ok ? "text-green" : "text-red")}>
              {msg.ok ? <CheckCircle size={11} /> : <AlertCircle size={11} />} {msg.text}
            </span>
          )}
          {!engine.running && (
            <label className="flex items-center gap-2 text-xs text-muted">
              资金上限
              <input
                aria-label="资金上限"
                type="number"
                min="1"
                step="100"
                value={capitalLimit}
                onChange={(event) => setCapitalLimit(event.target.value)}
                className="w-24 rounded-md border border-border bg-surface px-2 py-1 text-right font-mono text-white outline-none focus:border-accent"
              />
              USDT
            </label>
          )}
          <button onClick={() => toggleEngine()} disabled={busy || !symbol}
            className={clsx("flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold transition-colors disabled:opacity-50",
              engine.running
                ? "bg-red/10 border border-red/30 text-red hover:bg-red/20"
                : "bg-accent text-black hover:bg-accent/90")}>
            {busy ? <Loader size={12} className="animate-spin" />
              : engine.running ? <Square size={12} /> : <Play size={12} />}
            {engine.running ? "停止" : "启动策略"}
          </button>
        </div>
      </div>

      <div className="flex flex-wrap items-center gap-x-5 gap-y-1 px-4 py-2 text-xs text-muted">
        <span>策略 <strong className="font-medium text-white">CandleMind 趋势策略</strong></span>
        <span>执行方式 <strong className="font-medium text-white">交易所订单</strong></span>
        {engine.last_exchange_order_id && (
          <span>最近订单 <strong className="font-mono font-medium text-white">{engine.last_exchange_order_id}</strong></span>
        )}
      </div>

      {/* ── 运行状态条 ───────────────────────────────────────── */}
      {engine.circuit_open && (
        <div className="px-4 py-2 bg-red/5 border-b border-red/20 flex items-center gap-2 text-xs text-red">
          <AlertTriangle size={12} />
          熔断器触发：日内回撤超限，新入场已暂停。明日 UTC 0 点自动重置。
        </div>
      )}
      <div className="flex flex-wrap gap-x-5 gap-y-1 border-t border-border/40 bg-surface/40 px-4 py-2 text-xs text-muted">
        {stats.map(([label, value]) => (
          <span key={label}>{label} <strong className="font-mono text-white">{Number.isFinite(Number(value)) ? value : 0}</strong></span>
        ))}
        {noActionReason && <span className="min-w-0 truncate">未执行原因：{noActionReason}</span>}
      </div>

      {showMainnetConfirm && (
        <div role="dialog" aria-modal="true" aria-labelledby="mainnet-confirm-title" className="fixed inset-0 z-50 flex items-center justify-center bg-black/70 p-4">
          <div className="w-full max-w-md rounded-md border border-red/40 bg-card p-4 shadow-2xl">
            <h2 id="mainnet-confirm-title" className="text-base font-semibold text-white">确认启动真实网交易</h2>
            <p className="mt-2 text-sm leading-5 text-muted">
              策略将在出现有效信号后使用真实资金下单。请输入 <strong className="font-mono text-red">MAINNET:{symbol}</strong> 继续。
            </p>
            <input
              autoFocus
              aria-label="真实网确认文本"
              value={confirmation}
              onChange={(event) => setConfirmation(event.target.value)}
              className="mt-3 w-full rounded-md border border-border bg-surface px-3 py-2 font-mono text-sm text-white outline-none focus:border-red"
            />
            <div className="mt-4 flex justify-end gap-2">
              <button type="button" onClick={() => setShowMainnetConfirm(false)} className="rounded-md border border-border px-3 py-1.5 text-xs text-muted hover:text-white">取消</button>
              <button
                type="button"
                onClick={() => toggleEngine(true)}
                disabled={confirmation !== `MAINNET:${symbol}` || busy}
                className="rounded-md bg-red px-3 py-1.5 text-xs font-bold text-white disabled:opacity-40"
              >
                确认真实网启动
              </button>
            </div>
          </div>
        </div>
      )}

    </div>
  );
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function Orders() {
  const { symbol, openOrders } = useApp();
  const [tab, setTab]       = useState(0);
  const [history, setHistory] = useState([]);
  const [trades, setTrades]   = useState([]);
  const [loading, setLoading] = useState(false);

  const fetchData = useCallback(async () => {
    if (!symbol) return;
    setLoading(true);
    try {
      if (tab === 1) {
        const { data } = await getRecentTrades(symbol);
        setTrades(data.reverse());
      } else if (tab === 2) {
        const { data } = await getOrderHistory(symbol, 100);
        setHistory(data);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, [tab, symbol]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const fmt = (v, d = 2) => parseFloat(v || 0).toFixed(d);
  const fmtTime = ts => new Date(ts).toLocaleString("zh");

  return (
    <div className="space-y-0">
      <EnginePanel />

      <div className="bg-card border border-border rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="flex gap-1">
            {TABS.map((t, i) => (
              <button key={t} onClick={() => setTab(i)}
                className={clsx("text-sm px-3 py-1.5 rounded-lg transition-colors",
                  tab === i ? "bg-accent text-black font-bold" : "text-muted hover:text-white")}>
                {t}
                {i === 0 && openOrders.length > 0 && (
                  <span className="ml-1 text-xs bg-accent/20 text-accent px-1.5 rounded-full">
                    {openOrders.length}
                  </span>
                )}
              </button>
            ))}
          </div>
          <button onClick={fetchData} disabled={loading}
            className="text-muted hover:text-white transition-colors">
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        <div className="overflow-x-auto">
          {/* 挂单 */}
          {tab === 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  {["时间", "品种", "方向", "类型", "数量", "价格", "触发价", "状态"].map(h => (
                    <th key={h} className="text-left px-4 py-2.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {openOrders.length === 0 ? (
                  <tr><td colSpan={8} className="text-center text-muted py-10">暂无挂单</td></tr>
                ) : openOrders.map(o => (
                  <tr key={o.orderId} className="border-b border-border/40 hover:bg-surface/30">
                    <td className="px-4 py-2.5 text-muted">{fmtTime(o.time)}</td>
                    <td className="px-4 py-2.5 font-medium">{o.symbol}</td>
                    <td className={clsx("px-4 py-2.5 font-bold", o.side === "BUY" ? "text-green" : "text-red")}>
                      {o.side === "BUY" ? "买/多" : "卖/空"}
                    </td>
                    <td className="px-4 py-2.5"><Badge type={o.type} /></td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.origQty, 3)}</td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.price) === "0.00" ? "市价" : fmt(o.price)}</td>
                    <td className="px-4 py-2.5 font-mono text-orange-400">{fmt(o.stopPrice) !== "0.00" ? fmt(o.stopPrice) : "—"}</td>
                    <td className="px-4 py-2.5"><StatusBadge status={o.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {/* 交易所成交记录 */}
          {tab === 1 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  {["时间", "品种", "方向", "价格", "数量", "手续费", "已实现盈亏"].map(h => (
                    <th key={h} className="text-left px-4 py-2.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.length === 0 ? (
                  <tr><td colSpan={7} className="text-center text-muted py-10">暂无交易所成交记录</td></tr>
                ) : trades.map((t, i) => {
                  const pnl = parseFloat(t.realizedPnl || 0);
                  return (
                    <tr key={i} className="border-b border-border/40 hover:bg-surface/30">
                      <td className="px-4 py-2.5 text-muted">{fmtTime(t.time)}</td>
                      <td className="px-4 py-2.5 font-medium">{t.symbol}</td>
                      <td className={clsx("px-4 py-2.5 font-bold", t.side === "BUY" ? "text-green" : "text-red")}>
                        {t.side === "BUY" ? "买/多" : "卖/空"}
                      </td>
                      <td className="px-4 py-2.5 font-mono">{fmt(t.price)}</td>
                      <td className="px-4 py-2.5 font-mono">{fmt(t.qty, 3)}</td>
                      <td className="px-4 py-2.5 font-mono text-muted">{fmt(t.commission, 4)} {t.commissionAsset}</td>
                      <td className={clsx("px-4 py-2.5 font-mono font-bold", pnl >= 0 ? "text-green" : "text-red")}>
                        {pnl !== 0 ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {/* 历史订单 */}
          {tab === 2 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  {["时间", "品种", "方向", "类型", "委托价", "成交价", "数量", "状态"].map(h => (
                    <th key={h} className="text-left px-4 py-2.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {history.length === 0 ? (
                  <tr><td colSpan={8} className="text-center text-muted py-10">暂无历史订单</td></tr>
                ) : history.map(o => (
                  <tr key={o.orderId} className="border-b border-border/40 hover:bg-surface/30">
                    <td className="px-4 py-2.5 text-muted">{fmtTime(o.time)}</td>
                    <td className="px-4 py-2.5 font-medium">{o.symbol}</td>
                    <td className={clsx("px-4 py-2.5 font-bold", o.side === "BUY" ? "text-green" : "text-red")}>
                      {o.side === "BUY" ? "买/多" : "卖/空"}
                    </td>
                    <td className="px-4 py-2.5"><Badge type={o.type} /></td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.price) === "0.00" ? "市价" : fmt(o.price)}</td>
                    <td className="px-4 py-2.5 font-mono text-accent">{fmt(o.avgPrice) !== "0.00" ? fmt(o.avgPrice) : "—"}</td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.origQty, 3)}</td>
                    <td className="px-4 py-2.5"><StatusBadge status={o.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
