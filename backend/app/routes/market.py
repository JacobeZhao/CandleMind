import asyncio
import json

from fastapi import APIRouter, HTTPException, Query

from ..services.binance_errors import BinanceGatewayError, BinanceGatewayRejected
from ..services.binance_usdm_gateway import (
    BinanceUsdMGateway,
    gateway_error_detail,
    gateway_error_status,
)
from ..services.indicators import REGISTRY, compute_many
from ..services.exchange_provider import is_binance_provider, unavailable_provider_detail
from ..state import app_state

router = APIRouter()


def _require_client():
    if not is_binance_provider(app_state.exchange_provider):
        raise HTTPException(
            status_code=503,
            detail=unavailable_provider_detail(app_state.exchange_provider),
        )
    if not app_state.client:
        raise HTTPException(status_code=503, detail={
            "code": "binance_connection_required",
            "message": "尚未连接 Binance，请先在设置页完成配置。",
            "retryable": False,
        })
    return app_state.client


def _gateway_http_error(exc: BinanceGatewayError) -> HTTPException:
    return HTTPException(status_code=gateway_error_status(exc), detail=gateway_error_detail(exc))


def _invalid_response(exc: Exception) -> HTTPException:
    error = BinanceGatewayRejected("Binance returned an invalid response")
    return _gateway_http_error(error)


def _build_df(gateway: BinanceUsdMGateway, symbol: str, interval: str, limit: int):
    import pandas as pd

    raw = gateway.klines(symbol=symbol, interval=interval, limit=limit)
    df = pd.DataFrame(raw, columns=[
        "open_time", "open", "high", "low", "close", "volume",
        "close_time", "quote_volume", "trades",
        "taker_buy_base", "taker_buy_quote", "ignore",
    ])
    df[["open", "high", "low", "close", "volume"]] = df[
        ["open", "high", "low", "close", "volume"]
    ].astype(float)
    df["open_time"] = pd.to_datetime(df["open_time"], unit="ms")
    return df


@router.get("/ticker/{symbol}")
async def ticker(symbol: str):
    gateway = BinanceUsdMGateway(_require_client())
    symbol = symbol.upper()
    try:
        latest, stats, mark = await asyncio.gather(
            asyncio.to_thread(gateway.symbol_ticker, symbol=symbol),
            asyncio.to_thread(gateway.ticker, symbol=symbol),
            asyncio.to_thread(gateway.mark_price, symbol=symbol),
        )
        if not all(isinstance(item, dict) for item in (latest, stats, mark)):
            raise TypeError("symbol-scoped ticker reads must return objects")
    except BinanceGatewayError as exc:
        raise _gateway_http_error(exc) from exc
    except TypeError as exc:
        raise _invalid_response(exc) from exc

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
    symbol: str,
    interval: str = Query("15m"),
    limit: int = Query(200),
    inds: str = Query("psar"),
    params: str = Query("{}"),
):
    gateway = BinanceUsdMGateway(_require_client())
    try:
        df = await asyncio.to_thread(_build_df, gateway, symbol, interval, limit)
    except BinanceGatewayError as exc:
        raise _gateway_http_error(exc) from exc
    except (TypeError, ValueError) as exc:
        raise _invalid_response(exc) from exc

    ind_ids = [item.strip() for item in inds.split(",") if item.strip()]
    try:
        ind_params = json.loads(params)
    except Exception:
        ind_params = {}

    requests = [
        {"id": indicator_id, "params": ind_params.get(indicator_id, {})}
        for indicator_id in ind_ids
        if indicator_id in REGISTRY
    ]
    if requests:
        df = await asyncio.to_thread(compute_many, df, requests)

    base_cols = ["open_time", "open", "high", "low", "close", "volume"]
    excluded = {
        "close_time", "quote_volume", "trades", "taker_buy_base",
        "taker_buy_quote", "ignore",
    }
    extra_cols = [column for column in df.columns if column not in base_cols and column not in excluded]
    out = df[base_cols + extra_cols].copy()
    out["open_time"] = out["open_time"].astype(str)

    import numpy as np

    out = out.replace([np.inf, -np.inf], np.nan)
    records = out.to_dict("records")
    for record in records:
        for key, value in record.items():
            if isinstance(value, float) and value != value:
                record[key] = None
    return records


@router.get("/symbols")
async def symbols():
    gateway = BinanceUsdMGateway(_require_client())
    try:
        info = await asyncio.to_thread(gateway.exchange_info)
        return [
            symbol["symbol"]
            for symbol in info["symbols"]
            if symbol["quoteAsset"] == "USDT" and symbol["status"] == "TRADING"
        ]
    except BinanceGatewayError as exc:
        raise _gateway_http_error(exc) from exc
    except (KeyError, TypeError) as exc:
        raise _invalid_response(exc) from exc
