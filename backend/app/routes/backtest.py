import asyncio

import pandas as pd
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field, model_validator

router = APIRouter()
_sar_adx_lock = asyncio.Lock()


class SarAdxBacktestRequest(BaseModel):
    symbol: str = Field(default="SOLUSDT", pattern=r"^[A-Z0-9]{2,15}USDT$")
    start_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    end_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    initial_capital: float = Field(default=10_000.0, gt=0.0, le=100_000_000.0)
    fee_rate: float = Field(default=0.001, ge=0.0, le=0.01)
    slippage_bps: float = Field(default=2.0, ge=0.0, le=100.0)

    @model_validator(mode="after")
    def validate_window(self):
        try:
            start = pd.Timestamp(self.start_date)
            end = pd.Timestamp(self.end_date)
        except (TypeError, ValueError) as exc:
            raise ValueError("start_date and end_date must be valid dates") from exc
        if end <= start:
            raise ValueError("end_date must be after start_date")
        if end > start + pd.DateOffset(years=2):
            raise ValueError("backtest window cannot exceed two years")
        return self


@router.post("/sar-adx")
async def sar_adx_backtest(req: SarAdxBacktestRequest):
    """Run the frozen offline SAR/ADX Backtrader diagnostic."""

    from ..services.sar_adx_backtest import (
        SarAdxDataUnavailableError,
        SarAdxWindowError,
        run_sar_adx_backtest,
    )

    if _sar_adx_lock.locked():
        raise HTTPException(409, "A SAR/ADX backtest is already running.")
    await _sar_adx_lock.acquire()
    try:
        worker = asyncio.create_task(
            asyncio.to_thread(
                run_sar_adx_backtest,
                symbol=req.symbol,
                start=pd.Timestamp(req.start_date, tz="UTC"),
                end=pd.Timestamp(req.end_date, tz="UTC"),
                initial_capital=req.initial_capital,
                fee_rate=req.fee_rate,
                slippage_bps=req.slippage_bps,
            )
        )
        try:
            return await asyncio.shield(worker)
        except asyncio.CancelledError:
            while not worker.done():
                try:
                    await asyncio.shield(worker)
                except asyncio.CancelledError:
                    continue
                except BaseException:
                    break
            if not worker.cancelled():
                worker.exception()
            raise
    except SarAdxWindowError as exc:
        raise HTTPException(422, str(exc)) from exc
    except SarAdxDataUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
    finally:
        _sar_adx_lock.release()


@router.get("/sar-adx/capabilities")
async def sar_adx_capabilities():
    """Describe symbols and coverage from the verified release intersection."""

    from ..services.sar_adx_backtest import (
        SarAdxDataUnavailableError,
        get_sar_adx_capabilities,
    )

    try:
        return await asyncio.to_thread(get_sar_adx_capabilities)
    except SarAdxDataUnavailableError as exc:
        raise HTTPException(503, str(exc)) from exc
