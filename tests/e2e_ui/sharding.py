"""Assign collected UI tests to CI shards by recorded duration."""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median

import pytest

from tests.helpers.ui_timings import SHARD_PLAN

DEFAULT_DURATIONS = Path(__file__).with_name("durations.json")


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--splits", type=int, default=None, help="Total number of UI shards.")
    parser.addoption("--group", type=int, default=None, help="1-indexed UI shard to execute.")
    parser.addoption(
        "--ui-shard-durations",
        type=Path,
        default=DEFAULT_DURATIONS,
        help="Checked-in setup/call/teardown seconds per test, used only when sharding.",
    )


def _positive(seconds: object) -> bool:
    return (
        not isinstance(seconds, bool)
        and isinstance(seconds, (int, float))
        and math.isfinite(seconds)
        and seconds > 0
    )


def load_durations(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text())
        durations = data["durations"]
        if data["version"] != 1 or not isinstance(durations, dict):
            raise ValueError("expected version 1 and a durations object")
        parsed: dict[str, float] = {}
        for nodeid, seconds in durations.items():
            if not isinstance(nodeid, str) or not _positive(seconds):
                raise ValueError("durations must be finite positive numbers")
            parsed[nodeid] = float(seconds)
        return parsed
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise pytest.UsageError(f"Cannot load UI shard durations from {path}: {exc}") from exc


def assign_shards(
    nodeids: list[str], splits: int, durations: dict[str, float]
) -> tuple[list[int], list[float]]:
    """Put the longest tests on the least-loaded shard. Every id is assigned once."""
    default = float(median(durations.values())) if durations else 1.0
    costs = [durations.get(nodeid, default) for nodeid in nodeids]
    assignments = [0] * len(nodeids)
    loads = [0.0] * splits
    counts = [0] * splits
    order = sorted(range(len(nodeids)), key=lambda index: (-costs[index], nodeids[index], index))
    for index in order:
        shard = min(
            range(splits),
            key=lambda candidate: (loads[candidate], counts[candidate], candidate),
        )
        assignments[index] = shard
        loads[shard] += costs[index]
        counts[shard] += 1
    return assignments, loads


def apply_shard(config: pytest.Config, items: list[pytest.Item]) -> None:
    splits = config.getoption("--splits")
    group = config.getoption("--group")
    if splits is None and group is None:
        return
    if splits is None or group is None:
        raise pytest.UsageError("--splits and --group must be passed together")
    if splits < 1:
        raise pytest.UsageError("--splits must be >= 1")
    if not 1 <= group <= splits:
        raise pytest.UsageError(f"--group must be between 1 and {splits}")

    durations = load_durations(config.getoption("--ui-shard-durations"))
    nodeids = [item.nodeid for item in items]
    assignments, loads = assign_shards(nodeids, splits, durations)
    config.stash[SHARD_PLAN] = {
        "algorithm": "duration-lpt-v1",
        "estimated_seconds": round(loads[group - 1], 3),
        "unknown_tests": sum(nodeid not in durations for nodeid in nodeids),
        "collected_tests": len(items),
    }
    items[:] = [item for item, shard in zip(items, assignments, strict=True) if shard == group - 1]
