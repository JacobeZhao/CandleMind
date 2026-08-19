import React, { useEffect, useMemo, useState } from "react";
import { getTicker } from "../api/client";
import { useTicker } from "../context/MarketTickerContext";

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
  const number = Number(value);
  if (!Number.isFinite(number)) return "--";
  const digits = number >= 1000 ? 2 : number >= 1 ? 4 : 6;
  return number.toLocaleString("en-US", {
    minimumFractionDigits: 2,
    maximumFractionDigits: digits,
  });
}

function Quote({ label, value }) {
  return (
    <div className="flex shrink-0 items-baseline gap-1.5 whitespace-nowrap">
      <span className="text-xs text-muted">{label}</span>
      <span className="font-mono text-sm font-semibold text-white">{value}</span>
    </div>
  );
}

export default function MarketSummary({ symbol }) {
  const tickerValue = useTicker();
  const ticker = tickerValue?.ticker ?? tickerValue;
  const [restTicker, setRestTicker] = useState(null);

  useEffect(() => {
    if (!symbol) {
      setRestTicker(null);
      return undefined;
    }

    let cancelled = false;
    setRestTicker(null);
    getTicker(symbol)
      .then(({ data }) => {
        if (cancelled || (data.symbol && data.symbol !== symbol)) return;
        setRestTicker(normalizeTicker(data, symbol));
      })
      .catch(() => {});

    return () => {
      cancelled = true;
    };
  }, [symbol]);

  const display = useMemo(() => {
    const matchingRest = restTicker?.symbol === symbol ? restTicker : null;
    const matchingLive = ticker?.symbol === symbol ? normalizeTicker(ticker, symbol) : null;
    if (!matchingRest && !matchingLive) return null;
    return { ...matchingRest, ...matchingLive };
  }, [restTicker, ticker, symbol]);

  return (
    <section
      className="flex min-w-max flex-nowrap items-center gap-5"
      aria-label={`${symbol || "当前品种"} 行情摘要`}
    >
      <Quote label="当前价格" value={display?.price != null ? `$${formatPrice(display.price)}` : "--"} />
      <Quote label="24H高" value={display?.high != null ? `$${formatPrice(display.high)}` : "--"} />
      <Quote label="24H低" value={display?.low != null ? `$${formatPrice(display.low)}` : "--"} />
    </section>
  );
}
