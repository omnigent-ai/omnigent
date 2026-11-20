"""CockroachDB schema locks protect live DDL without consuming test timeouts."""

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from filelock import FileLock, Timeout

from tests.db import conftest as schema_fixtures

pytest_plugins = ["pytester"]


@pytest.mark.parametrize(
    ("name", "fixtures", "parameters", "expected"),
    [
        ("test_cockroachdb.py", ["db_uri"], {}, True),
        ("test_migration_workflows.py", ["db_uri"], {}, True),
        ("test_migration_scheduled_tasks.py", ["dispatch_migration_engine"], {}, True),
        ("test_migration_scheduled_tasks.py", ["db_engine"], {"db_engine": "configured"}, True),
        ("test_migration_scheduled_tasks.py", ["db_engine"], {"db_engine": "migration"}, False),
        ("test_migration_scheduled_tasks.py", ["db_engine"], {"db_engine": "metadata"}, False),
        ("test_migration_workspace_id.py", ["db_engine"], {}, False),
        ("test_migration_agents_session_id.py", ["db_engine"], {}, False),
        ("test_migration_preferences.py", ["migration_engine"], {}, False),
        ("test_crdb_core.py", ["db_uri"], {}, False),
    ],
)
def test_schema_ddl_selection(
    name: str, fixtures: list[str], parameters: dict[str, str], expected: bool
) -> None:
    item = Mock(
        path=Path(name), fixturenames=fixtures, callspec=SimpleNamespace(params=parameters)
    )
    assert schema_fixtures._runs_schema_ddl(item) is expected


@pytest.mark.parametrize("protocol_error", [False, True])
def test_schema_lock_is_released_after_protocol(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, protocol_error: bool
) -> None:
    monkeypatch.setenv("OMNIGENT_TEST_DB_URI", "cockroachdb://localhost/database")
    lock_path = tmp_path / "schema.lock"
    monkeypatch.setattr(schema_fixtures, "_CRDB_SCHEMA_LOCK", lock_path)
    item = Mock(path=Path("test_migration_example.py"), fixturenames=["db_uri"], callspec=None)
    protocol = schema_fixtures.pytest_runtest_protocol(item)
    next(protocol)
    with pytest.raises(Timeout), FileLock(lock_path, timeout=0):
        pass
    if protocol_error:
        with pytest.raises(RuntimeError, match="protocol failed"):
            protocol.throw(RuntimeError("protocol failed"))
    else:
        with pytest.raises(StopIteration):
            next(protocol)
    with FileLock(lock_path, timeout=0):
        pass


def test_schema_lock_wraps_timeout_timer(
    pytester: pytest.Pytester, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OMNIGENT_TEST_DB_URI", "cockroachdb://localhost/database")
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    pytester.makeini("[pytest]\n")
    pytester.makeconftest("""
from pathlib import Path

import pytest
from filelock import FileLock, Timeout

from tests.db import conftest as schema_fixtures

pytest_plugins = ["tests.db.conftest"]
schema_fixtures._CRDB_SCHEMA_LOCK = Path(__file__).with_suffix(".lock")

def assert_schema_locked():
    with pytest.raises(Timeout), FileLock(schema_fixtures._CRDB_SCHEMA_LOCK, timeout=0):
        pass

@pytest.hookimpl(tryfirst=True)
def pytest_timeout_set_timer(item, settings):
    assert_schema_locked()

@pytest.hookimpl(tryfirst=True)
def pytest_timeout_cancel_timer(item):
    assert_schema_locked()

@pytest.fixture
def db_uri():
    assert_schema_locked()
    return "sqlite://"

def pytest_sessionfinish():
    with FileLock(schema_fixtures._CRDB_SCHEMA_LOCK, timeout=0):
        pass
""")
    pytester.makepyfile(
        test_migration_example="""
def test_configured_schema(db_uri):
    assert db_uri == "sqlite://"
"""
    )
    pytester.runpytest_subprocess("-p", "pytest_timeout", "--timeout=1", "-q").assert_outcomes(
        passed=1
    )
