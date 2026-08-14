import React, { useEffect, useMemo, useState } from "react";
import { TrendingDown, TrendingUp } from "lucide-react";
import clsx from "clsx";
import { getTicker } from "../api/client";
import { useApp } from "../context/AppContext";

function compact(object) {
  return Object.fromEntries(
    Object.entries(object).filter(([, value]) => value !== undefined && value !== null),
  );
}

function normalizeTicker(data, fallbackSymbol) {
  if (!data) return null;
  return compact({
    symbol: data.symbol || fallbackSymbol,
    markPrice: data.markPrice,
    price: data.price,
    change: data.change ?? data.priceChangePercent,
    high: data.high ?? data.highPrice,
    low: data.low ?? data.lowPrice,
    volume: data.volume ?? data.quoteVolume,
  });
}

function formatPrice(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const digits = number >= 1000 ? 2 : number >= 1 ? 4 : 6;
  return number.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  });
}

function formatVolume(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  return new Intl.NumberFormat("en-US", {
    notation: "compact",
    maximumFractionDigits: 2,
  }).format(number);
}

function Stat({ label, value, valueClassName, compact: isCompact = false }) {
  return (
    <div className={clsx(isCompact ? "min-w-[92px] px-2 py-1" : "min-w-[112px] bg-card px-3 py-2 sm:min-w-0 sm:px-4")}>
      <div className="text-[10px] text-muted">{label}</div>
      <div className={clsx("mt-0.5 truncate font-mono text-xs text-white", valueClassName)}>
        {value}
      </div>
    </div>
  );
}

export default function MarketSummary({ symbol, compact: isCompact = false }) {
  const { ticker, connected } = useApp();
  const [restTicker, setRestTicker] = useState(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!symbol) {
      setRestTicker(null);
      setFailed(false);
      return undefined;
    }

    let cancelled = false;
    setRestTicker(null);
    setFailed(false);

    getTicker(symbol)
      .then(({ data }) => {
        if (cancelled || (data.symbol && data.symbol !== symbol)) return;
        setRestTicker(normalizeTicker(data, symbol));
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });

    return () => {
      cancelled = true;
    };
  }, [symbol]);

  const matchingRestTicker = restTicker?.symbol === symbol ? restTicker : null;
  const liveTicker = ticker?.symbol === symbol ? normalizeTicker(ticker, symbol) : null;
  const display = useMemo(() => {
    if (!matchingRestTicker && !liveTicker) return null;
    return { ...matchingRestTicker, ...liveTicker };
  }, [matchingRestTicker, liveTicker]);

  const change = Number(display?.change);
  const hasChange = Number.isFinite(change);
  const isUp = hasChange && change >= 0;
  const markPrice = display?.markPrice;
  const unavailable = !display && (failed || !connected);

  if (isCompact) {
    return (
      <section
        className="min-w-0 overflow-x-auto"
        aria-label={`${symbol || "Current symbol"} market summary`}
      >
        <div className="flex min-w-max items-center divide-x divide-border">
          <div className="min-w-[168px] px-2 py-1">
            <div className="flex items-center gap-2 text-[10px] text-muted">
              <span className="font-medium text-white">{symbol || "--"}</span>
              <span>Mark</span>
              {!connected && <span>REST</span>}
            </div>
            <div className="flex items-baseline gap-2">
              <span className="font-mono text-sm font-bold text-white">
                {markPrice != null ? `$${formatPrice(markPrice)}` : "--"}
              </span>
              {hasChange && (
                <span className={clsx("flex items-center gap-0.5 text-[10px]", isUp ? "text-green" : "text-red")}>
                  {isUp ? <TrendingUp size={10} /> : <TrendingDown size={10} />}
                  {isUp ? "+" : ""}{change.toFixed(2)}%
                </span>
              )}
              {unavailable && <span className="text-[10px] text-muted">Unavailable</span>}
            </div>
          </div>
          <Stat compact label="24H High" value={display?.high != null ? `$${formatPrice(display.high)}` : "--"} />
          <Stat compact label="24H Low" value={display?.low != null ? `$${formatPrice(display.low)}` : "--"} />
          <Stat compact label="24H Volume" value={display?.volume != null ? `$${formatVolume(display.volume)}` : "--"} />
        </div>
      </section>
    );
  }

  return (
    <section
      className="shrink-0 overflow-hidden rounded-lg border border-border bg-card"
      aria-label={`${symbol || "当前品种"} 行情摘要`}
    >
      <div className="flex min-h-[68px] flex-col items-stretch sm:flex-row sm:overflow-x-auto">
        <div className="flex min-h-[68px] flex-col justify-center border-b border-border px-4 py-2 sm:min-w-[220px] sm:border-b-0 sm:border-r">
          <div className="flex items-center gap-2 text-[10px] text-muted">
            <span className="font-medium text-white">{symbol || "--"}</span>
            <span>标记价格</span>
            {!connected && <span className="text-muted">REST</span>}
          </div>
          <div className="mt-0.5 flex items-baseline gap-2">
            <span className="font-mono text-lg font-bold text-white">
              {markPrice != null ? `$${formatPrice(markPrice)}` : "--"}
            </span>
            {hasChange && (
              <span className={clsx("flex items-center gap-0.5 text-xs", isUp ? "text-green" : "text-red")}>
                {isUp ? <TrendingUp size={12} /> : <TrendingDown size={12} />}
                {isUp ? "+" : ""}{change.toFixed(2)}%
              </span>
            )}
          </div>
          {unavailable && <span className="text-[10px] text-muted">行情暂不可用</span>}
        </div>

        <div className="grid flex-1 grid-cols-2 gap-px bg-border sm:min-w-[448px] sm:grid-cols-4">
          <Stat label="24H 涨跌" value={hasChange ? `${isUp ? "+" : ""}${change.toFixed(2)}%` : "--"} valueClassName={hasChange ? (isUp ? "text-green" : "text-red") : ""} />
          <Stat label="24H 高" value={display?.high != null ? `$${formatPrice(display.high)}` : "--"} />
          <Stat label="24H 低" value={display?.low != null ? `$${formatPrice(display.low)}` : "--"} />
          <Stat label="24H 成交额" value={display?.volume != null ? `$${formatVolume(display.volume)}` : "--"} />
        </div>
      </div>
    </section>
  );
}
