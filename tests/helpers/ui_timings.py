"""Record complete UI test timings without changing test selection or execution."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest


def pytest_addoption(parser: pytest.Parser) -> None:
    parser.addoption("--ui-timing-output", type=Path, help="Write per-phase JSONL timing records.")


def xdist_worker(config: pytest.Config) -> str | None:
    """Return this process's xdist worker id, e.g. ``"gw0"``, or ``None``.

    Reads xdist's own worker input rather than ``PYTEST_XDIST_WORKER``, which
    a pytest run launched from inside another worker inherits.
    """
    workerinput = getattr(config, "workerinput", None)
    return workerinput["workerid"] if workerinput is not None else None


def pytest_configure(config: pytest.Config) -> None:
    path = config.getoption("--ui-timing-output")
    if path is None:
        return
    worker = xdist_worker(config)
    if worker:
        # Each xdist worker records the tests it ran in its own file.
        path = path.with_name(f"{path.stem}-{worker}{path.suffix}")
    else:
        # Worker files from an earlier run would mix into this run's artifacts.
        worker_file = re.compile(rf"{re.escape(path.stem)}-gw\d+{re.escape(path.suffix)}")
        for stale in path.parent.glob(f"{path.stem}-gw*{path.suffix}"):
            if worker_file.fullmatch(stale.name):
                stale.unlink(missing_ok=True)
        if config.getoption("numprocesses", default=0):
            # The controller only relays worker reports.
            path.unlink(missing_ok=True)
            return
    config.pluginmanager.register(TimingRecorder(path, worker), "ui-timing-recorder")


class TimingRecorder:
    def __init__(self, path: Path, worker: str | None = None) -> None:
        self.path = path
        self.worker = worker
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def write(self, record: dict) -> None:
        # Append each report so diagnostics survive a later process crash.
        with self.path.open("a") as stream:
            stream.write(json.dumps(record, sort_keys=True) + "\n")

    def pytest_collection_finish(self, session: pytest.Session) -> None:
        config = session.config
        self.write(
            {
                "type": "plan",
                "version": 1,
                "splits": config.getoption("--splits", default=None),
                "group": config.getoption("--group", default=None),
                "worker": self.worker,
                "nodeids": [item.nodeid for item in session.items],
                "run_id": os.environ.get("GITHUB_RUN_ID"),
                "run_attempt": os.environ.get("GITHUB_RUN_ATTEMPT"),
                "commit": os.environ.get("GITHUB_SHA"),
                "markexpr": config.option.markexpr,
                "keyword": config.option.keyword,
            }
        )

    def pytest_runtest_logreport(self, report: pytest.TestReport) -> None:
        self.write(
            {
                "type": "phase",
                "nodeid": report.nodeid,
                "when": report.when,
                "outcome": report.outcome,
                "duration": report.duration,
                "attempt": getattr(report, "rerun", 0),
            }
        )

    def pytest_sessionfinish(self, exitstatus: int) -> None:
        self.write({"type": "finish", "exitstatus": int(exitstatus)})
