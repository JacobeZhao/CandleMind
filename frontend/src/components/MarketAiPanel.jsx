import React, { useCallback, useEffect, useRef, useState } from "react";
import {
  AlertCircle,
  Bot,
  Loader,
  MessageSquare,
  Play,
  RotateCcw,
  Send,
  Square,
  StopCircle,
  X,
} from "lucide-react";
import {
  getMarketAgentEvents,
  getMarketAgentStatus,
  sendMarketAgentMessage,
  startMarketAgent,
  stopMarketAgent,
} from "../api/client";
import { subscribeRealtimeEvent } from "../services/realtimeEvents";

const QUICK_QUESTIONS = ["现在的市场周期是什么？", "当前有没有可以交易的机会？"];
const ACTIVE_STATES = new Set(["starting", "running", "stopping"]);
const REASON_LABELS = {
  candle_closed: "K线收盘",
  large_candle: "大K线",
  sar_reversal: "SAR转向",
};

function errorMessage(error) {
  if (error?.code === "ERR_CANCELED" || error?.name === "CanceledError") return "";
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  return error?.message || "AI 分析暂时不可用，请稍后重试。";
}

function eventList(data) {
  const value = data?.events ?? data;
  return Array.isArray(value) ? value : [];
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

export default function MarketAiPanel({ onClose, symbol }) {
  const [agentStatus, setAgentStatus] = useState({ state: "stopped", enabled: false });
  const [agentEvents, setAgentEvents] = useState([]);
  const [agentBusy, setAgentBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const inputRef = useRef(null);
  const feedEndRef = useRef(null);
  const controllerRef = useRef(null);
  const generationRef = useRef(0);
  const lastQuestionRef = useRef("");
  const confirmedHistoryRef = useRef([]);

  const refreshAgent = useCallback(async () => {
    try {
      const { data: status } = await getMarketAgentStatus();
      setAgentStatus(status || { state: "stopped", enabled: false });
      const { data } = await getMarketAgentEvents(0, 100);
      setAgentEvents(eventList(data));
    } catch (requestError) {
      setError(errorMessage(requestError));
    }
  }, []);

  const cancel = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setLoading(false);
  }, []);

  const close = useCallback(() => {
    cancel();
    onClose();
  }, [cancel, onClose]);

  useEffect(() => {
    refreshAgent();
    const timer = window.setInterval(refreshAgent, 10_000);
    const unsubscribe = subscribeRealtimeEvent((message) => {
      if (message?.type === "market_agent_event" || message?.type === "market_agent_status") {
        refreshAgent();
      }
    });
    return () => {
      window.clearInterval(timer);
      unsubscribe();
    };
  }, [refreshAgent]);

  useEffect(() => {
    generationRef.current += 1;
    cancel();
    setDraft("");
    setError("");
    lastQuestionRef.current = "";
    confirmedHistoryRef.current = [];
  }, [symbol, cancel]);

  useEffect(() => () => controllerRef.current?.abort(), []);
  useEffect(() => {
    feedEndRef.current?.scrollIntoView?.({ block: "end" });
  }, [agentEvents, loading]);

  const toggleAgent = async () => {
    if (agentBusy) return;
    setAgentBusy(true);
    setError("");
    try {
      const response = ACTIVE_STATES.has(agentStatus.state) || agentStatus.enabled
        ? await stopMarketAgent()
        : await startMarketAgent({ symbol });
      setAgentStatus(response.data || { state: "stopped", enabled: false });
      await refreshAgent();
    } catch (requestError) {
      setError(errorMessage(requestError));
    } finally {
      setAgentBusy(false);
    }
  };

  const ask = async (question, retryHistory = null) => {
    const content = question.trim();
    if (!content || loading) return;
    confirmedHistoryRef.current = retryHistory || [];
    const generation = generationRef.current;
    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    lastQuestionRef.current = content;
    setDraft("");
    setError("");
    setLoading(true);
    try {
      await sendMarketAgentMessage({ symbol, content }, controller.signal);
      if (controller.signal.aborted || generation !== generationRef.current) return;
      await refreshAgent();
    } catch (requestError) {
      if (!controller.signal.aborted && generation === generationRef.current) {
        setError(errorMessage(requestError));
      }
    } finally {
      if (!controller.signal.aborted && generation === generationRef.current) {
        controllerRef.current = null;
        setLoading(false);
        requestAnimationFrame(() => inputRef.current?.focus());
      }
    }
  };

  const running = ACTIVE_STATES.has(agentStatus.state);
  const enabled = running || agentStatus.enabled;
  const paused = String(agentStatus.state || "").startsWith("paused");
  const matchingContext = !agentStatus.agent_id
    || agentStatus.symbol === symbol;
  const canChat = enabled && matchingContext;

  return (
    <aside
      role="region"
      aria-labelledby="market-ai-title"
      className="flex h-full min-h-0 w-full flex-col overflow-hidden rounded-lg border border-border bg-card"
    >
        <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3">
          <div className="min-w-0 flex-1">
            <h2 id="market-ai-title" className="flex items-center gap-2 text-sm font-semibold text-white"><Bot size={16} className="text-accent" />VibeTrading 实时助手</h2>
            <p className="mt-0.5 truncate text-xs text-muted">{symbol} · 多周期 · 仅分析已收盘 K 线</p>
          </div>
          <span className={`text-xs ${running && matchingContext ? "text-green" : "text-muted"}`}>
            {running && matchingContext ? "运行中" : paused ? "已暂停" : "未启动"}
          </span>
          <button type="button" onClick={toggleAgent} disabled={agentBusy || agentStatus.state === "stopping"} className={`flex h-8 items-center gap-1.5 border px-3 text-xs font-semibold disabled:opacity-50 ${enabled ? "border-red/40 text-red" : "border-accent/50 text-accent"}`}>
            {agentBusy ? <Loader size={13} className="animate-spin" /> : enabled ? <StopCircle size={13} /> : <Play size={13} />}
            {enabled ? "停止" : "启动"}
          </button>
          <button type="button" onClick={close} aria-label="收起实时助手" className="p-2 text-muted hover:text-white"><X size={18} /></button>
        </header>

        <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4" aria-live="polite">
          {!agentEvents.length && (
            <div className="space-y-3">
              <p className="text-sm text-muted">启动后，助手会在 K 线收盘、大 K 线和 SAR 转向时持续更新分析。</p>
              <div className="flex flex-col gap-2 sm:flex-row">
                {QUICK_QUESTIONS.map((question) => <button key={question} type="button" disabled={!canChat} onClick={() => ask(question)} className="border border-border bg-surface px-3 py-2 text-left text-sm text-white hover:border-accent/60 disabled:cursor-not-allowed disabled:opacity-40">{question}</button>)}
              </div>
            </div>
          )}
          {agentEvents.map((event) => (
            <article key={`${event.agent_id || "agent"}-${event.sequence}`} className={`border px-3 py-2 ${event.role === "user" ? "ml-8 border-accent/30 bg-accent/10" : "mr-4 border-border bg-surface"}`}>
              <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
                <MessageSquare size={12} className="text-accent" />
                <span>{formatTime(event.bar_closed_at || event.created_at)}</span>
                {(event.reasons || []).map((reason) => <span key={reason} className="border border-border px-1.5 py-0.5">{REASON_LABELS[reason] || reason}</span>)}
              </div>
              <p className="whitespace-pre-wrap break-words text-sm leading-6 text-white">{event.content || event.answer}</p>
            </article>
          ))}
          {loading && <div className="flex items-center gap-2 text-sm text-muted" role="status"><Loader size={15} className="animate-spin text-accent" />正在分析 {symbol}...</div>}
          {error && (
            <div className="flex flex-wrap items-center gap-2 border border-red/30 bg-red/10 px-3 py-2 text-sm text-red" role="alert">
              <AlertCircle size={15} /><span className="min-w-0 flex-1">{error}</span>
              {lastQuestionRef.current && <button type="button" onClick={() => ask(lastQuestionRef.current, confirmedHistoryRef.current)} className="flex items-center gap-1 text-xs font-semibold hover:text-white"><RotateCcw size={13} />重试</button>}
            </div>
          )}
          <div ref={feedEndRef} />
        </div>

        <footer className="shrink-0 border-t border-border p-3">
          <form onSubmit={(event) => { event.preventDefault(); ask(draft); }} className="flex items-end gap-2">
            <textarea ref={inputRef} rows={2} maxLength={1000} value={draft} disabled={loading || !canChat} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(draft); } }} placeholder={canChat ? "询问当前行情..." : "启动助手后可提问"} className="min-h-[42px] flex-1 resize-none border border-border bg-surface px-3 py-2 text-sm text-white outline-none placeholder:text-muted focus:border-accent disabled:opacity-60" />
            {loading ? <button type="button" onClick={cancel} aria-label="取消分析" title="取消分析" className="flex h-10 w-10 items-center justify-center border border-red/40 text-red hover:bg-red/10"><Square size={15} /></button> : <button type="submit" disabled={!canChat || !draft.trim()} aria-label="发送问题" title="发送问题" className="flex h-10 w-10 items-center justify-center bg-accent text-black disabled:opacity-40"><Send size={16} /></button>}
          </form>
          <p className="mt-2 text-[11px] text-muted">AI 只读分析，不会执行交易；内容不构成投资建议。</p>
        </footer>
    </aside>
  );
}
