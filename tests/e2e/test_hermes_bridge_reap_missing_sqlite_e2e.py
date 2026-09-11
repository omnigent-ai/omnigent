"""Regression: the native-harness orphan bridge-dir sweep must survive an
interpreter built without the ``_sqlite3`` C extension.

User journey: a user runs ``omnigent host`` (or a runner comes up) on a machine
whose Python was built without the ``_sqlite3`` C extension — a common
pyenv / source-build gap where ``import sqlite3`` fails with
``ModuleNotFoundError: No module named '_sqlite3'``. During startup the
host/runner performs the cross-harness orphan bridge-dir sweep
(:func:`omnigent.native.native_bridge_common.reap_orphaned_native_bridge_dirs`),
which imports every native harness's bridge module to call its pruner.
If ``omnigent.harnesses.hermes_native.bridge`` imports ``sqlite3`` at module
level, on such an interpreter that import raises and the sweep logs an ERROR
traceback for the hermes bridge module (KPI signature ``og-3c09ce2e837d``). The sweep is
best-effort guarded, so host/runner startup still proceeds and no session
creation fails — but the hermes harness is silently skipped from the sweep and a
noisy dev-build error is emitted on every startup.

This test faithfully reproduces the reported condition **in-process**: it blocks
``_sqlite3`` (so ``import sqlite3`` fails exactly as on the reported build) and
evicts the cached ``sqlite3`` / hermes-bridge modules so the sweep's
``importlib.import_module`` re-executes the bridge's module-level imports. It
then drives the real ``reap_orphaned_native_bridge_dirs`` that the runner and
host call at startup, and asserts the hermes bridge module imported cleanly with
**no** import-failure ERROR logged.

On the buggy code (eager module-level ``import sqlite3``) the sweep logs the
import failure and this test fails; once the sqlite import is made lazy / guarded
so the bridge module imports without ``_sqlite3``, the sweep imports it cleanly
and the test passes.
"""

from __future__ import annotations

import importlib
import logging
import sys

import pytest

_HERMES_BRIDGE = "omnigent.harnesses.hermes_native.bridge"
_SQLITE_MODULES = ("_sqlite3", "sqlite3", "sqlite3.dbapi2")


def _evict(names: tuple[str, ...]) -> None:
    """Drop *names* (and any ``sqlite3.*`` submodules) from ``sys.modules``.

    Forces the next ``import`` of each to re-execute, so the sweep sees the
    module-level imports run under the blocked-``_sqlite3`` condition rather
    than returning a cached module.
    """
    for name in list(sys.modules):
        if name in names or name.startswith("sqlite3."):
            del sys.modules[name]


def test_orphan_bridge_reap_survives_missing_sqlite(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The startup orphan bridge-dir sweep must not error on a no-sqlite build.

    Simulates ``omnigent host`` / runner startup on an interpreter without the
    ``_sqlite3`` extension and asserts the hermes-native bridge imports cleanly
    during :func:`reap_orphaned_native_bridge_dirs` (no import-failure ERROR).
    """
    from omnigent.native import native_bridge_common

    # Sanity: with sqlite present the hermes bridge imports fine.
    importlib.import_module(_HERMES_BRIDGE)

    # Preserve the current module table so a real, working sqlite3 is restored
    # for the rest of the session no matter how this test exits.
    saved = {name: sys.modules.get(name) for name in (*_SQLITE_MODULES, _HERMES_BRIDGE)}

    try:
        # Simulate an interpreter built without the _sqlite3 C extension:
        # evict the cached sqlite3 / hermes-bridge modules and block _sqlite3
        # so re-importing sqlite3 fails exactly as on the reported build.
        _evict((*_SQLITE_MODULES, _HERMES_BRIDGE))
        sys.modules["_sqlite3"] = None  # import _sqlite3 -> ImportError

        caplog.clear()
        with caplog.at_level(logging.ERROR, logger=native_bridge_common.__name__):
            # The real sweep the runner (_entry.py) and host (connect.py) call
            # at startup — imports each native bridge module to run its pruner.
            native_bridge_common.reap_orphaned_native_bridge_dirs()
    finally:
        # Restore a clean, working sqlite3 + hermes bridge for later tests.
        _evict((*_SQLITE_MODULES, _HERMES_BRIDGE))
        for name, module in saved.items():
            if module is not None:
                sys.modules[name] = module
            else:
                sys.modules.pop(name, None)
        importlib.import_module(_HERMES_BRIDGE)

    hermes_import_errors = [
        record
        for record in caplog.records
        if "Error importing native bridge module" in record.getMessage()
        and _HERMES_BRIDGE in record.getMessage()
    ]
    assert not hermes_import_errors, (
        "reap_orphaned_native_bridge_dirs logged an import failure for the "
        "hermes-native bridge on an interpreter without _sqlite3: "
        + " | ".join(record.getMessage() for record in hermes_import_errors)
    )
