import React, { useState, useEffect, useRef } from "react";
import { runBacktest, downloadData, listStrategies, getSymbols } from "../api/client";
import { Play, Download, BarChart2, Activity, AlertCircle, Layers } from "lucide-react";
import clsx from "clsx";
import {
  AreaChart, Area, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer,
} from "recharts";

function defaultDates() {
  const end = new Date();
  const start = new Date();
  start.setMonth(start.getMonth() - 3);
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
}

const inp = "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-white outline-none focus:border-accent";
const lbl = "text-xs text-muted block mb-1";

function MetricCard({ label, value, color = "text-white", sub }) {
  return (
    <div className="bg-card border border-border rounded-xl p-4">
      <div className="text-xs text-muted mb-1">{label}</div>
      <div className={clsx("text-lg font-bold font-mono", color)}>{value}</div>
      {sub && <div className="text-xs text-muted mt-0.5">{sub}</div>}
    </div>
  );
}

function ProgressBar({ running, done }) {
  const [pct, setPct] = useState(0);
  const timerRef = useRef(null);
  useEffect(() => {
    if (running) {
      setPct(0);
      timerRef.current = setInterval(() => setPct(p => p >= 92 ? p
        : Math.min(92, p + (p < 30 ? 2 : p < 60 ? 1 : p < 80 ? 0.5 : 0.2))), 300);
    } else { clearInterval(timerRef.current); setPct(done ? 100 : 0); }
    return () => clearInterval(timerRef.current);
  }, [running, done]);
  if (!running && !done) return null;
  return (
    <div className="bg-card border border-border rounded-xl p-4">
      <div className="flex items-center justify-between mb-2">
        <span className="text-xs text-muted">{running ? "回测进行中..." : "回测完成"}</span>
        <span className="text-xs font-mono text-accent">{Math.round(pct)}%</span>
      </div>
      <div className="h-1.5 bg-surface rounded-full overflow-hidden">
        <div className="h-full rounded-full transition-all duration-300"
          style={{ width: `${pct}%`, background: pct < 100 ? "linear-gradient(90deg,#f0b90b,#f0b90b88)" : "#0ecb81" }} />
      </div>
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

function Toggle({ label, on, onClick }) {
  return (
    <button onClick={onClick}
      className={clsx("flex items-center gap-2 px-3 py-1.5 rounded-lg border text-xs transition-colors",
        on ? "border-accent/50 bg-accent/10 text-white" : "border-border text-muted hover:text-white")}>
      <span className={clsx("w-3.5 h-3.5 rounded border flex items-center justify-center shrink-0",
        on ? "bg-accent border-accent" : "border-border")}>
        {on && <span className="text-[9px] text-black font-bold">✓</span>}
      </span>
      {label}
    </button>
  );
}

export default function Backtest() {
  const dates = defaultDates();
  const [strat, setStrat]   = useState(null);
  const [symbols, setSymbols] = useState([]);

  const [startDate, setStartDate] = useState(dates.start);
  const [endDate, setEndDate]     = useState(dates.end);
  const [capital, setCapital]     = useState(10000);
  const [symbolOverride, setSymbolOverride] = useState("");
  const [riskOverride, setRiskOverride]     = useState("");   // 空=用策略值

  // 成本模型
  const [takerFee, setTakerFee]       = useState(0.04);  // %
  const [slippageBps, setSlippageBps] = useState(5);
  const [fundingRate, setFundingRate] = useState(0.01);  // %/8h
  const [useFunding, setUseFunding]   = useState(true);
  const [feeStress, setFeeStress]     = useState(false); // 手续费×2

  const [walkForward, setWalkForward] = useState(false);
  const [splitDate, setSplitDate]     = useState("");

  const [status, setStatus] = useState("idle");
  const [result, setResult] = useState(null);
  const [errMsg, setErrMsg] = useState("");
  const [dlMsg, setDlMsg]   = useState("");

  useEffect(() => {
    listStrategies().then(({ data }) => {
      setStrat(data.find(s => s.strategy_type === "ml_trend") || data[0] || null);
    }).catch(() => {});
    getSymbols().then(({ data }) => setSymbols(data)).catch(() => {});
  }, []);

  const effectiveSymbol = symbolOverride.trim().toUpperCase() || strat?.symbol || "";

  const handleRun = async () => {
    if (!strat) return;
    setStatus("running"); setResult(null); setErrMsg("");
    try {
      const payload = {
        strategy_id: strat.id, start_date: startDate, end_date: endDate,
        initial_capital: capital,
        taker_fee: takerFee / 100, slippage_bps: slippageBps,
        funding_rate: fundingRate / 100, use_funding: useFunding,
        fee_mult: feeStress ? 2 : 1,
        walk_forward: walkForward,
      };
      if (symbolOverride.trim()) payload.symbol = symbolOverride.trim().toUpperCase();
      if (riskOverride !== "") payload.risk_pct = Number(riskOverride) / 100;
      if (walkForward && splitDate) payload.split_date = splitDate;
      const { data } = await runBacktest(payload);
      setResult(data); setStatus("done");
    } catch (e) { setErrMsg(e.response?.data?.detail || e.message); setStatus("error"); }
  };

  const handleDownload = async () => {
    if (!strat) return;
    setDlMsg("下载中...");
    try {
      const { data } = await downloadData({
        symbol: effectiveSymbol, interval: strat.strategy_params?.entry_interval || "5m",
        start_date: startDate, end_date: endDate,
      });
      setDlMsg(data.message);
    } catch (e) { setDlMsg(e.response?.data?.detail || "下载失败"); }
  };

  const m = result?.metrics;
  const wf = result?.walk_forward;
  const equity = result?.equity_curve ?? [];
  const trades = result?.trades ?? [];
  const isProfit = m && m.total_return_pct >= 0;
  const chartData = equity.map(e => ({ t: e.time.slice(5, 16).replace("T", " "), equity: e.equity }));

  return (
    <div className="space-y-4 pb-8 max-w-6xl">
      {/* 策略头 */}
      <div className="bg-card border border-border rounded-xl p-4 flex items-center gap-3 flex-wrap">
        <Layers size={18} className="text-accent" />
        <div>
          <h1 className="text-base font-bold">{strat?.name || "加载中..."}</h1>
          <p className="text-xs text-muted">回测控制台 · ML 趋势策略</p>
        </div>
        {strat && (
          <div className="flex flex-wrap gap-x-4 gap-y-1 text-xs ml-auto">
            <span className="text-muted">入场阈值：<span className="text-white">
              {strat.strategy_params?.entry_threshold ?? "auto"}
            </span></span>
            <span className="text-muted">ATR止损：<span className="text-white">
              {strat.strategy_params?.atr_stop_mult != null ? `${strat.strategy_params.atr_stop_mult}×` : "—"}
            </span></span>
            <span className="text-muted">ML早退阈值：<span className="text-white">
              {strat.strategy_params?.ml_exit_threshold ?? "—"}
            </span></span>
            <span className="text-muted">杠杆：<span className="text-accent font-bold">{strat.leverage}x</span></span>
            <span className="text-muted">风险：<span className="text-white">{(strat.risk_pct * 100).toFixed(1)}%</span></span>
          </div>
        )}
      </div>

      {/* 回测参数 */}
      <Section title="回测参数" color="#8b94b2">
        <div className="grid grid-cols-2 md:grid-cols-3 lg:grid-cols-6 gap-3">
          <div className="col-span-2 lg:col-span-2">
            <label className={lbl}>品种（默认用策略设置）</label>
            <select value={symbolOverride} onChange={e => setSymbolOverride(e.target.value)} className={inp + " cursor-pointer"}>
              <option value="">— 策略默认 ({strat?.symbol || "BTCUSDT"}) —</option>
              {symbols.map(s => <option key={s} value={s}>{s.replace("USDT", "/USDT")}</option>)}
            </select>
          </div>
          <div><label className={lbl}>开始日期</label>
            <input type="date" value={startDate} onChange={e => setStartDate(e.target.value)} className={inp} /></div>
          <div><label className={lbl}>结束日期</label>
            <input type="date" value={endDate} onChange={e => setEndDate(e.target.value)} className={inp} /></div>
          <div><label className={lbl}>初始资金</label>
            <input type="number" value={capital} min={100} onChange={e => setCapital(Number(e.target.value))} className={inp} /></div>
          <div><label className={lbl}>风险% 覆盖（选填）</label>
            <input type="number" step={0.5} placeholder={`默认 ${(strat?.risk_pct * 100 || 0).toFixed(1)}`}
              value={riskOverride} onChange={e => setRiskOverride(e.target.value)} className={inp} /></div>
        </div>
      </Section>

      {/* 成本模型 */}
      <Section title="成本模型（真实摩擦）" color="#f59e0b">
        <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
          <div><label className={lbl}>Taker 手续费 %</label>
            <input type="number" step={0.01} value={takerFee} onChange={e => setTakerFee(Number(e.target.value))} className={inp} /></div>
          <div><label className={lbl}>滑点（基点 bps）</label>
            <input type="number" step={1} value={slippageBps} onChange={e => setSlippageBps(Number(e.target.value))} className={inp} /></div>
          <div><label className={lbl}>资金费率 %/8h</label>
            <input type="number" step={0.005} value={fundingRate} onChange={e => setFundingRate(Number(e.target.value))} className={inp} disabled={!useFunding} /></div>
          <div className="flex items-end gap-2 flex-wrap">
            <Toggle label="计入资金费" on={useFunding} onClick={() => setUseFunding(v => !v)} />
            <Toggle label="手续费×2 压测" on={feeStress} onClick={() => setFeeStress(v => !v)} />
          </div>
        </div>
        <p className="text-[10px] text-muted mt-2">保守原则：手续费×2 压测下仍盈利才靠谱。</p>
      </Section>

      {/* 样本外验证 */}
      <Section title="样本外验证" color="#a855f7">
        <div className="flex flex-wrap items-center gap-3">
          <Toggle label="样本外对比（前段调参 / 后段验证）" on={walkForward} onClick={() => setWalkForward(v => !v)} />
          {walkForward && (
            <div className="flex items-center gap-2">
              <span className="text-xs text-muted">切分日期</span>
              <input type="date" value={splitDate} onChange={e => setSplitDate(e.target.value)}
                className={inp + " w-40"} placeholder="默认中点" />
            </div>
          )}
        </div>
      </Section>

      {/* 操作 */}
      <div className="flex items-center gap-2">
        <button onClick={handleRun} disabled={status === "running" || !strat}
          className="flex items-center gap-2 bg-accent text-black px-5 py-2 rounded-lg text-sm font-bold hover:bg-accent/90 disabled:opacity-60 transition-colors">
          <Play size={14} /> {status === "running" ? "回测中..." : "开始回测"}
        </button>
        <button onClick={handleDownload} disabled={!strat || status === "running"}
          className="flex items-center gap-2 bg-surface border border-border text-muted hover:text-white px-4 py-2 rounded-lg text-sm transition-colors disabled:opacity-40">
          <Download size={14} /> 预下载数据
        </button>
        {dlMsg && <span className="text-xs text-muted">{dlMsg}</span>}
      </div>

      <ProgressBar running={status === "running"} done={status === "done"} />

      {status === "error" && (
        <div className="flex items-center gap-2 bg-red/10 border border-red/20 text-red rounded-xl px-4 py-3 text-sm">
          <AlertCircle size={16} /> {errMsg}
        </div>
      )}

      {/* 样本外对比 */}
      {wf && (
        <Section title={`样本外对比（切分 ${wf.split_date}）`} color="#a855f7">
          <table className="w-full text-xs">
            <thead><tr className="text-muted border-b border-border">
              {["段", "净收益%", "回撤%", "夏普", "胜率%", "笔数"].map(h => <th key={h} className="text-left pb-2 pr-4">{h}</th>)}
            </tr></thead>
            <tbody>
              {[["前段(调参)", wf.in_sample], ["后段(样本外)", wf.out_sample]].map(([name, s]) => (
                <tr key={name} className="border-b border-border/40">
                  <td className="py-1.5 pr-4 text-white">{name}</td>
                  <td className={clsx("pr-4 font-mono", s.total_return_pct >= 0 ? "text-green" : "text-red")}>{s.total_return_pct}</td>
                  <td className="pr-4 font-mono">{s.max_drawdown}</td>
                  <td className="pr-4 font-mono">{s.sharpe}</td>
                  <td className="pr-4 font-mono">{s.win_rate}</td>
                  <td className="pr-4 font-mono">{s.num_trades}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <p className="text-[10px] text-muted mt-2">前后段差异越大、后段越差，越说明参数对前段过拟合。</p>
        </Section>
      )}

      {/* 指标卡 */}
      {m && (
        <div className="grid grid-cols-2 md:grid-cols-4 lg:grid-cols-4 gap-3">
          <MetricCard label="净收益率（扣成本）"
            value={`${m.total_return_pct >= 0 ? "+" : ""}${m.total_return_pct}%`}
            color={isProfit ? "text-green" : "text-red"}
            sub={`毛收益 ${m.gross_return_pct >= 0 ? "+" : ""}${m.gross_return_pct}%`} />
          <MetricCard label="最大回撤" value={`-${m.max_drawdown}%`} color={m.max_drawdown > 20 ? "text-red" : "text-white"} />
          <MetricCard label="夏普比率" value={m.sharpe} color={m.sharpe >= 1 ? "text-green" : "text-muted"} />
          <MetricCard label="胜率" value={`${m.win_rate}%`} sub={`${m.num_trades} 笔 / 加仓 ${m.total_adds}`} />
          <MetricCard label="总手续费" value={`$${m.total_fees}`} color="text-red" />
          <MetricCard label="滑点成本" value={`$${m.slippage_cost}`} color="text-red" />
          <MetricCard label="资金费" value={`$${m.total_funding}`} color={m.total_funding > 0 ? "text-red" : "text-green"} />
          <MetricCard label="平均每笔盈亏" value={`$${m.avg_pnl}`} color={m.avg_pnl >= 0 ? "text-green" : "text-red"} />
        </div>
      )}

      {/* 权益曲线 */}
      {chartData.length > 0 && (
        <div className="bg-card border border-border rounded-xl p-4">
          <h3 className="text-sm font-semibold mb-3 flex items-center gap-2"><BarChart2 size={15} className="text-accent" /> 权益曲线</h3>
          <ResponsiveContainer width="100%" height={260}>
            <AreaChart data={chartData} margin={{ top: 4, right: 8, left: 0, bottom: 0 }}>
              <defs><linearGradient id="eqGrad" x1="0" y1="0" x2="0" y2="1">
                <stop offset="5%" stopColor={isProfit ? "#0ecb81" : "#f6465d"} stopOpacity={0.3} />
                <stop offset="95%" stopColor={isProfit ? "#0ecb81" : "#f6465d"} stopOpacity={0} />
              </linearGradient></defs>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e2436" />
              <XAxis dataKey="t" tick={{ fill: "#8b94b2", fontSize: 10 }} interval="preserveStartEnd" tickLine={false} />
              <YAxis tick={{ fill: "#8b94b2", fontSize: 10 }} tickLine={false} axisLine={false}
                tickFormatter={v => `$${v.toLocaleString()}`} width={70} />
              <Tooltip contentStyle={{ background: "#141823", border: "1px solid #1e2436", borderRadius: 8 }}
                labelStyle={{ color: "#8b94b2", fontSize: 11 }} formatter={v => [`$${v.toLocaleString()}`, "权益"]} />
              <Area type="monotone" dataKey="equity" stroke={isProfit ? "#0ecb81" : "#f6465d"} strokeWidth={2} fill="url(#eqGrad)" dot={false} />
            </AreaChart>
          </ResponsiveContainer>
        </div>
      )}

      {/* 交易明细 */}
      {trades.length > 0 && (
        <div className="bg-card border border-border rounded-xl p-4">
          <h3 className="text-sm font-semibold mb-3 flex items-center gap-2"><Activity size={15} className="text-accent" /> 交易明细（{trades.length} 笔）</h3>
          <div className="overflow-x-auto max-h-96 overflow-y-auto">
            <table className="w-full text-xs">
              <thead className="sticky top-0 bg-card"><tr className="text-muted border-b border-border">
                {["入场时间", "出场时间", "方向", "档", "入场价", "出场价", "盈亏", "收益率", "原因"].map(h =>
                  <th key={h} className="text-left pb-2 pr-3 font-medium">{h}</th>)}
              </tr></thead>
              <tbody>
                {trades.map((t, i) => (
                  <tr key={i} className="border-b border-border/40 hover:bg-surface/50">
                    <td className="py-1.5 pr-3 text-muted">{t.entry_time.slice(0, 16).replace("T", " ")}</td>
                    <td className="py-1.5 pr-3 text-muted">{t.exit_time.slice(0, 16).replace("T", " ")}</td>
                    <td className={clsx("pr-3 font-bold", t.side === "LONG" ? "text-green" : "text-red")}>{t.side === "LONG" ? "多" : "空"}</td>
                    <td className="pr-3 font-mono">{t.adds}</td>
                    <td className="pr-3 font-mono">{t.entry_price}</td>
                    <td className="pr-3 font-mono">{t.exit_price}</td>
                    <td className={clsx("pr-3 font-mono font-bold", t.pnl >= 0 ? "text-green" : "text-red")}>{t.pnl >= 0 ? "+" : ""}{t.pnl}</td>
                    <td className={clsx("pr-3 font-mono", t.pnl_pct >= 0 ? "text-green" : "text-red")}>{t.pnl_pct >= 0 ? "+" : ""}{t.pnl_pct}%</td>
                    <td className="pr-3 text-muted">{t.exit_reason}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      )}

      {status === "done" && trades.length === 0 && (
        <div className="bg-card border border-border rounded-xl px-4 py-8 text-center text-muted text-sm">
          该时间段内未产生任何交易，尝试调整时间范围或参数
        </div>
      )}
    </div>
  );
}
