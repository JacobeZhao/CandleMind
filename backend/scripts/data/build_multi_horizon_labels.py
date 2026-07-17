"""Generate multi-horizon triple-barrier labels for crypto 5m bars.

Output: G:/CandleMind/CandleMind_data/processed/labels/{SYMBOL}_{horizon}_causal_v3_labels.parquet
       columns: open_time, {side}_barrier_hit, {side}_profit_r,
                {side}_duration_bars, {side}_label, {side}_meta_label

Horizons: 30m, 1h, 4h
Sides:    long, short

Barrier: TP=1.5R, SL=1.0R (R = ATR[14] known at decision time)
Max hold: capped at horizon to mimic "horizon-expired" exit

Features stamped at bar i may use that bar's close, so execution starts at the
next tradable price, open[i + 1]. The output timestamp remains bar i to align
the target with information available when the decision is made.

Uses the 5m OHLCV parquet under normalized/ohlcv_parquet to build horizons by
rolling windows of 5m bars (6, 12, 48). This keeps alignment trivial (every
5m bar gets a 30m/1h/4h label) and avoids re-fetching higher-TF klines.
"""
import os, sys, warnings, argparse
from pathlib import Path
import numpy as np
import pandas as pd

warnings.filterwarnings('ignore')
BACKEND = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
os.chdir(BACKEND)

from app.datastore import PARQUET_DIR, LABELS_DIR

HORIZONS = {
    '30m': 6,    # 6 x 5m bars
    '1h':  12,   # 12 x 5m bars
    '4h':  48,   # 48 x 5m bars
}

TP_R    = 1.5
SL_R    = 1.0
ATR_N   = 14


def _atr(df, n=ATR_N):
    high, low, close = df['high'], df['low'], df['close']
    prev_close = close.shift(1)
    tr = pd.concat([
        (high - low).abs(),
        (high - prev_close).abs(),
        (low  - prev_close).abs(),
    ], axis=1).max(axis=1)
    rma = tr.ewm(alpha=1 / n, adjust=False).mean()
    return rma


def _barrier_label(open_arr, high_arr, low_arr, atr_arr, horizon, side):
    n = len(open_arr)
    hit   = np.empty(n, dtype=object)
    pr    = np.full(n, np.nan, dtype=np.float32)
    dur   = np.full(n, horizon, dtype=np.int32)
    label = np.zeros(n, dtype=np.int8)

    for i in range(n - horizon):
        r = float(atr_arr[i])
        if not np.isfinite(r) or r <= 0:
            hit[i] = 'time'; pr[i] = 0.0; dur[i] = horizon; label[i] = 0
            continue
        entry = float(open_arr[i + 1])
        if side == 'long':
            tp_price = entry + TP_R * r
            sl_price = entry - SL_R * r
        else:
            tp_price = entry - TP_R * r
            sl_price = entry + SL_R * r
        outcome = 'time'; exit_r = 0.0; exit_bars = horizon
        for k in range(1, horizon + 1):
            h = float(high_arr[i + k]); l = float(low_arr[i + k])
            if side == 'long':
                hit_tp = h >= tp_price; hit_sl = l <= sl_price
            else:
                hit_tp = l <= tp_price; hit_sl = h >= sl_price
            if hit_tp and hit_sl:
                outcome = 'sl'; exit_r = -SL_R; exit_bars = k; break
            if hit_tp:
                outcome = 'tp'; exit_r = TP_R; exit_bars = k; break
            if hit_sl:
                outcome = 'sl'; exit_r = -SL_R; exit_bars = k; break
        hit[i] = outcome; pr[i] = exit_r; dur[i] = exit_bars
        label[i] = 1 if exit_r > 0 else 0

    hit[n - horizon:]   = 'time'
    pr[n - horizon:]    = 0.0
    dur[n - horizon:]   = horizon
    label[n - horizon:] = 0
    return hit, pr, dur, label


def build_for_symbol(symbol, variant='causal_v3', verbose=True):
    pq = PARQUET_DIR / f'{symbol}_5m.parquet'
    if not pq.exists():
        print(f'  [{symbol}] missing {pq} -> skip'); return
    df = pd.read_parquet(pq).sort_values('open_time').reset_index(drop=True)
    if 'open_time' not in df.columns:
        print(f'  [{symbol}] parquet missing open_time -> skip'); return
    if df['open_time'].dtype.kind == 'i':
        df['open_time'] = pd.to_datetime(df['open_time'], unit='ms')
    if verbose:
        print(f'  [{symbol}] 5m bars={len(df):,}  range={df["open_time"].min()} -> {df["open_time"].max()}')

    atr = _atr(df).values
    open_a  = df['open'].values
    high_a  = df['high'].values
    low_a   = df['low'].values

    for horizon, h_bars in HORIZONS.items():
        out_path = LABELS_DIR / f'{symbol}_{horizon}_{variant}_labels.parquet'
        if out_path.exists() and out_path.stat().st_mtime > pq.stat().st_mtime:
            if verbose:
                print(f'  [{symbol}/{horizon}] up-to-date -> skip')
            continue
        out = pd.DataFrame({'open_time': df['open_time']})
        for side in ('long', 'short'):
            hit, pr, dur, lab = _barrier_label(
                open_a, high_a, low_a, atr, h_bars, side
            )
            out[f'{side}_barrier_hit']    = hit
            out[f'{side}_profit_r']       = pr
            out[f'{side}_duration_bars']  = dur
            out[f'{side}_label']          = lab
            out[f'{side}_meta_label']     = (hit == 'tp').astype(np.int8)
        out.to_parquet(out_path, index=False)
        if verbose:
            for side in ('long', 'short'):
                pos = out[f'{side}_label'].mean()
                tp_rate = (out[f'{side}_barrier_hit'] == 'tp').mean()
                sl_rate = (out[f'{side}_barrier_hit'] == 'sl').mean()
                med_dur = out[f'{side}_duration_bars'].median()
                print(f'  [{symbol}/{horizon}/{side}] pos={pos:.3f} tp={tp_rate:.3f} '
                      f'sl={sl_rate:.3f} med_dur={med_dur:.0f}  -> {out_path.name}')


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--symbol', default='BTCUSDT',
                    help='symbol or "all" (BTC, ETH, SOL, BNB, XRP)')
    ap.add_argument('--variant', default='causal_v3')
    args = ap.parse_args()
    syms = ['BTCUSDT', 'ETHUSDT', 'SOLUSDT', 'BNBUSDT', 'XRPUSDT'] \
        if args.symbol == 'all' else [args.symbol]
    for s in syms:
        build_for_symbol(s, variant=args.variant)


if __name__ == '__main__':
    main()
