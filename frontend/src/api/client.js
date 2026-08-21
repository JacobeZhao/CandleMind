import axios from "axios";

const api = axios.create({ baseURL: "/api", timeout: 15000 });
const apiAI = axios.create({ baseURL: "/api", timeout: 35000 });

export const getSettings = () => api.get("/settings");
export const saveSettings = (data) => api.post("/settings", data);
export const testConnection = (testnet) => api.post("/settings/test-connection", null, { params: { testnet } });
export const getMyIp = () => api.get("/settings/myip");
export const getAccountBalance = () => api.get("/account/balance");

export const getTicker = (sym) => api.get(`/market/ticker/${sym}`);
export const getKlines = (sym, interval, limit, inds = "psar", indParams = {}, signal) =>
  api.get(`/market/klines/${sym}`, {
    signal,
    params: {
      interval,
      limit,
      inds: Array.isArray(inds) ? inds.join(",") : inds,
      params: JSON.stringify(indParams),
    },
  });
export const getSymbols = () => api.get("/market/symbols");
export const getOrderHistory = (sym, limit, signal) => api.get("/orders/history", {
  signal,
  params: { symbol: sym, limit },
});
export const getCombinedOpenOrders = (sym, signal) => api.get("/orders/open/combined", {
  signal,
  params: { symbol: sym },
});
export const getRecentTrades = (sym, signal) => api.get("/orders/trades", {
  signal,
  params: { symbol: sym },
});
export const getAccountTradingAnalytics = (sym, signal) => api.get("/orders/analytics", {
  signal,
  params: { symbol: sym },
});
export const getStrategyAnalytics = (signal) => api.get("/strategy/analytics", { signal });

export const startEngine = ({
  strategy_type,
  config_version,
  config_hash,
  symbol,
  capital_limit,
  mainnet_confirmation,
}) => api.post("/strategy/engine/start", {
  strategy_type,
  config_version,
  config_hash,
  symbol,
  capital_limit,
  ...(mainnet_confirmation ? { mainnet_confirmation } : {}),
});
export const stopEngine = () => api.post("/strategy/engine/stop");
export const getEngineStatus = () => api.get("/strategy/engine/status");
export const getStrategyCatalog = () => api.get("/strategy/catalog");
export const getStrategyConfig = () => api.get("/strategy/config");
export const saveStrategyConfig = (data) => api.put("/strategy/config", data);
export const listAIProviders = () => api.get("/ai/providers");
export const listAIConfigs = () => api.get("/ai/list");
export const createAIConfig = (data) => api.post("/ai/create", data);
export const updateAIConfig = (id, data) => api.put(`/ai/${id}`, data);
export const deleteAIConfig = (id) => api.delete(`/ai/${id}`);
export const activateAIConfig = (id) => api.post(`/ai/${id}/activate`);
export const testAIConfig = (id) => apiAI.post(`/ai/${id}/test`);
export const testAIConfigDraft = (data) => apiAI.post("/ai/test-draft", data);
export const marketChat = (data, signal) => apiAI.post("/ai/market-chat", data, { signal });
export const getMarketAgentStatus = (signal) => api.get("/ai/market-agent/status", { signal });
export const getMarketAgentEvents = (afterSequence = 0, limit = 100, signal) =>
  api.get("/ai/market-agent/events", {
    signal,
    params: { after_sequence: afterSequence, limit },
  });
export const startMarketAgent = (data) => api.post("/ai/market-agent/start", data);
export const stopMarketAgent = () => api.post("/ai/market-agent/stop");
export const sendMarketAgentMessage = (data, signal, clientMessageId) =>
  apiAI.post("/ai/market-agent/messages", data, {
    signal,
    headers: clientMessageId ? { "X-Client-Message-Id": clientMessageId } : undefined,
  });
