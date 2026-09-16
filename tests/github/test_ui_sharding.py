"""Coverage and timing contracts for the UI shard scheduler."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from tests.e2e_ui.sharding import build_snapshot, collection_digest, plan_shards, read_durations

pytest_plugins = ["pytester"]


def test_balances_cost_and_preserves_exactly_once_collection_order() -> None:
    nodeids = [f"test_{i}" for i in range(30)]
    durations = {n: (60.0 if i % 3 == 0 else 1.0) for i, n in enumerate(nodeids)}
    shards, loads = plan_shards(nodeids, 3, durations)
    assert sorted(n for shard in shards for n in shard) == sorted(nodeids)
    assert all(shard == sorted(shard, key=nodeids.index) for shard in shards)
    assert max(loads) < sum(durations[n] for n in nodeids[::3]) / 2
    reversed_shards, _ = plan_shards(list(reversed(nodeids)), 3, durations)
    assert [set(s) for s in shards] == [set(s) for s in reversed_shards]


@pytest.mark.parametrize("count,splits", [(0, 3), (2, 5), (33, 10)])
def test_unknown_tests_are_partitioned_with_or_without_history(count: int, splits: int) -> None:
    nodeids = [f"test_{i}" for i in range(count)]
    for durations in ({}, {"test_0": 20.0, "deleted_test": 1000.0}):
        shards, _ = plan_shards(nodeids, splits, durations)
        assert sorted(n for shard in shards for n in shard) == sorted(nodeids)
    assert plan_shards(nodeids, splits, {})[0] == [nodeids[i::splits] for i in range(splits)]


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan"), True, "1"])
def test_rejects_invalid_timing_data(tmp_path: Path, value: object) -> None:
    path = tmp_path / "durations.json"
    path.write_text(json.dumps({"version": 1, "durations": {"test_a": value}}))
    with pytest.raises(ValueError):
        read_durations(path)


def _timing_files(tmp_path: Path) -> list[Path]:
    paths = []
    for group, nodeids in enumerate((["a"], ["b", "skipped"]), start=1):
        records = [
            {
                "type": "plan",
                "version": 1,
                "splits": 2,
                "group": group,
                "collection_digest": collection_digest(["a", "b", "skipped"]),
                "collection_count": 3,
                "snapshot_digest": "snapshot",
                "run_id": "123",
                "run_attempt": "1",
                "commit": "abc",
                "markexpr": "",
                "keyword": "",
                "nodeids": nodeids,
            }
        ]
        for nodeid in nodeids:
            for when, duration in (("setup", 1.0), ("call", 2.0), ("teardown", 3.0)):
                records.append(
                    {
                        "type": "phase",
                        "nodeid": nodeid,
                        "when": when,
                        "duration": duration,
                        "outcome": "skipped" if nodeid == "skipped" else "passed",
                        "attempt": 0,
                    }
                )
        records.append({"type": "finish", "exitstatus": 0})
        path = tmp_path / f"shard-{group}.jsonl"
        path.write_text("\n".join(json.dumps(r) for r in records) + "\n")
        paths.append(path)
    return paths


def test_snapshot_includes_setup_and_teardown_without_learning_skips(tmp_path: Path) -> None:
    snapshot = build_snapshot(_timing_files(tmp_path))
    assert snapshot["durations"] == {"a": 6.0, "b": 6.0}
    assert snapshot["source_run_id"] == "123"


@pytest.mark.parametrize(
    "defect",
    [
        "missing_shard",
        "duplicate_shard",
        "mixed_run",
        "mixed_snapshot",
        "missing_result",
        "duplicate_test",
        "incomplete",
        "failed",
    ],
)
def test_refuses_incomplete_or_inconsistent_artifacts(tmp_path: Path, defect: str) -> None:
    paths = _timing_files(tmp_path)
    records = [json.loads(line) for line in paths[1].read_text().splitlines()]
    if defect == "missing_shard":
        paths.pop()
    elif defect == "duplicate_shard":
        paths.append(paths[0])
    elif defect == "mixed_run":
        records[0]["run_id"] = "456"
    elif defect == "mixed_snapshot":
        records[0]["snapshot_digest"] = "other"
    elif defect == "missing_result":
        records = [r for r in records if r.get("nodeid") != "b"]
    elif defect == "duplicate_test":
        records[0]["nodeids"] = ["a", "skipped"]
        for r in records:
            if r.get("nodeid") == "b":
                r["nodeid"] = "a"
    elif defect == "incomplete":
        records.pop()
    elif defect == "failed":
        records[-1]["exitstatus"] = 1
    if len(paths) > 1:
        paths[1].write_text("\n".join(json.dumps(r) for r in records) + "\n")
    with pytest.raises(ValueError):
        build_snapshot(paths)


def _configure_pytester(pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch) -> None:
    root = str(Path(__file__).resolve().parents[2])
    monkeypatch.setenv("PYTHONPATH", os.pathsep.join([root, os.environ.get("PYTHONPATH", "")]))
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    pytester.makeconftest("""
from tests.e2e_ui.sharding import (
    pytest_addoption, pytest_configure, pytest_collection_modifyitems,
)
""")
    pytester.makeini("[pytest]\nmarkers = nightly: scheduled test\n")
    pytester.makepyfile("""
import pytest
@pytest.mark.parametrize('variant', range(4))
def test_variant(variant):
    assert variant >= 0
def test_keyword_excluded():
    pass
@pytest.mark.nightly
def test_nightly():
    pass
""")


@pytest.mark.parametrize("use_history", [False, True])
def test_pytest_filters_before_sharding_and_reports_every_phase(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, use_history: bool
) -> None:
    _configure_pytester(pytester, monkeypatch)
    nodeids = [f"{pytester._name}.py::test_variant[{i}]" for i in range(4)]
    durations = dict(zip(nodeids, [30, 5, 20, 5], strict=True)) if use_history else {}
    duration_file = pytester.path / "durations.json"
    duration_file.write_text(json.dumps({"version": 1, "durations": durations}))
    expected, _ = plan_shards(nodeids, 2, durations)
    timings = []
    for group in (1, 2):
        path = pytester.path / f"timings-{group}.jsonl"
        timings.append(path)
        result = pytester.runpytest_subprocess(
            "-q",
            "-m",
            "not nightly",
            "-k",
            "not keyword_excluded",
            f"--ui-duration-file={duration_file}",
            "--splits=2",
            f"--group={group}",
            f"--ui-timing-output={path}",
        )
        count = len(expected[group - 1])
        result.assert_outcomes(passed=count, deselected=6 - count)
        plan = json.loads(path.read_text().splitlines()[0])
        assert plan["nodeids"] == expected[group - 1]
        assert plan["unknown_count"] == (0 if use_history else 4)
    snapshot = build_snapshot(timings)
    assert len(snapshot["durations"]) == 4
    assert all("nightly" not in n for n in snapshot["durations"])
    records = [json.loads(s) for s in timings[0].read_text().splitlines()]
    assert {r["when"] for r in records if r["type"] == "phase"} == {"setup", "call", "teardown"}


@pytest.mark.parametrize(
    "args",
    [["--splits=2"], ["--group=1"], ["--splits=0", "--group=1"], ["--splits=2", "--group=3"]],
)
def test_bad_shard_options_fail_instead_of_dropping_tests(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch, args: list[str]
) -> None:
    _configure_pytester(pytester, monkeypatch)
    assert pytester.runpytest_subprocess("-q", *args).ret == pytest.ExitCode.USAGE_ERROR


@pytest.mark.parametrize("data", [None, [], {"version": 2, "durations": {}}, {}])
def test_rejects_invalid_snapshot_shape(tmp_path: Path, data: object) -> None:
    path = tmp_path / "durations.json"
    path.write_text(json.dumps(data))
    with pytest.raises(ValueError):
        read_durations(path)


def test_duplicate_collection_is_rejected() -> None:
    with pytest.raises(ValueError, match="unique test IDs"):
        plan_shards(["a", "a"], 2, {})


def test_snapshot_counts_retry_phases(tmp_path: Path) -> None:
    paths = _timing_files(tmp_path)
    records = [json.loads(line) for line in paths[0].read_text().splitlines()]
    retry = [dict(r, attempt=1) for r in records[1:-1]]
    records[2]["outcome"] = "rerun"
    records[-1:-1] = retry
    paths[0].write_text("\n".join(json.dumps(r) for r in records) + "\n")
    assert build_snapshot(paths)["durations"]["a"] == 12.0


def test_empty_shards_can_finish_with_no_tests(tmp_path: Path) -> None:
    paths = _timing_files(tmp_path)
    records = [json.loads(line) for line in paths[0].read_text().splitlines()]
    plan = dict(records[0], splits=3, group=3, nodeids=[])
    path = tmp_path / "shard-3.jsonl"
    path.write_text(json.dumps(plan) + '\n{"type":"finish","exitstatus":5}\n')
    for existing in paths:
        data = [json.loads(line) for line in existing.read_text().splitlines()]
        data[0]["splits"] = 3
        existing.write_text("\n".join(json.dumps(r) for r in data) + "\n")
    assert build_snapshot([*paths, path])["durations"] == {"a": 6.0, "b": 6.0}
