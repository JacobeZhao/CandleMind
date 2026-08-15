import React, { createContext, useContext, useReducer, useCallback, useEffect, useRef } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import { getSettings, saveSettings } from "../api/client";

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
    case "SET_SYMBOL":      return { ...state, symbol: action.payload, ticker: null };
    case "SET_CONNECTED":   return { ...state, connected: action.payload };
    case "SET_NETWORK_TAB": return { ...state, networkTab: action.payload };
    case "WS_MSG": {
      const { type, data } = action.payload;
      if (type === "ticker") {
        if (!data?.symbol || data.symbol !== state.symbol) return state;
        const ticker = state.ticker?.symbol === data.symbol
          ? { ...state.ticker, ...data }
          : data;
        return { ...state, ticker, connected: true };
      }
      if (type === "account")     return { ...state, account: data };
      if (type === "positions")   return { ...state, positions: data };
      if (type === "open_orders") return { ...state, openOrders: data };
      if (type === "bot_status")  return { ...state, botStatus: data };
      return state;
    }
    default: return state;
  }
}

export function AppProvider({ children }) {
  const [state, dispatch] = useReducer(reducer, initial);
  const symbolRequestId = useRef(0);
  const symbolSaveQueue = useRef(Promise.resolve());
  const activeSymbol = useRef(initial.symbol);
  const pendingTicker = useRef(null);
  const switchingSymbol = useRef(false);

  useEffect(() => {
    activeSymbol.current = state.symbol;
  }, [state.symbol]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const data = pendingTicker.current;
      pendingTicker.current = null;
      if (!data || data.symbol !== activeSymbol.current || switchingSymbol.current) return;
      dispatch({ type: "WS_MSG", payload: { type: "ticker", data } });
    }, 500);
    return () => {
      window.clearInterval(timer);
      pendingTicker.current = null;
    };
  }, []);

  // 启动时从 settings 读取全局交易对和 networkTab。
  useEffect(() => {
    getSettings()
      .then(({ data }) => {
        if (data.symbol && symbolRequestId.current === 0) {
          activeSymbol.current = data.symbol;
          dispatch({ type: "SET_SYMBOL", payload: data.symbol });
        }
        dispatch({ type: "SET_NETWORK_TAB", payload: data.testnet !== false ? "test" : "main" });
      })
      .catch(() => {});
  }, []);

  const handleMessage = useCallback((msg) => {
    if (msg?.type === "ticker") {
      const data = msg.data;
      if (switchingSymbol.current || !data?.symbol || data.symbol !== activeSymbol.current) return;
      pendingTicker.current = pendingTicker.current?.symbol === data.symbol
        ? { ...pendingTicker.current, ...data }
        : data;
      return;
    }
    dispatch({ type: "WS_MSG", payload: msg });
  }, []);
  const handleConnectionChange = useCallback((connected) => {
    dispatch({ type: "SET_CONNECTED", payload: connected });
  }, []);

  useWebSocket(handleMessage, handleConnectionChange);

  const setSymbol = useCallback((sym) => {
    const requestId = ++symbolRequestId.current;
    pendingTicker.current = null;
    switchingSymbol.current = true;
    const save = symbolSaveQueue.current.then(async () => {
      try {
        const { data } = await saveSettings({ symbol: sym });
        if (requestId === symbolRequestId.current) {
          const nextSymbol = data.symbol || sym.trim().toUpperCase();
          activeSymbol.current = nextSymbol;
          dispatch({ type: "SET_SYMBOL", payload: nextSymbol });
        }
      } catch (error) {
        if (requestId === symbolRequestId.current) {
          console.error("Failed to switch symbol", error);
        }
      } finally {
        if (requestId === symbolRequestId.current) switchingSymbol.current = false;
      }
    });
    symbolSaveQueue.current = save;
    return save;
  }, []);
  const setConnected = (v)   => dispatch({ type: "SET_CONNECTED", payload: v   });

  // 切换测试网 / 真实网，同步写入后端设置
  const switchNetwork = async (tab) => {
    try {
      await saveSettings({ testnet: tab === "test" });
      dispatch({ type: "SET_NETWORK_TAB", payload: tab });
    } catch (error) {
      console.error("Failed to switch network", error);
    }
  };

  return (
    <AppContext.Provider value={{ ...state, setSymbol, setConnected, switchNetwork, dispatch }}>
      {children}
    </AppContext.Provider>
  );
}

export const useApp = () => useContext(AppContext);
