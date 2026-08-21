import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  AlertCircle,
  Bot,
  ChevronDown,
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
  sendMarketAgentMessage,
  startMarketAgent,
  stopMarketAgent,
} from "../api/client";
import { useApp } from "../context/AppContext";
import { useMarketAgentFeed } from "../hooks/useMarketAgentFeed";

const QUICK_QUESTIONS = ["现在的市场周期是什么？", "当前有没有可以交易的机会？"];
const REASON_LABELS = {
  candle_closed: "K线收盘",
  large_candle: "大K线",
  sar_reversal: "SAR转向",
};
const LIFECYCLE = {
  stopped: { label: "未启动", tone: "text-muted" },
  starting: { label: "启动中", tone: "text-accent" },
  running: { label: "运行中", tone: "text-green" },
  waiting_market: { label: "等待行情", tone: "text-yellow-400" },
  retry_wait: { label: "等待重试", tone: "text-yellow-400" },
  paused_budget: { label: "预算暂停", tone: "text-yellow-400" },
  paused_config: { label: "配置暂停", tone: "text-red" },
  stopping: { label: "停止中", tone: "text-muted" },
};

function errorMessage(error) {
  if (error?.code === "ERR_CANCELED" || error?.name === "CanceledError") return "";
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  return error?.message || "AI 分析暂时不可用，请稍后重试。";
}

function formatTime(value) {
  if (!value) return "";
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? String(value) : date.toLocaleString("zh-CN", { hour12: false });
}

function compactLine(value) {
  const dividerCharacters = "-:| \t";
  const lines = String(value || "")
    .split(/\r?\n/)
    .map((line) => line.replace(/^\s*(?:#{1,6}|[-*>]+|\d+\.)\s*/, "").replace(/[*_`]/g, "").trim())
    .filter((line) => line
      && !line.startsWith("|")
      && ![...line].every((character) => dividerCharacters.includes(character)));
  const first = lines[0] || "行情解读已更新";
  return first.length > 120 ? `${first.slice(0, 117)}...` : first;
}

function headline(event) {
  return compactLine(
    event.short_summary
      ?? event.headline
      ?? event.summary
      ?? event.context?.short_summary
      ?? event.content,
  );
}

function detailItems(value) {
  if (!value) return [];
  const items = Array.isArray(value) ? value : [value];
  return items.map((item) => {
    if (typeof item === "string") return item;
    if (item?.label && item?.value != null) return `${item.label}：${item.value}`;
    if (item?.text) return String(item.text);
    return JSON.stringify(item);
  }).filter(Boolean);
}

function correlationKeys(event) {
  return [event.client_message_id, event.job_id].filter(Boolean).map(String);
}

function timelineItems(events) {
  const items = [];
  const questions = new Map();
  events.forEach((event) => {
    if (event.type === "analysis") {
      items.push({ kind: "analysis", key: event._key, event });
      return;
    }
    if (event.type === "user_message" || event.role === "user") {
      const item = { kind: "question", key: event._key, question: event, answer: null };
      items.push(item);
      correlationKeys(event).forEach((key) => questions.set(key, item));
      return;
    }
    if (event.type === "assistant_message") {
      const match = correlationKeys(event).map((key) => questions.get(key)).find(Boolean);
      if (match && !match.answer) match.answer = event;
      else items.push({ kind: "reply", key: event._key, event });
      return;
    }
    items.push({ kind: event.role === "user" ? "question" : "reply", key: event._key, event });
  });
  return items;
}

function eventDetails(event) {
  const evidence = detailItems(event.evidence ?? event.context?.evidence);
  const risks = detailItems(event.risks ?? event.context?.risks);
  const summary = headline(event);
  const full = String(event.content || "").trim();
  const showFull = full && full !== summary;
  if (!evidence.length && !risks.length && !showFull) return null;
  return (
    <details className="mt-2 text-xs text-muted">
      <summary className="flex cursor-pointer list-none items-center gap-1 hover:text-white">
        <ChevronDown size={12} />证据与风险
      </summary>
      <div className="mt-2 space-y-2 border-l border-border pl-3">
        {!!evidence.length && <div><strong className="text-white">证据</strong>{evidence.map((item, index) => <p key={`${item}-${index}`} className="mt-1 break-words">{item}</p>)}</div>}
        {!!risks.length && <div><strong className="text-white">风险</strong>{risks.map((item, index) => <p key={`${item}-${index}`} className="mt-1 break-words">{item}</p>)}</div>}
        {showFull && <div><strong className="text-white">完整分析</strong><p className="mt-1 whitespace-pre-wrap break-words leading-5">{full}</p></div>}
      </div>
    </details>
  );
}

function EventMeta({ event, label }) {
  return (
    <div className="mb-1 flex flex-wrap items-center gap-2 text-[11px] text-muted">
      <MessageSquare size={12} className="text-accent" />
      {label && <span className="font-semibold text-white">{label}</span>}
      <span>{formatTime(event.bar_closed_at || event.created_at)}</span>
      {(event.reasons || []).map((reason) => <span key={reason} className="border border-border px-1.5 py-0.5">{REASON_LABELS[reason] || reason}</span>)}
    </div>
  );
}

function AnalysisItem({ event }) {
  return (
    <article className="relative border-l border-border py-2 pl-4 pr-1">
      <span className="absolute -left-1 top-4 h-2 w-2 rounded-full bg-accent" aria-hidden="true" />
      <EventMeta event={event} />
      <p className="break-words text-sm leading-6 text-white">{headline(event)}</p>
      {eventDetails(event)}
    </article>
  );
}

function QuestionItem({ question, answer }) {
  return (
    <article className="border border-accent/30 bg-accent/5 px-3 py-3">
      <EventMeta event={question} label="你的问题" />
      <p className="whitespace-pre-wrap break-words text-sm leading-6 text-white">{question.content}</p>
      {answer ? (
        <div className="mt-3 border-t border-border pt-3">
          <EventMeta event={answer} label="助手回复" />
          <p className="break-words text-sm leading-6 text-white">{headline(answer)}</p>
          {eventDetails(answer)}
        </div>
      ) : <p className="mt-2 text-xs text-muted">等待可关联的回复</p>}
    </article>
  );
}

function StandaloneReply({ event }) {
  return (
    <article className="border border-border bg-surface px-3 py-3">
      <EventMeta event={event} label="助手回复" />
      <p className="break-words text-sm leading-6 text-white">{headline(event)}</p>
      {eventDetails(event)}
    </article>
  );
}

function questionId() {
  return globalThis.crypto?.randomUUID?.() || `question-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function lifecycleDescription(status) {
  if (status.state === "retry_wait" && status.retry_not_before) {
    return `将在 ${formatTime(status.retry_not_before)} 后重试`;
  }
  if (status.state === "paused_budget") {
    const used = status.daily_usage_count ?? 0;
    const limit = status.daily_usage_limit ?? "-";
    return `今日预算 ${used}/${limit}`;
  }
  if (status.state === "paused_config") return status.paused_reason || "请检查 AI 配置";
  if (status.state === "waiting_market") return "等待已收盘 K 线数据";
  return "";
}

export default function MarketAiPanel({ onClose, symbol }) {
  const app = useApp();
  const connected = app?.connected;
  const {
    status: agentStatus,
    events: agentEvents,
    feedState,
    error: feedError,
    historyTruncated,
    refresh,
  } = useMarketAgentFeed({ symbol, connected });
  const [agentBusy, setAgentBusy] = useState(false);
  const [draft, setDraft] = useState("");
  const [questionError, setQuestionError] = useState("");
  const [pendingQuestions, setPendingQuestions] = useState([]);
  const [submitting, setSubmitting] = useState(false);
  const [unreadCount, setUnreadCount] = useState(0);
  const inputRef = useRef(null);
  const feedRef = useRef(null);
  const feedEndRef = useRef(null);
  const controllerRef = useRef(null);
  const generationRef = useRef(0);
  const followingLatestRef = useRef(true);
  const previousKeysRef = useRef(new Set());
  const items = useMemo(() => timelineItems(agentEvents), [agentEvents]);

  const scrollToLatest = useCallback((behavior = "smooth") => {
    followingLatestRef.current = true;
    setUnreadCount(0);
    feedEndRef.current?.scrollIntoView?.({ block: "end", behavior });
  }, []);

  useEffect(() => {
    generationRef.current += 1;
    controllerRef.current?.abort();
    controllerRef.current = null;
    setDraft("");
    setQuestionError("");
    setPendingQuestions([]);
    setSubmitting(false);
    setUnreadCount(0);
    followingLatestRef.current = true;
    previousKeysRef.current = new Set();
  }, [symbol]);

  useEffect(() => () => controllerRef.current?.abort(), []);

  useEffect(() => {
    const previous = previousKeysRef.current;
    const next = new Set(agentEvents.map((event) => event._key));
    const added = agentEvents.reduce((count, event) => count + (previous.has(event._key) ? 0 : 1), 0);
    previousKeysRef.current = next;
    if (!added) return;
    if (!previous.size || followingLatestRef.current) {
      requestAnimationFrame(() => {
        if (followingLatestRef.current) scrollToLatest(previous.size ? "smooth" : "auto");
      });
    } else {
      setUnreadCount((current) => current + added);
    }
  }, [agentEvents, scrollToLatest]);

  useEffect(() => {
    if (!pendingQuestions.length) return;
    setPendingQuestions((current) => current.flatMap((question) => {
      const identifiers = [question.clientMessageId, question.jobId].filter(Boolean).map(String);
      if (!identifiers.length) return [question];
      const related = agentEvents.filter((event) => correlationKeys(event).some((key) => identifiers.includes(key)));
      if (related.some((event) => event.type === "assistant_message")) return [];
      if (related.some((event) => event.type === "user_message")) return [{ ...question, state: "analyzing" }];
      return [question];
    }));
  }, [agentEvents, pendingQuestions.length]);

  const onFeedScroll = () => {
    const node = feedRef.current;
    if (!node) return;
    const nearBottom = node.scrollHeight - node.scrollTop - node.clientHeight <= 64;
    followingLatestRef.current = nearBottom;
    if (nearBottom) setUnreadCount(0);
  };

  const desiredEnabled = Boolean(agentStatus.desired_enabled ?? agentStatus.enabled);
  const matchingContext = !agentStatus.agent_id || agentStatus.symbol === symbol;
  const lifecycle = matchingContext
    ? LIFECYCLE[agentStatus.state] || { label: agentStatus.state || "状态未知", tone: "text-muted" }
    : { label: `${agentStatus.symbol || "其他品种"} 运行中`, tone: "text-yellow-400" };
  const canChat = desiredEnabled && matchingContext && agentStatus.state === "running";

  const toggleAgent = async () => {
    if (agentBusy) return;
    setAgentBusy(true);
    setQuestionError("");
    try {
      if (desiredEnabled) await stopMarketAgent();
      else await startMarketAgent({ symbol });
      await refresh({ refreshStatus: true });
    } catch (requestError) {
      setQuestionError(errorMessage(requestError));
    } finally {
      setAgentBusy(false);
    }
  };

  const cancelWaiting = useCallback(() => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setSubmitting(false);
    setPendingQuestions((current) => current.map((question) => (
      question.state === "submitting" ? { ...question, state: "cancel_requested" } : question
    )));
  }, []);

  const ask = async (question) => {
    const content = question.trim();
    if (!content || submitting || !canChat) return;
    const localId = questionId();
    const generation = generationRef.current;
    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    setDraft("");
    setQuestionError("");
    setSubmitting(true);
    setPendingQuestions((current) => [...current, {
      localId,
      clientMessageId: localId,
      jobId: null,
      content,
      state: "submitting",
    }]);
    requestAnimationFrame(() => scrollToLatest());
    try {
      const response = await sendMarketAgentMessage({ symbol, content }, controller.signal, localId);
      if (controller.signal.aborted || generation !== generationRef.current) return;
      const queued = response.status === 202 || ["queued", "pending", "accepted"].includes(response.data?.state);
      const responseClientId = response.data?.client_message_id || response.data?.request_id || localId;
      const responseJobId = response.data?.job_id || response.data?.analysis_job_id || null;
      setPendingQuestions((current) => current.flatMap((item) => {
        if (item.localId !== localId) return [item];
        if (!queued) return [];
        return [{ ...item, state: "queued", clientMessageId: responseClientId, jobId: responseJobId }];
      }));
      await refresh({ refreshStatus: true });
    } catch (requestError) {
      if (!controller.signal.aborted && generation === generationRef.current) {
        setQuestionError(errorMessage(requestError));
        setPendingQuestions((current) => current.map((item) => (
          item.localId === localId ? { ...item, state: "failed" } : item
        )));
      }
    } finally {
      if (!controller.signal.aborted && generation === generationRef.current) {
        controllerRef.current = null;
        setSubmitting(false);
        requestAnimationFrame(() => inputRef.current?.focus());
      }
    }
  };

  const retryQuestion = (question) => {
    setPendingQuestions((current) => current.filter((item) => item.localId !== question.localId));
    ask(question.content);
  };

  return (
    <aside role="region" aria-labelledby="market-ai-title" className="flex h-full min-h-0 w-full flex-col overflow-hidden rounded-lg border border-border bg-card">
      <header className="flex shrink-0 items-center gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0 flex-1">
          <h2 id="market-ai-title" className="flex items-center gap-2 text-sm font-semibold text-white"><Bot size={16} className="text-accent" />实时行情助手</h2>
        </div>
        <div className="text-right">
          <p className={`text-xs ${lifecycle.tone}`}>{lifecycle.label}</p>
          {matchingContext && lifecycleDescription(agentStatus) && <p className="mt-0.5 max-w-40 truncate text-[10px] text-muted" title={lifecycleDescription(agentStatus)}>{lifecycleDescription(agentStatus)}</p>}
        </div>
        <button type="button" onClick={toggleAgent} disabled={agentBusy || agentStatus.state === "stopping" || agentStatus.state === "starting"} className={`flex h-8 items-center gap-1.5 border px-3 text-xs font-semibold disabled:opacity-50 ${desiredEnabled ? "border-red/40 text-red" : "border-accent/50 text-accent"}`}>
          {agentBusy ? <Loader size={13} className="animate-spin" /> : desiredEnabled ? <StopCircle size={13} /> : <Play size={13} />}
          {desiredEnabled ? "停止" : "启动"}
        </button>
        <button type="button" onClick={() => { cancelWaiting(); onClose(); }} aria-label="收起实时助手" className="p-2 text-muted hover:text-white"><X size={18} /></button>
      </header>

      <div className="relative min-h-0 flex-1">
        <div ref={feedRef} data-testid="market-agent-feed" onScroll={onFeedScroll} className="h-full space-y-3 overflow-y-auto px-4 py-4">
          {feedState === "bootstrapping" && !agentEvents.length && <div className="flex items-center gap-2 text-sm text-muted" role="status"><Loader size={15} className="animate-spin text-accent" />正在同步实时解读...</div>}
          {!agentEvents.length && feedState !== "bootstrapping" && (
            <div className="space-y-3">
              <p className="text-sm text-muted">启动后，助手会在 K 线收盘、大 K 线和 SAR 转向时持续更新分析。</p>
              <div className="flex flex-col gap-2 sm:flex-row">
                {QUICK_QUESTIONS.map((question) => <button key={question} type="button" disabled={!canChat} onClick={() => ask(question)} className="border border-border bg-surface px-3 py-2 text-left text-sm text-white hover:border-accent/60 disabled:cursor-not-allowed disabled:opacity-40">{question}</button>)}
              </div>
            </div>
          )}
          {(historyTruncated || feedState === "stale") && <div className="border border-yellow-500/30 bg-yellow-500/5 px-3 py-2 text-xs text-yellow-200">历史解读可能不完整，当前内容已与服务端保留窗口对齐。</div>}
          {items.map((item) => {
            if (item.kind === "analysis") return <AnalysisItem key={item.key} event={item.event} />;
            if (item.kind === "question") return <QuestionItem key={item.key} question={item.question || item.event} answer={item.answer} />;
            return <StandaloneReply key={item.key} event={item.event} />;
          })}
          {pendingQuestions.map((question) => (
            <article key={question.localId} className="border border-accent/30 bg-accent/5 px-3 py-3">
              <div className="flex items-center gap-2 text-[11px] text-muted"><MessageSquare size={12} className="text-accent" /><span className="font-semibold text-white">你的问题</span><span>{question.state === "analyzing" ? "分析中" : question.state === "queued" ? "排队中" : question.state === "failed" ? "发送失败" : question.state === "cancel_requested" ? "已停止等待" : "正在提交"}</span></div>
              <p className="mt-1 whitespace-pre-wrap break-words text-sm leading-6 text-white">{question.content}</p>
              {question.state === "failed" && <button type="button" onClick={() => retryQuestion(question)} className="mt-2 flex items-center gap-1 text-xs font-semibold text-accent"><RotateCcw size={13} />重试</button>}
              {question.state === "cancel_requested" && <p className="mt-2 text-xs text-muted">仅停止浏览器等待，后台任务可能仍会完成。</p>}
            </article>
          ))}
          {(feedError || questionError) && (
            <div className="flex flex-wrap items-center gap-2 border border-red/30 bg-red/10 px-3 py-2 text-sm text-red" role="alert">
              <AlertCircle size={15} /><span className="min-w-0 flex-1">{questionError || feedError}</span>
              {feedError && <button type="button" onClick={() => refresh({ refreshStatus: true })} className="flex items-center gap-1 text-xs font-semibold hover:text-white"><RotateCcw size={13} />重新同步</button>}
            </div>
          )}
          <div ref={feedEndRef} />
        </div>
        {unreadCount > 0 && <button type="button" onClick={() => scrollToLatest()} className="absolute bottom-3 left-1/2 flex -translate-x-1/2 items-center gap-1 border border-accent/50 bg-card px-3 py-1.5 text-xs font-semibold text-accent shadow-lg" aria-live="polite"><ChevronDown size={14} />{unreadCount} 条新解读</button>}
      </div>

      <footer className="shrink-0 border-t border-border p-3">
        <form onSubmit={(event) => { event.preventDefault(); ask(draft); }} className="flex items-end gap-2">
          <textarea ref={inputRef} rows={1} maxLength={1000} value={draft} disabled={!canChat} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(draft); } }} placeholder={canChat ? "询问当前行情..." : "助手运行后可提问"} className="h-10 flex-1 resize-none overflow-y-auto border border-border bg-surface px-3 py-2 text-sm text-white outline-none placeholder:text-muted focus:border-accent disabled:opacity-60" />
          {submitting ? <button type="button" onClick={cancelWaiting} aria-label="停止等待" title="停止等待" className="flex h-10 w-10 items-center justify-center border border-red/40 text-red hover:bg-red/10"><Square size={15} /></button> : <button type="submit" disabled={!canChat || !draft.trim()} aria-label="发送问题" title="发送问题" className="flex h-10 w-10 items-center justify-center bg-accent text-black disabled:opacity-40"><Send size={16} /></button>}
        </form>
      </footer>
    </aside>
  );
}
