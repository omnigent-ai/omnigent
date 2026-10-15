"""A recorded ``page`` stops filming when the test body ends, before fixtures tear down.

Runs nested pytest sessions with recording on (real browser, no live server)."""

from __future__ import annotations

import json
import os
import platform
from pathlib import Path

import pytest

from tests.helpers.ui_recording import extract_frames, fraction_near

pytest_plugins = ["pytester"]

# ``pytester`` points HOME at a temp dir, where Playwright would look for its browsers.
_REAL_HOME = Path.home()

_PLAYWRIGHT_PLUGINS = (
    "-p",
    "pytest_playwright.pytest_playwright",
    "-p",
    "pytest_base_url.plugin",
)

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

# The cleanup fixture depends on ``page`` and deletes its seeded data in ``finally``.
_PAGE_DEPENDENT_CLEANUP_JOURNEY = """
import json
import os
from pathlib import Path

import pytest

REPORT = Path(os.environ["RECORDING_REPORT"])
RAW_DIR = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
RAW = Path(RAW_DIR) if RAW_DIR else None
RESULTS = ["file_name e540789f", "fileXname e540789f"]


@pytest.fixture
def search_sessions(page):
    try:
        yield RESULTS
    finally:
        # Like ``httpx.delete`` on the seeded sessions: filmed if the page still records.
        REPORT.write_text(json.dumps({
            "page_closed": page.is_closed(),
            "videos": sorted(path.name for path in RAW.glob("*.webm")) if RAW else [],
        }))
        RESULTS.clear()
        if not page.is_closed():
            page.set_content("<body style='background:#dc2626'>No results found</body>")
            page.wait_for_timeout(1_000)


def test_journey(page, search_sessions):
    page.set_content("<body style='background:#1d4ed8'><ul>" +
                     "".join(f"<li>{row}</li>" for row in search_sessions) + "</ul></body>")
    page.wait_for_timeout(1_000)"""


def _browsers_path() -> str:
    """Playwright's browser cache as resolved from the real home, not pytester's."""
    configured = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if configured:
        return configured
    if platform.system() == "Darwin":
        return str(_REAL_HOME / "Library" / "Caches" / "ms-playwright")
    cache_home = os.environ.get("XDG_CACHE_HOME") or str(_REAL_HOME / ".cache")
    return str(Path(cache_home) / "ms-playwright")


def _configure(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    record_dir: Path | None,
) -> Path:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", _browsers_path())
    monkeypatch.delenv("OMNIGENT_E2E_RECORD_DIR", raising=False)
    monkeypatch.delenv("OMNIGENT_COMPAT_SERVER_VERSION", raising=False)
    monkeypatch.delenv("OMNIGENT_COMPAT_SERVER_PYTHON", raising=False)
    if record_dir is not None:
        monkeypatch.setenv("OMNIGENT_E2E_RECORD_DIR", str(record_dir))
    report = tmp_path / "report.json"
    monkeypatch.setenv("RECORDING_REPORT", str(report))
    pytester.makeconftest("""
import pytest

pytest_plugins = ["tests.e2e_ui.conftest"]

@pytest.fixture(scope="session")
def built_spa():
    pytest.fail("recording regression must not build the SPA")

@pytest.fixture(scope="session")
def live_server():
    pytest.fail("recording regression must not start a server")
""")
    return report


@pytest.mark.parametrize("inherited_compat", [False, True])
def test_video_is_finalized_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inherited_compat: bool,
) -> None:
    if inherited_compat:
        monkeypatch.setenv("OMNIGENT_COMPAT_SERVER_VERSION", "0.16.0")
        monkeypatch.setenv("OMNIGENT_COMPAT_SERVER_PYTHON", "/unused/compat/python")
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_LATER_FIXTURE_JOURNEY)

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the session fixture tore down"
    assert observed["videos"], "no video had been written when the session fixture tore down"
    assert sorted(path.name for path in raw.glob("*.webm")) == observed["videos"]


def test_video_is_finalized_before_a_page_dependent_fixture_cleans_up(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    raw = tmp_path / "raw"
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=raw)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY)

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the fixture deleted its data"
    assert observed["videos"] == sorted(path.name for path in raw.glob("*.webm"))
    assert len(observed["videos"]) == 1
    frames = extract_frames(raw / observed["videos"][0], tmp_path / "frames")
    assert fraction_near(frames[-1], (0x1D, 0x4E, 0xD8)) > 0.5, (
        "the final video frame lost the demonstrated search results before cleanup"
    )


def test_video_option_is_finalized_before_a_page_dependent_fixture_cleans_up(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY)
    output = tmp_path / "pw-output"

    result = pytester.runpytest_subprocess(
        "-q", *_PLAYWRIGHT_PLUGINS, "--video", "on", "--output", str(output)
    )

    result.assert_outcomes(passed=1)
    observed = json.loads(report.read_text())
    assert observed["page_closed"], "page was still filming when the fixture deleted its data"
    assert list(output.rglob("video.webm")), "pytest-playwright did not save its video"


@pytest.mark.parametrize("fails", [False, True], ids=["passing", "failing"])
def test_early_close_preserves_failure_artifact_policy(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, fails: bool
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(_PAGE_DEPENDENT_CLEANUP_JOURNEY + f"\n    assert not {fails!r}\n")
    output = tmp_path / "pw-output"

    result = pytester.runpytest_subprocess(
        "-q",
        *_PLAYWRIGHT_PLUGINS,
        "--video",
        "retain-on-failure",
        "--tracing",
        "retain-on-failure",
        "--screenshot",
        "only-on-failure",
        "--output",
        str(output),
    )

    result.assert_outcomes(passed=int(not fails), failed=int(fails))
    assert json.loads(report.read_text())["page_closed"]
    for pattern in ("video.webm", "trace.zip", "test-failed-*.png"):
        artifacts = list(output.rglob(pattern))
        assert bool(artifacts) is fails, (pattern, artifacts)
        assert all(path.stat().st_size > 0 for path in artifacts)


def test_marker_recording_stops_before_cleanup(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=None)
    pytester.makepyfile(
        _PAGE_DEPENDENT_CLEANUP_JOURNEY.replace(
            "def test_journey(",
            f"@pytest.mark.browser_context_args(record_video_dir={str(tmp_path / 'raw')!r})\n"
            "def test_journey(",
        )
    )
    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)
    result.assert_outcomes(passed=1)
    assert json.loads(report.read_text())["page_closed"]
    assert list((tmp_path / "raw").glob("*.webm"))


def test_route_cleanup_fixtures_tolerate_early_close(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    report = _configure(pytester, monkeypatch, tmp_path, record_dir=tmp_path / "raw")
    conftest = pytester.path / "conftest.py"
    conftest.write_text(
        conftest.read_text()
        + """
from tests.e2e_ui.chat.test_claude_model_picker import _finish_snapshot_routes
from tests.e2e_ui.chat.test_side_chat_entrypoints import side_chat_forks
from tests.e2e_ui.files.test_files_panel_header import _drop_routes as files_routes
from tests.e2e_ui.files.test_reveal_in_file_manager import _drop_routes as reveal_routes
from tests.e2e_ui.sessions.test_host_badge import _drop_routes as badge_routes
from tests.e2e_ui.sessions.test_host_switch_reattaches_terminal import _drop_routes as switch
from tests.e2e_ui.sessions.test_reconnect_local_host_from_app import _drop_routes as reconnect

@pytest.fixture
def seeded_session():
    return ("http://127.0.0.1", "recording-test")
"""
    )
    pytester.makepyfile(
        _PAGE_DEPENDENT_CLEANUP_JOURNEY.replace(
            "def test_journey(page, search_sessions):",
            "def test_journey(page, search_sessions, side_chat_forks, files_routes):",
        )
    )
    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS)
    result.assert_outcomes(passed=1)
    assert json.loads(report.read_text())["page_closed"]
