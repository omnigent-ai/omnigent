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
    monkeypatch.delenv("OMNIGENT_COMPAT_SERVER_VERSION", raising=False)
    monkeypatch.delenv("OMNIGENT_COMPAT_SERVER_PYTHON", raising=False)
    if record_dir is not None:
        monkeypatch.setenv("OMNIGENT_E2E_RECORD_DIR", str(record_dir))
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


@pytest.mark.parametrize("inherited_compat", [False, True])
def test_record_dir_stops_the_context_before_a_later_fixture_tears_down(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    inherited_compat: bool,
) -> None:
    if inherited_compat:
        monkeypatch.setenv("OMNIGENT_COMPAT_SERVER_VERSION", "0.16.0")
        monkeypatch.setenv("OMNIGENT_COMPAT_SERVER_PYTHON", "/unused/compat/python")
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


def test_record_dir_stops_the_context_before_a_dependent_fixture_cleans_up(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(pytester, monkeypatch, record_dir=tmp_path / "raw")
    pytester.makepyfile(
        _FAKE_CONTEXT
        + """

@pytest.fixture
def context():
    STATE["context"] = FakeContext()
    return STATE["context"]


@pytest.fixture
def search_sessions(context):
    try:
        yield ["file_name", "fileXname"]
    finally:
        # Deleting the seeded sessions must happen after the video is finalized.
        assert context.closed is True


def test_journey(context, search_sessions):
    assert not context.closed"""
    )

    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_a_context_that_records_on_its_own_is_stopped_too(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _configure(pytester, monkeypatch, record_dir=None)
    pytester.makepyfile(
        _FAKE_CONTEXT
        + f"""

@pytest.fixture
def browser_context_args():
    return {{"record_video_dir": {str(tmp_path / "raw")!r}}}


@pytest.fixture
def context(browser_context_args):
    STATE["context"] = FakeContext()
    return STATE["context"]


@pytest.fixture
def native_session():
    yield "session"
    assert STATE["context"].closed is True


def test_journey(context, native_session):
    assert not context.closed"""
    )

    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)


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


@pytest.mark.parametrize("fails", [False, True], ids=["passing", "failing"])
@pytest.mark.parametrize("warning_flags", [(), ("-W", "error")], ids=["default", "warnings-error"])
def test_context_close_error_is_reported_without_changing_the_test_result(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fails: bool,
    warning_flags: tuple[str, ...],
) -> None:
    _configure(pytester, monkeypatch, record_dir=tmp_path / "raw")
    pytester.makepyfile(
        _FAKE_CONTEXT
        + f"""
from playwright.sync_api import Error

class BrokenContext(FakeContext):
    def close(self):
        raise Error("recording transport disconnected")

@pytest.fixture
def context():
    return BrokenContext()

def test_journey(context):
    assert not {fails!r}, "original journey assertion"
"""
    )

    result = pytester.runpytest_subprocess("-q", *warning_flags)
    result.assert_outcomes(passed=int(not fails), failed=int(fails))
    output = result.stdout.str() + result.stderr.str()
    assert "Could not finalize recording for" in output
    assert "::test_journey" in output
    assert "recording transport disconnected" in output
    if fails:
        assert "original journey assertion" in output


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
