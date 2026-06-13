import React, { useState, useEffect, useRef } from "react";
import { useApp } from "../context/AppContext";
import PriceChart from "../components/PriceChart";
import { getMLSignal } from "../api/client";
import {
  LineChart, Line, XAxis, YAxis, Tooltip, ResponsiveContainer, ReferenceLine,
} from "recharts";
import { AlertTriangle, Loader } from "lucide-react";

const ACTION_CONFIG = {
  long:    { label: "做多", color: "#0ecb81" },
  short:   { label: "做空", color: "#ef4444" },
  observe: { label: "观望", color: "#8b94b2" },
};

// ── ML 概率历史折线子图 ────────────────────────────────────────────────────
function MLProbChart({ history }) {
  if (!history || history.length < 2) {
    return (
      <div className="flex items-center justify-center h-full text-xs text-muted">
        等待概率历史数据…（每 30 秒更新一次）
      </div>
    );
  }

  const fmt = (v) => `${(v * 100).toFixed(1)}%`;

  return (
    <ResponsiveContainer width="100%" height="100%">
      <LineChart data={history} margin={{ top: 4, right: 10, bottom: 0, left: 0 }}>
        <XAxis dataKey="t" tick={{ fill: "#8b94b2", fontSize: 9 }} tickLine={false} axisLine={false} interval="preserveStartEnd" />
        <YAxis domain={[0, 1]} tickFormatter={fmt} tick={{ fill: "#8b94b2", fontSize: 9 }} tickLine={false} axisLine={false} width={38} />
        <ReferenceLine y={0.5} stroke="#8b94b2" strokeDasharray="3 3" strokeOpacity={0.4} />
        <Tooltip
          contentStyle={{ background: "#1a1d2e", border: "1px solid #2a2d3e", borderRadius: 8, fontSize: 11 }}
          formatter={(val, name) => [`${(val * 100).toFixed(1)}%`, name]}
          labelStyle={{ color: "#8b94b2" }}
        />
        <Line type="monotone" dataKey="long_prob"  stroke="#0ecb81" dot={false} strokeWidth={1.5} name="上涨概率" />
        <Line type="monotone" dataKey="short_prob" stroke="#ef4444" dot={false} strokeWidth={1.5} name="下跌概率" />
      </LineChart>
    </ResponsiveContainer>
  );
}

// ── ML 信号摘要卡片 ───────────────────────────────────────────────────────
function MLSignalCard({ signal, loading, error }) {
  if (loading) {
    return (
      <div className="bg-card border border-border rounded-xl px-4 py-3 flex items-center gap-2 text-sm text-muted">
        <Loader size={14} className="animate-spin" />
        加载 ML 信号…
      </div>
    );
  }

  if (error) {
    return (
      <div className="bg-card border border-red/30 rounded-xl px-4 py-3 flex items-center gap-2 text-xs text-red">
        <AlertTriangle size={13} />
        {error}
      </div>
    );
  }

  if (!signal) {
    return (
      <div className="bg-card border border-border rounded-xl px-4 py-3 text-xs text-muted">
        暂无 ML 信号
      </div>
    );
  }

  const cfg       = ACTION_CONFIG[signal.action] || ACTION_CONFIG.observe;
  const longPct   = signal.long_prob    != null ? (signal.long_prob  * 100).toFixed(1) : "--";
  const shortPct  = signal.short_prob   != null ? (signal.short_prob * 100).toFixed(1) : "--";
  const threshold = signal.threshold_used != null ? (signal.threshold_used * 100).toFixed(1) : "--";
  const kelly     = signal.pos_size_mult  != null ? signal.pos_size_mult.toFixed(2) : "--";
  const confPct   = signal.confidence     != null ? (signal.confidence * 100).toFixed(1) : "--";

  return (
    <div className="bg-card border rounded-xl px-4 py-3 space-y-2" style={{ borderColor: `${cfg.color}55` }}>
      <div className="flex items-center justify-between">
        <span className="text-[10px] text-muted uppercase tracking-wide">ML 信号</span>
        {signal.drift_warning && (
          <span className="flex items-center gap-1 text-[10px] text-amber-400">
            <AlertTriangle size={11} /> 漂移警告
          </span>
        )}
      </div>
      <div className="flex items-center gap-2">
        <span className="text-lg font-bold" style={{ color: cfg.color }}>{cfg.label}</span>
        <span className="text-[11px] px-2 py-0.5 rounded font-medium"
          style={{ background: `${cfg.color}22`, color: cfg.color, border: `1px solid ${cfg.color}55` }}>
          置信度 {confPct}%
        </span>
      </div>
      <div className="grid grid-cols-4 gap-2">
        <div className="flex flex-col">
          <span className="text-[10px] text-muted">上涨概率</span>
          <span className="text-xs font-medium text-green">{longPct}%</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] text-muted">下跌概率</span>
          <span className="text-xs font-medium text-red">{shortPct}%</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] text-muted">做空阈值</span>
          <span className="text-xs font-medium text-white">{threshold}%</span>
        </div>
        <div className="flex flex-col">
          <span className="text-[10px] text-muted">Kelly 倍率</span>
          <span className="text-xs font-medium text-white">×{kelly}</span>
        </div>
      </div>
    </div>
  );
}

const MAX_HISTORY = 60;

export default function Markets() {
  const { symbol } = useApp();
  const [mlSignal,  setMlSignal]  = useState(null);
  const [mlLoading, setMlLoading] = useState(false);
  const [mlError,   setMlError]   = useState(null);
  // rolling history of (t, long_prob, short_prob)
  const [mlHistory, setMlHistory] = useState([]);
  const prevSymbol = useRef(null);

  useEffect(() => {
    if (!symbol) return;

    // clear history when symbol changes
    if (prevSymbol.current !== symbol) {
      setMlHistory([]);
      prevSymbol.current = symbol;
    }

    let cancelled = false;
    let tick = 0;

    const load = () => {
      setMlLoading(true);
      setMlError(null);
      getMLSignal(symbol)
        .then(({ data }) => {
          if (cancelled) return;
          setMlSignal(data);
          // append to history
          if (data.long_prob != null && data.short_prob != null) {
            const now  = new Date();
            const label = `${now.getHours().toString().padStart(2,"0")}:${now.getMinutes().toString().padStart(2,"0")}:${now.getSeconds().toString().padStart(2,"0")}`;
            setMlHistory(h => {
              const next = [...h, { t: label, long_prob: data.long_prob, short_prob: data.short_prob }];
              return next.length > MAX_HISTORY ? next.slice(next.length - MAX_HISTORY) : next;
            });
          }
        })
        .catch(e => { if (!cancelled) setMlError(e.response?.data?.detail || "ML 信号获取失败"); })
        .finally(() => { if (!cancelled) setMlLoading(false); });
    };

    setMlSignal(null);
    load();
    const timer = setInterval(load, 30000);
    return () => { cancelled = true; clearInterval(timer); };
  }, [symbol]);

  return (
    <div className="flex gap-4 h-full">
      <div className="flex-1 min-w-0 h-full flex flex-col gap-3">
        {/* K 线主图 */}
        <div className="flex-1 min-h-0">
          <PriceChart symbol={symbol} defaultInterval="5m" />
        </div>

        {/* ML 概率趋势子图 */}
        <div className="shrink-0 bg-card border border-border rounded-xl p-3" style={{ height: 140 }}>
          <div className="flex items-center gap-3 mb-1">
            <span className="text-[10px] text-muted uppercase tracking-wide">ML 趋势概率</span>
            <span className="flex items-center gap-1 text-[10px]">
              <span className="w-2 h-0.5 bg-green inline-block rounded"/>
              <span className="text-muted">上涨</span>
            </span>
            <span className="flex items-center gap-1 text-[10px]">
              <span className="w-2 h-0.5 bg-red inline-block rounded"/>
              <span className="text-muted">下跌</span>
            </span>
            <span className="text-[10px] text-muted ml-auto">近 {mlHistory.length} 次 · 30s/次</span>
          </div>
          <div style={{ height: 100 }}>
            <MLProbChart history={mlHistory} />
          </div>
        </div>

        {/* ML 信号摘要 */}
        <div className="shrink-0">
          <MLSignalCard signal={mlSignal} loading={mlLoading} error={mlError} />
        </div>
      </div>
    </div>
  );
}
