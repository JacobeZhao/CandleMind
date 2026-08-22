import React, { createContext, useContext, useReducer, useCallback, useEffect, useRef } from "react";
import { useWebSocket } from "../hooks/useWebSocket";
import {
  getAccountBalance,
  getEngineStatus,
  getSettings,
  getStrategyConfig,
  saveSettings,
  startEngine,
  stopEngine,
} from "../api/client";
import { normalizeApiError } from "../api/errors";
import { normalizeConfiguration } from "../strategies/catalog";
import { clearTicker, publishTicker } from "./MarketTickerContext";
import { publishRealtimeEvent } from "../services/realtimeEvents";
import { refreshMountedReaders } from "../services/refreshCoordinator";
import { isExchangeAvailable, normalizeExchangeProvider } from "../exchanges/catalog";

const AppContext = createContext(null);

const initial = {
  settingsLoaded: false,
  exchangeProvider: "binance",
  exchangeSwitching: false,
  exchangeError: null,
  transportConnected: false,
  binanceConnected: false,
  account:    null,
  accountError: null,
  accountPhase: "loading",
  positions:  [],
  positionsError: null,
  positionsPhase: "loading",
  openOrders: [],
  botStatus:  null,
  botStatusLoaded: false,
  strategyCapitalLimit: "1000",
  strategyConfiguration: null,
  strategyConfigurationLoaded: false,
  strategyConfigurationError: null,
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
    case "SET_SYMBOL": return {
      ...state,
      symbol: action.payload,
      positions: [],
      positionsError: null,
      positionsPhase: "loading",
    };
    case "SET_SYMBOL_SWITCHING": return { ...state, symbolSwitching: action.payload };
    case "SET_TRANSPORT_CONNECTED": return state.transportConnected === action.payload
      ? state
      : { ...state, transportConnected: action.payload };
    case "SET_BINANCE_CONNECTED": return state.binanceConnected === action.payload
      ? state
      : { ...state, binanceConnected: action.payload };
    case "SET_INITIAL_SETTINGS": return {
      ...state,
      settingsLoaded: true,
      exchangeProvider: action.payload.exchangeProvider,
      binanceConnected: action.payload.binanceConnected,
      networkTab: action.payload.networkTab ?? state.networkTab,
      symbol: action.payload.symbol || state.symbol,
    };
    case "BEGIN_EXCHANGE_SWITCH": return {
      ...state,
      exchangeProvider: action.payload,
      exchangeSwitching: true,
      exchangeError: null,
      account: null,
      accountError: null,
      accountPhase: "loading",
      positions: [],
      positionsError: null,
      positionsPhase: "loading",
      openOrders: [],
      botStatus: null,
      botStatusLoaded: false,
      refreshError: null,
      refreshPending: false,
    };
    case "COMPLETE_EXCHANGE_SWITCH": return {
      ...state,
      exchangeProvider: action.payload.exchangeProvider,
      exchangeSwitching: false,
      exchangeError: null,
      binanceConnected: action.payload.binanceConnected,
      networkTab: action.payload.networkTab ?? state.networkTab,
      symbol: action.payload.symbol || state.symbol,
    };
    case "ROLLBACK_EXCHANGE_SWITCH": return {
      ...state,
      exchangeProvider: action.payload.exchangeProvider,
      exchangeSwitching: false,
      exchangeError: action.payload.message,
      binanceConnected: action.payload.binanceConnected,
    };
    case "SET_NETWORK_TAB": return { ...state, networkTab: action.payload };
    case "SET_NETWORK_SWITCHING": return { ...state, networkSwitching: action.payload };
    case "SET_NETWORK_ERROR": return { ...state, networkError: action.payload };
    case "SET_NETWORK_SESSION": return {
      ...state,
      networkTab: action.payload.testnet ? "test" : "main",
      binanceConnected: Boolean(action.payload.connected),
      account: action.payload.account ?? null,
      accountError: action.payload.account ? null : "账户数据尚未就绪。",
      accountPhase: action.payload.account ? "complete" : "error",
      positions: [],
      positionsError: null,
      positionsPhase: "loading",
      openOrders: [],
    };
    case "SET_BOT_STATUS":  return {
      ...state,
      botStatus: action.payload,
      botStatusLoaded: true,
      strategyStatusUncertain: false,
    };
    case "SET_STRATEGY_CAPITAL_LIMIT": return { ...state, strategyCapitalLimit: action.payload };
    case "SET_STRATEGY_CONFIGURATION": return {
      ...state,
      strategyConfiguration: action.payload,
      strategyConfigurationLoaded: true,
      strategyConfigurationError: null,
    };
    case "SET_STRATEGY_CONFIGURATION_ERROR": return {
      ...state,
      strategyConfiguration: null,
      strategyConfigurationLoaded: true,
      strategyConfigurationError: action.payload,
    };
    case "SET_STRATEGY_COMMAND_PENDING": return { ...state, strategyCommandPending: action.payload };
    case "SET_STRATEGY_COMMAND_ERROR": return { ...state, strategyCommandError: action.payload };
    case "SET_STRATEGY_STATUS_UNCERTAIN": return {
      ...state,
      strategyStatusUncertain: action.payload,
    };
    case "SET_REFRESH_PENDING": return { ...state, refreshPending: action.payload };
    case "SET_REFRESH_ERROR": return { ...state, refreshError: action.payload };
    case "SET_ACCOUNT_LOADING": return {
      ...state,
      accountPhase: state.account ? "refreshing" : "loading",
      accountError: null,
    };
    case "SET_ACCOUNT_FAILURE": return {
      ...state,
      account: action.payload.clear ? null : state.account,
      accountPhase: !action.payload.clear && state.account ? "stale" : "error",
      accountError: action.payload.message,
    };
    case "SET_POSITIONS_FAILURE": return {
      ...state,
      positions: action.payload.clear ? [] : state.positions,
      positionsPhase: !action.payload.clear && state.positions.length ? "stale" : "error",
      positionsError: action.payload.message,
    };
    case "COMPLETE_REFRESH": return {
      ...state,
      refreshRevision: state.refreshRevision + 1,
      refreshPending: false,
    };
    case "WS_MSG": {
      const { type, data } = action.payload;
      if (type === "account") return {
        ...state,
        account: data,
        accountError: null,
        accountPhase: data ? "complete" : "empty",
      };
      if (type === "positions") return {
        ...state,
        positions: Array.isArray(data) ? data : [],
        positionsError: null,
        positionsPhase: Array.isArray(data) && data.length ? "complete" : "empty",
      };
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
  const activeExchange = useRef(initial.exchangeProvider);
  const exchangeRevision = useRef(0);
  const exchangeSwitchInFlight = useRef(false);
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
  const accountRefreshInFlight = useRef(null);

  const loadStrategyConfiguration = useCallback(async () => {
    try {
      const { data } = await getStrategyConfig();
      const configuration = normalizeConfiguration(data);
      if (!configuration) throw new Error("Invalid strategy configuration response");
      dispatch({ type: "SET_STRATEGY_CONFIGURATION", payload: configuration });
      return configuration;
    } catch (error) {
      const detail = error?.response?.data?.detail;
      dispatch({
        type: "SET_STRATEGY_CONFIGURATION_ERROR",
        payload: typeof detail === "string" ? detail : "策略配置读取失败，请前往策略页面重试。",
      });
      return null;
    }
  }, []);

  const setStrategyConfiguration = useCallback((configuration) => {
    const normalized = normalizeConfiguration(configuration);
    if (normalized) dispatch({ type: "SET_STRATEGY_CONFIGURATION", payload: normalized });
  }, []);

  useEffect(() => {
    activeSymbol.current = state.symbol;
  }, [state.symbol]);

  useEffect(() => {
    activeExchange.current = state.exchangeProvider;
  }, [state.exchangeProvider]);

  useEffect(() => {
    const timer = window.setInterval(() => {
      const data = pendingTicker.current;
      pendingTicker.current = null;
      if (
        activeExchange.current !== "binance"
        || !data
        || data.symbol !== activeSymbol.current
        || switchingSymbol.current
      ) return;
      publishTicker(data);
    }, 500);
    return () => {
      window.clearInterval(timer);
      pendingTicker.current = null;
    };
  }, []);

  useEffect(() => {
    if (isExchangeAvailable(state.exchangeProvider)) {
      loadStrategyConfiguration();
    }
  }, [loadStrategyConfiguration, state.exchangeProvider]);

  const refreshAccount = useCallback(() => {
    if (activeExchange.current !== "binance") return Promise.resolve(true);
    if (accountRefreshInFlight.current) return accountRefreshInFlight.current;
    accountRevision.current += 1;
    const requestedAtRevision = accountRevision.current;
    const requestedExchangeRevision = exchangeRevision.current;
    dispatch({ type: "SET_ACCOUNT_LOADING" });
    const request = getAccountBalance()
      .then(({ data }) => {
        if (
          activeExchange.current === "binance"
          && exchangeRevision.current === requestedExchangeRevision
          && accountRevision.current === requestedAtRevision
        ) {
          dispatch({ type: "WS_MSG", payload: { type: "account", data } });
        }
        return true;
      })
      .catch((error) => {
        if (
          activeExchange.current === "binance"
          && exchangeRevision.current === requestedExchangeRevision
          && accountRevision.current === requestedAtRevision
        ) {
          const parsed = normalizeApiError(error, "Binance 账户读取失败，请稍后重试。");
          dispatch({
            type: "SET_ACCOUNT_FAILURE",
            payload: { message: parsed.message, clear: !parsed.retryable },
          });
        }
        return false;
      })
      .finally(() => {
        if (accountRefreshInFlight.current === request) accountRefreshInFlight.current = null;
      });
    accountRefreshInFlight.current = request;
    return request;
  }, []);

  useEffect(() => {
    if (state.settingsLoaded && state.exchangeProvider === "binance") refreshAccount();
  }, [refreshAccount, state.exchangeProvider, state.settingsLoaded]);

  // 启动时从 settings 读取全局交易所、交易对和 networkTab。
  useEffect(() => {
    const requestedNetworkRevision = networkRevision.current;
    const requestedExchangeRevision = exchangeRevision.current;
    getSettings()
      .then(({ data }) => {
        if (exchangeRevision.current !== requestedExchangeRevision) return;
        const exchangeProvider = normalizeExchangeProvider(data.exchange_provider);
        activeExchange.current = exchangeProvider;
        if (data.symbol && symbolRequestId.current === 0) {
          activeSymbol.current = data.symbol;
          clearTicker();
        }
        dispatch({
          type: "SET_INITIAL_SETTINGS",
          payload: {
            exchangeProvider,
            binanceConnected: Boolean(data.connected),
            networkTab: networkRevision.current === requestedNetworkRevision
              ? (data.testnet === false ? "main" : "test")
              : undefined,
            symbol: data.symbol,
          },
        });
      })
      .catch(() => {
        if (exchangeRevision.current === requestedExchangeRevision) {
          dispatch({
            type: "SET_INITIAL_SETTINGS",
            payload: {
              exchangeProvider: "binance",
              binanceConnected: false,
              networkTab: "test",
              symbol: initial.symbol,
            },
          });
        }
      });
  }, []);

  useEffect(() => {
    if (state.exchangeProvider !== "binance") return undefined;
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
  }, [state.exchangeProvider]);

  const handleMessage = useCallback((msg) => {
    if (activeExchange.current !== "binance") return;
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
    if (msg?.type === "account" || msg?.type === "account_error") {
      accountRevision.current += 1;
      if (msg.type === "account_error") {
        const parsed = normalizeApiError(
          { response: { status: msg.data?.status, data: { detail: msg.data } } },
          "Binance 账户读取失败，请稍后重试。",
        );
        dispatch({
          type: "SET_ACCOUNT_FAILURE",
          payload: { message: parsed.message, clear: !parsed.retryable },
        });
        return;
      }
    }
    if (msg?.type === "positions_error") {
      const parsed = normalizeApiError(
        { response: { status: msg.data?.status, data: { detail: msg.data } } },
        "Binance 持仓读取失败，请稍后重试。",
      );
      dispatch({
        type: "SET_POSITIONS_FAILURE",
        payload: { message: parsed.message, clear: !parsed.retryable },
      });
      return;
    }
    if (msg?.type === "bot_status") {
      botStatusRevision.current += 1;
      strategyStatusUncertain.current = false;
    }
    dispatch({ type: "WS_MSG", payload: msg });
  }, []);
  const handleConnectionChange = useCallback((connected) => {
    dispatch({ type: "SET_TRANSPORT_CONNECTED", payload: connected });
  }, []);

  useWebSocket(handleMessage, handleConnectionChange);

  const setSymbol = useCallback((sym) => {
    if (
      activeExchange.current !== "binance"
      || exchangeSwitchInFlight.current
      || refreshInFlight.current
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
  const setBinanceConnected = useCallback((value) => {
    dispatch({ type: "SET_BINANCE_CONNECTED", payload: Boolean(value) });
  }, []);
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
    if (activeExchange.current !== "binance") return Promise.resolve(false);
    if (refreshInFlight.current) return refreshInFlight.current;
    if (
      networkSwitchInFlight.current
      || strategyCommandInFlight.current
      || switchingSymbol.current
    ) {
      return Promise.resolve(false);
    }

    botStatusRevision.current += 1;
    const requestedBotStatusRevision = botStatusRevision.current;
    const requestedExchangeRevision = exchangeRevision.current;
    dispatch({ type: "SET_REFRESH_PENDING", payload: true });
    dispatch({ type: "SET_REFRESH_ERROR", payload: null });

    const request = Promise.allSettled([
      refreshAccount(),
      getEngineStatus(),
      refreshMountedReaders(),
    ])
      .then(([accountResult, botStatusResult, mountedResult]) => {
        if (exchangeRevision.current !== requestedExchangeRevision || activeExchange.current !== "binance") {
          return false;
        }
        if (
          botStatusResult.status === "fulfilled"
          && botStatusRevision.current === requestedBotStatusRevision
        ) {
          strategyStatusUncertain.current = false;
          dispatch({ type: "SET_BOT_STATUS", payload: botStatusResult.value.data });
        }
        const accountFailed = accountResult.status === "rejected" || accountResult.value === false;
        const mountedFailed = mountedResult.status === "rejected"
          || mountedResult.value.some((result) => result.status === "rejected" || result.value === false);
        if (accountFailed || botStatusResult.status === "rejected" || mountedFailed) {
          dispatch({
            type: "SET_REFRESH_ERROR",
            payload: "部分数据刷新失败，请稍后重试。",
          });
        }
        dispatch({ type: "COMPLETE_REFRESH" });
        return !accountFailed && botStatusResult.status === "fulfilled" && !mountedFailed;
      })
      .finally(() => {
        if (refreshInFlight.current === request) refreshInFlight.current = null;
      });

    refreshInFlight.current = request;
    return request;
  }, [refreshAccount]);

  const runStrategyCommand = useCallback(async (command, mainnetConfirmation) => {
    if (
      activeExchange.current !== "binance"
      || exchangeSwitchInFlight.current
      || strategyCommandInFlight.current
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
    if (command === "start" && !state.strategyConfiguration) {
      dispatch({ type: "SET_STRATEGY_COMMAND_ERROR", payload: "策略配置尚未加载，无法启动。" });
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
          strategy_type: state.strategyConfiguration.strategy_type,
          config_version: state.strategyConfiguration.config_version,
          config_hash: state.strategyConfiguration.config_hash,
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
  }, [
    refreshBotStatus,
    state.networkTab,
    state.strategyCapitalLimit,
    state.strategyConfiguration,
    state.symbol,
  ]);

  const startStrategy = useCallback(
    (mainnetConfirmation) => runStrategyCommand("start", mainnetConfirmation),
    [runStrategyCommand],
  );
  const stopStrategy = useCallback(() => runStrategyCommand("stop"), [runStrategyCommand]);

  // 切换测试网 / 真实网，同步写入后端设置
  const switchNetwork = useCallback(async (tab) => {
    if (
      activeExchange.current !== "binance"
      || exchangeSwitchInFlight.current
      || networkSwitchInFlight.current
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

  const switchExchange = useCallback(async (provider) => {
    const nextProvider = normalizeExchangeProvider(provider);
    const strategyKnownStopped = activeExchange.current !== "binance" || (
      state.botStatusLoaded
      && !strategyStatusUncertain.current
      && state.botStatus?.engine_state === "stopped"
      && !state.botStatus?.running
    );
    if (
      exchangeSwitchInFlight.current
      || networkSwitchInFlight.current
      || strategyCommandInFlight.current
      || switchingSymbol.current
      || !strategyKnownStopped
      || nextProvider === activeExchange.current
    ) return false;

    const previousProvider = activeExchange.current;
    const previousBinanceConnected = state.binanceConnected;
    const requestedRevision = ++exchangeRevision.current;
    exchangeSwitchInFlight.current = true;
    activeExchange.current = nextProvider;
    accountRevision.current += 1;
    botStatusRevision.current += 1;
    symbolRequestId.current += 1;
    accountRefreshInFlight.current = null;
    refreshInFlight.current = null;
    pendingTicker.current = null;
    clearTicker();
    dispatch({ type: "BEGIN_EXCHANGE_SWITCH", payload: nextProvider });

    try {
      const { data } = await saveSettings({ exchange_provider: nextProvider });
      if (exchangeRevision.current !== requestedRevision) return false;
      const authoritativeProvider = normalizeExchangeProvider(data?.exchange_provider ?? nextProvider);
      activeExchange.current = authoritativeProvider;
      if (data?.symbol) activeSymbol.current = data.symbol;
      dispatch({
        type: "COMPLETE_EXCHANGE_SWITCH",
        payload: {
          exchangeProvider: authoritativeProvider,
          binanceConnected: authoritativeProvider === "binance" && Boolean(data?.connected),
          networkTab: typeof data?.testnet === "boolean" ? (data.testnet ? "test" : "main") : undefined,
          symbol: data?.symbol,
        },
      });
      return true;
    } catch (error) {
      if (exchangeRevision.current !== requestedRevision) return false;
      activeExchange.current = previousProvider;
      const detail = error?.response?.data?.detail;
      dispatch({
        type: "ROLLBACK_EXCHANGE_SWITCH",
        payload: {
          exchangeProvider: previousProvider,
          binanceConnected: previousBinanceConnected,
          message: typeof detail === "string" ? detail : detail?.message || "交易所切换失败，请稍后重试。",
        },
      });
      return false;
    } finally {
      if (exchangeRevision.current === requestedRevision) exchangeSwitchInFlight.current = false;
    }
  }, [state.binanceConnected, state.botStatus, state.botStatusLoaded]);

  const connected = state.exchangeProvider === "binance"
    && state.binanceConnected
    && state.transportConnected;

  const exchangeSupported = isExchangeAvailable(state.exchangeProvider);

  return (
    <AppContext.Provider value={{
      ...state,
      connected,
      exchangeConnected: connected,
      exchangeSupported,
      setSymbol,
      setConnected: setBinanceConnected,
      setBinanceConnected,
      setStrategyCapitalLimit,
      loadStrategyConfiguration,
      setStrategyConfiguration,
      startStrategy,
      stopStrategy,
      switchNetwork,
      switchExchange,
      refreshAll,
      refreshAccount,
      dispatch,
    }}>
      {children}
    </AppContext.Provider>
  );
}

export const useApp = () => useContext(AppContext);
