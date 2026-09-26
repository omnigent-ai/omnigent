"""The e2e_ui recording hook ends a clip with the test body, before fixtures tear down."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_PLAYWRIGHT_PLUGINS = (
    "-p",
    "pytest_playwright.pytest_playwright",
    "-p",
    "pytest_base_url.plugin",
)

_FAKE_CONTEXT = '''
import pytest

STATE = {}


class FakeContext:
    """Stands in for pytest-playwright's ``context``; ``pages`` empties once closed."""

    def __init__(self, pages=("page",)):
        self.pages = list(pages)
        self.closed = False

    def close(self):
        assert self.pages, "closed a context whose video was already finalized"
        self.closed = True
        self.pages = []'''


def _journey(*, closed_at_teardown: bool) -> str:
    return (
        _FAKE_CONTEXT
        + f"""

@pytest.fixture
def context():
    STATE["context"] = FakeContext()
    return STATE["context"]


@pytest.fixture
def native_session():
    yield "session"
    # Set up after ``context``, so torn down before it, like a session fixture.
    assert STATE["context"].closed is {closed_at_teardown!r}


def test_journey(context, native_session):
    assert not context.closed"""
    )


def _configure(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, *, record_dir: Path | None
) -> None:
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv("OMNIGENT_E2E_RECORD_DIR", raising=False)
    if record_dir is not None:
        monkeypatch.setenv("OMNIGENT_E2E_RECORD_DIR", str(record_dir))
    pytester.makeconftest('pytest_plugins = ["tests.e2e_ui.conftest"]')


def test_record_dir_stops_the_context_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(pytester, monkeypatch, record_dir=tmp_path / "raw")
    pytester.makepyfile(_journey(closed_at_teardown=True))

    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_video_option_stops_the_context_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(pytester, monkeypatch, record_dir=None)
    pytester.makepyfile(_journey(closed_at_teardown=True))

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS, "--video", "on")
    result.assert_outcomes(passed=1)


@pytest.mark.parametrize("plugins", [(), _PLAYWRIGHT_PLUGINS], ids=["bare", "playwright"])
def test_ordinary_runs_leave_the_context_to_its_own_teardown(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, plugins: tuple[str, ...]
) -> None:
    _configure(pytester, monkeypatch, record_dir=None)
    pytester.makepyfile(_journey(closed_at_teardown=False))

    pytester.runpytest_subprocess("-q", *plugins).assert_outcomes(passed=1)


def test_a_context_the_test_already_closed_is_left_alone(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(pytester, monkeypatch, record_dir=tmp_path / "raw")
    pytester.makepyfile(
        _FAKE_CONTEXT
        + """

@pytest.fixture
def context():
    return FakeContext(pages=())


def test_journey(context):
    pass"""
    )

    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_record_dir_films_the_page_fixture(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_dir = tmp_path / "raw"
    _configure(pytester, monkeypatch, record_dir=record_dir)
    pytester.makepyfile(f"""
def test_context_args(browser_context_args):
    assert browser_context_args["record_video_dir"] == {str(record_dir)!r}""")

    pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS).assert_outcomes(passed=1)
    assert record_dir.is_dir()


def test_video_option_keeps_its_own_directory(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    record_dir = tmp_path / "raw"
    _configure(pytester, monkeypatch, record_dir=record_dir)
    pytester.makepyfile(f"""
def test_context_args(browser_context_args):
    assert browser_context_args["record_video_dir"] != {str(record_dir)!r}""")

    result = pytester.runpytest_subprocess("-q", *_PLAYWRIGHT_PLUGINS, "--video", "on")
    result.assert_outcomes(passed=1)
