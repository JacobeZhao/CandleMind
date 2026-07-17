import json
from sqlalchemy import create_engine, Column, String, Boolean, Float, Integer, Text
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from .runtime_paths import RUNTIME_DATA_DIR


data_dir = RUNTIME_DATA_DIR
data_dir.mkdir(parents=True, exist_ok=True)

DATABASE_URL = f"sqlite:///{data_dir}/trader.db"
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
SessionLocal = sessionmaker(bind=engine)


class Base(DeclarativeBase):
    pass


class Settings(Base):
    __tablename__ = "settings"
    id             = Column(Integer, primary_key=True, default=1)
    api_key_enc    = Column(Text,        nullable=True)   # legacy（迁移用）
    api_secret_enc = Column(Text,        nullable=True)
    # 测试网 / 真实网 两套 API，永久存盘，按 testnet 选当前激活
    api_key_test_enc    = Column(Text, nullable=True)
    api_secret_test_enc = Column(Text, nullable=True)
    api_key_main_enc    = Column(Text, nullable=True)
    api_secret_main_enc = Column(Text, nullable=True)
    testnet        = Column(Boolean,     default=True)
    symbol         = Column(String(20),  default="BTCUSDT")   # chart default
    interval       = Column(String(10),  default="15m")        # chart default
    proxy_url      = Column(String(200), nullable=True)


def active_keys(s) -> tuple:
    """按 testnet 标志返回当前激活的 (api_key_enc, api_secret_enc)。"""
    if s.testnet:
        return (s.api_key_test_enc or s.api_key_enc, s.api_secret_test_enc or s.api_secret_enc)
    return (s.api_key_main_enc, s.api_secret_main_enc)


class Strategy(Base):
    """Trading strategy — replaces the old Bot model."""
    __tablename__ = "strategies"
    id                  = Column(Integer,     primary_key=True, autoincrement=True)
    name                = Column(String(100), nullable=False)
    description         = Column(Text,        nullable=True)
    symbol              = Column(String(20),  default="BTCUSDT")
    interval            = Column(String(10),  default="15m")
    leverage            = Column(Integer,     default=5)
    risk_pct            = Column(Float,       default=0.01)
    stop_loss_pct       = Column(Float,       default=0.015)
    take_profit_pct     = Column(Float,       default=0.03)
    strategy_type       = Column(String(30),  default="supertrend")  # supertrend/macd/...
    strategy_params_json= Column(Text,        nullable=True)          # JSON params for built-in
    ai_strategy_json    = Column(Text,        nullable=True)          # AI-parsed conditions
    is_active           = Column(Boolean,     default=False)
    is_default          = Column(Boolean,     default=False)


class AIConfig(Base):
    __tablename__ = "ai_configs"
    id          = Column(Integer,     primary_key=True, autoincrement=True)
    name        = Column(String(50),  nullable=False)
    provider    = Column(String(30),  nullable=False)
    api_key_enc = Column(Text,        nullable=True)
    base_url    = Column(String(200), nullable=True)
    model_name  = Column(String(100), nullable=True)
    is_active   = Column(Boolean,     default=False)


# ── Default strategies seeded on first boot ──────────────────────────────────

_ML_TREND_PARAMS = json.dumps({
    "exit_threshold": 0.43,       # +8: zombie zone消灭（原0.35）
    "reversal_threshold": 0.53,   # +8: 缩小zombie zone（原0.58）
    "atr_trail": 3.0,
    "initial_stop_mult": 3.0,     # +2: BTC会覆盖为5.0，降低R分母
    "time_exit_bars": 12,         # +8: 假趋势保护窗口缩短为1h（原48=4h）
    "max_adds": 3,
    "add_min_atr": 0.8,
    "kelly_frac": 0.25,           # +7: BTC会覆盖为0.10，BNB为0.15
    "add_size_frac": 0.5,
    "min_hold_bars": 0,           # +5: BTC/BNB会覆盖为4（禁止极短持仓）
    "min_prob_gap_large_cap": 0.06, # +3: 大市值需更强方向确信才入场
    "fee": 0.0004,
    "slippage": 0.0002,
    "funding_rate_8h": 0.0001,
    "vol_gate": True,
    "ema_align_gate": True,
    "time_weighted_exit": True,
    "regime_kelly": True,
    "hurst_gate": True,           # +6: Hurst<0.50时禁止入场
    "hurst_entry_min": 0.50,
    # entry thresholds auto-loaded per-coin from thresholds.json
})

DEFAULT_STRATEGIES = [
    {
        "name": "SuperTrend 标准",
        "description": "超级趋势策略，ATR周期10，乘数3.0。趋势方向翻转时开仓，适合趋势行情。",
        "symbol": "BTCUSDT", "interval": "15m", "leverage": 5,
        "risk_pct": 0.01, "stop_loss_pct": 0.015, "take_profit_pct": 0.03,
        "strategy_type": "supertrend",
        "strategy_params_json": json.dumps({"atr_period": 10, "multiplier": 3.0}),
    },
    {
        "name": "MACD 金叉死叉",
        "description": "MACD线上穿信号线做多，下穿做空。适合趋势明显、波动较大的市场。",
        "symbol": "BTCUSDT", "interval": "1h", "leverage": 3,
        "risk_pct": 0.01, "stop_loss_pct": 0.02, "take_profit_pct": 0.04,
        "strategy_type": "macd",
        "strategy_params_json": json.dumps({"fast": 12, "slow": 26, "signal": 9}),
    },
    {
        "name": "布林带突破",
        "description": "价格收盘突破布林带上轨做多，跌破下轨做空。波动扩张时信号有效。",
        "symbol": "BTCUSDT", "interval": "4h", "leverage": 3,
        "risk_pct": 0.01, "stop_loss_pct": 0.02, "take_profit_pct": 0.05,
        "strategy_type": "bb_breakout",
        "strategy_params_json": json.dumps({"period": 20, "std_dev": 2.0}),
    },
    {
        "name": "RSI 超买超卖",
        "description": "RSI从超卖区回升做多，从超买区回落做空。适合震荡行情。",
        "symbol": "BTCUSDT", "interval": "1h", "leverage": 3,
        "risk_pct": 0.01, "stop_loss_pct": 0.02, "take_profit_pct": 0.04,
        "strategy_type": "rsi",
        "strategy_params_json": json.dumps({"period": 14, "oversold": 30, "overbought": 70}),
    },
    {
        "name": "EMA 金叉死叉",
        "description": "EMA12/26均线金叉做多，死叉做空。经典趋势跟踪策略。",
        "symbol": "BTCUSDT", "interval": "15m", "leverage": 5,
        "risk_pct": 0.01, "stop_loss_pct": 0.015, "take_profit_pct": 0.03,
        "strategy_type": "ema_cross",
        "strategy_params_json": json.dumps({"fast": 12, "slow": 26}),
    },
    {
        "name": "ADX 趋势过滤",
        "description": "ADX强势确认后跟随超级趋势方向，减少震荡行情误入。",
        "symbol": "BTCUSDT", "interval": "1h", "leverage": 3,
        "risk_pct": 0.01, "stop_loss_pct": 0.02, "take_profit_pct": 0.05,
        "strategy_type": "adx_trend",
        "strategy_params_json": json.dumps({"period": 14, "adx_threshold": 25}),
    },
    {
        "name": "ML趋势策略",
        "description": "机器学习驱动的纯趋势策略。ML概率入场，ATR跟踪止损，ML降概早退（假趋势少亏），高概率加仓（真趋势多吃）。",
        "symbol": "BTCUSDT", "interval": "5m", "leverage": 5,
        "risk_pct": 0.01, "stop_loss_pct": 0.015, "take_profit_pct": 0.03,
        "strategy_type": "ml_trend",
        "strategy_params_json": _ML_TREND_PARAMS,
    },
]


def _add_col_if_missing(conn, table: str, col: str, col_def: str):
    from sqlalchemy import text as _text
    existing = [r[1] for r in conn.execute(_text(f"PRAGMA table_info({table})"))]
    if col not in existing:
        conn.execute(_text(f"ALTER TABLE {table} ADD COLUMN {col} {col_def}"))
        conn.commit()


def init_db():
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        _add_col_if_missing(conn, "settings", "proxy_url", "VARCHAR(200)")
        for col in ("api_key_test_enc", "api_secret_test_enc",
                    "api_key_main_enc", "api_secret_main_enc"):
            _add_col_if_missing(conn, "settings", col, "TEXT")
    db = SessionLocal()
    try:
        if not db.query(Settings).first():
            db.add(Settings())
            db.commit()
        # 迁移旧的单套 api_key_enc → 按当时 testnet 归入测试网/真实网
        s = db.query(Settings).first()
        if s and s.api_key_enc and not (s.api_key_test_enc or s.api_key_main_enc):
            if s.testnet:
                s.api_key_test_enc, s.api_secret_test_enc = s.api_key_enc, s.api_secret_enc
            else:
                s.api_key_main_enc, s.api_secret_main_enc = s.api_key_enc, s.api_secret_enc
            db.commit()
        # Seed default strategies if table is empty
        if db.query(Strategy).count() == 0:
            for s in DEFAULT_STRATEGIES:
                db.add(Strategy(**s, is_default=True))
            db.commit()
        else:
            # 回填新增的默认策略（按 strategy_type 去重）
            existing_types = {t for (t,) in db.query(Strategy.strategy_type).all()}
            for s in DEFAULT_STRATEGIES:
                if s["strategy_type"] not in existing_types:
                    db.add(Strategy(**s, is_default=True))
            db.commit()
            # 迁移旧的 mtf_regime 策略 → ml_trend
            old_strategies = db.query(Strategy).filter(
                Strategy.strategy_type == "mtf_regime"
            ).all()
            for strat in old_strategies:
                strat.strategy_type = "ml_trend"
                strat.name = "ML趋势策略"
                strat.description = (
                    "机器学习驱动的纯趋势策略。ML概率入场，ATR跟踪止损，"
                    "ML降概早退（假趋势少亏），高概率加仓（真趋势多吃）。"
                )
                strat.symbol = "BTCUSDT"
                strat.interval = "5m"
                strat.leverage = 5
                strat.risk_pct = 0.01
                strat.stop_loss_pct = 0.015
                strat.take_profit_pct = 0.03
                strat.strategy_params_json = _ML_TREND_PARAMS
            if old_strategies:
                db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
