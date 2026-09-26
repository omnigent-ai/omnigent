"""A recorded ``page`` stops filming when the test body ends, before fixtures tear down.

Runs a nested pytest session with recording on (real browser, no live server)."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_LATER_FIXTURE_JOURNEY = """
import json
import os
from pathlib import Path

import pytest

REPORT = Path(os.environ["RECORDING_REPORT"])
RAW = Path(os.environ["OMNIGENT_E2E_RECORD_DIR"])
STATE = {}


@pytest.fixture
def native_session():
    yield "session"
    # Torn down before ``page``/``context``, like a fixture deleting its session.
    REPORT.write_text(json.dumps({
        "page_closed": STATE["page"].is_closed(),
        "videos": sorted(path.name for path in RAW.glob("*.webm")),
    }))


def test_journey(page, native_session):
    STATE["page"] = page
    page.set_content("<h1 style='font-size:72px'>live</h1>")
    page.wait_for_timeout(1_000)"""


def test_video_is_finalized_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    root = str(Path(__file__).resolve().parents[2])
    raw = tmp_path / "raw"
    report = tmp_path / "report.json"
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("OMNIGENT_E2E_RECORD_DIR", str(raw))
    monkeypatch.setenv("RECORDING_REPORT", str(report))
    pytester.makeconftest('pytest_plugins = ["tests.e2e_ui.conftest"]')
    pytester.makepyfile(_LATER_FIXTURE_JOURNEY)

    result = pytester.runpytest_subprocess(
        "-q", "-p", "pytest_playwright.pytest_playwright", "-p", "pytest_base_url.plugin"
    )

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the session fixture tore down"
    assert observed["videos"], "no video had been written when the session fixture tore down"
    assert sorted(path.name for path in raw.glob("*.webm")) == observed["videos"]
