import axios from "axios";

const api = axios.create({ baseURL: "/api", timeout: 15000 });

// Long-timeout instance for backtest (no limit)
const apiLong = axios.create({ baseURL: "/api", timeout: 0 });

export const getSettings   = ()        => api.get("/settings");
export const saveSettings  = (data)    => api.post("/settings", data);
export const connectApi     = ()         => api.post("/settings/connect");
export const testConnection = (testnet) => api.post("/settings/test-connection", null, { params: { testnet } });
export const getMyIp        = ()         => api.get("/settings/myip");

export const getBalance    = ()        => api.get("/account/balance");
export const getPositions  = ()        => api.get("/account/positions");

export const getTicker     = (sym)     => api.get(`/market/ticker/${sym}`);
export const getKlines     = (sym, interval, limit, inds = "supertrend", indParams = {}) =>
  api.get(`/market/klines/${sym}`, {
    params: {
      interval, limit,
      inds: Array.isArray(inds) ? inds.join(",") : inds,
      params: JSON.stringify(indParams),
    },
  });
export const getSymbols       = ()     => api.get("/market/symbols");
export const getIndicatorMeta = ()     => api.get("/market/indicators");

export const getOpenOrders   = (sym)        => api.get("/orders/open",    { params: { symbol: sym } });
export const getOrderHistory = (sym, limit) => api.get("/orders/history", { params: { symbol: sym, limit } });
export const getRecentTrades = (sym)        => api.get("/orders/trades",  { params: { symbol: sym } });
export const cancelOrder     = (sym, id)    => api.delete(`/orders/cancel/${sym}/${id}`);

// Strategy CRUD
export const listStrategies    = ()       => api.get("/strategy/list");
export const updateStrategy    = (id, d)  => api.put(`/strategy/${id}`, d);
export const activateStrategy  = (id)     => api.post(`/strategy/${id}/activate`);

// Engine control
export const startEngine  = (paper = false) => api.post("/strategy/engine/start", null, { params: { paper } });
export const stopEngine   = ()   => api.post("/strategy/engine/stop");
export const getEngineStatus = () => api.get("/strategy/engine/status");

export const runBacktest   = (data)    => apiLong.post("/backtest/run", data);
export const downloadData  = (params)  => api.get("/backtest/download", { params });

// Research / 稳健性
export const runRobustness   = (data)   => apiLong.post("/research/robustness", data);
export const runOptimize     = (data)   => apiLong.post("/research/optimize", data);
export const runPortfolio    = (data)   => apiLong.post("/research/portfolio", data);
export const runAttribution  = (data)   => apiLong.post("/research/attribution", data);
export const buildFeatures   = (data)   => apiLong.post("/research/features/build", data);
export const getFeatures     = ()       => api.get("/research/features");
export const listReports     = ()       => api.get("/research/reports");
export const reportUrl       = (name)   => `/api/research/report/${name}`;
export const getLeaderboard  = (metric) => api.get("/research/leaderboard", { params: { metric, limit: 20 } });
export const getHealth       = ()       => api.get("/health");

// ML signal
export const getMLSignal = (sym, mode='recommended') => api.get(`/research/ml/signal/${sym}`, { params: { mode } })

// AI config
export const listAIProviders  = ()       => api.get("/ai/providers");
export const listAIConfigs    = ()       => api.get("/ai/list");
export const createAIConfig   = (d)      => api.post("/ai/create", d);
export const updateAIConfig   = (id, d)  => api.put(`/ai/${id}`, d);
export const deleteAIConfig   = (id)     => api.delete(`/ai/${id}`);
export const activateAIConfig = (id)     => api.post(`/ai/${id}/activate`);
export const testAIConfig     = (id)     => api.post(`/ai/${id}/test`);

export default api;
