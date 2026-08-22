import React, { useCallback, useEffect, useRef, useState } from "react";
import { useApp } from "../context/AppContext";
import { getSymbols } from "../api/client";
import { Activity, AlertCircle, ChevronDown, Loader, RefreshCw, Search, Wifi, WifiOff } from "lucide-react";
import clsx from "clsx";
import StrategyEngineControl from "./StrategyEngineControl";
import { normalizeApiError } from "../api/errors";
import { registerRefreshReader } from "../services/refreshCoordinator";

function SymbolDropdown({ symbol, symbols, onSelect, disabled = false }) {
  const [open, setOpen] = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef(null);

  useEffect(() => {
    const onClick = (event) => {
      if (ref.current && !ref.current.contains(event.target)) setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const normalizedQuery = query.toUpperCase();
  const filtered = (normalizedQuery
    ? symbols.filter((item) => item.includes(normalizedQuery))
    : symbols
  ).slice(0, 60);

  return (
    <div className="relative min-w-0" ref={ref}>
      <button
        type="button"
        onClick={() => setOpen((value) => !value)}
        disabled={disabled}
        className="flex max-w-full items-center gap-1 rounded-lg border border-border px-2 py-1 text-base font-semibold text-muted transition-colors hover:border-accent/50 hover:text-white disabled:cursor-wait disabled:opacity-50"
      >
        <span className="truncate">{symbol}</span>
        <ChevronDown
          size={12}
          className={clsx("shrink-0 transition-transform", open && "rotate-180")}
        />
      </button>
      {open && (
        <div className="absolute left-0 top-full z-50 mt-1 w-52 max-w-[calc(100vw-2rem)] overflow-hidden rounded-xl border border-border bg-card shadow-xl">
          <div className="border-b border-border p-2">
            <div className="flex items-center gap-2 rounded-lg bg-surface px-2 py-1.5">
              <Search size={12} className="shrink-0 text-muted" />
              <input
                value={query}
                onChange={(event) => setQuery(event.target.value)}
                autoFocus
                placeholder="筛选品种..."
                className="min-w-0 flex-1 bg-transparent text-xs text-white outline-none"
              />
            </div>
          </div>
          <div className="max-h-72 overflow-y-auto">
            {filtered.map((item) => (
              <button
                type="button"
                key={item}
                onClick={() => {
                  onSelect(item);
                  setOpen(false);
                  setQuery("");
                }}
                disabled={disabled}
                className={clsx(
                  "w-full px-3 py-1.5 text-left text-xs transition-colors hover:bg-surface",
                  symbol === item ? "bg-accent/5 font-bold text-accent" : "text-muted",
                )}
              >
                {item.replace("USDT", "")}
                <span className="text-muted">/USDT</span>
              </button>
            ))}
            {filtered.length === 0 && (
              <p className="py-3 text-center text-xs text-muted">无匹配品种</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function NetworkTabs() {
  const {
    networkTab,
    networkSwitching,
    networkError,
    strategyCommandPending,
    symbolSwitching,
    strategyStatusUncertain,
    refreshPending,
    switchNetwork,
  } = useApp();

  return (
    <div className="relative shrink-0">
      <div className="flex items-center gap-0.5 rounded-lg border border-border bg-surface p-0.5">
        {[["test", "测试网"], ["main", "真实网"]].map(([tab, label]) => (
          <button
            type="button"
            key={tab}
            onClick={() => switchNetwork(tab)}
            disabled={networkSwitching || strategyCommandPending || refreshPending || symbolSwitching || strategyStatusUncertain}
            aria-pressed={networkTab === tab}
            className={clsx(
              "flex items-center gap-1 rounded-md px-2 py-1 text-xs font-medium transition-colors disabled:cursor-wait disabled:opacity-60 sm:px-3",
              networkTab === tab
                ? tab === "main"
                  ? "bg-red/80 text-white"
                  : "bg-accent/90 text-black"
                : "text-muted hover:text-white",
            )}
          >
            {networkSwitching && networkTab !== tab && <Loader size={11} className="animate-spin" />}
            {label}
          </button>
        ))}
      </div>
      {networkError && (
        <div role="alert" className="absolute right-0 top-full z-50 mt-2 flex w-72 max-w-[calc(100vw-1rem)] items-start gap-2 rounded-md border border-red/40 bg-card px-3 py-2 text-xs text-red shadow-xl">
          <AlertCircle size={14} className="mt-0.5 shrink-0" />
          <span>{networkError}</span>
        </div>
      )}
    </div>
  );
}

export default function Header() {
  const {
    botStatus,
    botStatusLoaded,
    connected,
    symbol,
    setSymbol,
    refreshAll,
    refreshPending,
    refreshError,
    networkSwitching,
    strategyCommandPending,
    symbolSwitching,
    strategyStatusUncertain,
    exchangeProvider,
    exchangeSupported,
    exchangeSwitching,
    settingsLoaded,
  } = useApp();
  const isExchangeSupported = exchangeSupported;
  const exchangeReady = settingsLoaded !== false && isExchangeSupported && !exchangeSwitching;
  const [symbols, setSymbols] = useState([]);
  const symbolsRequest = useRef({ id: 0, controller: null });
  const direction = botStatus?.position_direction ?? botStatus?.last_signal;
  const fillCount = botStatus?.filled_order_count;
  const engineState = botStatus?.engine_state || (botStatus?.running ? "running" : "stopped");
  const engineLabel = engineState === "retrying" ? "策略重试中" : "策略运行中";

  const loadSymbols = useCallback(async () => {
    symbolsRequest.current.controller?.abort();
    if (!exchangeReady) {
      setSymbols([]);
      return true;
    }
    const controller = new AbortController();
    const id = symbolsRequest.current.id + 1;
    symbolsRequest.current = { id, controller };
    try {
      const { data } = await getSymbols(controller.signal);
      if (controller.signal.aborted || symbolsRequest.current.id !== id) return true;
      setSymbols(Array.isArray(data) ? data : []);
      return true;
    } catch (error) {
      const parsed = normalizeApiError(error);
      return parsed.cancelled || symbolsRequest.current.id !== id;
    }
  }, [exchangeReady]);

  useEffect(() => {
    loadSymbols();
    return () => symbolsRequest.current.controller?.abort();
  }, [loadSymbols]);

  useEffect(() => registerRefreshReader("header:symbols", loadSymbols), [loadSymbols]);

  return (
    <header className="flex min-h-16 shrink-0 flex-wrap items-center justify-between gap-x-2 gap-y-2 border-b border-border bg-card px-2 py-2 sm:px-4">
      <div className="relative flex min-w-0 items-center gap-1.5">
        <SymbolDropdown
          symbol={symbol}
          symbols={symbols}
          onSelect={setSymbol}
          disabled={!exchangeReady || refreshPending || networkSwitching || strategyCommandPending || symbolSwitching || strategyStatusUncertain}
        />
        <button
          type="button"
          aria-label="刷新当前数据"
          title="刷新当前数据"
          onClick={refreshAll}
          disabled={!exchangeReady || refreshPending || networkSwitching || strategyCommandPending || symbolSwitching}
          className="flex h-8 shrink-0 items-center justify-center gap-1.5 rounded-md border border-border px-2 text-muted transition-colors hover:border-accent/50 hover:text-white disabled:cursor-wait disabled:opacity-50"
        >
          <RefreshCw size={15} className={clsx(refreshPending && "animate-spin")} />
          <span className="text-xs font-medium">刷新</span>
        </button>
        {refreshError && (
          <div role="alert" className="absolute left-0 top-full z-50 mt-2 flex w-72 max-w-[calc(100vw-1rem)] items-start gap-2 rounded-md border border-red/40 bg-card px-3 py-2 text-xs text-red shadow-xl">
            <AlertCircle size={14} className="mt-0.5 shrink-0" />
            <span>{refreshError}</span>
          </div>
        )}
      </div>

      <div className="flex min-w-0 shrink-0 items-center gap-2 sm:gap-3">
        <StrategyEngineControl />
        {exchangeReady && <NetworkTabs />}

        {exchangeReady && botStatusLoaded && direction && direction !== "NONE" && (
          <span
            className={clsx(
              "hidden items-center gap-1 rounded border px-2 py-1 text-xs md:flex",
              direction === "LONG"
                ? "border-green/30 bg-green/5 text-green"
                : "border-red/30 bg-red/5 text-red",
            )}
          >
            <Activity size={12} />
            {direction === "LONG" ? "多头持仓" : "空头持仓"}
          </span>
        )}

        {exchangeReady && botStatus?.running && (
          <span className="hidden items-center gap-1.5 text-xs text-accent lg:flex">
            <span className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse" />
            {engineLabel}
            {Number(fillCount) > 0 && (
              <span className="ml-1 text-muted">成交 {fillCount} 笔</span>
            )}
          </span>
        )}

        <div
          className={clsx(
            "flex shrink-0 items-center gap-1.5 rounded px-2 py-1 text-xs",
            exchangeReady && connected ? "bg-green/10 text-green" : "bg-surface text-muted",
          )}
          title={exchangeReady && connected ? "交易所已连接" : `${exchangeProvider || "当前交易所"}未连接`}
        >
          {exchangeReady && connected ? <Wifi size={13} /> : <WifiOff size={13} />}
          <span className="hidden lg:block">{exchangeReady && connected ? "已连接" : "未连接"}</span>
        </div>
      </div>
    </header>
  );
}
