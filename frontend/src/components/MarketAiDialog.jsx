import React, { useEffect, useRef, useState } from "react";
import { AlertCircle, Loader, MessageSquare, RotateCcw, Send, Square, X } from "lucide-react";
import { marketChat } from "../api/client";

const QUICK_QUESTIONS = ["现在的市场周期是什么？", "当前有没有可以交易的机会？"];

function errorMessage(error) {
  if (error?.code === "ERR_CANCELED" || error?.name === "CanceledError") return "";
  const detail = error?.response?.data?.detail;
  if (typeof detail === "string") return detail;
  if (detail?.message) return detail.message;
  return error?.message || "AI 分析暂时不可用，请稍后重试。";
}

export default function MarketAiDialog({ open, onClose, symbol, interval }) {
  const [messages, setMessages] = useState([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const dialogRef = useRef(null);
  const inputRef = useRef(null);
  const closeRef = useRef(null);
  const previousFocusRef = useRef(null);
  const controllerRef = useRef(null);
  const generationRef = useRef(0);
  const lastQuestionRef = useRef("");
  const confirmedHistoryRef = useRef([]);

  const cancel = () => {
    controllerRef.current?.abort();
    controllerRef.current = null;
    setLoading(false);
    setMessages((current) => current.at(-1)?.role === "user" ? current.slice(0, -1) : current);
  };

  const close = () => {
    cancel();
    onClose();
  };

  useEffect(() => {
    generationRef.current += 1;
    cancel();
    setMessages([]);
    setDraft("");
    setError("");
    lastQuestionRef.current = "";
    confirmedHistoryRef.current = [];
  }, [symbol, interval]);

  useEffect(() => {
    if (!open) return undefined;
    previousFocusRef.current = document.activeElement;
    const frame = requestAnimationFrame(() => closeRef.current?.focus());
    const onKeyDown = (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        close();
        return;
      }
      if (event.key !== "Tab" || !dialogRef.current) return;
      const focusable = [...dialogRef.current.querySelectorAll(
        'button:not([disabled]), textarea:not([disabled]), [href], [tabindex]:not([tabindex="-1"])',
      )];
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault();
        first.focus();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => {
      cancelAnimationFrame(frame);
      document.removeEventListener("keydown", onKeyDown);
      previousFocusRef.current?.focus?.();
    };
  }, [open, onClose]);

  useEffect(() => () => controllerRef.current?.abort(), []);

  const ask = async (question, retryHistory = null) => {
    const content = question.trim();
    if (!content || loading) return;
    const confirmed = (retryHistory || messages).at(-1)?.role === "user"
      ? (retryHistory || messages).slice(0, -1)
      : (retryHistory || messages);
    const bounded = confirmed.slice(-8);
    confirmedHistoryRef.current = confirmed;
    const history = [...bounded, { role: "user", content }];
    const requestMessages = history.map(({ role, content: text }) => ({ role, content: text }));
    const generation = generationRef.current;
    const controller = new AbortController();
    controllerRef.current?.abort();
    controllerRef.current = controller;
    lastQuestionRef.current = content;
    setMessages(requestMessages);
    setDraft("");
    setError("");
    setLoading(true);
    try {
      const { data } = await marketChat({ symbol, interval, messages: requestMessages }, controller.signal);
      if (controller.signal.aborted || generation !== generationRef.current) return;
      setMessages((current) => [...current, { role: "assistant", content: String(data?.answer || "未收到分析结果。") }]);
    } catch (requestError) {
      if (!controller.signal.aborted && generation === generationRef.current) {
        setMessages(confirmed);
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

  if (!open) return null;

  return (
    <div className="fixed inset-0 z-50 flex items-end justify-center bg-black/70 p-0 sm:items-center sm:p-4" onMouseDown={(event) => event.target === event.currentTarget && close()}>
      <section ref={dialogRef} role="dialog" aria-modal="true" aria-labelledby="market-ai-title" className="flex h-[94dvh] w-full flex-col border border-border bg-card shadow-2xl sm:h-[min(720px,86vh)] sm:max-w-2xl sm:rounded-lg">
        <header className="flex shrink-0 items-center justify-between border-b border-border px-4 py-3">
          <div className="min-w-0">
            <h2 id="market-ai-title" className="flex items-center gap-2 text-sm font-semibold text-white"><MessageSquare size={16} className="text-accent" />AI 行情分析</h2>
            <p className="mt-0.5 truncate text-xs text-muted">{symbol} · {interval} · 基于已收盘 K 线</p>
          </div>
          <button ref={closeRef} type="button" onClick={close} aria-label="关闭 AI 行情分析" className="p-2 text-muted hover:text-white"><X size={18} /></button>
        </header>

        <div className="flex-1 space-y-3 overflow-y-auto px-4 py-4" aria-live="polite">
          {!messages.length && (
            <div className="space-y-3">
              <p className="text-sm text-muted">选择一个问题开始分析当前行情。</p>
              <div className="flex flex-col gap-2 sm:flex-row">
                {QUICK_QUESTIONS.map((question) => <button key={question} type="button" onClick={() => ask(question)} className="border border-border bg-surface px-3 py-2 text-left text-sm text-white hover:border-accent/60">{question}</button>)}
              </div>
            </div>
          )}
          {messages.map((message, index) => (
            <div key={`${message.role}-${index}`} className={`flex ${message.role === "user" ? "justify-end" : "justify-start"}`}>
              <div className={`max-w-[88%] whitespace-pre-wrap break-words border px-3 py-2 text-sm leading-6 ${message.role === "user" ? "border-accent/30 bg-accent/10 text-white" : "border-border bg-surface text-white"}`}>{message.content}</div>
            </div>
          ))}
          {loading && <div className="flex items-center gap-2 text-sm text-muted" role="status"><Loader size={15} className="animate-spin text-accent" />正在分析 {symbol}...</div>}
          {error && (
            <div className="flex flex-wrap items-center gap-2 border border-red/30 bg-red/10 px-3 py-2 text-sm text-red" role="alert">
              <AlertCircle size={15} /><span className="min-w-0 flex-1">{error}</span>
              <button type="button" onClick={() => ask(lastQuestionRef.current, confirmedHistoryRef.current)} className="flex items-center gap-1 text-xs font-semibold hover:text-white"><RotateCcw size={13} />重试</button>
            </div>
          )}
        </div>

        <footer className="shrink-0 border-t border-border p-3">
          <form onSubmit={(event) => { event.preventDefault(); ask(draft); }} className="flex items-end gap-2">
            <textarea ref={inputRef} rows={2} maxLength={1000} value={draft} disabled={loading} onChange={(event) => setDraft(event.target.value)} onKeyDown={(event) => { if (event.key === "Enter" && !event.shiftKey) { event.preventDefault(); ask(draft); } }} placeholder="询问当前行情..." className="min-h-[42px] flex-1 resize-none border border-border bg-surface px-3 py-2 text-sm text-white outline-none placeholder:text-muted focus:border-accent disabled:opacity-60" />
            {loading ? <button type="button" onClick={cancel} aria-label="取消分析" title="取消分析" className="flex h-10 w-10 items-center justify-center border border-red/40 text-red hover:bg-red/10"><Square size={15} /></button> : <button type="submit" disabled={!draft.trim()} aria-label="发送问题" title="发送问题" className="flex h-10 w-10 items-center justify-center bg-accent text-black disabled:opacity-40"><Send size={16} /></button>}
          </form>
          <p className="mt-2 text-[11px] text-muted">AI 分析仅供研究，不构成投资建议或交易指令。</p>
        </footer>
      </section>
    </div>
  );
}
