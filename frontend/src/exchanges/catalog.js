export const EXCHANGE_PROVIDERS = Object.freeze([
  { id: "binance", label: "Binance", available: true },
  { id: "okx", label: "OKX", available: false },
  { id: "bybit", label: "Bybit", available: false },
  { id: "gateio", label: "Gate.io", available: false },
  { id: "a_share", label: "A股", available: false },
]);

const PROVIDER_IDS = new Set(EXCHANGE_PROVIDERS.map(({ id }) => id));

export function normalizeExchangeProvider(value) {
  const normalized = String(value || "").trim().toLowerCase();
  return PROVIDER_IDS.has(normalized) ? normalized : "binance";
}

export function getExchangeProvider(value) {
  const normalized = normalizeExchangeProvider(value);
  return EXCHANGE_PROVIDERS.find(({ id }) => id === normalized);
}

export function isExchangeAvailable(value) {
  return getExchangeProvider(value).available;
}
