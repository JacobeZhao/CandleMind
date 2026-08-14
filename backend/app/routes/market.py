import json
import asyncio
from fastapi import APIRouter, HTTPException, Query
from ..state import app_state
from ..services.indicators import REGISTRY, compute_many

router = APIRouter()


def _require_client():
    if not app_state.client:
        raise HTTPException(status_code=503, detail="未连接 Binance，请先配置 API Key")
    return app_state.client


def _build_df(client, symbol: str, interval: str, limit: int):
    import pandas as pd
    raw = client.futures_klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df[["open", "high", "low", "close", "volume"]] = \
        df[["open", "high", "low", "close", "volume"]].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


@router.get("/ticker/{symbol}")
async def ticker(symbol: str):
    client = _require_client()
    symbol = symbol.upper()
    latest, stats, mark = await asyncio.gather(
        asyncio.to_thread(client.futures_symbol_ticker, symbol=symbol),
        asyncio.to_thread(client.futures_ticker, symbol=symbol),
        asyncio.to_thread(client.futures_mark_price, symbol=symbol),
    )

    result = {
        **latest,
        "priceChangePercent": stats.get("priceChangePercent"),
        "highPrice": stats.get("highPrice"),
        "lowPrice": stats.get("lowPrice"),
        "quoteVolume": stats.get("quoteVolume"),
        "markPrice": mark.get("markPrice"),
    }
    optional_mark_fields = {
        "indexPrice": "indexPrice",
        "lastFundingRate": "lastFundingRate",
        "nextFundingTime": "nextFundingTime",
    }
    for output_name, source_name in optional_mark_fields.items():
        if source_name in mark:
            result[output_name] = mark[source_name]
    return result


@router.get("/klines/{symbol}")
async def klines(
    symbol:   str,
    interval: str = Query("15m"),
    limit:    int = Query(200),
    inds:     str = Query("psar"),          # comma-separated indicator IDs
    params:   str = Query("{}"),            # JSON: {indicatorId: {param: value}}
):
    client = _require_client()
    df = await asyncio.to_thread(_build_df, client, symbol, interval, limit)

    # Parse indicator requests
    ind_ids   = [i.strip() for i in inds.split(",") if i.strip()]
    try:
        ind_params = json.loads(params)
    except Exception:
        ind_params = {}

    requests = [
        {"id": iid, "params": ind_params.get(iid, {})}
        for iid in ind_ids
        if iid in REGISTRY
    ]

    if requests:
        df = await asyncio.to_thread(compute_many, df, requests)

    # Build output columns
    base_cols = ["open_time", "open", "high", "low", "close", "volume"]
    extra_cols = [c for c in df.columns if c not in base_cols
                  and c not in ("close_time","quote_volume","trades",
                                "taker_buy_base","taker_buy_quote","ignore")]

    out = df[base_cols + extra_cols].copy()
    out["open_time"] = out["open_time"].astype(str)

    # Replace NaN/inf with None for JSON serialization
    import numpy as np
    out = out.replace([np.inf, -np.inf], np.nan)
    records = out.to_dict("records")
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, float) and (v != v):   # NaN check
                rec[k] = None

    return records


@router.get("/symbols")
async def symbols():
    client = _require_client()
    info = await asyncio.to_thread(client.futures_exchange_info)
    return [s["symbol"] for s in info["symbols"]
            if s["quoteAsset"] == "USDT" and s["status"] == "TRADING"]
