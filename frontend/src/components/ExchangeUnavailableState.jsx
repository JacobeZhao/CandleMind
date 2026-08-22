import React from "react";
import { PlugZap } from "lucide-react";

const PROVIDER_LABELS = {
  binance: "Binance",
  okx: "OKX",
  bybit: "Bybit",
  gateio: "Gate.io",
  a_share: "A股",
};

export default function ExchangeUnavailableState({ exchangeProvider }) {
  const providerLabel = PROVIDER_LABELS[exchangeProvider] || exchangeProvider || "当前市场";

  return (
    <section
      aria-label={`${providerLabel}连接状态`}
      className="flex min-h-[360px] items-center justify-center border border-border bg-card px-6 py-12 text-center"
    >
      <div className="max-w-md">
        <span className="mx-auto flex h-11 w-11 items-center justify-center rounded-md border border-border bg-surface text-muted">
          <PlugZap size={20} aria-hidden="true" />
        </span>
        <h1 className="mt-4 text-lg font-semibold text-white">{providerLabel} 未连接</h1>
        <p className="mt-2 text-sm text-muted">未来会接入，敬请期待</p>
      </div>
    </section>
  );
}
