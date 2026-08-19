import ast
import asyncio
import json
from pathlib import Path

from backend.app.services import market_analysis_graph


def _snapshot():
    interval = {
        "bar_closed_at": "2026-08-20T00:04:59.999000Z",
        "close": 144.2,
        "returns": {"1": 0.01, "6": 0.02, "24": 0.03},
        "realized_volatility_20": 0.012,
        "atr_14": 1.1,
        "atr_percent": 0.007,
        "body_atr_ratio": 1.2,
        "large_candle": False,
        "sar": {"value": 140.0, "direction": "long", "reversal": False},
        "adx": {"value": 31.0, "change": 1.0, "plus_di": 34.0, "minus_di": 15.0},
        "recent_close_returns": [0.0, 0.01],
    }
    return {
        "source": "synthetic completed bars",
        "symbol": "SOLUSDT",
        "snapshot_at": "2026-08-20T00:07:00Z",
        "trigger_interval": "5m",
        "trigger_cutoff": "2026-08-20T00:04:59.999000Z",
        "analysis_intervals": ["1m", "5m", "15m", "1h", "4h", "1d"],
        "reasons": ["candle_closed"],
        "intervals": {name: dict(interval) for name in ("1m", "5m", "15m", "1h", "4h", "1d")},
    }


def _assert_json_primitives(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return
    if isinstance(value, list):
        for item in value:
            _assert_json_primitives(item)
        return
    assert isinstance(value, dict)
    assert all(isinstance(key, str) for key in value)
    for item in value.values():
        _assert_json_primitives(item)


def test_graph_makes_one_provider_call_and_persists_only_sanitized_primitives(
    tmp_path, monkeypatch
):
    provider_calls = []
    batches = []

    async def fetch(client, symbol):
        assert client is sentinel
        assert symbol == "SOLUSDT"
        return _snapshot()

    async def complete(provider, api_key, base_url, model_name, messages, proxy_url):
        provider_calls.append(messages)
        return "  analysis\x00 result  "

    async def on_batch_ready(batch_id, thread_id, cutoff):
        batches.append((batch_id, thread_id, cutoff))

    sentinel = object()
    monkeypatch.setattr(market_analysis_graph, "fetch_multi_timeframe_snapshot", fetch)
    monkeypatch.setattr(market_analysis_graph, "chat_complete", complete)
    graph = market_analysis_graph.MarketAnalysisGraph(tmp_path / "market_analysis.sqlite3")
    history = [
        {"role": "assistant" if index % 2 else "user", "content": f"item-{index}\x00"}
        for index in range(25)
    ]

    async def scenario():
        try:
            return await graph.run(
                symbol="SOLUSDT",
                mode="scheduled",
                manual_query=None,
                history=history,
                client=sentinel,
                provider_config={
                    "provider": "openai",
                    "api_key": "super-secret",
                    "base_url": "https://api.openai.com/v1",
                    "model_name": "test-model",
                },
                proxy_url=None,
                thread_id="thread-1",
                on_batch_ready=on_batch_ready,
            )
        finally:
            await graph.close()

    result = asyncio.run(scenario())

    assert len(provider_calls) == 1
    assert len(provider_calls[0]) <= 22  # system + 20 summaries + request
    assert "\x00" not in json.dumps(provider_calls[0])
    assert result["answer"] == "analysis result"
    assert batches == [
        (
            "SOLUSDT:2026-08-20T00:04:59.999000Z",
            "thread-1",
            "2026-08-20T00:04:59.999000Z",
        )
    ]
    _assert_json_primitives(result)
    assert "super-secret" not in (tmp_path / "market_analysis.sqlite3").read_bytes().decode(
        "utf-8", errors="ignore"
    )


def test_graph_has_no_trading_or_autonomous_tool_imports():
    source_path = Path(market_analysis_graph.__file__)
    source = source_path.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(node.module or "")

    forbidden = ("strategies", "paper_broker", "bot_engine", "orders", "subprocess")
    assert not any(token in module for module in imported for token in forbidden)
    assert "ToolNode" not in source
    assert "create_react_agent" not in source
    assert "pickle_fallback=False" in source
