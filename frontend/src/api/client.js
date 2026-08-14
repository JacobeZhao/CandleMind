import axios from "axios";

const api = axios.create({ baseURL: "/api", timeout: 15000 });
const apiBacktest = axios.create({ baseURL: "/api", timeout: 600000 });
const apiAI = axios.create({ baseURL: "/api", timeout: 35000 });

export const getSettings = () => api.get("/settings");
export const saveSettings = (data) => api.post("/settings", data);
export const testConnection = (testnet) => api.post("/settings/test-connection", null, { params: { testnet } });
export const getMyIp = () => api.get("/settings/myip");

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
export const getOrderHistory = (sym, limit) => api.get("/orders/history", { params: { symbol: sym, limit } });
export const getRecentTrades = (sym) => api.get("/orders/trades", { params: { symbol: sym } });
export const cancelOrder = (sym, id) => api.delete(`/orders/cancel/${sym}/${id}`);

export const startEngine = (data) => api.post("/strategy/engine/start", data);
export const stopEngine = () => api.post("/strategy/engine/stop");
export const getEngineStatus = () => api.get("/strategy/engine/status");

export const runSarAdxBacktest = (data) => apiBacktest.post("/backtest/sar-adx", data);
export const getSarAdxBacktestCapabilities = () => api.get("/backtest/sar-adx/capabilities");
export const listAIProviders = () => api.get("/ai/providers");
export const listAIConfigs = () => api.get("/ai/list");
export const createAIConfig = (data) => api.post("/ai/create", data);
export const updateAIConfig = (id, data) => api.put(`/ai/${id}`, data);
export const deleteAIConfig = (id) => api.delete(`/ai/${id}`);
export const activateAIConfig = (id) => api.post(`/ai/${id}/activate`);
export const testAIConfig = (id) => apiAI.post(`/ai/${id}/test`);
export const testAIConfigDraft = (data) => apiAI.post("/ai/test-draft", data);
export const marketChat = (data, signal) => apiAI.post("/ai/market-chat", data, { signal });
