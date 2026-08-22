import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { AlertCircle, Loader, RefreshCw } from "lucide-react";
import { getTicker } from "../api/client";
import { normalizeApiError } from "../api/errors";
import { useTicker } from "../context/MarketTickerContext";
import { registerRefreshReader } from "../services/refreshCoordinator";

function normalizeTicker(data, fallbackSymbol) {
  if (!data) return null;
  return {
    symbol: data.symbol || fallbackSymbol,
    price: data.price ?? data.markPrice,
    high: data.high ?? data.highPrice,
    low: data.low ?? data.lowPrice,
  };
}

function formatPrice(value) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const digits = number >= 1000 ? 2 : number >= 1 ? 4 : 6;
  return number.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: digits });
}

function formatIndicator(value, digits = 2) {
  if (value == null || value === "") return "--";
  const number = Number(value);
  return Number.isFinite(number) ? number.toFixed(digits) : "--";
}

function Quote({ label, value }) {
  return (
    <div className="flex shrink-0 items-baseline gap-1.5 whitespace-nowrap">
      <span className="text-xs text-muted">{label}</span>
      <span className="font-mono text-sm font-semibold text-white">{value}</span>
    </div>
  );
}

export default function MarketSummary({ symbol, indicators = null, refreshRevision: _refreshRevision = 0 }) {
  const tickerValue = useTicker();
  const ticker = tickerValue?.ticker ?? tickerValue;
  const [state, setState] = useState({ phase: "loading", data: null, error: null, scopeKey: null });
  const requestRef = useRef({ id: 0, controller: null });

  const loadTicker = useCallback(async (replace = false) => {
    if (!symbol) {
      setState({ phase: "empty", data: null, error: null, scopeKey: null });
      return true;
    }
    requestRef.current.controller?.abort();
    const controller = new AbortController();
    const id = requestRef.current.id + 1;
    requestRef.current = { id, controller };
    setState((current) => {
      const sameScope = current.scopeKey === symbol;
      return {
        phase: !replace && sameScope && current.data ? "refreshing" : "loading",
        data: replace || !sameScope ? null : current.data,
        error: null,
        scopeKey: symbol,
      };
    });
    try {
      const { data } = await getTicker(symbol, controller.signal);
      if (controller.signal.aborted || requestRef.current.id !== id) return true;
      if (data?.symbol && data.symbol !== symbol) {
        setState((current) => ({
          ...current,
          phase: current.data ? "stale" : "error",
          error: "行情响应范围与当前品种不一致，请重试。",
        }));
        return false;
      }
      const normalized = normalizeTicker(data, symbol);
      setState({
        phase: normalized ? "complete" : "empty",
        data: normalized,
        error: null,
        scopeKey: symbol,
      });
      return true;
    } catch (error) {
      const parsed = normalizeApiError(error, "行情摘要加载失败，请稍后重试。");
      if (parsed.cancelled || requestRef.current.id !== id) return true;
      setState((current) => ({
        ...current,
        data: parsed.retryable ? current.data : null,
        phase: parsed.retryable && current.data ? "stale" : "error",
        error: parsed.message,
      }));
      return false;
    }
  }, [symbol]);

  useEffect(() => {
    loadTicker(true);
    return () => requestRef.current.controller?.abort();
  }, [loadTicker]);

  useEffect(() => registerRefreshReader("markets:summary", () => loadTicker(false)), [loadTicker]);

  const display = useMemo(() => {
    const restTicker = state.scopeKey === symbol ? state.data : null;
    const matchingRest = restTicker?.symbol === symbol ? restTicker : null;
    const matchingLive = ticker?.symbol === symbol ? normalizeTicker(ticker, symbol) : null;
    if (!matchingRest && !matchingLive) return null;
    return { ...matchingRest, ...matchingLive };
  }, [state.data, state.scopeKey, ticker, symbol]);

  return (
    <section className="flex min-w-max flex-nowrap items-center gap-5" aria-label={`${symbol || "当前品种"} 行情摘要`}>
      <Quote label="当前价格" value={display?.price != null ? `$${formatPrice(display.price)}` : "--"} />
      <Quote label="24H高" value={display?.high != null ? `$${formatPrice(display.high)}` : "--"} />
      <Quote label="24H低" value={display?.low != null ? `$${formatPrice(display.low)}` : "--"} />
      <Quote label="ADX(14)" value={formatIndicator(indicators?.adx)} />
      <Quote label="ATR(14)" value={formatPrice(indicators?.atr)} />
      <Quote label="RSI(14)" value={formatIndicator(indicators?.rsi)} />
      {state.phase === "loading" && <span role="status" className="inline-flex items-center gap-1 text-xs text-muted"><Loader size={12} className="animate-spin" />行情加载中</span>}
      {["error", "stale"].includes(state.phase) && (
        <span role="alert" className="inline-flex max-w-72 items-center gap-1 text-xs text-accent">
          <AlertCircle size={12} className="shrink-0" />
          <span className="truncate">{state.phase === "stale" ? "行情可能已过期" : state.error}</span>
          <button type="button" aria-label="重试行情摘要" title="重试行情摘要" onClick={() => loadTicker(state.phase === "error")} className="shrink-0 text-white"><RefreshCw size={12} /></button>
        </span>
      )}
      {state.phase === "empty" && <span className="text-xs text-muted">暂无行情</span>}
    </section>
  );
}
