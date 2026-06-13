import React, { useState, useEffect } from "react";
import { runRobustness, runOptimize, runPortfolio, runAttribution, buildFeatures, getFeatures, listReports, reportUrl, getLeaderboard } from "../api/client";
import { Microscope, Play, Loader, AlertCircle, Trophy, Layers, Settings2, PieChart, Fingerprint, FileText, ExternalLink } from "lucide-react";
import clsx from "clsx";
import {
  LineChart, Line, BarChart, Bar, XAxis, YAxis, CartesianGrid, Tooltip, ResponsiveContainer, Cell,
} from "recharts";

const inp = "w-full bg-surface border border-border rounded-lg px-3 py-1.5 text-sm text-white outline-none focus:border-accent";
const lbl = "text-xs text-muted block mb-1";

function Card({ label, value, color = "text-white", sub }) {
  return (
    <div className="bg-card border border-border rounded-xl p-3">
      <div className="text-[11px] text-muted mb-1">{label}</div>
      <div className={clsx("text-lg font-bold font-mono", color)}>{value}</div>
      {sub && <div className="text-[10px] text-muted mt-0.5">{sub}</div>}
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

export default function Research() {
  const [symbols, setSymbols]   = useState("BTCUSDT,BNBUSDT,XRPUSDT");
  const [startDate, setStart]   = useState("2025-01-01");
  const [endDate, setEnd]       = useState("2026-06-05");
  const [splitDate, setSplit]   = useState("2026-01-01");
  const [windowM, setWindowM]   = useState(3);
  const [status, setStatus]     = useState("idle");
  const [rep, setRep]           = useState(null);
  const [err, setErr]           = useState("");
  const [board, setBoard]       = useState([]);
  const [boardMetric, setBoardMetric] = useState("oos_pct");
  const [pf, setPf]             = useState(null);
  const [opt, setOpt]           = useState(null);
  const [attr, setAttr]         = useState(null);
  const [feat, setFeat]         = useState(null);
  const [reports, setReports]   = useState([]);
  const [busy, setBusy]         = useState("");
  const loadReports = () => listReports().then(({ data }) => setReports(data)).catch(() => {});

  const loadBoard = (metric) => getLeaderboard(metric).then(({ data }) => setBoard(data)).catch(() => {});
  useEffect(() => { loadBoard(boardMetric); loadReports(); getFeatures().then(({ data }) => data.features?.length && setFeat(data)).catch(() => {}); }, []);
  const doFeatures = async () => {
    setBusy("feat");
    try { const { data } = await buildFeatures({ end: endDate }); setFeat(data); } catch (e) { setErr(e.response?.data?.detail || e.message); }
    finally { setBusy(""); }
  };

  const run = async () => {
    setStatus("running"); setErr(""); setRep(null);
    try {
      const { data } = await runRobustness({
        symbols: symbols.split(",").map(s => s.trim().toUpperCase()).filter(Boolean),
        start_date: startDate, end_date: endDate, split_date: splitDate, window_months: Number(windowM),
      });
      setRep(data); setStatus("done"); loadBoard(boardMetric);
    } catch (e) { setErr(e.response?.data?.detail || e.message); setStatus("error"); }
  };

  const symList = () => symbols.split(",").map(s => s.trim().toUpperCase()).filter(Boolean);
  const doPortfolio = async () => {
    setBusy("pf"); setPf(null);
    try { const { data } = await runPortfolio({ symbols: symList(), start_date: startDate, end_date: endDate });
      setPf(data); loadBoard(boardMetric); } catch (e) { setErr(e.response?.data?.detail || e.message); }
    finally { setBusy(""); }
  };
  const doOptimize = async () => {
    setBusy("opt"); setOpt(null);
    try { const { data } = await runOptimize({ symbols: symList(), start_date: startDate, end_date: endDate, split_date: splitDate });
      setOpt(data); loadBoard(boardMetric); } catch (e) { setErr(e.response?.data?.detail || e.message); }
    finally { setBusy(""); }
  };
  const doAttribution = async () => {
    setBusy("attr"); setAttr(null);
    try { const { data } = await runAttribution({ symbols: symList(), start_date: startDate, end_date: endDate });
      setAttr(data.attribution); loadBoard(boardMetric); } catch (e) { setErr(e.response?.data?.detail || e.message); }
    finally { setBusy(""); }
  };

  const ms = rep?.multi_symbol;
  const mc = rep?.monte_carlo;
  const sens = rep?.sensitivity;

  return (
    <div className="space-y-4 pb-8">
      <div className="flex items-center gap-2">
        <Microscope size={18} className="text-accent" />
        <div>
          <h1 className="text-lg font-bold">策略研究 / 稳健性</h1>
          <p className="text-xs text-muted">一键 walk-forward + 多品种 + 蒙特卡洛 + 参数敏感性，识别过拟合</p>
        </div>
      </div>

      {/* 配置 */}
      <Section title="稳健性报告参数" color="#8b94b2">
        <div className="grid grid-cols-2 md:grid-cols-5 gap-3">
          <div className="col-span-2"><label className={lbl}>品种（逗号分隔）</label>
            <input value={symbols} onChange={e => setSymbols(e.target.value)} className={inp} /></div>
          <div><label className={lbl}>开始</label>
            <input type="date" value={startDate} onChange={e => setStart(e.target.value)} className={inp} /></div>
          <div><label className={lbl}>结束</label>
            <input type="date" value={endDate} onChange={e => setEnd(e.target.value)} className={inp} /></div>
          <div><label className={lbl}>样本外切分</label>
            <input type="date" value={splitDate} onChange={e => setSplit(e.target.value)} className={inp} /></div>
          <div><label className={lbl}>滚动窗口(月)</label>
            <input type="number" value={windowM} min={1} onChange={e => setWindowM(e.target.value)} className={inp} /></div>
        </div>
        <button onClick={run} disabled={status === "running"}
          className="mt-3 flex items-center gap-2 bg-accent text-black px-5 py-2 rounded-lg text-sm font-bold hover:bg-accent/90 disabled:opacity-60 transition-colors">
          {status === "running" ? <Loader size={14} className="animate-spin" /> : <Play size={14} />}
          {status === "running" ? "计算中（多品种×多项检验，稍候）..." : "一键稳健性报告"}
        </button>
      </Section>

      {status === "error" && (
        <div className="flex items-center gap-2 bg-red/10 border border-red/20 text-red rounded-xl px-4 py-3 text-sm">
          <AlertCircle size={16} /> {err}
        </div>
      )}

      {/* 回测 HTML 报告 */}
      <Section title="回测报告（HTML · 点击在浏览器打开）" color="#22d3ee">
        <div className="flex items-center gap-2 mb-2">
          <FileText size={13} className="text-accent" />
          <span className="text-xs text-muted">每次回测自动生成自包含报告（权益曲线+指标+逐笔）</span>
          <button onClick={loadReports} className="text-xs text-muted hover:text-white border border-border rounded px-2 py-0.5">刷新</button>
        </div>
        <div className="max-h-64 overflow-y-auto space-y-1">
          {reports.length === 0 ? (
            <p className="text-xs text-muted py-3">还没有报告，去回测页跑一次（或下方组合回测）后出现</p>
          ) : reports.map(r => (
            <a key={r.name} href={reportUrl(r.name)} target="_blank" rel="noreferrer"
              className="flex items-center justify-between text-xs px-3 py-2 rounded-lg bg-surface/50 border border-border/50 hover:border-accent/40 hover:text-white text-muted transition-colors">
              <span className="flex items-center gap-2 truncate"><ExternalLink size={12} /> {r.name}</span>
              <span className="text-[10px] shrink-0 ml-2">{r.mtime} · {r.size_kb}KB</span>
            </a>
          ))}
        </div>
      </Section>

      {/* 组合回测 + 参数优化 */}
      <Section title="组合回测 / 参数优化" color="#06b6d4">
        <div className="flex flex-wrap gap-2">
          <button onClick={doPortfolio} disabled={!!busy}
            className="flex items-center gap-2 bg-surface border border-border text-sm text-muted hover:text-white px-4 py-2 rounded-lg disabled:opacity-50">
            {busy === "pf" ? <Loader size={14} className="animate-spin" /> : <Layers size={14} />} 组合回测（共享资金·合并回撤）
          </button>
          <button onClick={doOptimize} disabled={!!busy}
            className="flex items-center gap-2 bg-surface border border-border text-sm text-muted hover:text-white px-4 py-2 rounded-lg disabled:opacity-50">
            {busy === "opt" ? <Loader size={14} className="animate-spin" /> : <Settings2 size={14} />} 参数优化（OOS+防过拟合）
          </button>
          <button onClick={doAttribution} disabled={!!busy}
            className="flex items-center gap-2 bg-surface border border-border text-sm text-muted hover:text-white px-4 py-2 rounded-lg disabled:opacity-50">
            {busy === "attr" ? <Loader size={14} className="animate-spin" /> : <PieChart size={14} />} 归因分析（多空/加仓档/品种）
          </button>
        </div>
        {pf && (
          <div className="mt-3 flex flex-wrap gap-4 text-xs">
            <span>组合收益 <span className={clsx("font-bold", pf.metrics.total_return_pct >= 0 ? "text-green" : "text-red")}>{pf.metrics.total_return_pct}%</span></span>
            <span>合并回撤 <span className="text-white">{pf.metrics.max_drawdown}%</span></span>
            <span>夏普 <span className="text-white">{pf.metrics.sharpe}</span></span>
            <span className="text-muted">各品种: {Object.entries(pf.per_symbol).map(([s, v]) => `${s.replace("USDT", "")} ${v.net_pct}%`).join(" / ")}</span>
          </div>
        )}
        {opt && (
          <div className="mt-3">
            <div className="text-xs text-muted mb-1">测试 {opt.tested} 组 · 最优（按样本外+过拟合惩罚得分）：<span className="text-white">{JSON.stringify(opt.best.params)}</span></div>
            <table className="w-full text-xs">
              <thead><tr className="text-muted border-b border-border">
                {["参数", "IS均%", "OOS均%", "OOS正", "得分"].map(h => <th key={h} className="text-left pb-1 pr-3">{h}</th>)}
              </tr></thead>
              <tbody>
                {opt.results.slice(0, 8).map((r, i) => (
                  <tr key={i} className="border-b border-border/30">
                    <td className="py-1 pr-3 font-mono text-white">{JSON.stringify(r.params)}</td>
                    <td className="pr-3 font-mono text-muted">{r.is_avg}</td>
                    <td className={clsx("pr-3 font-mono", r.oos_avg >= 0 ? "text-green" : "text-red")}>{r.oos_avg}</td>
                    <td className="pr-3 font-mono">{r.oos_positive}/{r.total}</td>
                    <td className="pr-3 font-mono text-accent">{r.score}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
        {attr && (
          <div className="mt-3 grid md:grid-cols-3 gap-3">
            {[["按方向(多空)", "by_direction"], ["按加仓档", "by_adds"], ["按品种", "by_symbol"]].map(([title, key]) => (
              <div key={key}>
                <div className="text-xs text-muted mb-1">{title}</div>
                <table className="w-full text-[11px]">
                  <thead><tr className="text-muted border-b border-border">
                    {["", "盈亏", "胜率", "PF", "笔"].map(h => <th key={h} className="text-left pr-2 pb-1">{h}</th>)}
                  </tr></thead>
                  <tbody>
                    {Object.entries(attr[key]).map(([k, v]) => (
                      <tr key={k} className="border-b border-border/30">
                        <td className="pr-2 text-white">{k.replace("USDT", "")}</td>
                        <td className={clsx("pr-2 font-mono", v.pnl >= 0 ? "text-green" : "text-red")}>{v.pnl}</td>
                        <td className="pr-2 font-mono">{v.win_rate}%</td>
                        <td className="pr-2 font-mono">{v.profit_factor ?? "—"}</td>
                        <td className="pr-2 font-mono">{v.trades}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ))}
          </div>
        )}
      </Section>

      {/* 币种特征空间 */}
      <Section title="币种特征空间（每币特征 · 反比波动率配仓的依据）" color="#f472b6">
        <button onClick={doFeatures} disabled={!!busy}
          className="flex items-center gap-2 bg-surface border border-border text-sm text-muted hover:text-white px-4 py-2 rounded-lg disabled:opacity-50 mb-3">
          {busy === "feat" ? <Loader size={14} className="animate-spin" /> : <Fingerprint size={14} />} 构建/刷新特征库（~30币）
        </button>
        {feat && (
          <>
            <div className="overflow-x-auto max-h-96 overflow-y-auto">
              <table className="w-full text-[11px]">
                <thead className="sticky top-0 bg-card"><tr className="text-muted border-b border-border">
                  {["币", "vol%", "ATR%", "自相关", "方差比", "Hurst", "ADX", "日均额($M)"].map(h => <th key={h} className="text-left pr-3 pb-1">{h}</th>)}
                </tr></thead>
                <tbody>
                  {feat.features.map(r => (
                    <tr key={r.symbol} className="border-b border-border/30">
                      <td className="pr-3 text-white">{r.symbol.replace("USDT", "")}</td>
                      <td className="pr-3 font-mono">{r.vol}</td>
                      <td className="pr-3 font-mono">{r.atr_pct}</td>
                      <td className="pr-3 font-mono">{r.autocorr_1}</td>
                      <td className="pr-3 font-mono">{r.variance_ratio}</td>
                      <td className={clsx("pr-3 font-mono", r.hurst >= 0.52 ? "text-green" : r.hurst <= 0.46 ? "text-red" : "")}>{r.hurst}</td>
                      <td className="pr-3 font-mono">{r.adx_mean}</td>
                      <td className="pr-3 font-mono">{(r.dollar_vol / 1e6).toFixed(0)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            {feat.stability && (
              <p className="text-[10px] text-muted mt-2">
                跨期稳定性(2025 vs 2026)：{Object.entries(feat.stability).map(([k, v]) => `${k} ${v}`).join(" · ")}
                ——只有相关&gt;0.5 的特征(vol/自相关)可靠；ER/ADX 跨期不稳。
              </p>
            )}
            <p className="text-[10px] text-muted mt-1">
              注：实测「按特征逐币调参」留一交叉验证不泛化(5/22)，故仅用于**组合反比波动率配仓**与监控，不做每币参数适配。
            </p>
          </>
        )}
      </Section>

      {/* 多品种 */}
      {ms && (
        <Section title={`多品种结果（样本外切分 ${rep.split_date}）`} color="#0ecb81">
          <table className="w-full text-xs">
            <thead><tr className="text-muted border-b border-border">
              {["品种", "净收益%", "毛收益%", "回撤%", "夏普", "胜率%", "笔数", "训练%", "样本外%"].map(h =>
                <th key={h} className="text-left pb-2 pr-3">{h}</th>)}
            </tr></thead>
            <tbody>
              {ms.rows.map(r => (
                <tr key={r.symbol} className="border-b border-border/40">
                  <td className="py-1.5 pr-3 text-white font-medium">{r.symbol}</td>
                  <td className={clsx("pr-3 font-mono", r.net_pct >= 0 ? "text-green" : "text-red")}>{r.net_pct}</td>
                  <td className="pr-3 font-mono text-muted">{r.gross_pct}</td>
                  <td className="pr-3 font-mono">{r.max_drawdown}</td>
                  <td className="pr-3 font-mono">{r.sharpe}</td>
                  <td className="pr-3 font-mono">{r.win_rate}</td>
                  <td className="pr-3 font-mono">{r.num_trades}</td>
                  <td className="pr-3 font-mono text-muted">{r.is_pct}</td>
                  <td className={clsx("pr-3 font-mono font-bold", r.oos_pct >= 0 ? "text-green" : "text-red")}>{r.oos_pct}</td>
                </tr>
              ))}
            </tbody>
          </table>
          <div className="flex gap-4 mt-2 text-xs text-muted">
            <span>平均净 <span className="text-white font-bold">{ms.summary.avg_net}%</span></span>
            <span>正收益 <span className="text-white">{ms.summary.positive}/{ms.summary.total}</span></span>
            <span>样本外均值 <span className="text-white font-bold">{ms.summary.avg_oos}%</span> · 正 {ms.summary.oos_positive}/{ms.summary.total}</span>
            <span>最差 <span className="text-red">{ms.summary.worst}%</span></span>
          </div>
        </Section>
      )}

      {/* walk-forward + 蒙特卡洛 */}
      {rep && (
        <div className="grid md:grid-cols-2 gap-4">
          <Section title="滚动 walk-forward 一致性" color="#3b82f6">
            {rep.walk_forward.map(w => (
              <div key={w.symbol} className="mb-3">
                <div className="flex justify-between text-xs mb-1">
                  <span className="text-white">{w.symbol}</span>
                  <span className={clsx(w.consistency_pct >= 60 ? "text-green" : w.consistency_pct >= 40 ? "text-accent" : "text-red")}>
                    正收益窗口 {w.positive}/{w.total}（{w.consistency_pct}%）
                  </span>
                </div>
                <ResponsiveContainer width="100%" height={70}>
                  <BarChart data={w.windows} margin={{ top: 2, right: 4, left: 0, bottom: 0 }}>
                    <XAxis dataKey="window" hide /><YAxis hide />
                    <Tooltip contentStyle={{ background: "#141823", border: "1px solid #1e2436", borderRadius: 8, fontSize: 11 }}
                      formatter={(v) => [`${v}%`, "收益"]} />
                    <Bar dataKey="return_pct">
                      {w.windows.map((x, i) => <Cell key={i} fill={x.return_pct >= 0 ? "#0ecb81" : "#f6465d"} />)}
                    </Bar>
                  </BarChart>
                </ResponsiveContainer>
              </div>
            ))}
            <p className="text-[10px] text-muted">每根=一个滚动窗口收益；绿多红少且一致性高=稳健，反之=靠少数行情。</p>
          </Section>

          <Section title="蒙特卡洛（自助重采样 1000 次）" color="#a855f7">
            {mc?.error ? <p className="text-xs text-muted">{mc.error}</p> : mc && (
              <>
                <div className="grid grid-cols-3 gap-2">
                  <Card label="收益中位" value={`${mc.ret_median_pct}%`} color="text-green" />
                  <Card label="收益5分位(悲观)" value={`${mc.ret_p05_pct}%`} color={mc.ret_p05_pct >= 0 ? "text-green" : "text-red"} />
                  <Card label="收益95分位" value={`${mc.ret_p95_pct}%`} />
                  <Card label="回撤中位" value={`${mc.maxdd_median}%`} />
                  <Card label="回撤95分位(坏情况)" value={`${mc.maxdd_p95}%`} color="text-red" />
                  <Card label="破产概率(亏50%)" value={`${mc.risk_of_ruin_pct}%`}
                    color={mc.risk_of_ruin_pct > 1 ? "text-red" : "text-green"} />
                </div>
                <p className="text-[10px] text-muted mt-2">破产概率=有放回重抽交易后跌破本金一半的比例；越接近 0 越安全。</p>
              </>
            )}
          </Section>
        </div>
      )}

      {/* 敏感性 */}
      {sens && (
        <Section title={`参数敏感性：${sens.param}（平坦=稳健，尖刺=过拟合）`} color="#f59e0b">
          <ResponsiveContainer width="100%" height={220}>
            <LineChart data={sens.curve} margin={{ top: 8, right: 12, left: 0, bottom: 0 }}>
              <CartesianGrid strokeDasharray="3 3" stroke="#1e2436" />
              <XAxis dataKey="value" tick={{ fill: "#8b94b2", fontSize: 10 }} />
              <YAxis tick={{ fill: "#8b94b2", fontSize: 10 }} tickFormatter={v => `${v}%`} width={45} />
              <Tooltip contentStyle={{ background: "#141823", border: "1px solid #1e2436", borderRadius: 8, fontSize: 11 }} />
              <Line type="monotone" dataKey="net_pct" name="全程净%" stroke="#f0b90b" strokeWidth={2} dot={{ r: 3 }} />
              <Line type="monotone" dataKey="oos_pct" name="样本外%" stroke="#0ecb81" strokeWidth={2} dot={{ r: 3 }} />
            </LineChart>
          </ResponsiveContainer>
        </Section>
      )}

      {/* 实验榜单 */}
      <Section title="实验榜单（历史回测/研究存档）" color="#eab308">
        <div className="flex items-center gap-2 mb-2">
          <Trophy size={13} className="text-accent" />
          <span className="text-xs text-muted">排序：</span>
          {[["oos_pct", "样本外"], ["net_pct", "净收益"], ["sharpe", "夏普"]].map(([k, l]) => (
            <button key={k} onClick={() => { setBoardMetric(k); loadBoard(k); }}
              className={clsx("text-xs px-2 py-0.5 rounded border", boardMetric === k ? "border-accent text-accent bg-accent/10" : "border-border text-muted")}>
              {l}
            </button>
          ))}
        </div>
        <div className="overflow-x-auto max-h-80 overflow-y-auto">
          <table className="w-full text-xs">
            <thead className="sticky top-0 bg-card"><tr className="text-muted border-b border-border">
              {["时间", "类型", "品种", "区间", "净%", "样本外%", "夏普", "回撤%", "笔数"].map(h =>
                <th key={h} className="text-left pb-2 pr-3 font-medium">{h}</th>)}
            </tr></thead>
            <tbody>
              {board.map(r => (
                <tr key={r.id} className="border-b border-border/40 hover:bg-surface/50">
                  <td className="py-1.5 pr-3 text-muted">{r.ts?.slice(5, 16)}</td>
                  <td className="pr-3">{r.kind}</td>
                  <td className="pr-3 text-white">{r.symbols}</td>
                  <td className="pr-3 text-muted">{r.period}</td>
                  <td className={clsx("pr-3 font-mono", (r.net_pct ?? 0) >= 0 ? "text-green" : "text-red")}>{r.net_pct ?? "—"}</td>
                  <td className={clsx("pr-3 font-mono font-bold", (r.oos_pct ?? 0) >= 0 ? "text-green" : "text-red")}>{r.oos_pct ?? "—"}</td>
                  <td className="pr-3 font-mono">{r.sharpe ?? "—"}</td>
                  <td className="pr-3 font-mono">{r.max_dd ?? "—"}</td>
                  <td className="pr-3 font-mono">{r.num_trades ?? "—"}</td>
                </tr>
              ))}
              {board.length === 0 && <tr><td colSpan={9} className="text-center text-muted py-6">还没有实验记录，跑一次回测或稳健性报告后出现</td></tr>}
            </tbody>
          </table>
        </div>
      </Section>
    </div>
  );
}
