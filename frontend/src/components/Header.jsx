import React, { useEffect, useRef, useState } from "react";
import { useApp } from "../context/AppContext";
import { getSymbols } from "../api/client";
import { Activity, ChevronDown, Search, Wifi, WifiOff } from "lucide-react";
import clsx from "clsx";

function SymbolDropdown({ symbol, symbols, onSelect }) {
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
        className="flex max-w-full items-center gap-1 rounded-lg border border-border px-2 py-1 text-xs text-muted transition-colors hover:border-accent/50 hover:text-white"
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
  const { networkTab, switchNetwork } = useApp();

  return (
    <div className="flex shrink-0 items-center gap-0.5 rounded-lg border border-border bg-surface p-0.5">
      {[["test", "测试网"], ["main", "真实网"]].map(([tab, label]) => (
        <button
          type="button"
          key={tab}
          onClick={() => switchNetwork(tab)}
          className={clsx(
            "rounded-md px-2 py-1 text-xs font-medium transition-colors sm:px-3",
            networkTab === tab
              ? tab === "main"
                ? "bg-red/80 text-white"
                : "bg-accent/90 text-black"
              : "text-muted hover:text-white",
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export default function Header() {
  const { botStatus, botStatusLoaded, connected, symbol, setSymbol } = useApp();
  const [symbols, setSymbols] = useState([]);
  const direction = botStatus?.position_direction ?? botStatus?.last_signal;
  const fillCount = botStatus?.paper_fill_count ?? botStatus?.trade_count;
  const fillCountComplete = botStatus?.paper_fill_count_complete !== false;
  const engineState = botStatus?.engine_state || (botStatus?.running ? "running" : "stopped");
  const engineLabel = engineState === "retrying" ? "策略重试中" : "策略运行中";

  useEffect(() => {
    getSymbols().then(({ data }) => setSymbols(data)).catch(() => {});
  }, []);

  return (
    <header className="flex h-16 shrink-0 items-center justify-between gap-2 border-b border-border bg-card px-3 sm:px-4">
      <SymbolDropdown symbol={symbol} symbols={symbols} onSelect={setSymbol} />

      <div className="flex min-w-0 shrink-0 items-center gap-2 sm:gap-3">
        <div className="hidden sm:block">
          <NetworkTabs />
        </div>

        {botStatusLoaded && direction && direction !== "NONE" && (
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

        {botStatus?.running && (
          <span className="hidden items-center gap-1.5 text-xs text-accent lg:flex">
            <span className="h-1.5 w-1.5 rounded-full bg-accent animate-pulse" />
            {engineLabel}
            {fillCountComplete && Number(fillCount) > 0 && (
              <span className="ml-1 text-muted">纸面成交 {fillCount} 笔</span>
            )}
          </span>
        )}

        <div
          className={clsx(
            "flex shrink-0 items-center gap-1.5 rounded px-2 py-1 text-xs",
            connected ? "bg-green/10 text-green" : "bg-surface text-muted",
          )}
          title={connected ? "WebSocket 已连接" : "WebSocket 未连接"}
        >
          {connected ? <Wifi size={13} /> : <WifiOff size={13} />}
          <span className="hidden lg:block">{connected ? "已连接" : "未连接"}</span>
        </div>
      </div>
    </header>
  );
}
