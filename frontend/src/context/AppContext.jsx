import React, { createContext, useContext, useReducer, useCallback, useEffect, useRef } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import { getEngineStatus, getSettings, saveSettings } from "../api/client";
import { clearTicker, publishTicker } from "./MarketTickerContext";
import { publishRealtimeEvent } from "../services/realtimeEvents";

const AppContext = createContext(null);

const initial = {
  connected:  false,
  account:    null,
  positions:  [],
  openOrders: [],
  botStatus:  null,
  botStatusLoaded: false,
  symbol:     "BTCUSDT",
  networkTab: "test",   // "test" | "main"
  networkSwitching: false,
  networkError: null,
};

function reducer(state, action) {
  switch (action.type) {
    case "SET_SYMBOL":      return { ...state, symbol: action.payload };
    case "SET_CONNECTED":   return state.connected === action.payload
      ? state
      : { ...state, connected: action.payload };
    case "SET_NETWORK_TAB": return { ...state, networkTab: action.payload };
    case "SET_NETWORK_SWITCHING": return { ...state, networkSwitching: action.payload };
    case "SET_NETWORK_ERROR": return { ...state, networkError: action.payload };
    case "SET_BOT_STATUS":  return { ...state, botStatus: action.payload, botStatusLoaded: true };
    case "WS_MSG": {
      const { type, data } = action.payload;
      if (type === "account")     return { ...state, account: data };
      if (type === "positions")   return { ...state, positions: data };
      if (type === "open_orders") return { ...state, openOrders: data };
      if (type === "bot_status")  return { ...state, botStatus: data, botStatusLoaded: true };
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
  const networkSwitchInFlight = useRef(false);
  const botStatusRevision = useRef(0);

  useEffect(() => {
    activeSymbol.current = state.symbol;
  }, [state.symbol]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const data = pendingTicker.current;
      pendingTicker.current = null;
      if (!data || data.symbol !== activeSymbol.current || switchingSymbol.current) return;
      publishTicker(data);
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
          clearTicker();
          dispatch({ type: "SET_SYMBOL", payload: data.symbol });
        }
        dispatch({ type: "SET_NETWORK_TAB", payload: data.testnet !== false ? "test" : "main" });
      })
      .catch(() => {});
  }, []);

  useEffect(() => {
    let active = true;
    let retryTimer = null;
    const loadStatus = () => {
      const requestedAtRevision = botStatusRevision.current;
      getEngineStatus()
        .then(({ data }) => {
          if (active && botStatusRevision.current === requestedAtRevision) {
            dispatch({ type: "SET_BOT_STATUS", payload: data });
          }
        })
        .catch(() => {
          if (active && botStatusRevision.current === requestedAtRevision) {
            retryTimer = window.setTimeout(loadStatus, 3000);
          }
        });
    };
    loadStatus();
    return () => {
      active = false;
      if (retryTimer) window.clearTimeout(retryTimer);
    };
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
    if (msg?.type === "market_agent_event" || msg?.type === "market_agent_status") {
      publishRealtimeEvent(msg);
      return;
    }
    if (msg?.type === "bot_status") botStatusRevision.current += 1;
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
    clearTicker();
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
  const switchNetwork = useCallback(async (tab) => {
    if (networkSwitchInFlight.current || tab === state.networkTab) return;
    networkSwitchInFlight.current = true;
    dispatch({ type: "SET_NETWORK_SWITCHING", payload: true });
    dispatch({ type: "SET_NETWORK_ERROR", payload: null });
    try {
      const { data } = await saveSettings({ testnet: tab === "test" });
      if (typeof data?.testnet !== "boolean") {
        throw new Error("Network response did not include testnet state");
      }
      dispatch({ type: "SET_NETWORK_TAB", payload: data.testnet ? "test" : "main" });
    } catch (error) {
      const detail = error?.response?.data?.detail;
      const message = typeof detail === "string"
        ? detail
        : "网络切换失败，请检查连接和 API 配置。";
      dispatch({ type: "SET_NETWORK_ERROR", payload: message });
    } finally {
      networkSwitchInFlight.current = false;
      dispatch({ type: "SET_NETWORK_SWITCHING", payload: false });
    }
  }, [state.networkTab]);

  return (
    <AppContext.Provider value={{ ...state, setSymbol, setConnected, switchNetwork, dispatch }}>
      {children}
    </AppContext.Provider>
  );
}

export const useApp = () => useContext(AppContext);
