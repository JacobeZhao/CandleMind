"""
真 OOS 回测：2025-01-01 ~ 2026-06-13
模型训练截止 2024-06-30，此区间完全未参与训练/调参。
SOLUSDT 因校准结果极差（sharpe=-0.2）暂时排除。
"""
import sys, os
BACKEND = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

import warnings; warnings.filterwarnings('ignore')
from app.services.ml_strategy import backtest_multi, backtest_ml_trend, MLTrendParams, calc_metrics

SYMBOLS  = ['BTCUSDT', 'ETHUSDT', 'BNBUSDT', 'XRPUSDT']
OOS_START = '2025-01-01'
OOS_END   = '2026-06-13'

print('=' * 65)
print(f'  真 OOS 回测: {OOS_START} ~ {OOS_END}')
print(f'  币种: {SYMBOLS}  (SOLUSDT 暂排除)')
print('=' * 65)

results = backtest_multi(SYMBOLS, start=OOS_START, end=OOS_END)

print('\n\n' + '=' * 65)
print('  各币详细指标')
print('=' * 65)
for sym, r in results['by_symbol'].items():
    m = r.get('metrics', {})
    if not m or m.get('n_trades', 0) == 0:
        print(f'\n  {sym}: 无交易')
        continue
    print(f'\n  {sym}:')
    print(f'    交易数: {m["n_trades"]}  多:{m["long_trades"]}  空:{m["short_trades"]}')
    print(f'    胜率:   {m["win_rate"]:.1%}')
    print(f'    总R:    {m["total_r"]:.2f}R')
    print(f'    平均R:  {m["avg_r"]:.3f}R')
    print(f'    最大回撤:{m["max_dd_r"]:.2f}R')
    print(f'    盈亏比: {m["profit_factor"]:.2f}')
    print(f'    Sharpe: {m["sharpe"]:.3f}')
    print(f'    平均持仓:{m["avg_holds_bars"]:.0f} bars ({m["avg_holds_bars"]*5/60:.1f}h)')
    by_r = m.get('by_reason', {})
    for reason, stats in by_r.items():
        label = {'stop':'止损','ml_exit':'ML早退','ml_reversal':'ML反向','end':'收盘'}.get(reason, reason)
        print(f'    [{label}] n={stats["n"]}  胜率={stats["win_rate"]:.1%}  avgR={stats["avg_r"]:.3f}')

c = results['combined']
print(f'\n{"="*65}')
print(f'  合并汇总 (4币)')
print(f'  总交易:{c["n_trades"]}  胜率:{c["win_rate"]:.1%}  总R:{c["total_r"]:.2f}  MaxDD:{c["max_dd_r"]:.2f}R')
print(f'  PF:{c["profit_factor"]:.2f}  Sharpe:{c["sharpe"]:.3f}')
print('=' * 65)
