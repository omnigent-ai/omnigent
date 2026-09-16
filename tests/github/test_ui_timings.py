"""The UI timing artifact observes pytest without changing its results."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]


def _configure(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("GITHUB_RUN_ID", "123")
    monkeypatch.setenv("GITHUB_RUN_ATTEMPT", "2")
    pytester.makeconftest("from tests.e2e_ui.timings import pytest_addoption, pytest_configure")
    pytester.makeini("[pytest]\nmarkers = nightly: scheduled test\n")
    return pytester.path / "artifacts" / "timings.jsonl"


@pytest.mark.parametrize("record", [False, True])
def test_recording_preserves_results_and_filtered_collection(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, record: bool
) -> None:
    path = _configure(pytester, monkeypatch)
    pytester.makepyfile(
        test_cases="""
import pytest

def test_pass():
    pass

def test_fail():
    assert False

def test_skip():
    pytest.skip('synthetic skipped case')

@pytest.mark.nightly
def test_nightly():
    assert False

def test_excluded():
    assert False
"""
    )
    args = [f"--ui-timing-output={path}"] if record else []
    result = pytester.runpytest_subprocess("-q", "-m", "not nightly", "-k", "not excluded", *args)
    result.assert_outcomes(passed=1, failed=1, skipped=1, deselected=2)
    if not record:
        assert not path.exists()
        return
    records = [json.loads(line) for line in path.read_text().splitlines()]
    plan = records[0]
    assert plan["type"] == "plan"
    assert plan["run_id"] == "123" and plan["run_attempt"] == "2"
    assert plan["nodeids"] == [f"test_cases.py::test_{name}" for name in ("pass", "fail", "skip")]
    phases = records[1:-1]
    assert {r["nodeid"] for r in phases} == set(plan["nodeids"])
    assert {r["when"] for r in phases} == {"setup", "call", "teardown"}
    assert all(r["duration"] >= 0 and r["attempt"] == 0 for r in phases)
    assert records[-1] == {"type": "finish", "exitstatus": 1}


def test_records_both_retry_attempts(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _configure(pytester, monkeypatch)
    pytester.makepyfile("""
attempts = iter([False, True])
def test_retry():
    assert next(attempts)
""")
    result = pytester.runpytest_subprocess(
        "-q", "-p", "pytest_rerunfailures", "--reruns=1", f"--ui-timing-output={path}"
    )
    result.assert_outcomes(passed=1)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    calls = [r for r in records if r.get("when") == "call"]
    assert [(r["attempt"], r["outcome"]) for r in calls] == [(0, "rerun"), (1, "passed")]
    assert records[-1] == {"type": "finish", "exitstatus": 0}


def test_completed_reports_survive_process_exit(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _configure(pytester, monkeypatch)
    pytester.makepyfile("""
import os
def test_first():
    pass
def test_crash():
    os._exit(17)
""")
    result = pytester.runpytest_subprocess("-q", f"--ui-timing-output={path}")
    assert result.ret == 17
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert records[0]["type"] == "plan"
    assert any(r.get("when") == "call" and r["outcome"] == "passed" for r in records)
    assert not any(r["type"] == "finish" for r in records)


def test_parallel_pytest_is_rejected_before_touching_output(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _configure(pytester, monkeypatch)
    pytester.makepyfile("def test_pass(): pass")
    result = pytester.runpytest_subprocess(
        "-q", "-p", "xdist.plugin", "-n2", f"--ui-timing-output={path}"
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*--ui-timing-output requires serial pytest within each shard*"])
    assert not path.exists()


def test_new_run_replaces_previous_timing_file(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _configure(pytester, monkeypatch)
    path.parent.mkdir(parents=True)
    path.write_text("stale records from an earlier run\n")
    pytester.makepyfile("def test_pass(): pass")
    result = pytester.runpytest_subprocess("-q", f"--ui-timing-output={path}")
    result.assert_outcomes(passed=1)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["type"] for r in records] == ["plan", "phase", "phase", "phase", "finish"]
    assert records[-1] == {"type": "finish", "exitstatus": 0}


def test_plan_records_final_shard_selection(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _configure(pytester, monkeypatch)
    pytester.makeconftest("""
import pytest
from tests.e2e_ui.timings import pytest_addoption as timing_options, pytest_configure

def pytest_addoption(parser):
    timing_options(parser)
    parser.addoption('--splits', type=int)
    parser.addoption('--group', type=int)

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    items[:] = items[config.getoption('--group') - 1::config.getoption('--splits')]
""")
    pytester.makepyfile(
        test_cases="""
import pytest
@pytest.mark.parametrize('case', range(4))
def test_case(case):
    pass
"""
    )
    result = pytester.runpytest_subprocess(
        "-q", "--splits=2", "--group=2", f"--ui-timing-output={path}"
    )
    result.assert_outcomes(passed=2)
    records = [json.loads(line) for line in path.read_text().splitlines()]
    plan = records[0]
    assert plan["splits"] == 2 and plan["group"] == 2
    assert plan["nodeids"] == ["test_cases.py::test_case[1]", "test_cases.py::test_case[3]"]
    assert [r["nodeid"] for r in records if r.get("when") == "call"] == plan["nodeids"]
