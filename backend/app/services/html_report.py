"""
回测 HTML 报告：自包含单文件（内联 SVG 权益/回撤曲线 + 指标 + 逐笔表），
存 G 盘 reports/html，可在 web 端直接打开查看。无外部依赖、离线可看。
"""
import json
import html
from datetime import datetime

from ..datastore import REPORTS_DIR

HTML_DIR = REPORTS_DIR / "html"
HTML_DIR.mkdir(parents=True, exist_ok=True)


def _fmt(v, suf="", d=2):
    try:
        return f"{float(v):.{d}f}{suf}"
    except Exception:
        return "—"


def _equity_svg(equity, w=920, h=240, pad=30):
    pts = [(e.get("equity")) for e in equity if e.get("equity") is not None]
    if len(pts) < 2:
        return "<p style='color:#8b94b2'>无权益数据</p>"
    lo, hi = min(pts), max(pts)
    rng = (hi - lo) or 1
    n = len(pts)
    def x(i): return pad + i * (w - 2 * pad) / (n - 1)
    def y(v): return pad + (h - 2 * pad) * (1 - (v - lo) / rng)
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts))
    # 回撤填充（峰值回落）
    peak = pts[0]; dd_area = []
    for i, v in enumerate(pts):
        peak = max(peak, v)
        dd_area.append((i, v, peak))
    base0 = y(pts[0])
    # 基准线（初始资金）
    init = pts[0]
    grid = ""
    for frac in (0, 0.5, 1):
        gy = pad + (h - 2 * pad) * frac
        val = hi - rng * frac
        grid += f"<line x1='{pad}' y1='{gy:.0f}' x2='{w-pad}' y2='{gy:.0f}' stroke='#1e2436'/>"
        grid += f"<text x='4' y='{gy+3:.0f}' fill='#5b6680' font-size='10'>{val:.0f}</text>"
    last = pts[-1]
    color = "#0ecb81" if last >= init else "#f6465d"
    fill = f"M {x(0):.1f},{base0:.1f} " + " ".join(f"L {x(i):.1f},{y(v):.1f}" for i, v in enumerate(pts)) + f" L {x(n-1):.1f},{base0:.1f} Z"
    return f"""<svg viewBox='0 0 {w} {h}' width='100%' style='background:#0d1017;border:1px solid #1e2436;border-radius:8px'>
{grid}
<path d='{fill}' fill='{color}' opacity='0.08'/>
<polyline points='{line}' fill='none' stroke='{color}' stroke-width='1.5'/>
</svg>"""


def _card(label, val, color="#e6e9f0"):
    return f"<div style='background:#141823;border:1px solid #1e2436;border-radius:10px;padding:10px 14px'><div style='font-size:11px;color:#8b94b2'>{label}</div><div style='font-size:18px;font-weight:700;color:{color};font-family:monospace'>{val}</div></div>"


def render_backtest_html(meta: dict, result: dict) -> str:
    m = result.get("metrics", {})
    trades = result.get("trades", [])
    wf = result.get("walk_forward")
    wins = [t for t in trades if t.get("pnl", 0) > 0]
    losses = [t for t in trades if t.get("pnl", 0) <= 0]
    pf = (sum(t["pnl"] for t in wins) / abs(sum(t["pnl"] for t in losses))) if losses else 0
    net = m.get("total_return_pct", 0)
    ncol = "#0ecb81" if (net or 0) >= 0 else "#f6465d"

    cards = "".join([
        _card("净收益", _fmt(net, "%"), ncol),
        _card("毛收益(无成本)", _fmt(m.get("gross_return_pct"), "%")),
        _card("夏普", _fmt(m.get("sharpe"))),
        _card("最大回撤", _fmt(m.get("max_drawdown"), "%"), "#f6465d"),
        _card("胜率", _fmt(m.get("win_rate"), "%")),
        _card("盈利因子", _fmt(pf)),
        _card("交易数", str(m.get("num_trades", len(trades)))),
        _card("手续费", _fmt(m.get("total_fees"), "", 1)),
        _card("滑点成本", _fmt(m.get("slippage_cost"), "", 1)),
        _card("资金费", _fmt(m.get("total_funding"), "", 1)),
    ])

    wf_html = ""
    if wf:
        i, o = wf.get("in_sample", {}), wf.get("out_sample", {})
        wf_html = f"""<h3>样本外验证（切分 {html.escape(str(wf.get('split_date','')))}）</h3>
        <div style='display:flex;gap:12px'>
        {_card('训练段 收益', _fmt(i.get('total_return_pct'),'%'))}
        {_card('训练段 回撤', _fmt(i.get('max_drawdown'),'%'))}
        {_card('样本外 收益', _fmt(o.get('total_return_pct'),'%'), '#0ecb81' if (o.get('total_return_pct') or 0)>=0 else '#f6465d')}
        {_card('样本外 回撤', _fmt(o.get('max_drawdown'),'%'))}
        </div>"""

    rows = ""
    for t in trades[-200:][::-1]:
        pc = "#0ecb81" if t.get("pnl", 0) >= 0 else "#f6465d"
        rows += (f"<tr><td>{html.escape(str(t.get('entry_time','')))}</td><td>{html.escape(str(t.get('exit_time','')))}</td>"
                 f"<td>{html.escape(str(t.get('side','')))}</td><td>{t.get('adds','')}</td>"
                 f"<td>{_fmt(t.get('entry_price'),'',4)}</td><td>{_fmt(t.get('exit_price'),'',4)}</td>"
                 f"<td style='color:{pc}'>{_fmt(t.get('pnl'),'',1)}</td><td style='color:{pc}'>{_fmt(t.get('pnl_pct'),'%')}</td>"
                 f"<td>{html.escape(str(t.get('exit_reason','')))}</td></tr>")

    title = f"{meta.get('symbol','')} · {meta.get('strategy_type','')} · {html.escape(str(meta.get('period','')))}"
    return f"""<!doctype html><html lang=zh><head><meta charset=utf-8>
<meta name=viewport content="width=device-width,initial-scale=1">
<title>回测报告 {html.escape(title)}</title>
<style>
body{{background:#0a0c12;color:#e6e9f0;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;margin:0;padding:24px;max-width:1000px;margin:auto}}
h1{{font-size:20px;margin:0 0 4px}} h3{{font-size:14px;color:#c7ccd8;margin:22px 0 10px}}
.sub{{color:#8b94b2;font-size:12px;margin-bottom:18px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px}}
table{{width:100%;border-collapse:collapse;font-size:11px;margin-top:8px}}
th,td{{text-align:left;padding:5px 8px;border-bottom:1px solid #161b27}} th{{color:#8b94b2;position:sticky;top:0;background:#0a0c12}}
.tbl{{max-height:520px;overflow:auto;border:1px solid #1e2436;border-radius:8px;margin-top:8px}}
.tag{{display:inline-block;background:#141823;border:1px solid #1e2436;border-radius:6px;padding:2px 8px;font-size:11px;color:#8b94b2;margin-right:6px}}
</style></head><body>
<h1>回测报告 · {html.escape(title)}</h1>
<div class=sub>生成于 {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} ·
<span class=tag>初始资金 {meta.get('initial_capital','?')}</span>
<span class=tag>风险 {meta.get('risk_pct','?')}</span>
<span class=tag>执行 {meta.get('exec_mode','market')}</span></div>
<div class=cards>{cards}</div>
{wf_html}
<h3>权益曲线</h3>{_equity_svg(result.get('equity_curve', []))}
<h3>逐笔交易（最近 200）</h3>
<div class=tbl><table>
<thead><tr><th>入场</th><th>出场</th><th>方向</th><th>档</th><th>入场价</th><th>出场价</th><th>盈亏</th><th>盈亏%</th><th>离场</th></tr></thead>
<tbody>{rows}</tbody></table></div>
</body></html>"""


def save_report(meta: dict, result: dict) -> str:
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    name = f"{ts}_{meta.get('symbol','x')}_{meta.get('strategy_type','bt')}.html"
    (HTML_DIR / name).write_text(render_backtest_html(meta, result), encoding="utf-8")
    return name
