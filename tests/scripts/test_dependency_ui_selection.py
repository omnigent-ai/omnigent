"""Run the production UI collection hook against direct and indirect consumers."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]
ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture
def sidecar_suite(pytester, monkeypatch):
    monkeypatch.setenv("PYTHONPATH", str(ROOT) + os.pathsep + os.environ.get("PYTHONPATH", ""))
    pytester.makeconftest(
        "from tests.e2e_ui.conftest import (pytest_addoption, "
        "pytest_collection_modifyitems, pytest_runtest_makereport)"
    )
    pytester.makepyfile("""
        import pytest

        @pytest.fixture
        def mocked_native_codex_session():
            return "mocked Responses sidecar"

        @pytest.fixture
        def indirect_session(mocked_native_codex_session):
            return mocked_native_codex_session

        def test_direct(mocked_native_codex_session):
            assert mocked_native_codex_session

        def test_indirect(indirect_session):
            assert indirect_session

        def test_unrelated():
            pass
    """)
    return pytester


@pytest.mark.parametrize("group", [1, 2])
def test_sidecar_selection_precedes_sharding(sidecar_suite, group):
    result = sidecar_suite.runpytest_subprocess(
        "--ui-codex-sidecar-only", "--splits=2", f"--group={group}", "-v"
    )
    result.assert_outcomes(passed=1, deselected=1)
    expected = "direct" if group == 1 else "indirect"
    result.stdout.fnmatch_lines([f"*::test_{expected} PASSED*"])


def test_default_ui_collection_is_unchanged(sidecar_suite):
    sidecar_suite.runpytest_subprocess().assert_outcomes(passed=3)


def test_sidecar_selection_without_consumers_fails(sidecar_suite):
    result = sidecar_suite.runpytest_subprocess("--ui-codex-sidecar-only", "-k", "unrelated")
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*No Codex sidecar consumers collected*"])


def test_missing_sidecar_prerequisite_fails_selected_run(sidecar_suite):
    sidecar_suite.makepyfile("""
        import pytest

        @pytest.fixture
        def mocked_native_codex_session():
            pytest.skip("Codex CLI unavailable")

        def test_consumer(mocked_native_codex_session):
            pass
    """)
    result = sidecar_suite.runpytest_subprocess("--ui-codex-sidecar-only")
    assert result.ret == pytest.ExitCode.TESTS_FAILED
    result.stdout.fnmatch_lines(["*Selected sidecar test skipped:*Codex CLI unavailable*"])


def test_dependency_ui_reuses_compatibility_journeys_and_sidecar_consumers(sidecar_suite):
    sidecar_suite.makepyfile(
        test_compat="""
        import pytest

        @pytest.mark.compat_smoke
        def test_protocol_journey():
            pass

        def test_broad_regression():
            pass
    """
    )
    result = sidecar_suite.runpytest_subprocess("--ui-dependency-smoke")
    result.assert_outcomes(passed=3, deselected=2)


@pytest.mark.parametrize("fault", ["skip", "empty"])
def test_required_dependency_suite_cannot_pass_without_execution(sidecar_suite, fault):
    sidecar_suite.makepyfile("""
        import pytest

        def test_required():
            pytest.skip("Prerequisite unavailable")
    """)
    args = ["-p", "tests._helpers.required_tests"]
    if fault == "empty":
        args += ["-k", "no_matching_test"]
    result = sidecar_suite.runpytest_subprocess(*args)
    assert result.ret == (
        pytest.ExitCode.TESTS_FAILED if fault == "skip" else pytest.ExitCode.NO_TESTS_COLLECTED
    )
