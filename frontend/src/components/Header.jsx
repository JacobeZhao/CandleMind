import React, { useState, useEffect, useRef } from "react";
import { useApp } from "../context/AppContext";
import { getTicker, getSymbols } from "../api/client";
import { Wifi, WifiOff, TrendingUp, TrendingDown, Activity, ChevronDown, Search } from "lucide-react";
import clsx from "clsx";

function SymbolDropdown({ symbol, symbols, onSelect }) {
  const [open, setOpen]   = useState(false);
  const [query, setQuery] = useState("");
  const ref = useRef(null);

  useEffect(() => {
    const onClick = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  const q        = query.toUpperCase();
  const filtered = (q ? symbols.filter(s => s.includes(q)) : symbols).slice(0, 60);

  return (
    <div className="relative" ref={ref}>
      <button onClick={() => setOpen(o => !o)}
        className="flex items-center gap-1 text-xs text-muted hover:text-white px-2 py-1 rounded-lg border border-border hover:border-accent/50 transition-colors">
        {symbol}
        <ChevronDown size={12} className={clsx("transition-transform", open && "rotate-180")} />
      </button>
      {open && (
        <div className="absolute left-0 top-full mt-1 w-52 bg-card border border-border rounded-xl shadow-xl z-50 overflow-hidden">
          <div className="p-2 border-b border-border">
            <div className="flex items-center gap-2 bg-surface rounded-lg px-2 py-1.5">
              <Search size={12} className="text-muted" />
              <input value={query} onChange={e => setQuery(e.target.value)} autoFocus
                placeholder="筛选品种..." className="bg-transparent text-xs w-full outline-none text-white" />
            </div>
          </div>
          <div className="max-h-72 overflow-y-auto">
            {filtered.map(s => (
              <button key={s} onClick={() => { onSelect(s); setOpen(false); setQuery(""); }}
                className={clsx(
                  "w-full text-left px-3 py-1.5 text-xs transition-colors hover:bg-surface",
                  symbol === s ? "text-accent font-bold bg-accent/5" : "text-muted"
                )}>
                {s.replace("USDT", "")}<span className="text-muted">/USDT</span>
              </button>
            ))}
            {filtered.length === 0 && (
              <p className="text-xs text-muted text-center py-3">无匹配品种</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

// ── 测试网 / 真实网 tab switcher ────────────────────────────────────────────
function NetworkTabs() {
  const { networkTab, switchNetwork } = useApp();

  return (
    <div className="flex items-center gap-0.5 bg-surface rounded-lg p-0.5 border border-border">
      {[["test", "测试网"], ["main", "真实网"]].map(([tab, label]) => (
        <button
          key={tab}
          onClick={() => switchNetwork(tab)}
          className={clsx(
            "text-xs px-3 py-1 rounded-md transition-colors font-medium",
            networkTab === tab
              ? tab === "main"
                ? "bg-red/80 text-white"
                : "bg-accent/90 text-black"
              : "text-muted hover:text-white"
          )}
        >
          {label}
        </button>
      ))}
    </div>
  );
}

export default function Header() {
  const { ticker, botStatus, connected, symbol, setSymbol } = useApp();
  const [localTicker, setLocalTicker] = useState(null);
  const [symbols, setSymbols]         = useState([]);

  useEffect(() => {
    getSymbols().then(({ data }) => setSymbols(data)).catch(() => {});
  }, []);

  useEffect(() => {
    if (!symbol) return;
    if (ticker?.symbol === symbol) return;
    let cancelled = false;
    getTicker(symbol)
      .then(({ data }) => {
        if (!cancelled) setLocalTicker({
          symbol, price: data.price, change: data.priceChangePercent,
          high: data.highPrice, low: data.lowPrice, volume: data.quoteVolume,
        });
      })
      .catch(() => {});
    return () => { cancelled = true; };
  }, [symbol, ticker?.symbol]);

  useEffect(() => {
    if (ticker?.symbol === symbol) setLocalTicker(null);
  }, [ticker, symbol]);

  const display = ticker?.symbol === symbol ? ticker : localTicker;
  const pct     = display ? parseFloat(display.change) : 0;
  const isUp    = pct >= 0;

  return (
    <header className="flex items-center justify-between px-4 h-16 bg-card border-b border-border shrink-0">
      {/* 价格区 */}
      <div className="flex items-center gap-4">
        <SymbolDropdown symbol={symbol} symbols={symbols} onSelect={setSymbol} />
        {display ? (
          <>
            <span className="text-xl font-bold font-mono">
              ${parseFloat(display.price).toLocaleString("en", { minimumFractionDigits: 2 })}
            </span>
            <span className={clsx(
              "flex items-center gap-1 text-sm font-medium px-2 py-0.5 rounded",
              isUp ? "text-green bg-green/10" : "text-red bg-red/10"
            )}>
              {isUp ? <TrendingUp size={14}/> : <TrendingDown size={14}/>}
              {isUp ? "+" : ""}{pct.toFixed(2)}%
            </span>
            <div className="hidden md:flex gap-4 text-xs text-muted">
              <span>24H高 <span className="text-white">${parseFloat(display.high).toLocaleString()}</span></span>
              <span>24H低 <span className="text-white">${parseFloat(display.low).toLocaleString()}</span></span>
              <span>额 <span className="text-white">${(parseFloat(display.volume)/1e6).toFixed(1)}M</span></span>
            </div>
          </>
        ) : (
          <span className="text-muted text-sm">等待行情数据...</span>
        )}
      </div>

      {/* 右侧状态区 */}
      <div className="flex items-center gap-3">
        {/* 测试网 / 真实网 切换 */}
        <NetworkTabs />

        {botStatus?.last_signal && botStatus.last_signal !== "NONE" && (
          <span className={clsx(
            "hidden md:flex items-center gap-1 text-xs px-2 py-1 rounded border",
            botStatus.last_signal === "LONG"
              ? "text-green border-green/30 bg-green/5"
              : "text-red border-red/30 bg-red/5"
          )}>
            <Activity size={12}/>
            {botStatus.last_signal === "LONG" ? "多头信号" : "空头信号"}
          </span>
        )}

        {botStatus?.running && (
          <span className="hidden md:flex items-center gap-1.5 text-xs text-accent">
            <span className="w-1.5 h-1.5 rounded-full bg-accent animate-pulse"/>
            策略运行中
            {botStatus.trade_count > 0 && (
              <span className="text-muted ml-1">{botStatus.trade_count} 笔</span>
            )}
          </span>
        )}

        <div className={clsx(
          "flex items-center gap-1.5 text-xs px-2 py-1 rounded",
          connected ? "text-green bg-green/10" : "text-muted bg-surface"
        )}>
          {connected ? <Wifi size={13}/> : <WifiOff size={13}/>}
          <span className="hidden md:block">{connected ? "已连接" : "未连接"}</span>
        </div>
      </div>
    </header>
  );
}
