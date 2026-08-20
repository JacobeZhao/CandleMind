import React, { createContext, useContext, useReducer, useCallback, useEffect, useRef } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import {
  getAccountBalance,
  getEngineStatus,
  getSettings,
  saveSettings,
  startEngine,
  stopEngine,
} from "../api/client";
import { clearTicker, publishTicker } from "./MarketTickerContext";
import { publishRealtimeEvent } from "../services/realtimeEvents";

const AppContext = createContext(null);

const initial = {
  connected:  false,
  account:    null,
  accountError: null,
  positions:  [],
  openOrders: [],
  botStatus:  null,
  botStatusLoaded: false,
  strategyCapitalLimit: "1000",
  strategyCommandPending: false,
  strategyCommandError: null,
  strategyStatusUncertain: false,
  refreshRevision: 0,
  refreshPending: false,
  refreshError: null,
  symbol:     "BTCUSDT",
  symbolSwitching: false,
  networkTab: "test",   // "test" | "main"
  networkSwitching: false,
  networkError: null,
};

function reducer(state, action) {
  switch (action.type) {
    case "SET_SYMBOL":      return { ...state, symbol: action.payload };
    case "SET_SYMBOL_SWITCHING": return { ...state, symbolSwitching: action.payload };
    case "SET_CONNECTED":   return state.connected === action.payload
      ? state
      : { ...state, connected: action.payload };
    case "SET_NETWORK_TAB": return { ...state, networkTab: action.payload };
    case "SET_NETWORK_SWITCHING": return { ...state, networkSwitching: action.payload };
    case "SET_NETWORK_ERROR": return { ...state, networkError: action.payload };
    case "SET_NETWORK_SESSION": return {
      ...state,
      networkTab: action.payload.testnet ? "test" : "main",
      account: action.payload.account ?? null,
      accountError: action.payload.account ? null : "账户数据尚未就绪。",
      positions: [],
      openOrders: [],
    };
    case "SET_BOT_STATUS":  return {
      ...state,
      botStatus: action.payload,
      botStatusLoaded: true,
      strategyStatusUncertain: false,
    };
    case "SET_STRATEGY_CAPITAL_LIMIT": return { ...state, strategyCapitalLimit: action.payload };
    case "SET_STRATEGY_COMMAND_PENDING": return { ...state, strategyCommandPending: action.payload };
    case "SET_STRATEGY_COMMAND_ERROR": return { ...state, strategyCommandError: action.payload };
    case "SET_STRATEGY_STATUS_UNCERTAIN": return {
      ...state,
      strategyStatusUncertain: action.payload,
    };
    case "SET_REFRESH_PENDING": return { ...state, refreshPending: action.payload };
    case "SET_REFRESH_ERROR": return { ...state, refreshError: action.payload };
    case "COMPLETE_REFRESH": return {
      ...state,
      refreshRevision: state.refreshRevision + 1,
      refreshPending: false,
    };
    case "WS_MSG": {
      const { type, data } = action.payload;
      if (type === "account")     return { ...state, account: data, accountError: null };
      if (type === "account_error") return {
        ...state,
        account: null,
        accountError: data?.message || "Binance 账户读取失败。",
      };
      if (type === "positions")   return { ...state, positions: data };
      if (type === "open_orders") return { ...state, openOrders: data };
      if (type === "bot_status")  return {
        ...state,
        botStatus: data,
        botStatusLoaded: true,
        strategyStatusUncertain: false,
      };
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
  const networkRevision = useRef(0);
  const strategyCommandInFlight = useRef(false);
  const strategyStatusUncertain = useRef(false);
  const botStatusRevision = useRef(0);
  const accountRevision = useRef(0);
  const refreshInFlight = useRef(null);

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

  useEffect(() => {
    const requestedAtRevision = accountRevision.current;
    getAccountBalance()
      .then(({ data }) => {
        if (accountRevision.current === requestedAtRevision) {
          dispatch({ type: "WS_MSG", payload: { type: "account", data } });
        }
      })
      .catch(() => {
        if (accountRevision.current === requestedAtRevision) {
          dispatch({
            type: "WS_MSG",
            payload: {
              type: "account_error",
              data: { message: "Binance 账户读取失败，请检查 API Key、合约权限和出口 IP 白名单。" },
            },
          });
        }
      });
  }, []);

  // 启动时从 settings 读取全局交易对和 networkTab。
  useEffect(() => {
    const requestedNetworkRevision = networkRevision.current;
    getSettings()
      .then(({ data }) => {
        if (data.symbol && symbolRequestId.current === 0) {
          activeSymbol.current = data.symbol;
          clearTicker();
          dispatch({ type: "SET_SYMBOL", payload: data.symbol });
        }
        if (networkRevision.current === requestedNetworkRevision) {
          dispatch({ type: "SET_NETWORK_TAB", payload: data.testnet !== false ? "test" : "main" });
        }
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
            strategyStatusUncertain.current = false;
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
    if (msg?.type === "account" || msg?.type === "account_error") accountRevision.current += 1;
    if (msg?.type === "bot_status") {
      botStatusRevision.current += 1;
      strategyStatusUncertain.current = false;
    }
    dispatch({ type: "WS_MSG", payload: msg });
  }, []);
  const handleConnectionChange = useCallback((connected) => {
    dispatch({ type: "SET_CONNECTED", payload: connected });
  }, []);

  useWebSocket(handleMessage, handleConnectionChange);

  const setSymbol = useCallback((sym) => {
    if (
      refreshInFlight.current
      || networkSwitchInFlight.current
      || strategyCommandInFlight.current
      || strategyStatusUncertain.current
    ) return Promise.resolve(false);
    const requestId = ++symbolRequestId.current;
    pendingTicker.current = null;
    switchingSymbol.current = true;
    dispatch({ type: "SET_SYMBOL_SWITCHING", payload: true });
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
        if (requestId === symbolRequestId.current) {
          switchingSymbol.current = false;
          dispatch({ type: "SET_SYMBOL_SWITCHING", payload: false });
        }
      }
    });
    symbolSaveQueue.current = save;
    return save;
  }, []);
  const setConnected = (v)   => dispatch({ type: "SET_CONNECTED", payload: v   });
  const setStrategyCapitalLimit = useCallback((value) => {
    dispatch({ type: "SET_STRATEGY_CAPITAL_LIMIT", payload: value });
  }, []);

  const refreshBotStatus = useCallback(async () => {
    const requestedAtRevision = botStatusRevision.current;
    const { data } = await getEngineStatus();
    if (botStatusRevision.current === requestedAtRevision) {
      strategyStatusUncertain.current = false;
      dispatch({ type: "SET_BOT_STATUS", payload: data });
    }
    return data;
  }, []);

  const refreshAll = useCallback(() => {
    if (refreshInFlight.current) return refreshInFlight.current;
    if (
      networkSwitchInFlight.current
      || strategyCommandInFlight.current
      || switchingSymbol.current
    ) {
      return Promise.resolve(false);
    }

    accountRevision.current += 1;
    botStatusRevision.current += 1;
    const requestedAccountRevision = accountRevision.current;
    const requestedBotStatusRevision = botStatusRevision.current;
    dispatch({ type: "SET_REFRESH_PENDING", payload: true });
    dispatch({ type: "SET_REFRESH_ERROR", payload: null });

    const request = Promise.allSettled([getAccountBalance(), getEngineStatus()])
      .then(([accountResult, botStatusResult]) => {
        if (
          accountResult.status === "fulfilled"
          && accountRevision.current === requestedAccountRevision
        ) {
          dispatch({
            type: "WS_MSG",
            payload: { type: "account", data: accountResult.value.data },
          });
        }
        if (
          botStatusResult.status === "fulfilled"
          && botStatusRevision.current === requestedBotStatusRevision
        ) {
          strategyStatusUncertain.current = false;
          dispatch({ type: "SET_BOT_STATUS", payload: botStatusResult.value.data });
        }
        if (accountResult.status === "rejected" || botStatusResult.status === "rejected") {
          dispatch({
            type: "SET_REFRESH_ERROR",
            payload: "部分数据刷新失败，请稍后重试。",
          });
        }
        dispatch({ type: "COMPLETE_REFRESH" });
        return accountResult.status === "fulfilled" && botStatusResult.status === "fulfilled";
      })
      .finally(() => {
        refreshInFlight.current = null;
      });

    refreshInFlight.current = request;
    return request;
  }, []);

  const runStrategyCommand = useCallback(async (command, mainnetConfirmation) => {
    if (
      strategyCommandInFlight.current
      || networkSwitchInFlight.current
      || refreshInFlight.current
      || switchingSymbol.current
      || strategyStatusUncertain.current
    ) return false;

    const capitalLimit = Number(state.strategyCapitalLimit);
    if (command === "start" && !(capitalLimit > 0)) {
      dispatch({ type: "SET_STRATEGY_COMMAND_ERROR", payload: "资金上限必须大于 0" });
      return false;
    }
    if (
      command === "start"
      && state.networkTab === "main"
      && mainnetConfirmation !== `MAINNET:${state.symbol}`
    ) {
      dispatch({ type: "SET_STRATEGY_COMMAND_ERROR", payload: "真实网确认文本不匹配" });
      return false;
    }

    strategyCommandInFlight.current = true;
    dispatch({ type: "SET_STRATEGY_COMMAND_PENDING", payload: true });
    dispatch({ type: "SET_STRATEGY_COMMAND_ERROR", payload: null });
    try {
      if (command === "stop") {
        await stopEngine();
      } else {
        await startEngine({
          strategy_type: "sar_adx_pyramid",
          config_version: "sar_adx_v3",
          symbol: state.symbol,
          capital_limit: capitalLimit,
          ...(state.networkTab === "main"
            ? { mainnet_confirmation: mainnetConfirmation }
            : {}),
        });
      }
      botStatusRevision.current += 1;
      const requestedStatusRevision = botStatusRevision.current;
      try {
        await refreshBotStatus();
      } catch {
        if (botStatusRevision.current === requestedStatusRevision) {
          strategyStatusUncertain.current = true;
          dispatch({ type: "SET_STRATEGY_STATUS_UNCERTAIN", payload: true });
          dispatch({
            type: "SET_STRATEGY_COMMAND_ERROR",
            payload: "策略命令已提交，但状态刷新失败，请手动刷新确认。",
          });
        }
      }
      return true;
    } catch (error) {
      const detail = error?.response?.data?.detail;
      dispatch({
        type: "SET_STRATEGY_COMMAND_ERROR",
        payload: typeof detail === "string" ? detail : "策略操作失败，请稍后重试。",
      });
      return false;
    } finally {
      strategyCommandInFlight.current = false;
      dispatch({ type: "SET_STRATEGY_COMMAND_PENDING", payload: false });
    }
  }, [refreshBotStatus, state.networkTab, state.strategyCapitalLimit, state.symbol]);

  const startStrategy = useCallback(
    (mainnetConfirmation) => runStrategyCommand("start", mainnetConfirmation),
    [runStrategyCommand],
  );
  const stopStrategy = useCallback(() => runStrategyCommand("stop"), [runStrategyCommand]);

  // 切换测试网 / 真实网，同步写入后端设置
  const switchNetwork = useCallback(async (tab) => {
    if (
      networkSwitchInFlight.current
      || strategyCommandInFlight.current
      || refreshInFlight.current
      || switchingSymbol.current
      || strategyStatusUncertain.current
      || tab === state.networkTab
    ) return;
    networkSwitchInFlight.current = true;
    networkRevision.current += 1;
    accountRevision.current += 1;
    dispatch({ type: "SET_NETWORK_SWITCHING", payload: true });
    dispatch({ type: "SET_NETWORK_ERROR", payload: null });
    try {
      const { data } = await saveSettings({ testnet: tab === "test" });
      if (typeof data?.testnet !== "boolean") {
        throw new Error("Network response did not include testnet state");
      }
      dispatch({ type: "SET_NETWORK_SESSION", payload: data });
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
    <AppContext.Provider value={{
      ...state,
      setSymbol,
      setConnected,
      setStrategyCapitalLimit,
      startStrategy,
      stopStrategy,
      switchNetwork,
      refreshAll,
      dispatch,
    }}>
      {children}
    </AppContext.Provider>
  );
}

export const useApp = () => useContext(AppContext);
