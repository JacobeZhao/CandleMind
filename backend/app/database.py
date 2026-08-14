from sqlalchemy import Boolean, Column, Integer, String, Text, create_engine
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

    id = Column(Integer, primary_key=True, default=1)
    api_key_enc = Column(Text, nullable=True)
    api_secret_enc = Column(Text, nullable=True)
    api_key_test_enc = Column(Text, nullable=True)
    api_secret_test_enc = Column(Text, nullable=True)
    api_key_main_enc = Column(Text, nullable=True)
    api_secret_main_enc = Column(Text, nullable=True)
    testnet = Column(Boolean, default=True)
    symbol = Column(String(20), default="BTCUSDT")
    interval = Column(String(10), default="15m")
    proxy_url = Column(String(200), nullable=True)


def active_keys(settings: Settings) -> tuple[str | None, str | None]:
    """Return the encrypted credentials for the selected Binance network."""

    if settings.testnet:
        return (
            settings.api_key_test_enc or settings.api_key_enc,
            settings.api_secret_test_enc or settings.api_secret_enc,
        )
    return settings.api_key_main_enc, settings.api_secret_main_enc


class AIConfig(Base):
    __tablename__ = "ai_configs"

    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)
    provider = Column(String(30), nullable=False)
    api_key_enc = Column(Text, nullable=True)
    base_url = Column(String(200), nullable=True)
    model_name = Column(String(100), nullable=True)
    is_active = Column(Boolean, default=False)


def _add_col_if_missing(conn, table: str, column: str, definition: str) -> None:
    from sqlalchemy import text

    existing = [
        row[1] for row in conn.execute(text(f"PRAGMA table_info({table})"))
    ]
    if column not in existing:
        conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {column} {definition}"))
        conn.commit()


def init_db() -> None:
    Base.metadata.create_all(bind=engine)
    with engine.connect() as conn:
        _add_col_if_missing(conn, "settings", "proxy_url", "VARCHAR(200)")
        for column in (
            "api_key_test_enc",
            "api_secret_test_enc",
            "api_key_main_enc",
            "api_secret_main_enc",
        ):
            _add_col_if_missing(conn, "settings", column, "TEXT")

    db = SessionLocal()
    try:
        settings = db.query(Settings).first()
        if settings is None:
            settings = Settings()
            db.add(settings)
            db.commit()

        if settings.api_key_enc and not (
            settings.api_key_test_enc or settings.api_key_main_enc
        ):
            if settings.testnet:
                settings.api_key_test_enc = settings.api_key_enc
                settings.api_secret_test_enc = settings.api_secret_enc
            else:
                settings.api_key_main_enc = settings.api_key_enc
                settings.api_secret_main_enc = settings.api_secret_enc
            db.commit()
    finally:
        db.close()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
