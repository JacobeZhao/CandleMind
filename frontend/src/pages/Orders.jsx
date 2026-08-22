import React, { useCallback, useEffect, useRef, useState } from "react";
import { AlertCircle, Loader, RefreshCw } from "lucide-react";
import clsx from "clsx";
import { getCombinedOpenOrders, getOrderHistory, getRecentTrades } from "../api/client";
import { normalizeApiError } from "../api/errors";
import StrategyAnalyticsPanel from "../components/StrategyAnalyticsPanel";
import { useApp } from "../context/AppContext";
import { registerRefreshReader } from "../services/refreshCoordinator";
import ExchangeUnavailableState from "../components/ExchangeUnavailableState";

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
const OPEN_ORDER_WARNINGS = {
  algo_orders_unavailable: "Algo 挂单暂不可用",
  regular_orders_unavailable: "普通挂单暂不可用",
};

function warningMessage(data) {
  const warnings = [...(data?.warnings || []), ...(data?.reasons || [])];
  return warnings.map((warning) => OPEN_ORDER_WARNINGS[warning] || warning).join("；") || "部分挂单来源读取失败";
}

function normalizeOpenOrder(order, index) {
  const source = String(order.source || order.order_source || "regular").toLowerCase();
  return {
    ...order,
    key: `${source}:${order.id ?? order.orderId ?? order.algoId ?? index}`,
    source,
    time: order.time ?? order.createTime ?? order.updateTime,
    type: order.type ?? order.orderType ?? order.algoType,
    origQty: order.origQty ?? order.quantity ?? order.qty,
    stopPrice: order.triggerPrice ?? order.stopPrice,
  };
}

function responseMatchesScope(data, networkTab, symbol) {
  const scope = data?.scope;
  if (!scope) return true;
  const responseNetwork = String(scope.network || "").toLowerCase();
  const expectedNetwork = networkTab === "main" ? "mainnet" : "testnet";
  const normalizedNetwork = responseNetwork === "main" ? "mainnet" : responseNetwork === "test" ? "testnet" : responseNetwork;
  return normalizedNetwork === expectedNetwork && scope.symbol === symbol;
}

function OrdersWorkspace() {
  const { networkTab, symbol } = useApp();
  const [tab, setTab] = useState(0);
  const [openOrderState, setOpenOrderState] = useState({ phase: "loading", orders: [], error: null, asOf: null, scopeKey: null });
  const [collectionStates, setCollectionStates] = useState({
    1: { phase: "idle", rows: [], error: null, scopeKey: null },
    2: { phase: "idle", rows: [], error: null, scopeKey: null },
  });
  const requestId = useRef(0);
  const activeController = useRef(null);
  const openOrderRequestId = useRef(0);
  const openOrderController = useRef(null);
  const tabRefs = useRef([]);

  const fetchOpenOrders = useCallback(async () => {
    openOrderController.current?.abort();
    const controller = new AbortController();
    openOrderController.current = controller;
    const currentRequest = ++openOrderRequestId.current;
    const expectedScope = `${networkTab}:${symbol}`;
    if (!symbol) {
      setOpenOrderState({ phase: "empty", orders: [], error: null, asOf: null, scopeKey: expectedScope });
      return true;
    }
    setOpenOrderState((current) => ({
      ...(current.scopeKey === expectedScope ? current : { orders: [], asOf: null }),
      phase: current.scopeKey === expectedScope && current.orders.length ? "refreshing" : "loading",
      error: null,
      scopeKey: expectedScope,
    }));
    try {
      const { data } = await getCombinedOpenOrders(symbol, controller.signal);
      if (
        controller.signal.aborted
        || openOrderRequestId.current !== currentRequest
        || expectedScope !== `${networkTab}:${symbol}`
      ) return true;
      if (!responseMatchesScope(data, networkTab, symbol)) {
        setOpenOrderState((current) => ({
          ...current,
          phase: current.orders.length ? "stale" : "error",
          error: "挂单响应范围与当前网络或品种不一致，请重试。",
        }));
        return false;
      }
      const orders = (Array.isArray(data) ? data : data?.orders || []).map(normalizeOpenOrder);
      const partial = !Array.isArray(data) && (
        String(data?.status || "complete").toLowerCase() !== "complete"
        || (data?.warnings || []).length > 0
      );
      setOpenOrderState({
        phase: partial ? "partial" : orders.length ? "complete" : "empty",
        orders,
        error: partial ? warningMessage(data) : null,
        asOf: data?.as_of || new Date().toISOString(),
        scopeKey: expectedScope,
      });
      return true;
    } catch (error) {
      const parsed = normalizeApiError(error, "挂单数据加载失败，请稍后重试。");
      if (parsed.cancelled || openOrderRequestId.current !== currentRequest) return true;
      setOpenOrderState((current) => ({
        ...current,
        orders: parsed.retryable ? current.orders : [],
        phase: parsed.retryable && current.orders.length ? "stale" : "error",
        error: parsed.message,
      }));
      return false;
    }
  }, [networkTab, symbol]);

  const fetchData = useCallback(async (targetTab = tab) => {
    activeController.current?.abort();
    const currentRequest = ++requestId.current;
    if (targetTab === 0 || !symbol) {
      return true;
    }
    const controller = new AbortController();
    activeController.current = controller;
    const expectedScope = `${networkTab}:${symbol}:${targetTab}`;
    setCollectionStates((current) => {
      const previous = current[targetTab];
      const sameScope = previous.scopeKey === expectedScope;
      return {
        ...current,
        [targetTab]: {
          phase: sameScope && previous.rows.length ? "refreshing" : "loading",
          rows: sameScope ? previous.rows : [],
          error: null,
          scopeKey: expectedScope,
        },
      };
    });
    try {
      const { data } = targetTab === 1 ? await getRecentTrades(symbol, controller.signal) : await getOrderHistory(symbol, 100, controller.signal);
      if (controller.signal.aborted || requestId.current !== currentRequest) return true;
      if (!Array.isArray(data) && !responseMatchesScope(data, networkTab, symbol)) {
        setCollectionStates((current) => ({
          ...current,
          [targetTab]: {
            ...current[targetTab],
            phase: current[targetTab].rows.length ? "stale" : "error",
            error: "订单响应范围与当前网络或品种不一致，请重试。",
          },
        }));
        return false;
      }
      const payload = Array.isArray(data)
        ? data
        : targetTab === 1 ? data?.trades || [] : data?.orders || data?.history || [];
      const rows = targetTab === 1 ? [...payload].reverse() : payload;
      setCollectionStates((current) => ({
        ...current,
        [targetTab]: { phase: rows.length ? "complete" : "empty", rows, error: null, scopeKey: expectedScope },
      }));
      return true;
    } catch (error) {
      const parsed = normalizeApiError(error, "订单数据加载失败，请稍后重试。");
      if (parsed.cancelled || requestId.current !== currentRequest) return true;
      setCollectionStates((current) => ({
        ...current,
        [targetTab]: {
          ...current[targetTab],
          rows: parsed.retryable ? current[targetTab].rows : [],
          phase: parsed.retryable && current[targetTab].rows.length ? "stale" : "error",
          error: parsed.message,
        },
      }));
      return false;
    }
  }, [networkTab, symbol, tab]);

  useEffect(() => {
    fetchData();
    return () => activeController.current?.abort();
  }, [fetchData]);

  useEffect(() => {
    fetchOpenOrders();
    return () => openOrderController.current?.abort();
  }, [fetchOpenOrders]);

  useEffect(() => registerRefreshReader("orders:detail", async () => {
    const results = await Promise.all([fetchOpenOrders(), fetchData(tab)]);
    return results.every(Boolean);
  }), [fetchData, fetchOpenOrders, tab]);

  const requestState = collectionStates[tab] || { phase: "idle", rows: [], error: null };
  const trades = collectionStates[1].rows;
  const history = collectionStates[2].rows;

  const tableState = (columns, emptyLabel, rows) => {
    if (requestState.phase === "loading") return <EmptyRow columns={columns}><span role="status" className="inline-flex items-center gap-2"><Loader size={14} className="animate-spin" />正在加载</span></EmptyRow>;
    if (requestState.phase === "error") return <EmptyRow columns={columns}><span role="alert" className="inline-flex items-center gap-2 text-red"><AlertCircle size={14} />{requestState.error}</span></EmptyRow>;
    if (requestState.phase === "empty") return <EmptyRow columns={columns}>{emptyLabel}</EmptyRow>;
    return null;
  };

  const openOrderTableState = () => {
    if (openOrderState.phase === "loading") return <EmptyRow columns={9}><span role="status" className="inline-flex items-center gap-2"><Loader size={14} className="animate-spin" />正在加载挂单</span></EmptyRow>;
    if (openOrderState.phase === "error") return <EmptyRow columns={9}><span role="alert" className="inline-flex items-center gap-2 text-red"><AlertCircle size={14} />{openOrderState.error}</span></EmptyRow>;
    if (openOrderState.phase === "partial" && !openOrderState.orders.length) return <EmptyRow columns={9}><span role="alert" className="inline-flex items-center gap-2 text-accent"><AlertCircle size={14} />{openOrderState.error}</span></EmptyRow>;
    if (openOrderState.phase === "empty") return <EmptyRow columns={9}>暂无挂单</EmptyRow>;
    return null;
  };

  const refreshActiveTab = () => {
    if (tab === 0) fetchOpenOrders();
    else fetchData();
  };

  const handleTabKeyDown = (event, index) => {
    const keys = { ArrowLeft: -1, ArrowRight: 1 };
    let nextIndex;
    if (event.key === "Home") nextIndex = 0;
    else if (event.key === "End") nextIndex = TABS.length - 1;
    else if (Object.hasOwn(keys, event.key)) nextIndex = (index + keys[event.key] + TABS.length) % TABS.length;
    else return;
    event.preventDefault();
    setTab(nextIndex);
    tabRefs.current[nextIndex]?.focus();
  };

  return (
    <div>
      <StrategyAnalyticsPanel />
      <section className="overflow-hidden rounded-md border border-border bg-card" aria-label="订单明细">
        <div className="flex flex-wrap items-center justify-between gap-2 border-b border-border px-3 py-3 sm:px-4">
          <div role="tablist" aria-label="订单数据视图" className="flex min-w-0 gap-1 overflow-x-auto">
            {TABS.map((label, index) => <button type="button" role="tab" id={`orders-tab-${index}`} aria-controls={`orders-panel-${index}`} aria-selected={tab === index} tabIndex={tab === index ? 0 : -1} ref={(node) => { tabRefs.current[index] = node; }} key={label} onClick={() => setTab(index)} onKeyDown={(event) => handleTabKeyDown(event, index)} className={clsx("whitespace-nowrap rounded-md px-2 py-1.5 text-xs transition-colors sm:px-3 sm:text-sm", tab === index ? "bg-accent font-bold text-black" : "text-muted hover:text-white")}>{label}{index === 0 && openOrderState.orders.length > 0 && <span className="ml-1 rounded-full bg-accent/20 px-1.5 text-xs text-accent">{openOrderState.orders.length}</span>}</button>)}
          </div>
          <div className="flex items-center gap-2">
            {tab === 0 && ["refreshing", "partial", "stale"].includes(openOrderState.phase) && <span role={openOrderState.phase === "refreshing" ? "status" : "alert"} className={clsx("text-xs", openOrderState.phase === "refreshing" ? "text-muted" : "text-accent")}>{openOrderState.phase === "refreshing" ? "更新中" : openOrderState.phase === "stale" ? `数据可能已过期：${openOrderState.error}` : `部分数据：${openOrderState.error}`}</span>}
            {tab !== 0 && ["refreshing", "stale"].includes(requestState.phase) && <span role={requestState.phase === "refreshing" ? "status" : "alert"} className={clsx("text-xs", requestState.phase === "refreshing" ? "text-muted" : "text-accent")}>{requestState.phase === "refreshing" ? "更新中" : `显示上次成功数据：${requestState.error}`}</span>}
            <button type="button" aria-label="刷新订单数据" title="刷新订单数据" onClick={refreshActiveTab} disabled={tab === 0 ? openOrderState.phase === "loading" || openOrderState.phase === "refreshing" : requestState.phase === "loading" || requestState.phase === "refreshing"} className="flex h-8 w-8 shrink-0 items-center justify-center text-muted transition-colors hover:text-white disabled:opacity-40"><RefreshCw size={14} className={(tab === 0 ? openOrderState.phase === "loading" || openOrderState.phase === "refreshing" : requestState.phase === "loading" || requestState.phase === "refreshing") ? "animate-spin" : ""} /></button>
          </div>
        </div>
        <div role="tabpanel" id={`orders-panel-${tab}`} aria-labelledby={`orders-tab-${tab}`} className="overflow-x-auto">
          {tab === 0 && <table className="w-full min-w-[840px] text-xs"><thead><tr className="border-b border-border text-muted">{["时间", "品种", "来源", "方向", "类型", "数量", "价格", "触发价", "状态"].map((heading) => <th key={heading} className="px-4 py-2.5 text-left">{heading}</th>)}</tr></thead><tbody>
            {openOrderTableState()}
            {openOrderState.orders.map((order) => <tr key={order.key} className={clsx("border-b border-border/40 hover:bg-surface/30", openOrderState.phase === "stale" && "opacity-60")}><td className="px-4 py-2.5 text-muted">{formatTime(order.time)}</td><td className="px-4 py-2.5 font-medium">{order.symbol}</td><td className="px-4 py-2.5 text-muted">{order.source === "algo" ? "Algo" : "普通"}</td><td className={clsx("px-4 py-2.5 font-bold", order.side === "BUY" ? "text-green" : "text-red")}>{order.side === "BUY" ? "买/多" : "卖/空"}</td><td className="px-4 py-2.5"><Badge type={order.type} /></td><td className="px-4 py-2.5 font-mono">{formatNumber(order.origQty, 3)}</td><td className="px-4 py-2.5 font-mono">{formatNumber(order.price) === "0.00" ? "市价" : formatNumber(order.price)}</td><td className="px-4 py-2.5 font-mono text-orange-400">{formatNumber(order.stopPrice) !== "0.00" ? formatNumber(order.stopPrice) : "—"}</td><td className="px-4 py-2.5"><StatusBadge status={order.status} /></td></tr>)}
          </tbody></table>}
          {tab === 1 && <table className="w-full min-w-[700px] text-xs"><thead><tr className="border-b border-border text-muted">{["时间", "品种", "方向", "价格", "数量", "手续费", "已实现盈亏"].map((heading) => <th key={heading} className="px-4 py-2.5 text-left">{heading}</th>)}</tr></thead><tbody>
            {tableState(7, "暂无交易所成交记录", trades)}
            {trades.map((trade, index) => { const pnl = Number.parseFloat(trade.realizedPnl || 0); return <tr key={trade.id ?? `${trade.time}-${index}`} className={clsx("border-b border-border/40 hover:bg-surface/30", requestState.phase === "stale" && "opacity-60")}><td className="px-4 py-2.5 text-muted">{formatTime(trade.time)}</td><td className="px-4 py-2.5 font-medium">{trade.symbol}</td><td className={clsx("px-4 py-2.5 font-bold", trade.side === "BUY" ? "text-green" : "text-red")}>{trade.side === "BUY" ? "买/多" : "卖/空"}</td><td className="px-4 py-2.5 font-mono">{formatNumber(trade.price)}</td><td className="px-4 py-2.5 font-mono">{formatNumber(trade.qty, 3)}</td><td className="px-4 py-2.5 font-mono text-muted">{formatNumber(trade.commission, 4)} {trade.commissionAsset}</td><td className={clsx("px-4 py-2.5 font-mono font-bold", pnl >= 0 ? "text-green" : "text-red")}>{pnl !== 0 ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "—"}</td></tr>; })}
          </tbody></table>}
          {tab === 2 && <table className="w-full min-w-[760px] text-xs"><thead><tr className="border-b border-border text-muted">{["时间", "品种", "方向", "类型", "委托价", "成交价", "数量", "状态"].map((heading) => <th key={heading} className="px-4 py-2.5 text-left">{heading}</th>)}</tr></thead><tbody>
            {tableState(8, "暂无历史订单", history)}
            {history.map((order) => <tr key={order.orderId} className={clsx("border-b border-border/40 hover:bg-surface/30", requestState.phase === "stale" && "opacity-60")}><td className="px-4 py-2.5 text-muted">{formatTime(order.time)}</td><td className="px-4 py-2.5 font-medium">{order.symbol}</td><td className={clsx("px-4 py-2.5 font-bold", order.side === "BUY" ? "text-green" : "text-red")}>{order.side === "BUY" ? "买/多" : "卖/空"}</td><td className="px-4 py-2.5"><Badge type={order.type} /></td><td className="px-4 py-2.5 font-mono">{formatNumber(order.price) === "0.00" ? "市价" : formatNumber(order.price)}</td><td className="px-4 py-2.5 font-mono text-accent">{formatNumber(order.avgPrice) !== "0.00" ? formatNumber(order.avgPrice) : "—"}</td><td className="px-4 py-2.5 font-mono">{formatNumber(order.origQty, 3)}</td><td className="px-4 py-2.5"><StatusBadge status={order.status} /></td></tr>)}
          </tbody></table>}
        </div>
      </section>
    </div>
  );
}

export default function Orders() {
  const { exchangeProvider, exchangeSupported, exchangeSwitching, settingsLoaded } = useApp();
  const isExchangeSupported = settingsLoaded !== false
    && exchangeSupported;

  if (!isExchangeSupported || exchangeSwitching) {
    return <ExchangeUnavailableState exchangeProvider={exchangeProvider} />;
  }

  return <OrdersWorkspace />;
}
