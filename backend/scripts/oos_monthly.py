"""
OOS 月度拆解分析：找出哪几个月是系统性亏损
"""
import sys, os
BACKEND = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)
import warnings; warnings.filterwarnings('ignore')

import pandas as pd
import numpy as np
from app.services.ml_strategy import (
    MLTrendParams, load_scored_bars, simulate_ml_trend, TradeRecord
)
from typing import List

SYMBOLS   = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT']
OOS_START = '2025-01-01'
OOS_END   = '2026-06-13'

def monthly_breakdown(trades: List[TradeRecord]) -> pd.DataFrame:
    rows = []
    for t in trades:
        dt = pd.Timestamp(int(t.exit_time), unit='ms')
        rows.append({
            'month':     dt.strftime('%Y-%m'),
            'direction': 'long' if t.direction == 1 else 'short',
            'pnl_r':     t.pnl_r,
            'reason':    t.reason,
            'win':       t.pnl_r > 0,
        })
    if not rows:
        return pd.DataFrame()
    df = pd.DataFrame(rows)
    grp = df.groupby('month').agg(
        n=('pnl_r', 'count'),
        total_r=('pnl_r', 'sum'),
        win_rate=('win', 'mean'),
        avg_r=('pnl_r', 'mean'),
        stop_n=('reason', lambda x: (x == 'stop').sum()),
    ).reset_index()
    grp['stop_rate'] = grp['stop_n'] / grp['n']
    grp['cumR'] = grp['total_r'].cumsum()
    return grp

print('加载数据并打分（首次较慢）...\n')

all_trades_by_month = {}

for sym in SYMBOLS:
    print(f'[{sym}] 加载+打分...', flush=True)
    bars = load_scored_bars(sym, start=OOS_START, end=OOS_END)
    params = MLTrendParams.from_thresholds(sym)
    trades = simulate_ml_trend(bars, params, symbol=sym)

    df = monthly_breakdown(trades)
    all_trades_by_month[sym] = (trades, df)

    print(f'  共 {len(trades)} 笔交易')
    if df.empty:
        print('  无交易\n'); continue

    print(f'  {"月份":<8} {"笔数":>4} {"总R":>8} {"胜率":>7} {"止损率":>7} {"累计R":>8}')
    print('  ' + '-'*50)
    for _, row in df.iterrows():
        mark = ' ←亏' if row['total_r'] < -5 else (' ★' if row['total_r'] > 5 else '')
        print(f'  {row["month"]:<8} {row["n"]:>4} {row["total_r"]:>8.2f}R '
              f'{row["win_rate"]:>6.1%} {row["stop_rate"]:>6.1%} '
              f'{row["cumR"]:>8.2f}R{mark}')
    print()

# ── 合并所有币月度汇总
print('=' * 65)
print('合并月度汇总（4 币）')
print('=' * 65)

all_trades = []
for sym, (trades, _) in all_trades_by_month.items():
    all_trades.extend(trades)

combined_df = monthly_breakdown(all_trades)
if not combined_df.empty:
    print(f'  {"月份":<8} {"笔数":>4} {"总R":>8} {"胜率":>7} {"止损率":>7} {"累计R":>8}')
    print('  ' + '-'*50)
    for _, row in combined_df.iterrows():
        mark = ' ←亏' if row['total_r'] < -10 else (' ★' if row['total_r'] > 10 else '')
        print(f'  {row["month"]:<8} {row["n"]:>4} {row["total_r"]:>8.2f}R '
              f'{row["win_rate"]:>6.1%} {row["stop_rate"]:>6.1%} '
              f'{row["cumR"]:>8.2f}R{mark}')

# ── 多空分离
print('\n' + '=' * 65)
print('多空方向对比（合并4币）')
print('=' * 65)
all_rows = []
for sym, (trades, _) in all_trades_by_month.items():
    for t in trades:
        dt = pd.Timestamp(int(t.exit_time), unit='ms')
        all_rows.append({'sym': sym, 'dir': 'long' if t.direction==1 else 'short',
                         'pnl_r': t.pnl_r, 'win': t.pnl_r > 0, 'reason': t.reason})
df_all = pd.DataFrame(all_rows)
if not df_all.empty:
    for d in ['long', 'short']:
        sub = df_all[df_all['dir'] == d]
        if sub.empty: continue
        print(f'  {d.upper()}: n={len(sub)}  胜率={sub["win"].mean():.1%}  '
              f'总R={sub["pnl_r"].sum():.2f}  avgR={sub["pnl_r"].mean():.3f}  '
              f'止损率={( sub["reason"]=="stop").mean():.1%}')
