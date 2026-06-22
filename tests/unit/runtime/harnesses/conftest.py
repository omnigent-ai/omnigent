"""
Conftest for the harness process-manager / runner tests.

Ensures the runner subprocesses these tests spawn can import both
the production ``omnigent`` package AND the test fixture harness
at ``tests.unit.runtime.harnesses._test_harness``.

pytest's :data:`pyproject.toml` ``pythonpath = ["."]`` adds the
project root to ``sys.path`` of the test process — but
:func:`asyncio.create_subprocess_exec` only inherits the OS env
(``PYTHONPATH``), not the parent's ``sys.path`` mutations. Without
this fixture the runner subprocess starts with no project root on
its path and fails to import either ``omnigent.runtime.harnesses._runner``
or the test harness module.

The fixture is autouse-scoped to this directory, so every spawn
in these tests inherits a PYTHONPATH that includes the project
root. Setting it via :func:`monkeypatch.setenv` keeps the
modification scoped to one test — other test modules that don't
care about PYTHONPATH are unaffected.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

# Project root: three parents up from this conftest
# (tests/runtime/harnesses/conftest.py → repo root).
_PROJECT_ROOT = Path(__file__).resolve().parents[4]


@pytest.fixture(autouse=True)
def _ensure_subprocess_pythonpath(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Prepend the project root to the ``PYTHONPATH`` env var for the
    duration of the test, so spawned subprocesses can import
    ``omnigent`` and ``tests.*``.

    Prepend (don't overwrite) so any developer-set ``PYTHONPATH``
    is preserved as the suffix.
    """
    existing = os.environ.get("PYTHONPATH", "")
    new_path = f"{_PROJECT_ROOT}{os.pathsep}{existing}" if existing else str(_PROJECT_ROOT)
    monkeypatch.setenv("PYTHONPATH", new_path)
