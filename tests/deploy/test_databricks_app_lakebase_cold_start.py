"""Databricks App boot must ride out a Lakebase endpoint that is resuming.

``deploy/databricks/src/app.py`` connects to Lakebase and runs Alembic
migrations at module import time.  A managed endpoint suspends after an
idle window, and a boot that lands while it is still resuming gets a
transient ``sqlalchemy.exc.OperationalError`` ("the database system is
starting up").  If the boot treats that first error as fatal, the platform
restarts the container into the same cold window and the app crash-loops
until a restart happens to hit a warm endpoint.

These tests drive the real module-level boot path and assert that it
retries transient errors, still fails loudly when the database never comes
up or the failure is not transient, and boots cleanly when migrations
succeed.
"""

from __future__ import annotations

import importlib
import sys
import time
from collections.abc import Callable, Iterator
from typing import NoReturn
from unittest.mock import MagicMock

import pytest
import sqlalchemy
from sqlalchemy.exc import OperationalError

_APP_MODULE = "deploy.databricks.src.app"

# Env the module reads at import time — required vars, optional vars pinned to
# their defaults so ambient values can't steer the boot, and retry knobs pinned
# small so a retrying boot stays fast under test (ignored by a boot with no
# retry).
_BOOT_ENV = {
    "AP_LAKEBASE_ENDPOINT": "projects/test/endpoints/primary",
    "AP_ARTIFACT_VOLUME_PATH": "/Volumes/test/catalog/vol",
    "PGHOST": "test-host.lakebase.azuredatabricks.net",
    "PGDATABASE": "omnigent_db",
    "PGUSER": "svc_omnigent",
    "PGPORT": "5432",
    "PGSSLMODE": "require",
    "DATABRICKS_APP_PORT": "8000",
    "AP_POOL_RECYCLE_SECONDS": "300",
    "AP_MIGRATE_MAX_ATTEMPTS": "3",
    "AP_MIGRATE_BACKOFF_SECONDS": "0",
}

# The module-level boot path constructs these against the real DB URI; stub
# them so a boot that gets past migrations needs no live database.
_BOOT_STORE_CLASSES = (
    "omnigent.stores.agent_store.sqlalchemy_store.SqlAlchemyAgentStore",
    "omnigent.stores.artifact_store.databricks_volumes.DatabricksVolumesArtifactStore",
    "omnigent.stores.comment_store.sqlalchemy_store.SqlAlchemyCommentStore",
    "omnigent.stores.conversation_store.sqlalchemy_store.SqlAlchemyConversationStore",
    "omnigent.stores.file_store.sqlalchemy_store.SqlAlchemyFileStore",
    "omnigent.stores.host_store.HostStore",
    "omnigent.stores.permission_store.sqlalchemy_store.SqlAlchemyPermissionStore",
    "omnigent.stores.policy_store.sqlalchemy_store.SqlAlchemyPolicyStore",
    "omnigent.stores.project_store.sqlalchemy_store.SqlAlchemyProjectStore",
    "omnigent.stores.scheduled_task_store.sqlalchemy_store.SqlAlchemyScheduledTaskStore",
)


@pytest.fixture
def boot_exit_codes(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[int]]:
    """Prepare an importable boot module and capture ``sys.exit`` calls.

    Yields the list receiving every exit code the boot path passes to
    ``sys.exit``; the capture raises ``SystemExit`` so module execution
    stops exactly like a real exit.
    """
    for key, val in _BOOT_ENV.items():
        monkeypatch.setenv(key, val)
    # The module setdefault()s these into os.environ; registering them with
    # monkeypatch restores the ambient state after the test.
    monkeypatch.delenv("OMNIGENT_AUTH_PROVIDER", raising=False)
    monkeypatch.delenv("OMNIGENT_WEB_UI_DIST", raising=False)

    # Databricks SDK: the Lakebase token mint must not hit a real workspace.
    mock_cred = MagicMock()
    mock_cred.token = "fake-lakebase-token"
    mock_ws = MagicMock()
    mock_ws.postgres.generate_database_credential.return_value = mock_cred
    sdk_stub = MagicMock()
    sdk_stub.WorkspaceClient = MagicMock(return_value=mock_ws)
    monkeypatch.setitem(sys.modules, "databricks", MagicMock(sdk=sdk_stub))
    monkeypatch.setitem(sys.modules, "databricks.sdk", sdk_stub)

    # The SPA archive helper lives beside the entry point, not on sys.path.
    monkeypatch.setitem(sys.modules, "web_ui_archive", MagicMock())

    # No real engines: the migration step is patched per test, so engines
    # are never used for I/O.
    monkeypatch.setattr(sqlalchemy, "create_engine", lambda *a, **kw: MagicMock())

    # The fatal path sleeps before exiting so platform logs get captured;
    # keep tests fast.
    monkeypatch.setattr(time, "sleep", lambda _seconds: None)

    exit_codes: list[int] = []

    def _capture_exit(code: int = 0) -> NoReturn:
        exit_codes.append(code)
        raise SystemExit(code)

    monkeypatch.setattr(sys, "exit", _capture_exit)

    # The module registers a class-level ``Engine.do_connect`` listener at
    # import time; capture every registration so it can be removed after the
    # test instead of accumulating global listeners across imports.
    registered: list[tuple[object, str, Callable[..., object]]] = []
    _real_listens_for = sqlalchemy.event.listens_for

    def _recording_listens_for(
        target: object, identifier: str, *args: object, **kw: object
    ) -> Callable[..., object]:
        real_decorator = _real_listens_for(target, identifier, *args, **kw)

        def _decorator(fn: Callable[..., object]) -> Callable[..., object]:
            result = real_decorator(fn)
            registered.append((target, identifier, fn))
            return result

        return _decorator

    monkeypatch.setattr(sqlalchemy.event, "listens_for", _recording_listens_for)

    # Each test re-executes the module-level boot from scratch.
    monkeypatch.delitem(sys.modules, _APP_MODULE, raising=False)
    try:
        yield exit_codes
    finally:
        for target, identifier, fn in registered:
            if sqlalchemy.event.contains(target, identifier, fn):
                sqlalchemy.event.remove(target, identifier, fn)


def _patch_migrations(
    monkeypatch: pytest.MonkeyPatch,
    behavior: Callable[[int], None],
) -> dict[str, int]:
    """Route the boot's migration step through ``behavior``; count attempts."""
    import omnigent.db.utils as db_utils

    attempts = {"n": 0}

    def _migrate(_engine: object, _uri: str) -> None:
        attempts["n"] += 1
        behavior(attempts["n"])

    monkeypatch.setattr(db_utils, "_run_migrations", _migrate)
    return attempts


def _stub_downstream_boot(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub everything the boot path runs after a successful migration."""
    import omnigent.runtime as runtime

    monkeypatch.setattr(runtime, "init", lambda **kw: None)

    import omnigent.runtime.telemetry as telemetry

    monkeypatch.setattr(telemetry, "init", lambda: None)

    import omnigent.server.app as server_app

    monkeypatch.setattr(server_app, "create_app", lambda **kw: MagicMock())

    import omnigent.server.auth as auth

    monkeypatch.setattr(auth, "create_auth_provider", lambda: MagicMock())
    monkeypatch.setattr(auth, "warn_if_single_user_exposed", lambda *_a: None)

    for dotted in _BOOT_STORE_CLASSES:
        module_path, cls_name = dotted.rsplit(".", 1)
        mod = importlib.import_module(module_path)
        monkeypatch.setattr(mod, cls_name, MagicMock(return_value=MagicMock()))


def _transient_resume_error() -> OperationalError:
    """Build the error a resuming managed Postgres endpoint raises."""
    return OperationalError(
        "connection to server failed",
        None,
        Exception("the database system is starting up"),
    )


def test_boot_retries_past_transient_resume_error(
    boot_exit_codes: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """One transient error while the endpoint resumes must not kill the boot.

    The crash-loop scenario: the first migration attempt fails inside the
    resume window, and the endpoint is available moments later.  The boot
    must retry and come up rather than exit — every platform restart of an
    exiting boot lands back in the same cold window.
    """

    def _cold_once(attempt: int) -> None:
        if attempt < 2:
            raise _transient_resume_error()

    attempts = _patch_migrations(monkeypatch, _cold_once)
    _stub_downstream_boot(monkeypatch)

    try:
        importlib.import_module(_APP_MODULE)
    except SystemExit as exc:
        pytest.fail(
            f"boot exited (code {exc.code}) after {attempts['n']} migration "
            "attempt(s) instead of retrying past a transient resume error — "
            "the platform restarts the container into the same cold window "
            "(crash-loop)"
        )

    assert attempts["n"] == 2, (
        f"expected a retry after the transient error, got {attempts['n']} attempt(s)"
    )
    assert boot_exit_codes == []


def test_boot_gives_up_loudly_when_db_never_comes_up(
    boot_exit_codes: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A database that never answers exhausts bounded retries, then exits.

    Guards both halves of the contract: transient errors get more than one
    attempt, and a genuinely unreachable database still fails the boot
    loudly instead of hanging or being swallowed.
    """

    def _always_cold(_attempt: int) -> None:
        raise _transient_resume_error()

    attempts = _patch_migrations(monkeypatch, _always_cold)

    with pytest.raises(SystemExit):
        importlib.import_module(_APP_MODULE)

    assert boot_exit_codes == [1]
    # Exactly the AP_MIGRATE_MAX_ATTEMPTS pinned in _BOOT_ENV: proves the
    # boot both retries transient errors and honors the env-var wiring.
    assert attempts["n"] == 3, (
        f"boot made {attempts['n']} attempt(s), expected the 3 configured via "
        "AP_MIGRATE_MAX_ATTEMPTS; a single-attempt boot turns every "
        "cold-resume window into a crash-loop"
    )


def test_boot_fails_fast_on_non_transient_migration_error(
    boot_exit_codes: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A real migration/schema error is not retried — it fails immediately."""

    def _schema_error(_attempt: int) -> None:
        raise RuntimeError("database revision is newer than this build")

    attempts = _patch_migrations(monkeypatch, _schema_error)

    with pytest.raises(SystemExit):
        importlib.import_module(_APP_MODULE)

    assert boot_exit_codes == [1]
    assert attempts["n"] == 1


def test_boot_without_migration_failure_does_not_exit(
    boot_exit_codes: list[int],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A warm endpoint boots cleanly with no exit at module level."""
    attempts = _patch_migrations(monkeypatch, lambda _n: None)
    _stub_downstream_boot(monkeypatch)

    importlib.import_module(_APP_MODULE)

    assert attempts["n"] == 1
    assert boot_exit_codes == []
