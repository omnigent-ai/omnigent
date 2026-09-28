"""Exercise real pytest filtering, sharding, execution, and timing output together."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from tests.e2e_ui.sharding import assign_shards, load_durations
from tests.e2e_ui.update_durations import summarize

pytest_plugins = ["pytester"]


def _configure(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeconftest("""
from tests.e2e_ui import sharding, timings
from tests.e2e_ui.sharding import pytest_collection_modifyitems
from tests.e2e_ui.timings import pytest_configure

def pytest_addoption(parser):
    sharding.pytest_addoption(parser)
    timings.pytest_addoption(parser)
""")
    pytester.makeini("[pytest]\nmarkers = nightly: scheduled test\n")
    pytester.makepyfile(
        test_cases="""
import pytest

@pytest.fixture
def fresh():
    state = []
    yield state
    assert len(state) == 1

@pytest.mark.parametrize('case', range(8))
def test_case(case, fresh):
    assert fresh == []
    fresh.append(case)

@pytest.mark.nightly
def test_nightly():
    pass

def test_excluded():
    raise AssertionError('filtered tests must not execute')
"""
    )
    path = pytester.path / "durations.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "durations": {
                    **{
                        f"test_cases.py::test_case[{i}]": cost
                        for i, cost in enumerate([80, 40, 20, 10])
                    },
                    "test_cases.py::test_nightly": 100,
                    "test_removed.py::test_stale": 2,
                },
            }
        )
    )
    return path


@pytest.mark.parametrize("nightly", [False, True])
def test_shards_execute_every_filtered_case_once_in_collection_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, nightly: bool
) -> None:
    durations = _configure(pytester, monkeypatch)
    expected = [f"test_cases.py::test_case[{i}]" for i in range(8)]
    if nightly:
        expected.append("test_cases.py::test_nightly")
    executed = []
    for group in range(1, 5):
        output = pytester.path / f"shard-{group}.jsonl"
        result = pytester.runpytest_subprocess(
            "-q",
            "-m",
            "" if nightly else "not nightly",
            "-k",
            "not excluded",
            "--splits=4",
            f"--group={group}",
            f"--ui-shard-durations={durations}",
            f"--ui-timing-output={output}",
        )
        records = [json.loads(line) for line in output.read_text().splitlines()]
        plan = records[0]
        result.assert_outcomes(passed=len(plan["nodeids"]), deselected=1 if nightly else 2)
        calls = [r["nodeid"] for r in records if r.get("when") == "call"]
        assert calls == plan["nodeids"] == [n for n in expected if n in calls]
        assert plan["sharding"]["algorithm"] == "duration-lpt-v1"
        assert plan["sharding"]["collected_tests"] == len(expected)
        assert plan["sharding"]["unknown_tests"] == 4
        executed.extend(calls)
    assert Counter(executed) == Counter(expected)


def test_unsharded_run_does_not_need_duration_file(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _configure(pytester, monkeypatch)
    path.unlink()
    result = pytester.runpytest_subprocess(
        "-q", "-k", "not excluded", f"--ui-shard-durations={path}"
    )
    result.assert_outcomes(passed=9, deselected=1)


@pytest.mark.parametrize(
    "args",
    [("--splits=2",), ("--group=1",), ("--splits=0", "--group=1"), ("--splits=2", "--group=3")],
)
def test_invalid_shard_options_fail_before_execution(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, args: tuple[str, ...]
) -> None:
    _configure(pytester, monkeypatch)
    result = pytester.runpytest_subprocess("-q", *args)
    assert result.ret == pytest.ExitCode.USAGE_ERROR


def test_heavy_cases_are_spread_without_losing_unknown_or_duplicate_ids() -> None:
    ids = ["heavy-a", "small-a", "heavy-b", "small-b", "new", "new"]
    costs = {"heavy-a": 100, "heavy-b": 100, "small-a": 1, "small-b": 1}
    assignment, loads = assign_shards(ids, 2, costs)
    assert assignment[0] != assignment[2]
    assert len(assignment) == len(ids)
    assert loads == [151.5, 151.5]
    assert assign_shards(ids, 2, costs) == (assignment, loads)
    assert assign_shards([], 3, costs) == ([], [0, 0, 0])
    assert assign_shards(["a", "b"], 4, {}) == ([0, 1], [1, 1, 0, 0])


@pytest.mark.parametrize("cost", [-1, 0, True, "2", float("nan"), float("inf")])
def test_invalid_costs_fail_closed(tmp_path: Path, cost: object) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "durations": {"test": cost}}))
    with pytest.raises(pytest.UsageError, match="Cannot load UI shard durations"):
        load_durations(path)


def _artifact(path: Path, *, group: int, nodeid: str, retry: bool = False) -> Path:
    rows = [
        {
            "type": "plan",
            "version": 1,
            "run_id": "123",
            "run_attempt": "1",
            "commit": "abc",
            "splits": 2,
            "group": group,
            "nodeids": [nodeid],
            "markexpr": "not nightly",
            "keyword": "",
        }
    ]
    rows.extend(
        {
            "type": "phase",
            "nodeid": nodeid,
            "when": phase,
            "duration": cost,
            "outcome": "passed",
            "attempt": int(retry),
        }
        for phase, cost in [("setup", 3), ("call", 4), ("teardown", 2)]
    )
    rows.append({"type": "finish", "exitstatus": 0})
    path.write_text("\n".join(json.dumps(r) for r in rows))
    return path


def test_snapshot_sums_phases_and_excludes_retried_tests(tmp_path: Path) -> None:
    a = _artifact(tmp_path / "a.jsonl", group=1, nodeid="ordinary")
    b = _artifact(tmp_path / "b.jsonl", group=2, nodeid="retried", retry=True)
    snapshot = summarize([a, b])
    assert snapshot["durations"] == {"ordinary": 9}
    assert snapshot["sources"][0]["commit"] == "abc"
    with pytest.raises(ValueError, match="Missing shards"):
        summarize([a])
    with pytest.raises(ValueError, match="Duplicate shard"):
        summarize([a, a, b])
    b.write_text(b.read_text().replace('"exitstatus": 0', '"exitstatus": 1'))
    with pytest.raises(ValueError, match="Incomplete or unsuccessful"):
        summarize([a, b])
