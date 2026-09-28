"""Balance CI shards while preserving collection order within each shard."""

from __future__ import annotations

import json
import math
from pathlib import Path
from statistics import median

import pytest

DEFAULT_DURATIONS = Path(__file__).with_name("durations.json")
SHARD_PLAN: pytest.StashKey[dict] = pytest.StashKey()


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--splits", type=int, help="Total number of UI shards.")
    parser.addoption("--group", type=int, help="1-indexed UI shard to execute.")
    parser.addoption(
        "--ui-shard-durations",
        type=Path,
        default=DEFAULT_DURATIONS,
        help="Checked-in estimates of total setup/call/teardown seconds per test.",
    )


def load_durations(path: Path) -> dict[str, float]:
    try:
        data = json.loads(path.read_text())
        durations = data["durations"]
        if data["version"] != 1 or not isinstance(durations, dict):
            raise ValueError("expected version 1 and a durations object")
        for nodeid, seconds in durations.items():
            if (
                not isinstance(nodeid, str)
                or isinstance(seconds, bool)
                or not isinstance(seconds, (int, float))
                or not math.isfinite(seconds)
                or seconds <= 0
            ):
                raise ValueError("durations must be finite positive numbers")
        return durations
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise pytest.UsageError(f"Cannot load UI shard durations from {path}: {exc}") from exc


def assign_shards(
    nodeids: list[str], splits: int, durations: dict[str, float]
) -> tuple[list[int], list[float]]:
    """Assign every collected occurrence once; unseen IDs use the known median."""
    default = median(durations.values()) if durations else 1.0
    costs = [durations.get(nodeid, default) for nodeid in nodeids]
    assignments = [0] * len(nodeids)
    loads = [0.0] * splits
    counts = [0] * splits
    for index in sorted(range(len(nodeids)), key=lambda i: (-costs[i], nodeids[i], i)):
        shard = min(range(splits), key=lambda i: (loads[i], counts[i], i))
        assignments[index] = shard
        loads[shard] += costs[index]
        counts[shard] += 1
    return assignments, loads


@pytest.hookimpl(trylast=True)
def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
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
        "estimated_seconds": loads[group - 1],
        "unknown_tests": sum(nodeid not in durations for nodeid in nodeids),
        "collected_tests": len(items),
    }
    items[:] = [item for item, shard in zip(items, assignments, strict=True) if shard == group - 1]
