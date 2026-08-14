import json
import os
from pathlib import Path
import subprocess
import sys

from backend.app.main import app


def test_backtest_api_retains_only_sar_endpoints() -> None:
    backtest_routes = {
        (route.path, frozenset(route.methods or set()))
        for route in app.routes
        if route.path.startswith("/api/backtest")
    }

    assert backtest_routes == {
        ("/api/backtest/sar-adx", frozenset({"POST"})),
        ("/api/backtest/sar-adx/capabilities", frozenset({"GET"})),
    }
    assert not any(route.path.startswith("/api/research") for route in app.routes)


def test_strategy_api_retains_only_sar_engine_endpoints() -> None:
    strategy_routes = {
        (route.path, frozenset(route.methods or set()))
        for route in app.routes
        if route.path.startswith("/api/strategy")
    }

    assert strategy_routes == {
        ("/api/strategy/engine/status", frozenset({"GET"})),
        ("/api/strategy/engine/start", frozenset({"POST"})),
        ("/api/strategy/engine/stop", frozenset({"POST"})),
    }


def test_ai_api_does_not_expose_removed_strategy_parser() -> None:
    assert not any(route.path == "/api/ai/parse" for route in app.routes)


def test_health_api_retains_only_the_runtime_health_endpoint() -> None:
    health_routes = {
        (route.path, frozenset(route.methods or set()))
        for route in app.routes
        if route.path.startswith("/api/health")
    }

    assert health_routes == {
        ("/api/health", frozenset({"GET"})),
    }


def test_market_api_excludes_redundant_indicator_metadata_endpoint() -> None:
    market_routes = {
        (route.path, frozenset(route.methods or set()))
        for route in app.routes
        if route.path.startswith("/api/market")
    }

    assert market_routes == {
        ("/api/market/ticker/{symbol}", frozenset({"GET"})),
        ("/api/market/klines/{symbol}", frozenset({"GET"})),
        ("/api/market/symbols", frozenset({"GET"})),
    }


def test_settings_api_excludes_redundant_connect_endpoint() -> None:
    settings_routes = {
        (route.path, frozenset(route.methods or set()))
        for route in app.routes
        if route.path.startswith("/api/settings")
    }

    assert settings_routes == {
        ("/api/settings", frozenset({"GET"})),
        ("/api/settings", frozenset({"POST"})),
        ("/api/settings/test-connection", frozenset({"POST"})),
        ("/api/settings/myip", frozenset({"GET"})),
    }


def test_main_import_does_not_load_retired_research_modules() -> None:
    module_names = (
        "backend.app.routes.research",
        "backend.app.services.robustness",
        "backend.app.services.ml_strategy",
        "backend.app.services.ml_signal",
    )
    script = (
        "import json, sys; "
        "import backend.app.main; "
        f"print(json.dumps({{name: name in sys.modules for name in {module_names!r}}}))"
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        name: False for name in module_names
    }


def test_strategy_import_closure_excludes_retired_engines() -> None:
    module_names = (
        "backend.app.services.ml_strategy",
        "backend.app.services.ml_signal",
        "backend.app.services.trend_decision",
    )
    script = (
        "import json, sys; "
        "import backend.app.routes.strategy; "
        f"print(json.dumps({{name: name in sys.modules for name in {module_names!r}}}))"
    )
    environment = os.environ.copy()
    environment["PYTHONDONTWRITEBYTECODE"] = "1"

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[2],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {
        name: False for name in module_names
    }
