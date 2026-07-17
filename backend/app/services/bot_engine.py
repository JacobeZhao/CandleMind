"""
策略引擎 — 后台 asyncio 任务
支持 ml_trend 策略。
"""
import asyncio
import math
from loguru import logger

from .trend_decision import (
    TrendFeatureSnapshot,
    TrendPositionSnapshot,
    decide_entry,
    decide_ml_exit,
)


def _base_df(client, symbol, interval, limit=100):
    import pandas as pd

    try:
        raw = client.futures_klines(
            symbol=symbol,
            interval=interval,
            limit=limit,
        )
        frame = pd.DataFrame(raw, columns=[
            "open_time", "open", "high", "low", "close", "volume",
            "close_time", "quote_volume", "trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        numeric = ["open", "high", "low", "close", "volume"]
        frame[numeric] = frame[numeric].astype(float)
        frame["open_time"] = pd.to_datetime(frame["open_time"], unit="ms")
        return frame
    except Exception:
        return pd.DataFrame()


def _atr_last(df, period=14):
    import numpy as np
    if len(df) < period + 1:
        c = df['close'].values
        return float(np.std(c[-period:])) if len(c) >= period else 1.0
    h, l, c = df['high'].values, df['low'].values, df['close'].values
    tr = np.maximum(h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
    return float(tr[-period:].mean())


def _trend_feature_snapshot(sig, feature_frame, params, fallback_close):
    """Adapt the model's completed feature row to the pure decision contract."""
    import pandas as pd

    row = None
    row_position = None
    if feature_frame is not None and not feature_frame.empty:
        if sig.feature_timestamp and 'open_time' in feature_frame.columns:
            target = pd.to_datetime(sig.feature_timestamp, utc=True, errors='coerce')
            times = pd.to_datetime(feature_frame['open_time'], utc=True, errors='coerce')
            matches = times == target
            if matches.any():
                row_position = int(matches.to_numpy().nonzero()[0][-1])
        if row_position is None:
            row_position = len(feature_frame) - 1
        row = feature_frame.iloc[row_position]

    def feature_value(name, fallback):
        if row is None or name not in row:
            return fallback
        value = pd.to_numeric(row[name], errors='coerce')
        return fallback if pd.isna(value) else float(value)

    monthly_sma = None
    if (
        params.monthly_trend_filter
        and row_position is not None
        and 'close' in feature_frame.columns
    ):
        closes = pd.to_numeric(
            feature_frame['close'].iloc[:row_position + 1], errors='coerce'
        )
        value = closes.rolling(params.monthly_sma_bars, min_periods=500).mean().iloc[-1]
        if not pd.isna(value):
            monthly_sma = float(value)

    return TrendFeatureSnapshot(
        long_prob=float(sig.long_prob),
        short_prob=float(sig.short_prob),
        close=feature_value('close', float(fallback_close)),
        vol_regime=feature_value('5m_vol_regime', 1.0),
        ema_align=feature_value('5m_ema_align_score', 0.0),
        hurst=feature_value('5m_hurst', 0.99),
        monthly_sma=monthly_sma,
        model_available=bool(sig.model_available),
        feature_fresh=bool(sig.feature_fresh),
        feature_timestamp=sig.feature_timestamp,
    )


def _trend_bars_held(entry_feature_timestamp, current_feature_timestamp):
    """Translate completed 5m feature timestamps into backtest bar age."""
    import pandas as pd

    entry = pd.to_datetime(entry_feature_timestamp, utc=True, errors='coerce')
    current = pd.to_datetime(current_feature_timestamp, utc=True, errors='coerce')
    if pd.isna(entry) or pd.isna(current):
        return 1
    elapsed = max(0.0, float((current - entry).total_seconds()))
    return max(1, int(elapsed // (5 * 60)))


class BotEngine:
    def __init__(self):
        self.running        = False
        self._task: asyncio.Task | None = None
        self.last_signal    = "NONE"
        self.last_action    = ""
        self.trade_count    = 0
        self.error_msg      = ""
        self._strategy_name = ""
        self._symbol        = ""
        self._filters       = {}     # symbol → (step_size, tick_size) 缓存
        self.paper          = False  # 纸面交易（模拟成交、不下真单）
        self._paper_pos     = None
        self.paper          = True
        self._open_trade    = None
        self._paper_cap     = 10000.0
        # Circuit breaker state
        self.circuit_open   = False  # True = entries halted by intraday drawdown
        self._day_date      = ""     # YYYY-MM-DD of the current baseline
        self._day_start_eq  = 0.0   # equity at start of today (UTC)
        # Direction frequency monitor — recent entry history for imbalance detection
        self._dir_history: list = []  # [(monotonic_seconds, direction)]
        self._last_ml_feature_timestamp: dict[str, str] = {}

    def _claim_ml_feature(self, symbol: str, sig) -> tuple[bool, str]:
        if not sig.model_available:
            return False, "model_unavailable"
        if not sig.feature_fresh:
            return False, "feature_stale"
        timestamp = sig.feature_timestamp
        if not timestamp:
            return False, "feature_timestamp_missing"
        if self._last_ml_feature_timestamp.get(symbol) == timestamp:
            return False, "duplicate_feature"
        self._last_ml_feature_timestamp[symbol] = timestamp
        return True, ""

    @property
    def status(self) -> dict:
        return {
            "running":        self.running,
            "last_signal":    self.last_signal,
            "last_action":    self.last_action,
            "trade_count":    self.trade_count,
            "error":          self.error_msg,
            "strategy_name":  self._strategy_name,
            "symbol":         self._symbol,
            "paper":          self.paper,
            "paper_equity":   round(self._paper_cap, 2) if self.paper else None,
            "circuit_open":   self.circuit_open,
        }

    async def start(self, client, cfg: dict):
        if self.running:
            return
        paper = bool(cfg.get("paper", True))
        if not paper and not cfg.get("live_authorized", False):
            raise ValueError("live trading requires explicit server authorization")
        self.running        = True
        self.error_msg      = ""
        self._strategy_name = cfg.get("name", cfg.get("strategy_type", ""))
        self._symbol        = cfg.get("symbol", "")
        self.paper          = paper
        if self.paper and self._paper_pos is None:
            self._paper_cap = float(cfg.get("initial_capital", 10000))
        self._task          = asyncio.create_task(self._loop(client, cfg))
        logger.info(f"Engine started: {cfg['symbol']} {cfg['interval']} "
                    f"[{cfg.get('strategy_type')}]{' PAPER' if self.paper else ''}")

    async def stop(self):
        self.running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Engine stopped")

    async def _loop(self, client, cfg: dict):
        symbol        = cfg["symbol"]
        interval      = cfg["interval"]
        leverage      = cfg.get("leverage", 5)
        risk_pct      = cfg.get("risk_pct", 0.01)
        check_sec     = cfg.get("check_interval", 60)
        strategy_type = cfg.get("strategy_type", "ml_trend")

        await asyncio.to_thread(self._setup, client, symbol, leverage)

        while self.running:
            try:
                await self._cycle(client, symbol, interval, strategy_type, cfg, risk_pct)
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.error_msg = str(e)
                logger.error(f"Engine cycle error: {e}")
            await asyncio.sleep(check_sec)

    def _setup(self, client, symbol: str, leverage: int):
        try:
            client.futures_change_leverage(symbol=symbol, leverage=leverage)
        except Exception:
            pass
        try:
            client.futures_change_margin_type(symbol=symbol, marginType="ISOLATED")
        except Exception:
            pass

    async def _cycle(self, client, symbol: str, interval: str,
                     strategy_type: str, cfg: dict, risk_pct: float):
        self.error_msg = ""
        strategy_params = cfg.get("strategy_params", {})

        if strategy_type == "ml_trend":
            await self._ml_trend_cycle(client, symbol, strategy_params, risk_pct)
            return

        self.last_signal = "NONE"
        self.last_action = f"未知策略类型: {strategy_type}"

    # ══ Pure-ML 趋势策略 ════════════════════════════════════════════════════════

    async def _ml_trend_cycle(self, client, symbol: str, params: dict, risk_pct: float):
        """ML 趋势策略主循环 — 不依赖 EMA/ADX 体系，直接用 ML 概率驱动入场/加仓/止损/早退。"""
        from . import ml_signal
        from .ml_strategy import (
            MLTrendParams,
            _add_tranche_qty,
            _initial_stop_price,
            _kelly_mult,
        )

        df5 = await asyncio.to_thread(
            lambda: _base_df(client, symbol, params.get("entry_interval", "5m"), 60))
        if len(df5) < 20:
            self.error_msg = "5m 数据不足"; return

        price = float(df5["close"].iloc[-1])
        atr5  = _atr_last(df5)

        sig = await asyncio.to_thread(ml_signal.get_ml_signal, symbol)
        feature_frame = await asyncio.to_thread(ml_signal._latest_features, symbol)
        ml_p = MLTrendParams.from_runtime_config(
            symbol, params, risk_pct=risk_pct
        )
        snapshot = _trend_feature_snapshot(sig, feature_frame, ml_p, price)
        allow_ml_decision, decision_reason = self._claim_ml_feature(symbol, sig)

        if self.paper:
            self._ml_trend_paper_step(
                symbol, params, sig, ml_p, snapshot, price, atr5, risk_pct, df5,
                allow_ml_decision=allow_ml_decision,
                decision_reason=decision_reason,
            )
            return

        positions = await asyncio.to_thread(client.futures_position_information, symbol=symbol)
        pos = next((p for p in positions if float(p["positionAmt"]) != 0), None)

        if pos:
            if not allow_ml_decision:
                self.last_action = (
                    f"[ML trend] holding with exchange stop: {decision_reason}"
                )
                return
            await self._manage_ml_trend_open(
                client, symbol, params, pos, price, atr5, sig, ml_p, snapshot
            )
            return
        self._open_trade = None

        if not allow_ml_decision:
            self.last_signal = "NONE"
            self.last_action = f"[ML trend] no decision: {decision_reason}"
            return

        entry_intent = decide_entry(symbol, snapshot, ml_p.to_decision_params())
        can_long = entry_intent.action == 'entry' and entry_intent.direction == 1
        can_short = entry_intent.action == 'entry' and entry_intent.direction == -1

        # Direction frequency guard — block shorts when short/long ratio > 10:1 (last 60 trades)
        if can_short and not can_long:
            import time as _t
            now_mono = _t.monotonic()
            cutoff   = now_mono - 30 * 24 * 3600
            self._dir_history = [(t, d) for t, d in self._dir_history if t > cutoff]
            recent = self._dir_history[-60:]
            if len(recent) >= 10:
                n_short = sum(1 for _, dd in recent if dd == -1)
                n_long  = sum(1 for _, dd in recent if dd == 1)
                if n_long == 0 or n_short / max(n_long, 1) > 10:
                    can_short = False
                    self.last_action = (f"[频率监控] 空/多={n_short}/{n_long} > 10:1，暂停做空 {symbol}")

        if not (can_long or can_short):
            self.last_signal = "NONE"
            self.last_action = (
                f"ML观望 reason={entry_intent.reason_code} "
                f"long={sig.long_prob:.3f} short={sig.short_prob:.3f}"
            )
            return

        # Extreme volatility guard — skip new entries on ATR spikes
        if self._extreme_vol(df5, atr5):
            self.last_signal = "NONE"
            self.last_action = f"[熔断] ATR极端波动({atr5:.4f})，跳过入场"
            return

        d = entry_intent.direction
        prob = entry_intent.probability
        stop = _initial_stop_price(price, atr5, d, ml_p.initial_stop_mult)

        balances = await asyncio.to_thread(client.futures_account_balance)
        usdt = next((float(b["availableBalance"]) for b in balances if b["asset"] == "USDT"), 0.0)
        if usdt < 10:
            self.error_msg = f"余额不足（{usdt:.2f} USDT）"; return

        # Intraday drawdown circuit breaker
        if self._check_circuit(usdt):
            self.last_signal = "NONE"
            self.last_action = f"[熔断] 日内回撤超限，暂停入场 equity={usdt:.2f}"
            return

        step, tick = self._get_filters(client, symbol)
        stop_dist = abs(price - stop)
        if stop_dist <= 0:
            self.error_msg = "ATR=0"; return

        kelly     = _kelly_mult(prob, ml_p.win_mult, ml_p.kelly_frac) * sig.pos_size_mult
        if sig.drift_warning:
            logger.warning(f"ML drift detected for {symbol}, pos_size_mult={sig.pos_size_mult:.2f}")
        base_qty  = usdt * risk_pct / stop_dist
        first_qty = self._round_step(base_qty * kelly, step)
        add_qty = self._round_step(
            _add_tranche_qty(first_qty, ml_p.add_size_frac, 0), step
        )
        if first_qty <= 0:
            self.error_msg = "首仓数量为 0"; return

        stop_r    = self._round_tick(stop, tick)
        side_open = "BUY" if d == 1 else "SELL"
        await asyncio.to_thread(client.futures_create_order, symbol=symbol,
                                side=side_open, type="MARKET", quantity=first_qty)
        try:
            stop_id = await self._place_close(
                client,
                symbol,
                "SELL" if d == 1 else "BUY",
                "STOP_MARKET",
                stop_r,
            )
        except Exception as stop_error:
            logger.exception("Initial protective stop failed for {}", symbol)
            self.running = False
            self.last_signal = "NONE"
            try:
                await self._close_all(client, symbol, first_qty * d, d)
            except Exception as flatten_error:
                self.error_msg = (
                    "CRITICAL: entry is unprotected and emergency close failed: "
                    f"stop={stop_error}; close={flatten_error}"
                )
                raise RuntimeError(self.error_msg) from flatten_error
            self.error_msg = f"protective stop failed; entry was closed: {stop_error}"
            self.last_action = "Engine halted after protective-stop failure"
            return

        self._open_trade = {
            "mode": "ml_trend", "dir": d, "adds": 0, "max_adds": ml_p.max_adds,
            "tranche": first_qty, "add_qty": add_qty,
            "add_size_frac": ml_p.add_size_frac, "step_size": step,
            "stop_price": stop_r, "stop_id": stop_id, "tp_id": None, "target": None,
            "peak": price, "last_add_ref": price, "atr_trail": ml_p.atr_trail,
            "avg": price, "init_risk": stop_dist, "be_done": False, "ptp_done": False,
            "decision_params": ml_p.to_decision_params(),
            "entry_feature_timestamp": sig.feature_timestamp,
        }
        self.last_signal = "LONG" if d == 1 else "SHORT"
        self.trade_count += 1
        self.last_action  = (f"[ML趋势] 开仓{'多' if d == 1 else '空'} "
                             f"prob={prob:.3f} kelly={kelly:.2f} qty={first_qty} "
                             f"@{price} stop={stop_r}")
        logger.info(f"ML trend entry dir={d} prob={prob:.3f} qty={first_qty} @{price} stop={stop_r}")
        import time as _t
        self._dir_history.append((_t.monotonic(), d))

    async def _manage_ml_trend_open(
        self, client, symbol, params, pos, price, atr5, sig, ml_p, snapshot
    ):
        """实盘 ML 趋势：持仓管理（跟踪止损 + ML 早退 + 加仓）。"""
        ot      = self._open_trade
        pos_amt = float(pos["positionAmt"])
        pos_dir = 1 if pos_amt > 0 else -1
        if ot is None:
            self.last_action = "[ML趋势] 持仓中（无上下文，等待止损）"; return

        d         = ot["dir"]
        same_prob = sig.long_prob  if d == 1 else sig.short_prob

        ot["peak"] = max(ot["peak"], price) if d == 1 else min(ot["peak"], price)
        new_stop = (ot["peak"] - ot["atr_trail"] * atr5 if d == 1
                    else ot["peak"] + ot["atr_trail"] * atr5)
        if ((d == 1 and new_stop > ot["stop_price"]) or
                (d == -1 and new_stop < ot["stop_price"])):
            await self._replace_stop(client, symbol, "SELL" if d == 1 else "BUY", new_stop)

        bars_held = _trend_bars_held(
            ot.get("entry_feature_timestamp"), sig.feature_timestamp
        )
        decision_params = ot.get("decision_params") or ml_p.to_decision_params()
        ml_intent = decide_ml_exit(
            snapshot,
            TrendPositionSnapshot(direction=d, bars_held=bars_held),
            decision_params,
        )
        if ml_intent.action in ('ml_exit', 'ml_reversal'):
            reason = "ML反向" if ml_intent.action == 'ml_reversal' else "ML降概"
            await self._close_all(client, symbol, pos_amt, pos_dir)
            self._open_trade = None
            self.last_action = f"[ML趋势] {reason}平仓 prob={same_prob:.3f}"
            logger.info(
                f"ML trend {ml_intent.reason_code} exit dir={d} prob={same_prob:.3f}"
            )
            return

        from .ml_strategy import _add_tranche_qty

        if "tranche" in ot and "step_size" in ot:
            add_qty = self._round_step(
                _add_tranche_qty(
                    ot["tranche"],
                    ot.get("add_size_frac", ml_p.add_size_frac),
                    ot["adds"],
                ),
                ot["step_size"],
            )
        else:
            add_qty = ot.get("add_qty", 0.0)
        if ot["adds"] < ot["max_adds"] and add_qty > 0:
            moved = abs(price - ot["last_add_ref"]) >= ml_p.add_atr_dist * atr5
            high_prob = same_prob >= ml_p.add_threshold
            if moved and high_prob:
                await asyncio.to_thread(client.futures_create_order, symbol=symbol,
                                        side="BUY" if d == 1 else "SELL",
                                        type="MARKET", quantity=add_qty)
                new_avg = (ot["avg"] * abs(pos_amt) + price * add_qty) / (abs(pos_amt) + add_qty)
                ot["avg"] = new_avg
                ot["adds"] += 1; ot["last_add_ref"] = price
                self.trade_count += 1
                # Avg-price anchor: after each add, ensure stop >= avg - 1.5×ATR (long)
                #                                              stop <= avg + 1.5×ATR (short)
                anchor = new_avg - 1.5 * atr5 if d == 1 else new_avg + 1.5 * atr5
                if (d == 1 and anchor > ot["stop_price"]) or (d == -1 and anchor < ot["stop_price"]):
                    await self._replace_stop(client, symbol, "SELL" if d == 1 else "BUY", anchor)
                self.last_action = f"[ML趋势] 加仓#{ot['adds']} prob={same_prob:.3f} @{price}"
                return

        self.last_action = (f"[ML趋势] 持仓{'多' if d == 1 else '空'} "
                            f"{ot['adds']}/{ot['max_adds']}档 prob={same_prob:.3f} "
                            f"stop={ot['stop_price']}")

    def _ml_trend_paper_step(
        self, symbol, params, sig, ml_p, snapshot, price, atr5, risk_pct, df5=None,
        *, allow_ml_decision=True, decision_reason="",
    ):
        """纸面 ML 趋势：持仓管理（ML 早退 > ATR 止损 > 加仓）以及入场。"""
        from .journal import append as jlog
        from .ml_strategy import _add_tranche_qty, _initial_stop_price, _kelly_mult
        fee = ml_p.fee
        pos = self._paper_pos

        if pos and pos.get("mode") == "ml_trend":
            d         = pos["dir"]
            same_prob = sig.long_prob  if d == 1 else sig.short_prob

            if allow_ml_decision:
                pos["peak"] = max(pos["peak"], price) if d == 1 else min(pos["peak"], price)
                new_stop = (pos["peak"] - ml_p.atr_trail * atr5 if d == 1
                            else pos["peak"] + ml_p.atr_trail * atr5)
                if ((d == 1 and new_stop > pos["stop"]) or
                        (d == -1 and new_stop < pos["stop"])):
                    pos["stop"] = new_stop

            stop_hit    = (d == 1 and price <= pos["stop"]) or (d == -1 and price >= pos["stop"])
            bars_held = _trend_bars_held(
                pos.get("entry_feature_timestamp"), sig.feature_timestamp
            )
            decision_params = pos.get("decision_params") or ml_p.to_decision_params()
            ml_intent = None
            if allow_ml_decision:
                ml_intent = decide_ml_exit(
                    snapshot,
                    TrendPositionSnapshot(direction=d, bars_held=bars_held),
                    decision_params,
                )

            reason_code = reason_label = exit_price = None
            if stop_hit:
                reason_code, reason_label, exit_price = "stop", "止损", pos["stop"]
            elif ml_intent and ml_intent.action == 'ml_reversal':
                reason_code, reason_label, exit_price = "ml_reversal", "ML反向", price
            elif ml_intent and ml_intent.action == 'ml_exit':
                reason_code, reason_label, exit_price = "ml_exit", "ML早退", price

            if reason_code:
                pnl = (exit_price - pos["avg"]) * pos["qty"] * d - exit_price * pos["qty"] * fee
                self._paper_cap += pnl
                self.trade_count += 1
                jlog({"type": "paper_exit", "symbol": symbol, "mode": "ml_trend",
                      "dir": d, "reason": reason_code, "exit": round(exit_price, 4),
                      "qty": round(pos["qty"], 6), "pnl": round(pnl, 2),
                      "equity": round(self._paper_cap, 2)})
                self.last_action = (f"[ML纸面] {reason_label}平仓 prob={same_prob:.3f} "
                                    f"pnl={round(pnl,2)} 权益={round(self._paper_cap,2)}")
                self._paper_pos = None
                return

            if allow_ml_decision and pos["adds"] < pos["max_adds"]:
                moved = abs(price - pos["last_add_ref"]) >= ml_p.add_atr_dist * atr5
                if moved and same_prob >= ml_p.add_threshold:
                    aq = _add_tranche_qty(
                        pos["tranche"], pos["add_size_frac"], pos["adds"]
                    )
                    self._paper_cap -= price * aq * fee
                    new_avg = (pos["avg"] * pos["qty"] + price * aq) / (pos["qty"] + aq)
                    pos["avg"] = new_avg
                    pos["qty"] += aq; pos["adds"] += 1; pos["last_add_ref"] = price
                    # Avg-price anchor stop
                    anchor = new_avg - 1.5 * atr5 if d == 1 else new_avg + 1.5 * atr5
                    if (d == 1 and anchor > pos["stop"]) or (d == -1 and anchor < pos["stop"]):
                        pos["stop"] = anchor
                    jlog({"type": "paper_add", "symbol": symbol, "mode": "ml_trend",
                          "dir": d, "price": round(price, 4), "qty": round(aq, 6),
                          "adds": pos["adds"], "prob": round(same_prob, 4)})
                    self.last_action = f"[ML纸面] 加仓#{pos['adds']} prob={same_prob:.3f} @{price}"
                    return

            if not allow_ml_decision:
                self.last_action = (
                    f"[ML paper] holding with risk stop: {decision_reason}"
                )
            else:
                self.last_action = (f"[ML纸面] 持仓{'多' if d == 1 else '空'} "
                                    f"{pos['adds']}/{pos['max_adds']}档 prob={same_prob:.3f} "
                                    f"stop={round(pos['stop'],2)}")
            return

        # ── 空仓 → 入场 ──────────────────────────────────────────────────────
        if not allow_ml_decision:
            self.last_signal = "NONE"
            self.last_action = f"[ML paper] no decision: {decision_reason}"
            return

        entry_intent = decide_entry(symbol, snapshot, ml_p.to_decision_params())

        if entry_intent.action != 'entry':
            self.last_signal = "NONE"
            self.last_action = (
                f"[ML纸面] 观望 reason={entry_intent.reason_code} "
                f"long={sig.long_prob:.3f} short={sig.short_prob:.3f}"
            )
            return

        # Extreme volatility guard
        if df5 is not None and self._extreme_vol(df5, atr5):
            self.last_signal = "NONE"
            self.last_action = f"[熔断] ATR极端波动({atr5:.4f})，跳过入场"
            return

        # Intraday drawdown circuit breaker
        if self._check_circuit(self._paper_cap):
            self.last_signal = "NONE"
            self.last_action = f"[熔断] 日内回撤超限，暂停入场 equity={self._paper_cap:.2f}"
            return

        d = entry_intent.direction
        prob = entry_intent.probability
        stop = _initial_stop_price(price, atr5, d, ml_p.initial_stop_mult)
        stop_dist = abs(price - stop)
        if stop_dist <= 0:
            return

        kelly     = _kelly_mult(prob, ml_p.win_mult, ml_p.kelly_frac) * sig.pos_size_mult
        base_qty  = self._paper_cap * risk_pct / stop_dist
        first_qty = base_qty * kelly
        self._paper_cap -= price * first_qty * fee
        self._paper_pos = {
            "mode": "ml_trend", "dir": d, "avg": price, "qty": first_qty,
            "tranche": first_qty, "add_size_frac": ml_p.add_size_frac,
            "stop": stop, "target": None,
            "peak": price, "last_add_ref": price,
            "adds": 0, "max_adds": ml_p.max_adds, "init_risk": stop_dist, "be": False,
            "decision_params": ml_p.to_decision_params(),
            "entry_feature_timestamp": sig.feature_timestamp,
        }
        self.trade_count += 1
        self.last_signal = "LONG" if d == 1 else "SHORT"
        jlog({"type": "paper_entry", "symbol": symbol, "mode": "ml_trend", "dir": d,
              "price": round(price, 4), "qty": round(first_qty, 6),
              "stop": round(stop, 2), "prob": round(prob, 4)})
        self.last_action = (f"[ML纸面] 开仓{'多' if d == 1 else '空'} "
                            f"prob={prob:.3f} kelly={kelly:.2f} @{price} stop={round(stop,2)}")

    def _ml_gate(self, symbol: str, direction: int, params: dict) -> bool:
        """
        ML 概率门。仅当 params['use_ml_filter']=True 时生效。
        模型确认方向 → True；拦截 → False；加载出错 → 放行(True)。
        """
        if not params.get('use_ml_filter', False):
            return True
        try:
            from .ml_signal import get_ml_signal
            sig = get_ml_signal(symbol)
            expected = 'long' if direction == 1 else 'short'
            if sig.action != expected:
                prob = sig.long_prob if direction == 1 else sig.short_prob
                thr  = sig.threshold_used
                logger.info(f'ML gate blocked [{symbol}] {expected} prob={prob:.3f} < thr={thr:.3f}')
                self.last_action = f'ML过滤·{expected} prob={prob:.3f}<{thr:.3f}'
                return False
            return True
        except Exception as e:
            logger.warning(f'ML gate error [{symbol}]: {e}')
            return True   # fail-open：模型不可用时不阻断策略

    async def _place_close(self, client, symbol, side, otype, stop_price):
        r = await asyncio.to_thread(client.futures_create_order, symbol=symbol, side=side,
                                    type=otype, stopPrice=stop_price, closePosition=True)
        return r.get("orderId") if isinstance(r, dict) else None

    async def _replace_stop(self, client, symbol, close_side, new_stop):
        ot = self._open_trade
        tick = self._get_filters(client, symbol)[1]
        new_r = self._round_tick(new_stop, tick)
        old_stop_id = ot.get("stop_id")
        try:
            new_stop_id = await self._place_close(
                client, symbol, close_side, "STOP_MARKET", new_r
            )
            if old_stop_id:
                try:
                    await asyncio.to_thread(
                        client.futures_cancel_order,
                        symbol=symbol,
                        orderId=old_stop_id,
                    )
                except Exception as exc:
                    logger.warning(
                        "New stop {} is active but old stop {} could not be cancelled: {}",
                        new_stop_id,
                        old_stop_id,
                        exc,
                    )
            ot["stop_id"] = new_stop_id
            ot["stop_price"] = new_r
        except Exception as e:
            logger.warning("Replacement stop failed; existing stop remains active: {}", e)

    def _check_circuit(self, equity: float, threshold: float = 0.06) -> bool:
        """
        Intraday drawdown circuit breaker.
        Returns True (circuit OPEN → block new entries) when equity has fallen
        more than `threshold` from today's UTC baseline.  Resets each UTC midnight.
        """
        import datetime
        today = datetime.datetime.utcnow().strftime('%Y-%m-%d')
        if self._day_date != today:
            self._day_date     = today
            self._day_start_eq = equity
            self.circuit_open  = False
            return False
        if self._day_start_eq <= 0:
            self._day_start_eq = equity
            return False
        dd = (self._day_start_eq - equity) / self._day_start_eq
        if dd >= threshold:
            if not self.circuit_open:
                logger.warning(
                    f"Circuit breaker OPEN: intraday DD={dd:.1%} >= {threshold:.1%}  "
                    f"start={self._day_start_eq:.2f} current={equity:.2f}"
                )
            self.circuit_open = True
        elif dd < threshold * 0.5:
            # Auto-reset once recovered to half the threshold
            if self.circuit_open:
                logger.info(f"Circuit breaker RESET: DD recovered to {dd:.1%}")
            self.circuit_open = False
        return self.circuit_open

    @staticmethod
    def _extreme_vol(df5, atr_short: float, multiplier: float = 3.0) -> bool:
        """
        Returns True when the current short-window ATR is more than `multiplier`×
        the 50-bar rolling average ATR — flags an abnormal intraday volatility spike.
        """
        import numpy as np
        if len(df5) < 50:
            return False
        h, l, c = df5['high'].values, df5['low'].values, df5['close'].values
        tr = np.maximum(h[1:] - l[1:], np.abs(h[1:] - c[:-1]), np.abs(l[1:] - c[:-1]))
        atr_long = float(tr[-50:].mean())
        return atr_long > 0 and atr_short > multiplier * atr_long

    async def _close_all(self, client, symbol, pos_amt, pos_dir):
        side = "SELL" if pos_dir == 1 else "BUY"
        await asyncio.to_thread(client.futures_create_order, symbol=symbol, side=side,
                                type="MARKET", quantity=abs(pos_amt), reduceOnly=True)
        try:
            await asyncio.to_thread(client.futures_cancel_all_open_orders, symbol=symbol)
        except Exception:
            pass

    def _get_filters(self, client, symbol: str):
        """Return validated exchange precision filters, cached by symbol."""
        if symbol in self._filters:
            return self._filters[symbol]
        try:
            info = client.futures_exchange_info()
        except Exception as exc:
            logger.exception("Failed to load exchange filters for {}", symbol)
            raise RuntimeError(f"exchange filters unavailable for {symbol}") from exc

        symbol_info = next(
            (item for item in info.get("symbols", []) if item.get("symbol") == symbol),
            None,
        )
        if symbol_info is None:
            raise RuntimeError(f"exchange filters missing symbol {symbol}")
        filters = {
            item.get("filterType"): item
            for item in symbol_info.get("filters", [])
        }
        try:
            market_lot = filters.get("MARKET_LOT_SIZE") or filters["LOT_SIZE"]
            step = float(market_lot["stepSize"])
            tick = float(filters["PRICE_FILTER"]["tickSize"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(f"exchange precision filters incomplete for {symbol}") from exc
        if not math.isfinite(step) or not math.isfinite(tick) or step <= 0 or tick <= 0:
            raise RuntimeError(f"exchange precision filters invalid for {symbol}")
        self._filters[symbol] = (step, tick)
        return step, tick

    @staticmethod
    def _round_step(qty: float, step: float) -> float:
        if step <= 0:
            return round(qty, 3)
        prec = max(0, int(round(-math.log10(step))))
        return round(math.floor(qty / step) * step, prec)

    @staticmethod
    def _round_tick(price: float, tick: float) -> float:
        if tick <= 0:
            return round(price, 4)
        prec = max(0, int(round(-math.log10(tick))))
        return round(round(price / tick) * tick, prec)


bot_engine = BotEngine()
