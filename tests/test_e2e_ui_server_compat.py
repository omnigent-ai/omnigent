"""Exercise the UI version gate through pytest without starting a server."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from tests._helpers.compat import COMPAT_SERVER_VERSION_ENV

pytest_plugins = ["pytester"]


def _configure(
    pytester: pytest.Pytester,
    monkeypatch: pytest.MonkeyPatch,
    *,
    reported: str = "0.15.0.dev0",
    pinned: str | None = None,
    expect_server: bool = True,
) -> None:
    root = str(Path(__file__).resolve().parents[1])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.delenv(COMPAT_SERVER_VERSION_ENV, raising=False)
    if pinned is not None:
        monkeypatch.setenv(COMPAT_SERVER_VERSION_ENV, pinned)
    pytester.makeini("[pytest]\nmarkers = min_server_version(version): minimum live version\n")
    pytester.makeconftest(f"""
from unittest.mock import patch

import httpx
import pytest

from tests.e2e_ui.conftest import _enforce_min_server_version, server_version


@pytest.fixture(scope="session")
def live_server():
    assert {expect_server!r}, "version gate unexpectedly started live_server"
    response = httpx.Response(200, json={{"version": {reported!r}}})
    with patch("tests._helpers.compat.httpx.get", return_value=response) as get:
        yield "http://compat.invalid"
        get.assert_called_once_with("http://compat.invalid/api/version", timeout=10)
""")


def test_unmarked_test_does_not_start_server(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(pytester, monkeypatch, expect_server=False)
    pytester.makepyfile("def test_unmarked(): pass")

    pytester.runpytest_subprocess("-q").assert_outcomes(passed=1)


def test_current_dev_server_passes_and_version_is_cached(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(pytester, monkeypatch, pinned="0.15.0")
    pytester.makepyfile("""
import pytest

@pytest.mark.min_server_version("0.15.0")
@pytest.mark.parametrize("case", range(2))
def test_current(case):
    pass
""")

    pytester.runpytest_subprocess("-q").assert_outcomes(passed=2)


def test_older_pinned_server_skips_unsupported_feature(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    _configure(pytester, monkeypatch, reported="0.14.9", pinned="0.14.9")
    pytester.makepyfile("""
import pytest

@pytest.mark.min_server_version("0.15.0")
def test_unsupported():
    pytest.fail("unsupported feature executed")
""")

    result = pytester.runpytest_subprocess("-q", "-rs")
    result.assert_outcomes(skipped=1)
    result.stdout.fnmatch_lines(["*requires server >= 0.15.0; running 0.14.9*"])


@pytest.mark.parametrize("marked", [False, True], ids=["unmarked", "marked"])
def test_shadowed_server_fails_instead_of_skipping(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, marked: bool
) -> None:
    _configure(pytester, monkeypatch, reported="0.15.0.dev0", pinned="0.14.9")
    marker = '@pytest.mark.min_server_version("0.16.0")' if marked else ""
    pytester.makepyfile(f"""
import pytest

{marker}
def test_shadowed():
    pass
""")

    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*RuntimeError: server version mismatch:*"])


@pytest.mark.parametrize(
    "arguments",
    ["", "None", '"0.15.0", "0.16.0"', 'version="0.15.0"', '"invalid"', '""'],
    ids=["missing", "non-string", "extra", "keyword", "invalid-version", "empty"],
)
def test_malformed_marker_errors_without_starting_server(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, arguments: str
) -> None:
    _configure(pytester, monkeypatch, expect_server=False)
    pytester.makepyfile(f"""
import pytest

@pytest.mark.min_server_version({arguments})
def test_malformed():
    pass
""")

    result = pytester.runpytest_subprocess("-q")
    result.assert_outcomes(errors=1)
    result.stdout.fnmatch_lines(["*UsageError:*min_server_version marker*"])
