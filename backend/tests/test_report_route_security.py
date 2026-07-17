from pathlib import Path

import pytest

from backend.app.routes.research import _resolve_report_path


def test_report_path_accepts_plain_html_file(tmp_path: Path) -> None:
    report = tmp_path / "result.html"
    report.write_text("<h1>result</h1>", encoding="utf-8")

    assert _resolve_report_path(tmp_path, "result.html") == report.resolve()


@pytest.mark.parametrize(
    "name",
    [
        "../secret.html",
        "subdir/report.html",
        r"subdir\report.html",
        r"C:\secret.html",
        "report.txt",
        "",
    ],
)
def test_report_path_rejects_non_local_or_non_html_names(
    tmp_path: Path, name: str
) -> None:
    with pytest.raises(FileNotFoundError):
        _resolve_report_path(tmp_path, name)
