"""Fixtures for resilience-lab tests."""

from __future__ import annotations

import shutil
from collections.abc import Callable, Generator, Iterator

import pytest

from tests.e2e.resilience.lab.lab import Lab, LabConfig, LabMode


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None]:
    """Expose each phase's report on the item so fixtures can keep failed labs."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"_resilience_{report.when}", report)


def require_claude_native() -> None:
    """Skip unless the real Claude Code CLI and tmux are installed."""
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary!r} is not on PATH; claude-native labs need it")


@pytest.fixture
def lab_factory(request: pytest.FixtureRequest) -> Iterator[Callable[..., Lab]]:
    """Start labs on demand; stop them after the test and delete passing runs.

    A failing run keeps its root (logs, database, proxy events) and prints it.
    """
    labs: list[Lab] = []

    def _start(mode: LabMode = "host", **options: object) -> Lab:
        require_claude_native()
        lab = Lab(LabConfig(mode=mode, **options))  # type: ignore[arg-type]
        labs.append(lab)
        return lab.start()

    yield _start
    failed = any(
        getattr(getattr(request.node, f"_resilience_{when}", None), "failed", False)
        for when in ("setup", "call")
    )
    for lab in labs:
        lab.stop()
        if failed:
            print(f"\nresilience lab kept for inspection:\n{lab.describe()}")
        else:
            lab.remove()
