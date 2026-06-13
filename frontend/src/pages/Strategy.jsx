import React, { useState, useEffect } from "react";
import {
  listStrategies, updateStrategy, activateStrategy,
  startEngine, stopEngine, getEngineStatus, getSymbols,
} from "../api/client";
import {
  Play, Square, Save, Loader, CheckCircle, AlertCircle, RefreshCw, Layers,
} from "lucide-react";
import clsx from "clsx";

const inp = "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-white outline-none focus:border-accent transition-colors";
const lbl = "text-xs text-muted mb-1 block";

// ML 趋势策略参数分组
const PARAM_GROUPS = [
  {
    title: "ML 出场控制",
    color: "#f59e0b",
    fields: [
      ["exit_threshold",     "出场阈值",   0.01],
      ["reversal_threshold", "反转阈值",   0.01],
    ],
  },
  {
    title: "仓位 / 加仓",
    color: "#0ecb81",
    fields: [
      ["atr_trail",      "跟踪止损 ATR",  0.1],
      ["max_adds",       "最多加仓档",    1],
      ["add_min_atr",    "加仓间隔 ATR",  0.1],
      ["add_size_frac",  "加仓仓位占比",  0.05],
      ["kelly_frac",     "Kelly 系数",    0.05],
    ],
  },
];

const PARAM_DEFAULTS = {
  exit_threshold:     0.35,
  reversal_threshold: 0.58,
  atr_trail:          3.0,
  max_adds:           3,
  add_min_atr:        0.8,
  add_size_frac:      0.5,
  kelly_frac:         0.25,
};

export default function StrategyPage() {
  const [strat, setStrat]         = useState(null);
  const [form, setForm]           = useState(null);
  const [params, setParams]       = useState({});
  const [symbols, setSymbols]     = useState([]);
  const [engine, setEngine]       = useState({ running: false });
  const [loading, setLoading]     = useState(true);
  const [saving, setSaving]       = useState(false);
  const [busy, setBusy]           = useState(false);
  const [msg, setMsg]             = useState(null);
  const [paper, setPaper]         = useState(true);
  const [thresholds, setThresholds] = useState({});

  const reload = async () => {
    setLoading(true);
    try {
      const [{ data: list }, { data: eng }] = await Promise.all([listStrategies(), getEngineStatus()]);
      const s = list.find(x => x.strategy_type === "ml_trend") || list[0];
      setStrat(s);
      if (s) {
        setForm({
          name:             s.name,
          description:      s.description || "",
          symbol:           s.symbol,
          leverage:         s.leverage,
          risk_pct:         s.risk_pct,
          stop_loss_pct:    s.stop_loss_pct,
          take_profit_pct:  s.take_profit_pct,
          interval:         s.interval,
          is_active:        s.is_active,
        });
        setParams({ ...PARAM_DEFAULTS, ...(s.strategy_params || {}) });
      }
      setEngine(eng);
    } catch (e) {
      setMsg({ ok: false, text: "加载失败：" + (e.response?.data?.detail || e.message) });
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    reload();
    getSymbols().then(({ data }) => setSymbols(data)).catch(() => {});
    const id = setInterval(() => getEngineStatus().then(({ data }) => setEngine(data)).catch(() => {}), 5000);
    return () => clearInterval(id);
  }, []);

  // 拉取入场阈值（自动校准，只读）
  useEffect(() => {
    fetch("/api/research/ml/thresholds")
      .then(r => r.ok ? r.json() : null)
      .then(data => { if (data) setThresholds(data); })
      .catch(() => {});
  }, []);

  const setF = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const setP = (k, v) => setParams(p => ({ ...p, [k]: Number(v) }));

  const handleSave = async () => {
    if (!strat) return;
    setSaving(true); setMsg(null);
    try {
      await updateStrategy(strat.id, {
        name:                 form.name,
        description:          form.description,
        symbol:               form.symbol,
        interval:             form.interval || "5m",
        leverage:             Number(form.leverage),
        risk_pct:             Number(form.risk_pct),
        stop_loss_pct:        form.stop_loss_pct,
        take_profit_pct:      form.take_profit_pct,
        strategy_type:        "ml_trend",
        strategy_params_json: JSON.stringify(params),
        ai_strategy_json:     null,
      });
      setMsg({ ok: true, text: "已保存" });
      reload();
    } catch (e) {
      setMsg({ ok: false, text: "保存失败：" + (e.response?.data?.detail || e.message) });
    } finally {
      setSaving(false);
    }
  };

  const toggleEngine = async () => {
    setBusy(true); setMsg(null);
    try {
      if (engine.running) {
        await stopEngine();
        setMsg({ ok: true, text: "策略已停止" });
      } else {
        if (!form?.is_active) await activateStrategy(strat.id);
        const { data } = await startEngine(paper);
        setMsg({ ok: true, text: data.message || "已启动" });
      }
      const { data } = await getEngineStatus(); setEngine(data);
      reload();
    } catch (e) {
      setMsg({ ok: false, text: e.response?.data?.detail || "操作失败" });
    } finally {
      setBusy(false);
    }
  };

  if (loading || !form) {
    return (
      <div className="flex items-center justify-center h-64 text-muted text-sm">
        <Loader size={16} className="animate-spin mr-2" /> 加载策略...
      </div>
    );
  }

  const riskHigh = form.risk_pct >= 0.05;
  const thresholdCoins = Object.keys(thresholds);

  return (
    <div className="space-y-5 pb-8">

      {/* 顶部：标题 + 引擎按钮 */}
      <div className="flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Layers size={18} className="text-accent" />
          <div>
            <h1 className="text-lg font-bold">{form.name}</h1>
            <p className="text-xs text-muted">ML 趋势策略 · 机器学习驱动</p>
          </div>
        </div>
        <div className="flex items-center gap-3">
          {msg && (
            <span className={clsx("text-xs flex items-center gap-1", msg.ok ? "text-green" : "text-red")}>
              {msg.ok ? <CheckCircle size={12} /> : <AlertCircle size={12} />} {msg.text}
            </span>
          )}
          <button onClick={handleSave} disabled={saving}
            className="flex items-center gap-2 bg-accent/10 border border-accent/40 text-accent px-4 py-2 rounded-lg text-sm font-bold hover:bg-accent/20 disabled:opacity-60 transition-colors">
            {saving ? <Loader size={14} className="animate-spin" /> : <Save size={14} />} 保存配置
          </button>
          {!engine.running && (
            <button onClick={() => setPaper(v => !v)} title="纸面=模拟成交不下真单"
              className={clsx("flex items-center gap-1.5 px-3 py-2 rounded-lg text-xs border transition-colors",
                paper ? "border-accent/50 bg-accent/10 text-accent" : "border-border text-muted hover:text-white")}>
              <span className={clsx("w-3.5 h-3.5 rounded border flex items-center justify-center",
                paper ? "bg-accent border-accent" : "border-border")}>
                {paper && <span className="text-[9px] text-black font-bold">✓</span>}
              </span>
              纸面交易
            </button>
          )}
          <button onClick={toggleEngine} disabled={busy}
            className={clsx("flex items-center gap-2 px-4 py-2 rounded-lg text-sm font-bold transition-colors disabled:opacity-50",
              engine.running
                ? "bg-red/10 border border-red/30 text-red hover:bg-red/20"
                : "bg-accent text-black hover:bg-accent/90")}>
            {busy ? <Loader size={14} className="animate-spin" /> : engine.running ? <Square size={14} /> : <Play size={14} />}
            {engine.running ? "停止运行" : "启动策略"}
          </button>
          <button onClick={reload} className="p-2 text-muted hover:text-white rounded-lg hover:bg-surface transition-colors">
            <RefreshCw size={14} />
          </button>
        </div>
      </div>

      {/* 运行状态条 */}
      {engine.running && (
        <div className="bg-green/5 border border-green/20 rounded-xl px-4 py-3 flex items-center gap-4 text-xs flex-wrap">
          <span className="flex items-center gap-1.5 text-green font-medium">
            <span className="w-2 h-2 rounded-full bg-green animate-pulse" /> 运行中
          </span>
          {engine.paper && <span className="text-accent">纸面 · 权益 ${engine.paper_equity}</span>}
          {engine.last_signal && engine.last_signal !== "NONE" && (
            <span className="text-muted">信号：<span className="text-white">{engine.last_signal}</span></span>
          )}
          {engine.last_action && (
            <span className="text-muted truncate max-w-md">{engine.last_action}</span>
          )}
          <span className="text-muted ml-auto">成交 {engine.trade_count}</span>
        </div>
      )}

      {/* 基本设置 */}
      <Section title="基本设置" color="#8b94b2">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div className="col-span-2 md:col-span-1">
            <label className={lbl}>交易品种</label>
            <input list="symlist" value={form.symbol}
              onChange={e => setF("symbol", e.target.value.toUpperCase())} className={inp} />
            <datalist id="symlist">
              {symbols.slice(0, 80).map(s => <option key={s} value={s} />)}
            </datalist>
          </div>
          <div>
            <label className={lbl}>杠杆</label>
            <input type="number" value={form.leverage} min={1} max={50}
              onChange={e => setF("leverage", Number(e.target.value))} className={inp} />
          </div>
          <div>
            <label className={lbl}>每笔风险 %</label>
            <input type="number" step={0.5} value={(form.risk_pct * 100).toFixed(1)}
              onChange={e => setF("risk_pct", Number(e.target.value) / 100)}
              className={clsx(inp, riskHigh && "border-red/50")} />
          </div>
          <div className="flex items-end">
            {riskHigh && (
              <span className="text-[10px] text-red leading-tight pb-1.5">
                ⚠ ≥5% 偏激进，回撤会显著放大
              </span>
            )}
          </div>
        </div>
      </Section>

      {/* 入场阈值（自动校准，只读） */}
      <Section title="入场信号" color="#22d3ee">
        <p className="text-xs text-muted mb-3">入场阈值由模型自动校准，不可手动修改。每次 research 后更新。</p>
        {thresholdCoins.length > 0 ? (
          <div className="overflow-x-auto">
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  <th className="text-left pb-2 pr-4 font-medium">币种</th>
                  <th className="text-right pb-2 pr-4 font-medium">做多阈值</th>
                  <th className="text-right pb-2 font-medium">做空阈值</th>
                </tr>
              </thead>
              <tbody>
                {thresholdCoins.map(coin => {
                  const row = thresholds[coin];
                  const longVal  = typeof row === "object" ? (row.long  ?? row.long_threshold  ?? "—") : row;
                  const shortVal = typeof row === "object" ? (row.short ?? row.short_threshold ?? "—") : "—";
                  return (
                    <tr key={coin} className="border-b border-border/40 last:border-0">
                      <td className="py-1.5 pr-4 text-white font-mono">{coin}</td>
                      <td className="py-1.5 pr-4 text-right">
                        <span className="text-cyan-400 font-mono">
                          {typeof longVal === "number" ? longVal.toFixed(3) : longVal}
                        </span>
                      </td>
                      <td className="py-1.5 text-right">
                        <span className="text-cyan-400 font-mono">
                          {typeof shortVal === "number" ? shortVal.toFixed(3) : shortVal}
                        </span>
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          </div>
        ) : (
          <p className="text-xs text-muted italic">暂无阈值数据（尚未运行 research 或 API 未就绪）</p>
        )}
      </Section>

      {/* ML 参数分组 */}
      {PARAM_GROUPS.map(g => (
        <Section key={g.title} title={g.title} color={g.color}>
          <div className="grid grid-cols-2 md:grid-cols-3 gap-3">
            {g.fields.map(([k, label, step]) => (
              <div key={k}>
                <label className={lbl}>{label}</label>
                <input type="number" step={step} value={params[k] ?? PARAM_DEFAULTS[k] ?? 0}
                  onChange={e => setP(k, e.target.value)} className={inp} />
              </div>
            ))}
          </div>
        </Section>
      ))}

    </div>
  );
}

function Section({ title, color, children }) {
  return (
    <div className="bg-card border border-border rounded-xl p-4">
      <div className="flex items-center gap-2 mb-3">
        <span className="w-1.5 h-1.5 rounded-full" style={{ background: color }} />
        <h2 className="text-sm font-semibold text-white">{title}</h2>
      </div>
      {children}
    </div>
  );
}

function Toggle({ label, hint, on, onClick }) {
  return (
    <button onClick={onClick}
      className={clsx("flex items-start gap-2 text-left px-3 py-2 rounded-lg border transition-colors flex-1 min-w-[260px]",
        on ? "border-accent/50 bg-accent/10" : "border-border hover:border-border/80")}>
      <span className={clsx("w-4 h-4 rounded mt-0.5 shrink-0 flex items-center justify-center border",
        on ? "bg-accent border-accent" : "border-border")}>
        {on && <span className="text-[10px] text-black font-bold">✓</span>}
      </span>
      <span>
        <span className={clsx("text-xs block", on ? "text-white" : "text-muted")}>{label}</span>
        {hint && <span className="text-[10px] text-muted">{hint}</span>}
      </span>
    </button>
  );
}
