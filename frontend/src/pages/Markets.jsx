import React from "react";
import { useApp } from "../context/AppContext";
import MarketSummary from "../components/MarketSummary";
import PriceChart from "../components/PriceChart";

export default function Markets() {
  const { symbol } = useApp();

  return (
    <div className="h-full min-h-[640px] min-w-0">
      <PriceChart
        symbol={symbol}
        defaultInterval="5m"
        headerLeading={<MarketSummary symbol={symbol} compact />}
      />
    </div>
  );
}
