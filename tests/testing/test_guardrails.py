"""Unit tests for the test-environment guardrails.

In-process only: no server, no browser. Exercises the pass case, each
violation's warning, and the contract that ``warn_only=True`` never
raises. Also covers the hard-fail path now used by the pytest session
guardrail hook.
"""

from __future__ import annotations

import logging

import pytest

from omnigent.testing.guardrails import (
    DEV_PORTS,
    TestGuardrailError,
    base_url_violation,
    check_test_environment,
    looks_like_pytest,
    looks_like_test_db,
)

# An env that asserts a test run without relying on the ambient
# PYTEST_CURRENT_TEST (which pytest only sets per-test).
_TEST_ENV = {"OMNIGENT_TEST_MODE": "1"}
_TMP_DB = "sqlite:////tmp/pytest-abc/test.db"


def _guardrail_warnings(records: list[logging.LogRecord]) -> list[str]:
    """Return the messages of records emitted by the guardrail logger."""
    return [
        r.getMessage()
        for r in records
        if r.name == "omnigent.testing.guardrails" and "TEST GUARDRAIL:" in r.getMessage()
    ]


# ── pass case ────────────────────────────────────────────


def test_clean_environment_emits_no_warning(caplog: pytest.LogCaptureFixture) -> None:
    """A test process + tmp DB + ephemeral base URL is clean."""
    with caplog.at_level(logging.WARNING):
        violations = check_test_environment(
            env=_TEST_ENV,
            db_uri=_TMP_DB,
            base_url="http://127.0.0.1:54321",
            warn_only=True,
        )
    assert violations == []
    assert _guardrail_warnings(caplog.records) == []


@pytest.mark.parametrize(
    "db_uri",
    [
        "sqlite://",
        "sqlite:///:memory:",
        "sqlite:///file::memory:?cache=shared",
        "sqlite:////tmp/test.db",
        "sqlite:////tmp/foo/test.db",
        "sqlite:///foo_test.db",
        "sqlite:////home/user/project/tests/session.db",
        "sqlite:///tests/session.db",
        "sqlite:////var/data/my_test_store.db",
    ],
)
def test_looks_like_test_db_accepts_throwaway_uris(db_uri: str) -> None:
    assert looks_like_test_db(db_uri) is True


@pytest.mark.parametrize(
    "db_uri",
    [
        "",
        "sqlite:////home/alice/.omnigent/chat.db",
        "sqlite:///testing.db",
        "sqlite:///test123.db",
        "sqlite:///contest.db",
        "sqlite:///latest.db",
        "postgresql://prod-host:5432/omnigent",
        "postgresql://prod-test-cluster/app",
        "postgres://h/latest",
    ],
)
def test_looks_like_test_db_rejects_real_uris(db_uri: str) -> None:
    assert looks_like_test_db(db_uri) is False


def test_looks_like_test_db_rejects_symlink_to_real_db(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A test-named symlink in a writable dir must not launder a real DB.

    A file in a world-writable dir like /tmp can be a symlink an attacker
    (or a stale fixture) planted to point a ``test``-named path at a real
    database. The classifier resolves symlinks, so the real (non-throwaway)
    target is what gets judged.
    """
    # The symlink sits in a temp dir with a ``test`` name (both of which the
    # classifier would otherwise trust); its target is a real DB outside any
    # temp root. Without resolution this passes on name/location alone.
    link = tmp_path / "test.db"
    link.symlink_to("/opt/omnigent/prod.db")
    assert looks_like_test_db(f"sqlite:///{link}") is False


@pytest.mark.parametrize("db_uri", ["", "   "])
def test_empty_db_uri_skips_db_check(
    db_uri: str,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        assert check_test_environment(env=_TEST_ENV, db_uri=db_uri, warn_only=False) == []
    assert any("db_uri is blank; skipping DB check" in r.getMessage() for r in caplog.records)


def test_looks_like_pytest_via_flag() -> None:
    assert looks_like_pytest({"OMNIGENT_TEST_MODE": "1"}) is True
    assert looks_like_pytest({"PYTEST_CURRENT_TEST": "x::y (call)"}) is True


# ── each violation emits a warning ───────────────────────


def test_non_test_process_warns(
    caplog: pytest.LogCaptureFixture, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A process with no test signal emits the process-check warning.

    ``looks_like_pytest`` consults ``sys.modules`` (pytest is always
    imported while these tests run), so we stub the module-set helper to
    simulate a non-test process, then confirm the violation is logged and
    warn mode still returns rather than raising.
    """
    from omnigent.testing import guardrails

    monkeypatch.setattr(guardrails, "_imported_modules", frozenset)
    with caplog.at_level(logging.WARNING):
        violations = check_test_environment(
            env={},
            db_uri=_TMP_DB,
            warn_only=True,
        )
    warnings = _guardrail_warnings(caplog.records)
    assert any("does not look like a pytest run" in w for w in warnings)
    assert any("pytest run" in v for v in violations)


def test_real_db_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        check_test_environment(
            env=_TEST_ENV,
            db_uri="sqlite:////home/alice/.omnigent/chat.db",
            warn_only=True,
        )
    warnings = _guardrail_warnings(caplog.records)
    assert any("does not look like a test DB" in w for w in warnings)


@pytest.mark.parametrize("port", sorted(DEV_PORTS))
def test_dev_port_base_url_warns(port: int, caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        violations = check_test_environment(
            env=_TEST_ENV,
            db_uri=_TMP_DB,
            base_url=f"http://localhost:{port}",
            warn_only=True,
        )
    warnings = _guardrail_warnings(caplog.records)
    assert any(f"port {port}" in w for w in warnings)
    assert any(str(port) in v for v in violations)


def test_dev_host_no_port_warns() -> None:
    assert base_url_violation("http://localhost") is not None
    assert base_url_violation("http://host.docker.internal") is not None


def test_ephemeral_base_url_is_clean() -> None:
    # Random free port on loopback -> the fixture-server shape. Clean.
    assert base_url_violation("http://127.0.0.1:49152") is None


def test_multiple_violations_each_warn(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING):
        violations = check_test_environment(
            env=_TEST_ENV,
            db_uri="sqlite:////home/alice/.omnigent/chat.db",
            base_url="http://localhost:8000",
            warn_only=True,
        )
    warnings = _guardrail_warnings(caplog.records)
    assert len(violations) == 2
    assert len(warnings) == 2


# ── warn_only=True never raises ──────────────────────────


def test_warn_only_never_raises() -> None:
    """Every violation at once must not raise when warn_only=True."""
    # Strip both the flag and PYTEST_CURRENT_TEST so even the process
    # check has its best shot at a violation; warn mode must still return.
    violations = check_test_environment(
        env={},
        db_uri="sqlite:////home/alice/.omnigent/chat.db",
        base_url="http://localhost:6767",
        warn_only=True,
    )
    assert isinstance(violations, list)
    # DB + base_url violations are deterministic regardless of sys.modules.
    assert any("test DB" in v for v in violations)
    assert any("6767" in v for v in violations)


# ── hard-fail mode (warn_only=False) ─────────────────────


def test_hard_fail_raises_on_violation() -> None:
    with pytest.raises(TestGuardrailError) as exc:
        check_test_environment(
            env=_TEST_ENV,
            db_uri="sqlite:////home/alice/.omnigent/chat.db",
            warn_only=False,
        )
    assert "TEST GUARDRAIL:" in str(exc.value)


@pytest.mark.parametrize(
    "db_uri",
    [
        "postgresql://prod-test-cluster/app",
        "postgres://h/latest",
    ],
)
def test_hard_fail_rejects_non_sqlite_test_substrings(db_uri: str) -> None:
    with pytest.raises(TestGuardrailError) as exc:
        check_test_environment(env=_TEST_ENV, db_uri=db_uri, warn_only=False)
    assert "does not look like a test DB" in str(exc.value)


def test_escape_hatch_downgrades_hard_fail_to_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {**_TEST_ENV, "OMNIGENT_DISABLE_TEST_GUARDRAILS": "yes"}
    with caplog.at_level(logging.WARNING):
        violations = check_test_environment(
            env=env,
            db_uri="sqlite:////home/alice/.omnigent/chat.db",
            warn_only=False,
        )
    assert any("does not look like a test DB" in v for v in violations)
    assert any("escape hatch active" in w for w in _guardrail_warnings(caplog.records))
    assert any("does not look like a test DB" in w for w in _guardrail_warnings(caplog.records))


def test_escape_hatch_message_only_for_hard_fail_suppression(
    caplog: pytest.LogCaptureFixture,
) -> None:
    env = {**_TEST_ENV, "OMNIGENT_DISABLE_TEST_GUARDRAILS": "yes"}
    with caplog.at_level(logging.WARNING):
        check_test_environment(
            env=env,
            db_uri="sqlite:////home/alice/.omnigent/chat.db",
            warn_only=True,
        )
        check_test_environment(env=env, db_uri=_TMP_DB, warn_only=False)
    assert not any("escape hatch active" in r.getMessage() for r in caplog.records)


def test_dev_port_base_url_hard_fails() -> None:
    with pytest.raises(TestGuardrailError) as exc:
        check_test_environment(
            env=_TEST_ENV,
            db_uri=_TMP_DB,
            base_url="http://localhost:6767",
            warn_only=False,
        )
    assert "port 6767" in str(exc.value)


def test_hard_fail_passes_when_clean() -> None:
    # No exception, returns empty list.
    assert (
        check_test_environment(
            env=_TEST_ENV,
            db_uri=_TMP_DB,
            base_url="http://127.0.0.1:49152",
            warn_only=False,
        )
        == []
    )
