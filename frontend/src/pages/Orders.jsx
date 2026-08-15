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

const TABS = ["挂单", "成交记录", "历史订单"];

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
  const [engine, setEngine] = useState({ running: false, circuit_open: false });
  const [busy, setBusy] = useState(false);
  const [msg, setMsg] = useState(null);

  const loadStatus = useCallback(() => {
    getEngineStatus().then(({ data }) => setEngine(data)).catch(() => {});
  }, []);

  useEffect(() => {
    loadStatus();
    const id = setInterval(loadStatus, 5000);
    return () => clearInterval(id);
  }, [loadStatus]);

  const toggleEngine = async () => {
    setBusy(true); setMsg(null);
    try {
      if (engine.running) {
        await stopEngine();
        setMsg({ ok: true, text: "策略已停止" });
      } else {
        const { data } = await startEngine({
          strategy_type: "sar_adx_pyramid",
          config_version: "sar_adx_v3",
          symbol,
          paper: true,
          initial_capital: 10000,
        });
        setMsg({ ok: true, text: data.message || "已启动" });
      }
      const { data } = await getEngineStatus();
      setEngine(data);
    } catch (e) {
      setMsg({ ok: false, text: e.response?.data?.detail || "操作失败" });
    } finally {
      setBusy(false);
    }
  };

  const isTestnet = networkTab === "test";
  const boundSymbol = engine.symbol || engine.strategy_symbol || symbol;

  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden mb-4">

      {/* ── 顶栏 ─────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-3">
          <Zap size={15} className={engine.running ? "text-green" : "text-muted"} />
          <div className="flex items-center gap-2">
            {engine.running ? (
              <span className="flex items-center gap-1.5 text-green text-xs font-medium">
                <span className="w-1.5 h-1.5 rounded-full bg-green animate-pulse" /> 运行中
              </span>
            ) : (
              <span className="text-muted text-xs">未运行</span>
            )}
            <span className={clsx("text-xs px-1.5 py-0.5 rounded border font-mono",
              isTestnet
                ? "border-accent/40 text-accent bg-accent/5"
                : "border-red/40 text-red bg-red/5")}>
              {isTestnet ? "测试网" : "真实网"}
            </span>
            <span className="text-xs text-muted font-mono">{engine.running ? `绑定 ${boundSymbol}` : `待启动 ${symbol}`}</span>
            {engine.running && engine.paper && (
              <span className="text-xs text-accent">纸面 · ${engine.paper_equity}</span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {msg && (
            <span className={clsx("text-xs flex items-center gap-1", msg.ok ? "text-green" : "text-red")}>
              {msg.ok ? <CheckCircle size={11} /> : <AlertCircle size={11} />} {msg.text}
            </span>
          )}
          <span className="border border-accent/40 bg-accent/10 px-2 py-1 text-xs text-accent">仅纸面</span>
          <button onClick={toggleEngine} disabled={busy || !symbol}
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
        <span>策略 <strong className="font-mono font-medium text-white">SAR + ADX 分批加仓 V3</strong></span>
        <span>执行周期 <strong className="font-mono font-medium text-white">5m</strong></span>
        <span>趋势过滤 <strong className="font-mono font-medium text-white">1h ADX</strong></span>
        <span>目标仓位 <strong className="font-mono font-medium text-white">5 x 20%</strong></span>
      </div>

      {/* ── 运行状态条 ───────────────────────────────────────── */}
      {engine.circuit_open && (
        <div className="px-4 py-2 bg-red/5 border-b border-red/20 flex items-center gap-2 text-xs text-red">
          <AlertTriangle size={12} />
          熔断器触发：日内回撤超限，新入场已暂停。明日 UTC 0 点自动重置。
        </div>
      )}
      {engine.running && engine.last_action && (
        <div className="px-4 py-2 bg-surface/40 border-b border-border/40 text-xs text-muted truncate">
          {engine.last_action}
          {engine.trade_count > 0 && (
            <span className="ml-3 text-accent">共成交 {engine.trade_count} 次</span>
          )}
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

          {/* 成交记录 */}
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
                  <tr><td colSpan={7} className="text-center text-muted py-10">暂无成交记录</td></tr>
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
