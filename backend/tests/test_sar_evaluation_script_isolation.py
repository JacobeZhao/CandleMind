from __future__ import annotations

import ast
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
SCRIPT_MODULES = (
    "backend.scripts.evaluation.run_backtrader_sar_pyramid",
    "backend.scripts.evaluation.run_sar_pyramid_backtest",
    "backend.scripts.evaluation.sweep_sol_adx_sar",
    "backend.scripts.evaluation.sweep_sol_adx_sar_v2",
    "backend.scripts.evaluation.sweep_sol_adx_sar_v3",
)
FORBIDDEN_IMPORTS = (
    "backend.app.services.ema_data_release",
    "backend.app.services.ema_setup_input_release",
    "backend.app.rl",
)


@pytest.mark.parametrize("module_name", SCRIPT_MODULES)
def test_sar_evaluation_script_import_is_isolated(
    module_name: str,
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "runtime"
    market_data_dir = tmp_path / "market"
    data_dir.mkdir()
    market_data_dir.mkdir()

    environment = os.environ.copy()
    environment["DATA_DIR"] = str(data_dir.resolve())
    environment["MARKET_DATA_DIR"] = str(market_data_dir.resolve())
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    script = f"""
import contextlib
import importlib
import io
import json
import sys

module = importlib.import_module({module_name!r})
help_exit_code = None
try:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        module.build_parser().parse_args(["--help"])
except SystemExit as exc:
    help_exit_code = exc.code

print(json.dumps({{
    "help_exit_code": help_exit_code,
    "required_exports": {{
        name: hasattr(module, name)
        for name in (
            "verify_market_release",
            "verify_observed_funding_release",
            "load_observed_funding_symbol",
        )
    }},
    "ema_setup_loaded": "backend.app.services.ema_setup_input_release" in sys.modules,
    "rl_modules": sorted(
        name for name in sys.modules
        if name == "backend.app.rl" or name.startswith("backend.app.rl.")
    ),
}}))
"""
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    result = json.loads(completed.stdout)
    assert result == {
        "help_exit_code": 0,
        "required_exports": {
            "verify_market_release": True,
            "verify_observed_funding_release": True,
            "load_observed_funding_symbol": True,
        },
        "ema_setup_loaded": False,
        "rl_modules": [],
    }


@pytest.mark.parametrize("module_name", SCRIPT_MODULES)
def test_sar_evaluation_script_has_no_legacy_imports(module_name: str) -> None:
    source_path = REPOSITORY_ROOT / Path(*module_name.split(".")).with_suffix(".py")
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

    imported_modules: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.append(node.module)

    violations = sorted(
        imported
        for imported in imported_modules
        if any(
            imported == forbidden or imported.startswith(f"{forbidden}.")
            for forbidden in FORBIDDEN_IMPORTS
        )
    )
    assert violations == []
