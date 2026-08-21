import { useCallback, useEffect, useRef, useState } from "react";
import { getMarketAgentEvents, getMarketAgentStatus } from "../api/client";
import {
  isCompleteMarketAgentEvent,
  marketAgentEventFromMessage,
  subscribeRealtimeEvent,
} from "../services/realtimeEvents";

const PAGE_SIZE = 100;
const RECONCILE_INTERVAL_MS = 30_000;
const EMPTY_STATUS = { state: "stopped", enabled: false, desired_enabled: false };

function cancelled(error) {
  return error?.code === "ERR_CANCELED" || error?.name === "CanceledError";
}

function eventType(event) {
  if (event.type && event.type !== "market_agent_event") return event.type;
  if (event.event_type) return event.event_type;
  return event.role === "user" ? "user_message" : "analysis";
}

export function normalizeMarketAgentEvent(raw, fallback = {}) {
  if (!raw || typeof raw !== "object") return null;
  const context = raw.context && typeof raw.context === "object" ? raw.context : {};
  const sequence = Number(raw.sequence);
  const normalizedSequence = Number.isInteger(sequence) && sequence > 0 ? sequence : null;
  const type = eventType(raw);
  const role = raw.role || (type === "user_message" ? "user" : "assistant");
  const content = String(raw.content ?? raw.answer ?? "").trim();
  const agentId = raw.agent_id ?? fallback.agentId ?? null;
  const symbol = raw.symbol ?? fallback.symbol ?? null;
  const clientMessageId = raw.client_message_id
    ?? raw.reply_to_client_message_id
    ?? context.client_message_id
    ?? context.reply_to_client_message_id
    ?? null;
  const jobId = raw.job_id ?? raw.analysis_job_id ?? context.job_id ?? context.analysis_job_id ?? null;
  return {
    ...raw,
    type,
    role,
    content,
    sequence: normalizedSequence,
    agent_id: agentId,
    symbol,
    client_message_id: clientMessageId,
    job_id: jobId,
    reasons: Array.isArray(raw.reasons) ? raw.reasons : [],
    context,
    _key: normalizedSequence
      ? `${agentId || "legacy"}:${normalizedSequence}`
      : `${agentId || "legacy"}:${raw.batch_id || clientMessageId || jobId || `${type}:${raw.created_at || content}`}`,
  };
}

function mergeEvents(current, incoming) {
  const byKey = new Map(current.map((event) => [event._key, event]));
  incoming.forEach((event) => {
    if (!event) return;
    byKey.set(event._key, { ...byKey.get(event._key), ...event });
  });
  return [...byKey.values()].sort((left, right) => {
    if (left.sequence && right.sequence) return left.sequence - right.sequence;
    if (left.sequence) return -1;
    if (right.sequence) return 1;
    return String(left.created_at || "").localeCompare(String(right.created_at || ""));
  });
}

function requestErrorMessage(error) {
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  return error?.message || "实时解读暂时无法同步。";
}

export function useMarketAgentFeed({ symbol, connected }) {
  const [status, setStatus] = useState(EMPTY_STATUS);
  const [events, setEvents] = useState([]);
  const [feedState, setFeedState] = useState("idle");
  const [error, setError] = useState("");
  const [historyTruncated, setHistoryTruncated] = useState(false);
  const statusRef = useRef(EMPTY_STATUS);
  const symbolRef = useRef(symbol);
  const agentIdRef = useRef(null);
  const cursorRef = useRef(0);
  const eventsRef = useRef([]);
  const requestIdRef = useRef(0);
  const controllerRef = useRef(null);
  const recoveryRef = useRef(null);
  const recoveryRequestedRef = useRef(false);
  const mountedRef = useRef(true);
  const previousConnectedRef = useRef(connected);

  const commitEvents = useCallback((incoming, reset = false) => {
    const next = mergeEvents(reset ? [] : eventsRef.current, incoming);
    eventsRef.current = next;
    cursorRef.current = next.reduce(
      (maximum, event) => Math.max(maximum, event.sequence || 0),
      reset ? 0 : cursorRef.current,
    );
    setEvents(next);
  }, []);

  const runRecovery = useCallback(async ({ bootstrap = false, refreshStatus = true, silent = false } = {}) => {
    const requestedSymbol = symbolRef.current;
    const requestId = ++requestIdRef.current;
    const controller = new AbortController();
    controllerRef.current = controller;
    if (!silent) setFeedState(bootstrap ? "bootstrapping" : "catching_up");

    const currentRequest = () => mountedRef.current
      && !controller.signal.aborted
      && requestId === requestIdRef.current
      && requestedSymbol === symbolRef.current;

    try {
      let currentStatus = statusRef.current;
      if (refreshStatus) {
        const response = await getMarketAgentStatus(controller.signal);
        if (!currentRequest()) return;
        currentStatus = response.data || EMPTY_STATUS;
        statusRef.current = currentStatus;
        setStatus(currentStatus);
      }

      const statusSymbol = currentStatus?.symbol || null;
      const nextAgentId = currentStatus?.agent_id || null;
      if (!nextAgentId || statusSymbol !== requestedSymbol) {
        agentIdRef.current = nextAgentId;
        cursorRef.current = 0;
        eventsRef.current = [];
        setEvents([]);
        setHistoryTruncated(false);
        setError("");
        setFeedState("ready");
        return;
      }

      if (agentIdRef.current !== nextAgentId) {
        agentIdRef.current = nextAgentId;
        cursorRef.current = 0;
        eventsRef.current = [];
        setEvents([]);
        setHistoryTruncated(false);
      }

      let cursor = cursorRef.current;
      let latestSequence = Number(currentStatus.latest_sequence) || cursor;
      let pages = 0;
      do {
        const response = await getMarketAgentEvents(cursor, PAGE_SIZE, controller.signal);
        if (!currentRequest()) return;
        const payload = response.data || {};
        const rawEvents = Array.isArray(payload.events)
          ? payload.events
          : Array.isArray(payload) ? payload : [];
        const normalized = rawEvents
          .map((event) => normalizeMarketAgentEvent(event, { agentId: nextAgentId, symbol: requestedSymbol }))
          .filter((event) => event
            && (!event.symbol || event.symbol === requestedSymbol)
            && (!event.agent_id || event.agent_id === nextAgentId));
        const sequenced = normalized.filter((event) => event.sequence);
        if (cursor > 0 && sequenced.length && sequenced[0].sequence > cursor + 1) {
          setHistoryTruncated(true);
        }
        commitEvents(normalized);
        const nextCursor = sequenced.reduce(
          (maximum, event) => Math.max(maximum, event.sequence),
          cursor,
        );
        latestSequence = Math.max(Number(payload.latest_sequence) || 0, latestSequence);
        pages += 1;
        if (nextCursor === cursor || rawEvents.length === 0) break;
        cursor = nextCursor;
      } while (cursor < latestSequence && pages < 20);

      if (!currentRequest()) return;
      const incomplete = cursorRef.current < latestSequence;
      setHistoryTruncated((current) => current || incomplete);
      setError(incomplete ? "部分历史解读已过期，当前显示服务端仍保留的内容。" : "");
      setFeedState(incomplete ? "stale" : "ready");
    } catch (requestError) {
      if (!currentRequest() || cancelled(requestError)) return;
      setError(requestErrorMessage(requestError));
      setFeedState(eventsRef.current.length ? "stale" : "error");
    }
  }, [commitEvents]);

  const recover = useCallback((options = {}) => {
    if (recoveryRef.current) {
      recoveryRequestedRef.current = true;
      return recoveryRef.current;
    }
    const recovery = runRecovery(options).finally(() => {
      if (recoveryRef.current !== recovery) return;
      recoveryRef.current = null;
      if (recoveryRequestedRef.current) {
        recoveryRequestedRef.current = false;
        recover({ refreshStatus: true });
      }
    });
    recoveryRef.current = recovery;
    return recovery;
  }, [runRecovery]);

  const mergeRealtimeEvent = useCallback((rawEvent) => {
    const event = normalizeMarketAgentEvent(rawEvent, {
      agentId: agentIdRef.current,
      symbol: symbolRef.current,
    });
    if (!event || (event.symbol && event.symbol !== symbolRef.current)) return false;
    if (agentIdRef.current && event.agent_id && event.agent_id !== agentIdRef.current) return false;
    if (!event.sequence) {
      commitEvents([event]);
      return true;
    }
    if (event.sequence <= cursorRef.current) return true;
    if (event.sequence !== cursorRef.current + 1) return false;
    commitEvents([event]);
    return true;
  }, [commitEvents]);

  useEffect(() => {
    mountedRef.current = true;
    symbolRef.current = symbol;
    requestIdRef.current += 1;
    controllerRef.current?.abort();
    recoveryRef.current = null;
    recoveryRequestedRef.current = false;
    agentIdRef.current = null;
    cursorRef.current = 0;
    eventsRef.current = [];
    setStatus(EMPTY_STATUS);
    statusRef.current = EMPTY_STATUS;
    setEvents([]);
    setError("");
    setHistoryTruncated(false);
    setFeedState("bootstrapping");
    recover({ bootstrap: true, refreshStatus: true });

    const unsubscribe = subscribeRealtimeEvent((message) => {
      if (message?.type === "market_agent_status") {
        recover({ refreshStatus: true });
        return;
      }
      if (message?.type !== "market_agent_event") return;
      const payload = marketAgentEventFromMessage(message);
      if (payload?.symbol && payload.symbol !== symbolRef.current) return;
      if (isCompleteMarketAgentEvent(message) && mergeRealtimeEvent(payload)) return;
      recover({ refreshStatus: true });
    });
    const onOnline = () => recover({ refreshStatus: true });
    window.addEventListener("online", onOnline);
    const timer = window.setInterval(
      () => recover({ refreshStatus: true, silent: true }),
      RECONCILE_INTERVAL_MS,
    );
    return () => {
      mountedRef.current = false;
      requestIdRef.current += 1;
      controllerRef.current?.abort();
      window.clearInterval(timer);
      window.removeEventListener("online", onOnline);
      unsubscribe();
    };
  }, [mergeRealtimeEvent, recover, symbol]);

  useEffect(() => {
    const wasConnected = previousConnectedRef.current;
    previousConnectedRef.current = connected;
    if (connected && wasConnected === false) recover({ refreshStatus: true });
  }, [connected, recover]);

  return {
    status,
    events,
    feedState,
    error,
    historyTruncated,
    refresh: recover,
    clearError: () => setError(""),
  };
}
