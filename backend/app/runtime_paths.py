import os
import sys
from collections.abc import Mapping
from pathlib import Path


WINDOWS_RUNTIME_DATA_DIR = Path("G:/CandleMind/CandleMind_data/runtime/app")


def resolve_runtime_data_dir(
    *,
    environ: Mapping[str, str] | None = None,
    platform: str | None = None,
    windows_default_path: str | Path = WINDOWS_RUNTIME_DATA_DIR,
    default_path: str | Path | None = None,
) -> Path:
    """Resolve the directory shared by all persistent application state."""
    environment = os.environ if environ is None else environ
    configured_path = environment.get("DATA_DIR")
    if configured_path is not None:
        return Path(configured_path)

    platform_name = sys.platform if platform is None else platform
    if platform_name.lower().startswith("win"):
        return Path(windows_default_path)
    if default_path is None:
        raise ValueError("DATA_DIR is required outside Windows")
    return Path(default_path)


RUNTIME_DATA_DIR = resolve_runtime_data_dir()
