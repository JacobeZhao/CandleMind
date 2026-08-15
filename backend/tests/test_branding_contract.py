from pathlib import Path


REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (REPOSITORY_ROOT / relative_path).read_text(encoding="utf-8")


def test_user_visible_surfaces_use_candlemind_brand():
    surfaces = {
        "frontend/index.html": _read("frontend/index.html"),
        "frontend/src/components/Sidebar.jsx": _read(
            "frontend/src/components/Sidebar.jsx"
        ),
        "backend/app/main.py": _read("backend/app/main.py"),
    }

    for path, content in surfaces.items():
        assert "CandleMind" in content, path
        assert "Livermore" not in content, path


def test_readme_states_rl_runtime_and_synthetic_performance_boundaries():
    readme = _read("README.md")
    rl_status = _read("docs/research/RL_RESEARCH_STATUS.md")

    assert "基于 EMA 特征的强化学习趋势跟踪研究基础设施" in readme
    assert "SAR + ADX V3 paper trading" in readme
    assert "强化学习模型尚未接入" in readme
    assert "不是事实业绩" in readme
    assert "SAR+ADX V3 paper trading" in rl_status
    assert "RL 尚未接入在线推理" in rl_status
