import React, { useState, useEffect, useCallback } from "react";
import { useApp } from "../context/AppContext";
import {
  getOpenOrders, getOrderHistory, getRecentTrades, cancelOrder,
  listStrategies, updateStrategy, activateStrategy,
  startEngine, stopEngine, getEngineStatus, getSymbols,
} from "../api/client";
import {
  RefreshCw, X, Play, Square, Save, Loader,
  ChevronDown, ChevronUp, AlertTriangle, CheckCircle, AlertCircle,
  Zap,
} from "lucide-react";
import clsx from "clsx";

const TABS = ["挂单", "成交记录", "历史订单"];

const inp = "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-white outline-none focus:border-accent transition-colors";
const lbl = "text-xs text-muted mb-1 block";

const PARAM_DEFAULTS = {
  exit_threshold:     0.35,
  reversal_threshold: 0.58,
  atr_trail:          3.0,
  max_adds:           3,
  add_min_atr:        0.8,
  add_size_frac:      0.5,
  kelly_frac:         0.25,
};

const PARAM_FIELDS = [
  ["exit_threshold",     "出场阈值",    0.01],
  ["reversal_threshold", "反转阈值",    0.01],
  ["atr_trail",          "跟踪止损ATR", 0.1],
  ["max_adds",           "最多加仓档",  1],
  ["add_min_atr",        "加仓间隔ATR", 0.1],
  ["add_size_frac",      "加仓仓位占比",0.05],
  ["kelly_frac",         "Kelly系数",   0.05],
];

// ── 订单表格辅助 ──────────────────────────────────────────────────────────────

function Badge({ type }) {
  const map = {
    LIMIT: "bg-blue-500/10 text-blue-400",
    MARKET: "bg-purple-500/10 text-purple-400",
    STOP_MARKET: "bg-orange-500/10 text-orange-400",
    TAKE_PROFIT_MARKET: "bg-green/10 text-green",
  };
  return (
    <span className={clsx("text-xs px-1.5 py-0.5 rounded", map[type] || "bg-surface text-muted")}>
      {type?.replace("_", " ")}
    </span>
  );
}

function StatusBadge({ status }) {
  const map = {
    FILLED: "text-green", NEW: "text-accent", CANCELED: "text-muted",
    PARTIALLY_FILLED: "text-blue-400", EXPIRED: "text-muted",
  };
  return <span className={clsx("text-xs font-medium", map[status] || "text-muted")}>{status}</span>;
}

// ── 引擎控制面板 ──────────────────────────────────────────────────────────────

function EnginePanel() {
  const { networkTab } = useApp();

  const [strat, setStrat]       = useState(null);
  const [form, setForm]         = useState(null);
  const [params, setParams]     = useState({});
  const [symbols, setSymbols]   = useState([]);
  const [engine, setEngine]     = useState({ running: false, circuit_open: false });
  const [paper, setPaper]       = useState(true);
  const [expanded, setExpanded] = useState(false);
  const [saving, setSaving]     = useState(false);
  const [busy, setBusy]         = useState(false);
  const [msg, setMsg]           = useState(null);
  const [thresholds, setThresholds] = useState({});

  const loadStrategy = useCallback(async () => {
    try {
      const [{ data: list }, { data: eng }] = await Promise.all([
        listStrategies(), getEngineStatus(),
      ]);
      const s = list.find(x => x.strategy_type === "ml_trend") || list[0];
      setStrat(s);
      if (s) {
        setForm({
          name:           s.name,
          symbol:         s.symbol,
          leverage:       s.leverage,
          risk_pct:       s.risk_pct,
          interval:       s.interval || "5m",
          stop_loss_pct:  s.stop_loss_pct,
          take_profit_pct:s.take_profit_pct,
          is_active:      s.is_active,
        });
        setParams({ ...PARAM_DEFAULTS, ...(s.strategy_params || {}) });
      }
      setEngine(eng);
    } catch (_) {}
  }, []);

  useEffect(() => {
    loadStrategy();
    getSymbols().then(({ data }) => setSymbols(data)).catch(() => {});
    fetch("/api/research/ml/thresholds")
      .then(r => r.ok ? r.json() : null)
      .then(d => { if (d) setThresholds(d); })
      .catch(() => {});
    const id = setInterval(() =>
      getEngineStatus().then(({ data }) => setEngine(data)).catch(() => {}), 5000);
    return () => clearInterval(id);
  }, []);

  const setF = (k, v) => setForm(f => ({ ...f, [k]: v }));
  const setP = (k, v) => setParams(p => ({ ...p, [k]: Number(v) }));

  const handleSave = async () => {
    if (!strat) return;
    setSaving(true); setMsg(null);
    try {
      await updateStrategy(strat.id, {
        name:                 form.name,
        symbol:               form.symbol,
        interval:             form.interval,
        leverage:             Number(form.leverage),
        risk_pct:             Number(form.risk_pct),
        stop_loss_pct:        form.stop_loss_pct,
        take_profit_pct:      form.take_profit_pct,
        strategy_type:        "ml_trend",
        strategy_params_json: JSON.stringify(params),
        ai_strategy_json:     null,
      });
      setMsg({ ok: true, text: "配置已保存" });
      loadStrategy();
    } catch (e) {
      setMsg({ ok: false, text: e.response?.data?.detail || "保存失败" });
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
        if (!form?.is_active && strat) await activateStrategy(strat.id);
        const { data } = await startEngine(paper);
        setMsg({ ok: true, text: data.message || "已启动" });
      }
      const { data } = await getEngineStatus();
      setEngine(data);
      loadStrategy();
    } catch (e) {
      setMsg({ ok: false, text: e.response?.data?.detail || "操作失败" });
    } finally {
      setBusy(false);
    }
  };

  const isTestnet = networkTab === "test";
  const thresholdCoins = Object.keys(thresholds);

  return (
    <div className="bg-card border border-border rounded-xl overflow-hidden mb-4">

      {/* ── 顶栏 ─────────────────────────────────────────────── */}
      <div className="flex items-center justify-between px-4 py-3 border-b border-border">
        <div className="flex items-center gap-3">
          <Zap size={15} className={engine.running ? "text-green" : "text-muted"} />
          <div className="flex items-center gap-2">
            {engine.running ? (
              <span className="flex items-center gap-1.5 text-green text-xs font-medium">
                <span className="w-1.5 h-1.5 rounded-full bg-green animate-pulse" /> 运行中
              </span>
            ) : (
              <span className="text-muted text-xs">未运行</span>
            )}
            <span className={clsx("text-xs px-1.5 py-0.5 rounded border font-mono",
              isTestnet
                ? "border-accent/40 text-accent bg-accent/5"
                : "border-red/40 text-red bg-red/5")}>
              {isTestnet ? "测试网" : "真实网"}
            </span>
            {form && (
              <span className="text-xs text-muted font-mono">
                {form.symbol} · {form.leverage}x · {(form.risk_pct * 100).toFixed(1)}%风险
              </span>
            )}
            {engine.running && engine.paper && (
              <span className="text-xs text-accent">纸面 · ${engine.paper_equity}</span>
            )}
          </div>
        </div>

        <div className="flex items-center gap-2">
          {msg && (
            <span className={clsx("text-xs flex items-center gap-1", msg.ok ? "text-green" : "text-red")}>
              {msg.ok ? <CheckCircle size={11} /> : <AlertCircle size={11} />} {msg.text}
            </span>
          )}
          {!engine.running && (
            <button onClick={() => setPaper(v => !v)}
              className={clsx("flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs border transition-colors",
                paper
                  ? "border-accent/50 bg-accent/10 text-accent"
                  : "border-red/40 bg-red/5 text-red")}>
              <span className={clsx("w-3 h-3 rounded border flex items-center justify-center shrink-0",
                paper ? "bg-accent border-accent" : "border-red")}>
                {paper && <span className="text-[8px] text-black font-bold">✓</span>}
              </span>
              {paper ? "纸面" : "实盘"}
            </button>
          )}
          <button onClick={toggleEngine} disabled={busy || !strat}
            className={clsx("flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-xs font-bold transition-colors disabled:opacity-50",
              engine.running
                ? "bg-red/10 border border-red/30 text-red hover:bg-red/20"
                : "bg-accent text-black hover:bg-accent/90")}>
            {busy ? <Loader size={12} className="animate-spin" />
              : engine.running ? <Square size={12} /> : <Play size={12} />}
            {engine.running ? "停止" : "启动策略"}
          </button>
          <button onClick={() => setExpanded(v => !v)}
            className="flex items-center gap-1 px-2.5 py-1.5 rounded-lg text-xs text-muted hover:text-white border border-border hover:border-border/80 transition-colors">
            {expanded ? <ChevronUp size={12} /> : <ChevronDown size={12} />}
            配置
          </button>
        </div>
      </div>

      {/* ── 运行状态条 ───────────────────────────────────────── */}
      {engine.circuit_open && (
        <div className="px-4 py-2 bg-red/5 border-b border-red/20 flex items-center gap-2 text-xs text-red">
          <AlertTriangle size={12} />
          熔断器触发：日内回撤超限，新入场已暂停。明日 UTC 0 点自动重置。
        </div>
      )}
      {engine.running && engine.last_action && (
        <div className="px-4 py-2 bg-surface/40 border-b border-border/40 text-xs text-muted truncate">
          {engine.last_action}
          {engine.trade_count > 0 && (
            <span className="ml-3 text-accent">共成交 {engine.trade_count} 次</span>
          )}
        </div>
      )}

      {/* ── 展开配置区 ──────────────────────────────────────── */}
      {expanded && form && (
        <div className="p-4 space-y-4 border-t border-border/60">

          {/* 基础参数 */}
          <div>
            <p className="text-xs text-muted font-medium mb-2 uppercase tracking-wide">基础参数</p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              <div className="col-span-2 md:col-span-1">
                <label className={lbl}>交易品种</label>
                <input list="eng-symlist" value={form.symbol}
                  onChange={e => setF("symbol", e.target.value.toUpperCase())}
                  className={inp} />
                <datalist id="eng-symlist">
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
                <input type="number" step={0.5}
                  value={(form.risk_pct * 100).toFixed(1)}
                  onChange={e => setF("risk_pct", Number(e.target.value) / 100)}
                  className={clsx(inp, form.risk_pct >= 0.05 && "border-red/50")} />
                {form.risk_pct >= 0.05 && (
                  <p className="text-[10px] text-red mt-0.5">⚠ ≥5% 偏激进</p>
                )}
              </div>
              <div>
                <label className={lbl}>K线周期</label>
                <select value={form.interval} onChange={e => setF("interval", e.target.value)}
                  className={inp}>
                  {["1m","3m","5m","15m","30m","1h"].map(v =>
                    <option key={v} value={v}>{v}</option>)}
                </select>
              </div>
            </div>
          </div>

          {/* ML 策略参数 */}
          <div>
            <p className="text-xs text-muted font-medium mb-2 uppercase tracking-wide">ML 策略参数</p>
            <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
              {PARAM_FIELDS.map(([k, label, step]) => (
                <div key={k}>
                  <label className={lbl}>{label}</label>
                  <input type="number" step={step}
                    value={params[k] ?? PARAM_DEFAULTS[k] ?? 0}
                    onChange={e => setP(k, e.target.value)}
                    className={inp} />
                </div>
              ))}
            </div>
          </div>

          {/* 入场阈值（只读） */}
          {thresholdCoins.length > 0 && (
            <div>
              <p className="text-xs text-muted font-medium mb-2 uppercase tracking-wide">
                入场阈值（模型自动校准，只读）
              </p>
              <div className="grid grid-cols-3 md:grid-cols-5 gap-x-6 gap-y-1">
                {thresholdCoins.map(coin => {
                  const row = thresholds[coin];
                  const lv = typeof row === "object" ? (row.long ?? row.long_threshold ?? "—") : row;
                  const sv = typeof row === "object" ? (row.short ?? row.short_threshold ?? "—") : "—";
                  return (
                    <div key={coin} className="text-xs flex justify-between font-mono py-0.5">
                      <span className="text-muted">{coin.replace("USDT", "")}</span>
                      <span className="text-cyan-400">
                        {typeof lv === "number" ? lv.toFixed(3) : lv}
                        {" / "}
                        {typeof sv === "number" ? sv.toFixed(3) : sv}
                      </span>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* 保存 */}
          <div className="flex items-center justify-end gap-3 pt-1">
            {msg && (
              <span className={clsx("text-xs flex items-center gap-1", msg.ok ? "text-green" : "text-red")}>
                {msg.ok ? <CheckCircle size={11} /> : <AlertCircle size={11} />} {msg.text}
              </span>
            )}
            <button onClick={handleSave} disabled={saving}
              className="flex items-center gap-1.5 bg-accent/10 border border-accent/40 text-accent px-4 py-1.5 rounded-lg text-xs font-bold hover:bg-accent/20 disabled:opacity-60 transition-colors">
              {saving ? <Loader size={12} className="animate-spin" /> : <Save size={12} />}
              保存配置
            </button>
          </div>
        </div>
      )}
    </div>
  );
}

// ── 主页面 ────────────────────────────────────────────────────────────────────

export default function Orders() {
  const { symbol, openOrders } = useApp();
  const [tab, setTab]       = useState(0);
  const [history, setHistory] = useState([]);
  const [trades, setTrades]   = useState([]);
  const [loading, setLoading] = useState(false);
  const [cancelling, setCancelling] = useState(null);

  const fetchData = useCallback(async () => {
    if (!symbol) return;
    setLoading(true);
    try {
      if (tab === 1) {
        const { data } = await getRecentTrades(symbol);
        setTrades(data.reverse());
      } else if (tab === 2) {
        const { data } = await getOrderHistory(symbol, 100);
        setHistory(data);
      }
    } catch (e) {
      console.error(e);
    } finally {
      setLoading(false);
    }
  }, [tab, symbol]);

  useEffect(() => { fetchData(); }, [fetchData]);

  const handleCancel = async (sym, orderId) => {
    setCancelling(orderId);
    try {
      await cancelOrder(sym, orderId);
    } catch (e) {
      alert(e.response?.data?.detail || "撤单失败");
    } finally {
      setCancelling(null);
    }
  };

  const fmt = (v, d = 2) => parseFloat(v || 0).toFixed(d);
  const fmtTime = ts => new Date(ts).toLocaleString("zh");

  return (
    <div className="space-y-0">
      <EnginePanel />

      <div className="bg-card border border-border rounded-xl overflow-hidden">
        <div className="flex items-center justify-between px-4 py-3 border-b border-border">
          <div className="flex gap-1">
            {TABS.map((t, i) => (
              <button key={t} onClick={() => setTab(i)}
                className={clsx("text-sm px-3 py-1.5 rounded-lg transition-colors",
                  tab === i ? "bg-accent text-black font-bold" : "text-muted hover:text-white")}>
                {t}
                {i === 0 && openOrders.length > 0 && (
                  <span className="ml-1 text-xs bg-accent/20 text-accent px-1.5 rounded-full">
                    {openOrders.length}
                  </span>
                )}
              </button>
            ))}
          </div>
          <button onClick={fetchData} disabled={loading}
            className="text-muted hover:text-white transition-colors">
            <RefreshCw size={14} className={loading ? "animate-spin" : ""} />
          </button>
        </div>

        <div className="overflow-x-auto">
          {/* 挂单 */}
          {tab === 0 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  {["时间", "品种", "方向", "类型", "数量", "价格", "触发价", "状态", "操作"].map(h => (
                    <th key={h} className="text-left px-4 py-2.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {openOrders.length === 0 ? (
                  <tr><td colSpan={9} className="text-center text-muted py-10">暂无挂单</td></tr>
                ) : openOrders.map(o => (
                  <tr key={o.orderId} className="border-b border-border/40 hover:bg-surface/30">
                    <td className="px-4 py-2.5 text-muted">{fmtTime(o.time)}</td>
                    <td className="px-4 py-2.5 font-medium">{o.symbol}</td>
                    <td className={clsx("px-4 py-2.5 font-bold", o.side === "BUY" ? "text-green" : "text-red")}>
                      {o.side === "BUY" ? "买/多" : "卖/空"}
                    </td>
                    <td className="px-4 py-2.5"><Badge type={o.type} /></td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.origQty, 3)}</td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.price) === "0.00" ? "市价" : fmt(o.price)}</td>
                    <td className="px-4 py-2.5 font-mono text-orange-400">{fmt(o.stopPrice) !== "0.00" ? fmt(o.stopPrice) : "—"}</td>
                    <td className="px-4 py-2.5"><StatusBadge status={o.status} /></td>
                    <td className="px-4 py-2.5">
                      <button onClick={() => handleCancel(o.symbol, o.orderId)}
                        disabled={cancelling === o.orderId}
                        className="text-red hover:text-red/70 transition-colors">
                        <X size={14} />
                      </button>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          {/* 成交记录 */}
          {tab === 1 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  {["时间", "品种", "方向", "价格", "数量", "手续费", "已实现盈亏"].map(h => (
                    <th key={h} className="text-left px-4 py-2.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {trades.length === 0 ? (
                  <tr><td colSpan={7} className="text-center text-muted py-10">暂无成交记录</td></tr>
                ) : trades.map((t, i) => {
                  const pnl = parseFloat(t.realizedPnl || 0);
                  return (
                    <tr key={i} className="border-b border-border/40 hover:bg-surface/30">
                      <td className="px-4 py-2.5 text-muted">{fmtTime(t.time)}</td>
                      <td className="px-4 py-2.5 font-medium">{t.symbol}</td>
                      <td className={clsx("px-4 py-2.5 font-bold", t.side === "BUY" ? "text-green" : "text-red")}>
                        {t.side === "BUY" ? "买/多" : "卖/空"}
                      </td>
                      <td className="px-4 py-2.5 font-mono">{fmt(t.price)}</td>
                      <td className="px-4 py-2.5 font-mono">{fmt(t.qty, 3)}</td>
                      <td className="px-4 py-2.5 font-mono text-muted">{fmt(t.commission, 4)} {t.commissionAsset}</td>
                      <td className={clsx("px-4 py-2.5 font-mono font-bold", pnl >= 0 ? "text-green" : "text-red")}>
                        {pnl !== 0 ? `${pnl >= 0 ? "+" : ""}${pnl.toFixed(2)}` : "—"}
                      </td>
                    </tr>
                  );
                })}
              </tbody>
            </table>
          )}

          {/* 历史订单 */}
          {tab === 2 && (
            <table className="w-full text-xs">
              <thead>
                <tr className="text-muted border-b border-border">
                  {["时间", "品种", "方向", "类型", "委托价", "成交价", "数量", "状态"].map(h => (
                    <th key={h} className="text-left px-4 py-2.5">{h}</th>
                  ))}
                </tr>
              </thead>
              <tbody>
                {history.length === 0 ? (
                  <tr><td colSpan={8} className="text-center text-muted py-10">暂无历史订单</td></tr>
                ) : history.map(o => (
                  <tr key={o.orderId} className="border-b border-border/40 hover:bg-surface/30">
                    <td className="px-4 py-2.5 text-muted">{fmtTime(o.time)}</td>
                    <td className="px-4 py-2.5 font-medium">{o.symbol}</td>
                    <td className={clsx("px-4 py-2.5 font-bold", o.side === "BUY" ? "text-green" : "text-red")}>
                      {o.side === "BUY" ? "买/多" : "卖/空"}
                    </td>
                    <td className="px-4 py-2.5"><Badge type={o.type} /></td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.price) === "0.00" ? "市价" : fmt(o.price)}</td>
                    <td className="px-4 py-2.5 font-mono text-accent">{fmt(o.avgPrice) !== "0.00" ? fmt(o.avgPrice) : "—"}</td>
                    <td className="px-4 py-2.5 font-mono">{fmt(o.origQty, 3)}</td>
                    <td className="px-4 py-2.5"><StatusBadge status={o.status} /></td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </div>
      </div>
    </div>
  );
}
