import React, { createContext, useContext, useReducer, useCallback, useEffect } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import { getSettings, getEngineStatus, saveSettings } from "../api/client";

const AppContext = createContext(null);

const initial = {
  connected:  false,
  ticker:     null,
  account:    null,
  positions:  [],
  openOrders: [],
  botStatus:  { running: false, last_signal: "NONE", last_action: "", trade_count: 0 },
  symbol:     "BTCUSDT",
  networkTab: "test",   // "test" | "main"
};

function reducer(state, action) {
  switch (action.type) {
    case "SET_SYMBOL":      return { ...state, symbol: action.payload };
    case "SET_CONNECTED":   return { ...state, connected: action.payload };
    case "SET_NETWORK_TAB": return { ...state, networkTab: action.payload };
    case "WS_MSG": {
      const { type, data } = action.payload;
      if (type === "ticker")      return { ...state, ticker: data, connected: true };
      if (type === "account")     return { ...state, account: data };
      if (type === "positions")   return { ...state, positions: data };
      if (type === "open_orders") return { ...state, openOrders: data };
      if (type === "bot_status") {
        const sym = data?.symbol;
        if (sym && sym !== state.symbol)
          return { ...state, botStatus: data, symbol: sym };
        return { ...state, botStatus: data };
      }
      return state;
    }
    default: return state;
  }
}

export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initial);

  // 启动时从引擎/settings读取 symbol 和 networkTab
  useEffect(() => {
    getEngineStatus()
      .then(r => {
        const sym = r.data?.symbol;
        if (sym) dispatch({ type: "SET_SYMBOL", payload: sym });
      })
      .catch(() => {});

    getSettings()
      .then(({ data }) => {
        if (data.symbol) dispatch({ type: "SET_SYMBOL", payload: data.symbol });
        dispatch({ type: "SET_NETWORK_TAB", payload: data.testnet !== false ? "test" : "main" });
      })
      .catch(() => {});
  }, []);

  const handleMessage = useCallback((msg) => {
    dispatch({ type: "WS_MSG", payload: msg });
  }, []);

  useWebSocket(handleMessage);

  const setSymbol    = (sym) => dispatch({ type: "SET_SYMBOL",    payload: sym });
  const setConnected = (v)   => dispatch({ type: "SET_CONNECTED", payload: v   });

  // 切换测试网 / 真实网，同步写入后端设置
  const switchNetwork = async (tab) => {
    dispatch({ type: "SET_NETWORK_TAB", payload: tab });
    try {
      await saveSettings({ testnet: tab === "test" });
    } catch (_) {}
  };

  return (
    <AppContext.Provider value={{ ...state, setSymbol, setConnected, switchNetwork, dispatch }}>
      {children}
    </AppContext.Provider>
  );
}

export const useApp = () => useContext(AppContext);
