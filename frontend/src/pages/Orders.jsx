import React, { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Loader, RefreshCw } from "lucide-react";
import clsx from "clsx";
import { getOrderHistory, getRecentTrades } from "../api/client";
import StrategyAnalyticsPanel from "../components/StrategyAnalyticsPanel";
import { useApp } from "../context/AppContext";

const TABS = ["挂单", "交易所成交记录", "历史订单"];

function Badge({ type }) {
  const map = { LIMIT: "bg-blue-500/10 text-blue-400", MARKET: "bg-purple-500/10 text-purple-400", STOP_MARKET: "bg-orange-500/10 text-orange-400", TAKE_PROFIT_MARKET: "bg-green/10 text-green" };
  return <span className={clsx("rounded px-1.5 py-0.5 text-xs", map[type] || "bg-surface text-muted")}>{type?.replaceAll("_", " ")}</span>;
}

function StatusBadge({ status }) {
  const map = { FILLED: "text-green", NEW: "text-accent", CANCELED: "text-muted", PARTIALLY_FILLED: "text-blue-400", EXPIRED: "text-muted" };
  return <span className={clsx("text-xs font-medium", map[status] || "text-muted")}>{status}</span>;
}

const formatNumber = (value, digits = 2) => Number.parseFloat(value || 0).toFixed(digits);
const formatTime = (timestamp) => new Date(timestamp).toLocaleString("zh-CN");
const EmptyRow = ({ columns, children }) => <tr><td colSpan={columns} className="py-10 text-center text-muted">{children}</td></tr>;

export default function Orders() {
  const { networkTab, openOrders, refreshRevision, symbol } = useApp();
  const [tab, setTab] = useState(0);
  const [history, setHistory] = useState([]);
  const [trades, setTrades] = useState([]);
  const [requestState, setRequestState] = useState({ loading: false, error: null });
  const requestId = useRef(0);
  const activeController = useRef(null);

  const fetchData = useCallback(async () => {
    activeController.current?.abort();
    const currentRequest = ++requestId.current;
    if (tab === 0 || !symbol) {
      setRequestState({ loading: false, error: null });
      return;
    }
    const controller = new AbortController();
    activeController.current = controller;
    setRequestState({ loading: true, error: null });
    if (tab === 1) setTrades([]);
    if (tab === 2) setHistory([]);
    try {
      const { data } = tab === 1 ? await getRecentTrades(symbol, controller.signal) : await getOrderHistory(symbol, 100, controller.signal);
      if (controller.signal.aborted || requestId.current !== currentRequest) return;
      if (tab === 1) setTrades(Array.isArray(data) ? [...data].reverse() : []);
      if (tab === 2) setHistory(Array.isArray(data) ? data : []);
      setRequestState({ loading: false, error: null });
    } catch (error) {
      if (controller.signal.aborted || requestId.current !== currentRequest) return;
      const detail = error?.response?.data?.detail;
      setRequestState({ loading: false, error: typeof detail === "string" ? detail : "订单数据加载失败" });
    }
  }, [networkTab, refreshRevision, symbol, tab]);

  useEffect(() => {
    fetchData();
    return () => activeController.current?.abort();
  }, [fetchData]);

  const tableState = (columns, emptyLabel, rows) => {
    if (requestState.loading) return <EmptyRow columns={columns}><span role="status" className="inline-flex items-center gap-2"><Loader size={14} className="animate-spin" />正在加载</span></EmptyRow>;
    if (requestState.error) return <EmptyRow columns={columns}><span role="alert" className="inline-flex items-center gap-2 text-red"><AlertCircle size={14} />{requestState.error}</span></EmptyRow>;
    if (!rows.length) return <EmptyRow columns={columns}>{emptyLabel}</EmptyRow>;
    return null;
  };

  return (
    <div>
      <StrategyAnalyticsPanel />
      <section className="overflow-hidden rounded-md border border-border bg-card" aria-label="订单明细">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-3 sm:px-4">
          <div className="flex min-w-0 gap-1 overflow-x-auto">
            {TABS.map((label, index) => <button type="button" key={label} onClick={() => setTab(index)} className={clsx("whitespace-nowrap rounded-md px-2 py-1.5 text-xs transition-colors sm:px-3 sm:text-sm", tab === index ? "bg-accent font-bold text-black" : "text-muted hover:text-white")}>{label}{index === 0 && openOrders.length > 0 && <span className="ml-1 rounded-full bg-accent/20 px-1.5 text-xs text-accent">{openOrders.length}</span>}</button>)}
          </div>
          <button type="button" aria-label="刷新订单数据" title="刷新订单数据" onClick={fetchData} disabled={requestState.loading || tab === 0} className="flex h-8 w-8 shrink-0 items-center justify-center text-muted transition-colors hover:text-white disabled:opacity-40"><RefreshCw size={14} className={requestState.loading ? "animate-spin" : ""} /></button>
        </div>
        <div className="overflow-x-auto">
          {tab === 0 && <table className="w-full min-w-[760px] text-xs"><thead><tr className="border-b border-border text-muted">{["时间", "品种", "方向", "类型", "数量", "价格", "触发价", "状态"].map((heading) => <th key={heading} className="px-4 py-2.5 text-left">{heading}</th>)}</tr></thead><tbody>
            {tableState(8, "暂无挂单", openOrders)}
            {openOrders.map((order) => <tr key={order.orderId} className="border-b border-border/40 hover:bg-surface/30"><td className="px-4 py-2.5 text-muted">{formatTime(order.time)}</td><td className="px-4 py-2.5 font-medium">{order.symbol}</td><td className={clsx("px-4 py-2.5 font-bold", order.side === "BUY" ? "text-green" : "text-red")}>{order.side === "BUY" ? "买/多" : "卖/空"}</td><td className="px-4 py-2.5"><Badge type={order.type} /></td><td className="px-4 py-2.5 font-mono">{formatNumber(order.origQty, 3)}</td><td className="px-4 py-2.5 font-mono">{formatNumber(order.price) === "0.00" ? "市价" : formatNumber(order.price)}</td><td className="px-4 py-2.5 font-mono text-orange-400">{formatNumber(order.stopPrice) !== "0.00" ? formatNumber(order.stopPrice) : "—"}</td><td className="px-4 py-2.5"><StatusBadge status={order.status} /></td></tr>)}
          </tbody></table>}
          {tab === 1 && <table className="w-full min-w-[700px] text-xs"><thead><tr className="border-b border-border text-muted">{["时间", "品种", "方向", "价格", "数量", "手续费", "已实现盈亏"].map((heading) => <th key={heading} className="px-4 py-2.5 text-left">{heading}</th>)}</tr></thead><tbody>
            {tableState(7, "暂无交易所成交记录", trades)}
            {!requestState.loading && !requestState.error && trades.map((trade, index) => { const pnl = Number.parseFloat(trade.realizedPnl || 0); return <tr key={trade.id ?? `${trade.time}-${index}`} className="border-b border-border/40 hover:bg-surface/30"><td className="px-4 py-2.5 text-muted">{formatTime(trade.time)}</td><td className="px-4 py-2.5 font-medium">{trade.symbol}</td><td className={clsx("px-4 py-2.5 font-bold", trade.side === "BUY" ? "text-green" : "text-red")}>{trade.side === "BUY" ? "买/多" : "卖/空"}</td><td className="px-4 py-2.5 font-mono">{formatNumber(trade.price)}</td><td className="px-4 py-2.5 font-mono">{formatNumber(trade.qty, 3)}</td><td className="px-4 py-2.5 font-mono text-muted">{formatNumber(trade.commission, 4)} {trade.commissionAsset}</td><td className={clsx("px-4 py-2.5 font-mono font-bold", pnl >= 0 ? "text-green" : "text-red")}>{pnl !== 0 ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "—"}</td></tr>; })}
          </tbody></table>}
          {tab === 2 && <table className="w-full min-w-[760px] text-xs"><thead><tr className="border-b border-border text-muted">{["时间", "品种", "方向", "类型", "委托价", "成交价", "数量", "状态"].map((heading) => <th key={heading} className="px-4 py-2.5 text-left">{heading}</th>)}</tr></thead><tbody>
            {tableState(8, "暂无历史订单", history)}
            {!requestState.loading && !requestState.error && history.map((order) => <tr key={order.orderId} className="border-b border-border/40 hover:bg-surface/30"><td className="px-4 py-2.5 text-muted">{formatTime(order.time)}</td><td className="px-4 py-2.5 font-medium">{order.symbol}</td><td className={clsx("px-4 py-2.5 font-bold", order.side === "BUY" ? "text-green" : "text-red")}>{order.side === "BUY" ? "买/多" : "卖/空"}</td><td className="px-4 py-2.5"><Badge type={order.type} /></td><td className="px-4 py-2.5 font-mono">{formatNumber(order.price) === "0.00" ? "市价" : formatNumber(order.price)}</td><td className="px-4 py-2.5 font-mono text-accent">{formatNumber(order.avgPrice) !== "0.00" ? formatNumber(order.avgPrice) : "—"}</td><td className="px-4 py-2.5 font-mono">{formatNumber(order.origQty, 3)}</td><td className="px-4 py-2.5"><StatusBadge status={order.status} /></td></tr>)}
          </tbody></table>}
        </div>
      </section>
    </div>
  );
}
