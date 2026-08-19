from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd
import pytest

from backend.app.services.sar_adx_runtime import SarAdxPaperRuntime, SarAdxRuntimeError
from backend.app.services.sar_adx_state_store import SarAdxStateStore


def _bars(count: int = 500) -> pd.DataFrame:
    opened = pd.date_range("2025-01-01", periods=count, freq="5min", tz="UTC")
    close = 100.0 + np.arange(count) * 0.03 + np.sin(np.arange(count) / 10.0)
    return pd.DataFrame(
        {
            "open_time": opened,
            "open": close,
            "high": close + 0.2,
            "low": close - 0.2,
            "close": close,
            "close_time": opened + pd.Timedelta(minutes=5) - pd.Timedelta(milliseconds=1),
        }
    )


def test_runtime_drops_unfinished_bar_and_is_idempotent(tmp_path) -> None:
    bars = _bars()
    cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=SarAdxStateStore(tmp_path))
    first = runtime.process_bars(bars, server_time=cutoff)
    assert first == []
    processed = runtime.last_processed_decision_time
    second = runtime.process_bars(bars, server_time=cutoff)
    assert runtime.last_processed_decision_time == processed
    assert second == []
    assert all(fill.decision_id not in {item.decision_id for item in first[:0]} for fill in second)


def test_new_runtime_warms_up_without_historical_fills(tmp_path) -> None:
    bars = _bars()
    cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=SarAdxStateStore(tmp_path))
    assert runtime.process_bars(bars, server_time=cutoff) == []
    assert runtime.broker.processed_decisions == set()
    assert runtime.last_processed_decision_time == bars.iloc[-2]["close_time"]


def test_runtime_uses_current_open_for_previous_completed_decision(tmp_path) -> None:
    bars = _bars()
    initial_cutoff = bars.iloc[-2]["open_time"] + pd.Timedelta(seconds=15)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=SarAdxStateStore(tmp_path))
    runtime.process_bars(bars.iloc[:-1], server_time=initial_cutoff)
    current_cutoff = bars.iloc[-1]["open_time"] + pd.Timedelta(seconds=15)
    runtime.process_bars(bars, server_time=current_cutoff, execution_price=123.45)
    assert runtime.last_execution_open_time == bars.iloc[-1]["open_time"]
    if runtime.broker.position.entries:
        assert runtime.broker.position.entries[-1]["price"] != bars.iloc[-1]["open"]


def test_runtime_rejects_late_execution_in_current_bar(tmp_path) -> None:
    bars = _bars()
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=SarAdxStateStore(tmp_path))
    runtime.process_bars(bars.iloc[:-1], server_time=bars.iloc[-2]["open_time"])
    with pytest.raises(SarAdxRuntimeError, match="window expired"):
        runtime.process_bars(
            bars,
            server_time=bars.iloc[-1]["open_time"] + pd.Timedelta(minutes=2),
        )


def test_funding_millisecond_timestamp_is_parsed_as_utc(tmp_path) -> None:
    broker_runtime = SarAdxPaperRuntime("SOLUSDT", state_store=SarAdxStateStore(tmp_path))
    broker_runtime._settle_funding(
        broker_runtime.broker,
        pd.DataFrame({"funding_time": [1_786_464_000_000], "funding_rate": [0.001]}),
        pd.Timestamp("2026-08-12T00:00:00Z"),
        100.0,
    )
    assert "2026-08-11T16:00:00+00:00" in broker_runtime.broker.processed_funding


def test_runtime_does_not_backfill_missed_execution_opens(tmp_path) -> None:
    bars = _bars(505)
    store = SarAdxStateStore(tmp_path)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    runtime.process_bars(bars.iloc[:500], server_time=bars.iloc[499]["open_time"])
    with pytest.raises(SarAdxRuntimeError, match="missed"):
        runtime.process_bars(bars, server_time=bars.iloc[-1]["open_time"])


def test_runtime_can_explicitly_rebaseline_stale_flat_state(tmp_path) -> None:
    bars = _bars(505)
    store = SarAdxStateStore(tmp_path)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    runtime.process_bars(bars.iloc[:500], server_time=bars.iloc[499]["open_time"])
    runtime.state = runtime.state.__class__(armed=True, regime_direction=1)

    fills = runtime.process_bars(
        bars,
        server_time=bars.iloc[-1]["open_time"],
        allow_flat_rebaseline=True,
    )

    assert fills == []
    assert runtime.state == runtime.state.__class__()
    assert runtime.last_processed_decision_time == bars.iloc[-2]["close_time"]
    assert runtime.last_execution_open_time is None
    assert runtime.recovery_status == "rebaselined"
    recovered = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    assert recovered.last_processed_decision_time == bars.iloc[-2]["close_time"]


def test_runtime_never_rebaselines_stale_open_position(tmp_path) -> None:
    bars = _bars(505)
    runtime = SarAdxPaperRuntime(
        "SOLUSDT",
        state_store=SarAdxStateStore(tmp_path),
    )
    runtime.process_bars(bars.iloc[:500], server_time=bars.iloc[499]["open_time"])
    runtime.broker.open(1, 100.0, "existing-paper-position", runtime.config)

    with pytest.raises(SarAdxRuntimeError, match="missed"):
        runtime.process_bars(
            bars,
            server_time=bars.iloc[-1]["open_time"],
            allow_flat_rebaseline=True,
        )


def test_failed_state_save_does_not_advance_memory(tmp_path, monkeypatch) -> None:
    bars = _bars()
    store = SarAdxStateStore(tmp_path)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    before = runtime.broker.to_dict()
    monkeypatch.setattr(store, "save", lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        runtime.process_bars(bars, server_time=bars.iloc[-1]["open_time"])
    assert runtime.last_processed_decision_time is None
    assert runtime.broker.to_dict() == before


def test_runtime_recovers_persisted_progress(tmp_path) -> None:
    bars = _bars()
    cutoff = bars.iloc[-1]["close_time"] + pd.Timedelta(milliseconds=1)
    store = SarAdxStateStore(tmp_path)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    runtime.process_bars(bars, server_time=cutoff)
    recovered = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    assert recovered.recovery_status == "recovered"
    assert recovered.last_processed_decision_time == runtime.last_processed_decision_time
    assert recovered.broker.to_dict() == runtime.broker.to_dict()


def test_runtime_status_reports_persisted_paper_fill_count(tmp_path) -> None:
    store = SarAdxStateStore(tmp_path)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    runtime.broker.open(1, 100.0, "d1", runtime.config)
    runtime._save()

    recovered = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    status = recovered.status(101.0)

    assert status["paper_fill_count"] == 1
    assert status["paper_fill_count_complete"] is True


def test_runtime_status_does_not_report_incomplete_legacy_count_as_zero(tmp_path) -> None:
    runtime = SarAdxPaperRuntime(
        "SOLUSDT",
        state_store=SarAdxStateStore(tmp_path),
    )
    runtime.broker.paper_fill_count_complete = False

    status = runtime.status(101.0)

    assert status["paper_fill_count"] is None
    assert status["paper_fill_count_complete"] is False


def test_runtime_rejects_bar_gaps(tmp_path) -> None:
    bars = _bars().drop(index=100).reset_index(drop=True)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=SarAdxStateStore(tmp_path))
    with pytest.raises(SarAdxRuntimeError, match="gap"):
        runtime.process_bars(bars, server_time=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_runtime_rejects_out_of_order_bars(tmp_path) -> None:
    bars = _bars()
    bars.iloc[[100, 101]] = bars.iloc[[101, 100]].to_numpy()
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=SarAdxStateStore(tmp_path))
    with pytest.raises(SarAdxRuntimeError, match="out-of-order"):
        runtime.process_bars(bars, server_time=datetime(2026, 1, 1, tzinfo=timezone.utc))


def test_runtime_rejects_history_that_skips_persisted_progress(tmp_path) -> None:
    bars = _bars(600)
    store = SarAdxStateStore(tmp_path)
    runtime = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    runtime.process_bars(bars.iloc[:500], server_time=bars.iloc[500]["open_time"])
    recovered = SarAdxPaperRuntime("SOLUSDT", state_store=store)
    with pytest.raises(SarAdxRuntimeError, match="missed"):
        recovered.process_bars(
            bars.iloc[550:].reset_index(drop=True),
            server_time=datetime(2026, 1, 1, tzinfo=timezone.utc),
        )
