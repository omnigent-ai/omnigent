"""Duration sharding runs every filtered test once and ignores stale estimates."""

from __future__ import annotations

import json
import os
from collections import Counter
from pathlib import Path

import pytest

from tests.e2e_ui.sharding import DEFAULT_DURATIONS, assign_shards, load_durations
from tests.e2e_ui.update_durations import summarize

pytest_plugins = ["pytester"]


def _configure(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeconftest(
        """
import pytest

from tests.e2e_ui import sharding
from tests.helpers.ui_timings import pytest_addoption as timing_options, pytest_configure

def pytest_addoption(parser):
    sharding.pytest_addoption(parser)
    timing_options(parser)

@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config, items):
    sharding.apply_shard(config, items)
"""
    )
    pytester.makeini("[pytest]\nmarkers = nightly: scheduled test\n")
    pytester.makepyfile(
        test_cases="""
import pytest

@pytest.fixture
def fresh():
    state = []
    yield state
    assert len(state) == 1

@pytest.mark.parametrize("case", range(8))
def test_case(case, fresh):
    assert fresh == []
    fresh.append(case)

@pytest.mark.nightly
def test_nightly():
    pass

def test_excluded():
    raise AssertionError("filtered tests must not execute")
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


def _plans(pytester: pytest.Pytester, durations: Path, nightly: bool) -> list[list[str]]:
    plans = []
    for group in range(1, 5):
        output = pytester.path / f"shard-{group}.jsonl"
        args = ["-q", "-k", "not excluded", "--splits=4", f"--group={group}"]
        if not nightly:
            args[1:1] = ["-m", "not nightly"]
        result = pytester.runpytest_subprocess(
            *args,
            f"--ui-shard-durations={durations}",
            f"--ui-timing-output={output}",
        )
        records = [json.loads(line) for line in output.read_text().splitlines()]
        plan = records[0]
        result.assert_outcomes(passed=len(plan["nodeids"]), deselected=1 if nightly else 2)
        calls = [record["nodeid"] for record in records if record.get("when") == "call"]
        assert calls == plan["nodeids"]
        assert plan["sharding"]["algorithm"] == "duration-lpt-v1"
        assert plan["sharding"]["collected_tests"] == (9 if nightly else 8)
        assert plan["sharding"]["unknown_tests"] == 4
        plans.append(calls)
    return plans


@pytest.mark.parametrize("nightly", [False, True])
def test_shards_execute_every_filtered_case_once_in_collection_order(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, nightly: bool
) -> None:
    durations = _configure(pytester, monkeypatch)
    expected = [f"test_cases.py::test_case[{i}]" for i in range(8)]
    if nightly:
        expected.append("test_cases.py::test_nightly")
    plans = _plans(pytester, durations, nightly)
    executed = [nodeid for plan in plans for nodeid in plan]
    assert Counter(executed) == Counter(expected)
    for plan in plans:
        assert plan == [nodeid for nodeid in expected if nodeid in plan]
    # Positional sharding would keep case 0 and case 4 together (indexes 0 and 4).
    homes = [
        next(index for index, plan in enumerate(plans) if nodeid in plan)
        for nodeid in ("test_cases.py::test_case[0]", "test_cases.py::test_case[4]")
    ]
    assert homes[0] != homes[1]


def test_parallel_workers_still_run_each_sharded_test_once(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    durations = _configure(pytester, monkeypatch)
    expected = [f"test_cases.py::test_case[{i}]" for i in range(8)]
    executed = []
    for group in range(1, 3):
        output = pytester.path / f"parallel-{group}.jsonl"
        result = pytester.runpytest_subprocess(
            "-q",
            "-p",
            "xdist.plugin",
            "-n2",
            "--dist=loadscope",
            "-m",
            "not nightly",
            "-k",
            "not excluded",
            "--splits=2",
            f"--group={group}",
            f"--ui-shard-durations={durations}",
            f"--ui-timing-output={output}",
        )
        files = sorted(output.parent.glob(f"{output.stem}-gw*.jsonl"))
        assert files
        assert not output.exists()
        plans = []
        calls = []
        for path in files:
            records = [json.loads(line) for line in path.read_text().splitlines()]
            plans.append(records[0]["nodeids"])
            assert records[0]["sharding"]["algorithm"] == "duration-lpt-v1"
            assert records[-1] == {"exitstatus": 0, "type": "finish"}
            calls.extend(record["nodeid"] for record in records if record.get("when") == "call")
        assert plans == [plans[0]] * len(plans)
        assert Counter(calls) == Counter(plans[0])
        # xdist's summary does not report the controller's deselected count.
        result.assert_outcomes(passed=len(plans[0]))
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


def test_sharded_run_rejects_a_missing_duration_file(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = _configure(pytester, monkeypatch)
    path.unlink()
    result = pytester.runpytest_subprocess(
        "-q", "--splits=2", "--group=1", f"--ui-shard-durations={path}"
    )
    assert result.ret == pytest.ExitCode.USAGE_ERROR
    result.stderr.fnmatch_lines(["*Cannot load UI shard durations*"])


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
    message = result.stderr.str()
    assert (
        "must be passed together" in message
        or "must be >= 1" in message
        or "must be between" in message
    )


def test_heavy_cases_are_spread_without_losing_unknown_or_duplicate_ids() -> None:
    ids = ["heavy-a", "small-a", "heavy-b", "small-b", "new", "new"]
    costs = {"heavy-a": 100.0, "heavy-b": 100.0, "small-a": 1.0, "small-b": 1.0}
    assignment, loads = assign_shards(ids, 2, costs)
    assert assignment[0] != assignment[2]
    assert assignment.count(0) == assignment.count(1) == 3
    assert loads == [151.5, 151.5]
    assert assign_shards(ids, 2, costs) == (assignment, loads)
    assert assign_shards([], 3, costs) == ([], [0.0, 0.0, 0.0])
    assert assign_shards(["a", "b"], 4, {}) == ([0, 1], [1.0, 1.0, 0.0, 0.0])


def test_checked_in_snapshot_balances_its_own_tests() -> None:
    durations = load_durations(DEFAULT_DURATIONS)
    nodeids = list(durations)
    assignment, loads = assign_shards(nodeids, 10, durations)
    assert len(assignment) == len(nodeids)
    assert max(loads) - min(loads) <= max(durations.values())


@pytest.mark.parametrize("cost", [-1, 0, True, "2", float("nan"), float("inf")])
def test_invalid_costs_fail_closed(tmp_path: Path, cost: object) -> None:
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"version": 1, "durations": {"test": cost}}))
    with pytest.raises(pytest.UsageError, match="Cannot load UI shard durations"):
        load_durations(path)
    path.write_text(json.dumps({"version": 2, "durations": {"test": 1}}))
    with pytest.raises(pytest.UsageError, match="Cannot load UI shard durations"):
        load_durations(path)


def _phase(
    nodeid: str, setup: float, call: float, teardown: float, *, attempt: int = 0
) -> list[dict]:
    return [
        {
            "type": "phase",
            "nodeid": nodeid,
            "when": when,
            "duration": duration,
            "outcome": "passed",
            "attempt": attempt,
        }
        for when, duration in (("setup", setup), ("call", call), ("teardown", teardown))
    ]


def _artifact(
    path: Path,
    *,
    run_id: str,
    group: int,
    nodeids: list[str],
    phases: list[dict],
    worker: str | None = None,
    splits: int = 2,
) -> Path:
    plan = {
        "type": "plan",
        "version": 1,
        "run_id": run_id,
        "run_attempt": "1",
        "commit": "abc",
        "splits": splits,
        "group": group,
        "nodeids": nodeids,
        "markexpr": "not nightly",
        "keyword": "",
        "worker": worker,
    }
    rows = [plan, *phases, {"type": "finish", "exitstatus": 0}]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n")
    return path


def test_snapshot_medians_clean_runs_and_combines_workers(tmp_path: Path) -> None:
    first = [
        _artifact(
            tmp_path / "a1.jsonl",
            run_id="1",
            group=1,
            nodeids=["ordinary", "skipped"],
            phases=[
                *_phase("ordinary", 1, 2, 3),
                {
                    "type": "phase",
                    "nodeid": "skipped",
                    "when": "setup",
                    "duration": 0.1,
                    "outcome": "skipped",
                    "attempt": 0,
                },
            ],
        ),
        _artifact(
            tmp_path / "a2.jsonl",
            run_id="1",
            group=2,
            nodeids=["retried"],
            phases=_phase("retried", 3, 4, 2, attempt=1),
        ),
    ]
    second = [
        _artifact(
            tmp_path / "b-gw0.jsonl",
            run_id="2",
            group=1,
            worker="gw0",
            nodeids=["ordinary", "other"],
            phases=_phase("ordinary", 3, 4, 5),
        ),
        _artifact(
            tmp_path / "b-gw1.jsonl",
            run_id="2",
            group=1,
            worker="gw1",
            nodeids=["ordinary", "other"],
            phases=_phase("other", 1, 1, 1),
        ),
        _artifact(
            tmp_path / "b2.jsonl",
            run_id="2",
            group=2,
            nodeids=["alone"],
            phases=_phase("alone", 2, 2, 2),
        ),
    ]
    snapshot = summarize(first + second)
    assert snapshot["durations"] == {"alone": 6, "ordinary": 9, "other": 3}
    assert [source["run_id"] for source in snapshot["sources"]] == ["1", "2"]
    with pytest.raises(ValueError, match="Missing shards"):
        summarize(first[:1])
    with pytest.raises(ValueError, match="Duplicate shard or worker"):
        summarize([*first, first[0]])
    failed = tmp_path / "failed.jsonl"
    failed.write_text(first[1].read_text().replace('"exitstatus": 0', '"exitstatus": 1'))
    with pytest.raises(ValueError, match="Incomplete or unsuccessful"):
        summarize([first[0], failed])
    overlap = _artifact(
        tmp_path / "overlap.jsonl",
        run_id="1",
        group=2,
        nodeids=["ordinary"],
        phases=_phase("ordinary", 1, 1, 1),
    )
    with pytest.raises(ValueError, match="Overlapping test plans"):
        summarize([first[0], overlap])
