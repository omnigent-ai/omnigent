"""Fixtures for resilience-lab tests."""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Generator, Iterator

import pytest

from tests.e2e.resilience.lab.lab import Lab, LabConfig, LabMode

#: Set to ``1`` to keep every lab root, not only failing ones.
KEEP_ENV = "OMNIGENT_RESILIENCE_KEEP"


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[None]) -> Generator[None]:
    """Expose each phase's report on the item so fixtures can keep failed labs."""
    outcome = yield
    report = outcome.get_result()
    setattr(item, f"_resilience_{report.when}", report)


def require_tmux() -> None:
    """Skip unless tmux is installed; native harness terminals need it.

    Each harness CLI is checked when a session for it is created.
    """
    if shutil.which("tmux") is None:
        pytest.skip("'tmux' is not on PATH; native harness labs need it")


@pytest.fixture
def lab_factory(request: pytest.FixtureRequest) -> Iterator[Callable[..., Lab]]:
    """Start labs on demand; stop them after the test and delete passing runs.

    A failing run (or any run with ``OMNIGENT_RESILIENCE_KEEP=1``) keeps its
    root (logs, database, proxy events) and prints it.
    """
    labs: list[Lab] = []

    def _start(mode: LabMode = "host", **options: object) -> Lab:
        require_tmux()
        lab = Lab(LabConfig(mode=mode, **options))  # type: ignore[arg-type]
        labs.append(lab)
        return lab.start()

    yield _start
    reports = [getattr(request.node, f"_resilience_{when}", None) for when in ("setup", "call")]
    # An expected failure (a pinned gap) is evidence too; keep its logs.
    failed = any(
        getattr(r, "failed", False) or hasattr(r, "wasxfail") for r in reports if r is not None
    )
    keep = failed or os.environ.get(KEEP_ENV) == "1"
    errors: list[Exception] = []
    for lab in labs:
        # One lab failing to stop must not leave the others running.
        try:
            lab.stop()
            if keep:
                print(f"\nresilience lab kept for inspection:\n{lab.describe()}")
            else:
                lab.remove()
        except Exception as exc:
            errors.append(exc)
    if errors:
        raise errors[0]
