import asyncio
from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from binance.binance_kline_stream import BinanceKlineStream

app = FastAPI(
    title="CandleMind Market Service",
    description="Binance Realtime Kline Service",
    version="0.1.0"
)
app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",   # Vite 默认
        "http://127.0.0.1:5173",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ================== 全局行情实例 ==================

kline_stream: BinanceKlineStream | None = None


# ================== 启动 & 关闭事件 ==================

@app.on_event("startup")
async def startup_event():
    global kline_stream

    # 启动 BTCUSDT 1分钟 K线（你后面可以改成配置）
    kline_stream = BinanceKlineStream(
        symbol="btcusdt",
        interval="1m"
    )

    print("🚀 行情服务已启动")


@app.on_event("shutdown")
async def shutdown_event():
    print("🛑 行情服务关闭")


# ================== 对外接口 ==================

@app.get("/health")
def health_check():
    return {"status": "ok"}


@app.get("/klines/latest")
def get_latest_kline():
    """
    获取最新一根 K 线（可能未收盘）
    """
    if not kline_stream:
        return JSONResponse(status_code=503, content={"error": "service not ready"})

    return kline_stream.get_latest_kline()


@app.get("/klines")
def get_klines(limit: int = 100):
    """
    获取最近 N 根已收盘 K 线
    """
    if not kline_stream:
        return JSONResponse(status_code=503, content={"error": "service not ready"})

    klines = kline_stream.get_klines()

    if limit > 0:
        klines = klines[-limit:]

    return klines
