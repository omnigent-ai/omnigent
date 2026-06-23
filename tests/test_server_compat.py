"""
Unit tests for the server-version backwards-compat helpers
(:mod:`tests._helpers.compat`). Pure logic only — no live server.

See ``docs/SERVER_VERSION_COMPAT_CI.md``.
"""

from __future__ import annotations

import os
import sys

import pytest

from tests._helpers import compat


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ("0.1.1", (0, 1, 1)),
        ("0.1.2.dev0", (0, 1, 2)),
        ("0.1.2rc1", (0, 1, 2)),
        ("1.2.3.post4", (1, 2, 3)),
        ("2.0", (2, 0)),
    ],
)
def test_release_tuple_ignores_suffixes(version: str, expected: tuple[int, ...]) -> None:
    assert compat.release_tuple(version) == expected


@pytest.mark.parametrize(
    ("server", "required", "expected"),
    [
        # Dev version of X must satisfy a feature gated on X (the whole
        # reason we compare release tuples, not full PEP 440 ordering).
        ("0.1.2.dev0", "0.1.2", True),
        # Equal releases.
        ("0.1.2", "0.1.2", True),
        # Newer server runs older-gated features.
        ("0.2.0", "0.1.2", True),
        # Old server skips a newer feature.
        ("0.1.1", "0.1.2", False),
        ("0.1.2.dev0", "0.1.3", False),
    ],
)
def test_meets_min_server_version(server: str, required: str, expected: bool) -> None:
    assert compat.meets_min_server_version(server, required) is expected


@pytest.mark.parametrize(
    ("reported", "override", "expected"),
    [
        # /api/version is source of truth.
        ("0.1.1", None, "0.1.1"),
        # Backstop used only when the report is missing.
        (None, "0.1.1", "0.1.1"),
        # Agreement (dev vs final of the same release counts as agreeing).
        ("0.1.2.dev0", "0.1.2", "0.1.2.dev0"),
        ("0.1.1", "0.1.1", "0.1.1"),
    ],
)
def test_reconcile_server_version_ok(
    reported: str | None, override: str | None, expected: str
) -> None:
    assert compat.reconcile_server_version(reported, override) == expected


def test_reconcile_server_version_disagreement_raises() -> None:
    # The PYTHONPATH-shadow tripwire: report and pinned version differ.
    with pytest.raises(RuntimeError, match="version mismatch"):
        compat.reconcile_server_version("0.1.2.dev0", "0.1.1")


def test_reconcile_server_version_unreadable_without_backstop_raises() -> None:
    with pytest.raises(RuntimeError, match="could not read"):
        compat.reconcile_server_version(None, None, source="http://localhost:6767")


def test_server_redirect_inert_without_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(compat.COMPAT_SERVER_PYTHON_ENV, raising=False)
    monkeypatch.delenv("PYTHONPATH", raising=False)
    assert compat.compat_server_python() is None
    assert compat.server_executable() == sys.executable
    # Inherit CWD (None) outside compat mode.
    assert compat.compat_server_cwd() is None
    # Normal mode prepends the worktree root to PYTHONPATH.
    env: dict[str, str] = {}
    compat.apply_server_env(env, "/repo/root")
    assert env["PYTHONPATH"].startswith(f"/repo/root{os.pathsep}")


def test_server_redirect_active_with_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(compat.COMPAT_SERVER_PYTHON_ENV, "/srv-venv/bin/python")
    assert compat.compat_server_python() == "/srv-venv/bin/python"
    assert compat.server_executable() == "/srv-venv/bin/python"
    # Compat mode drops the worktree prepend so the pinned install resolves.
    env = {"PYTHONPATH": "/repo/root:/preexisting"}
    compat.apply_server_env(env, "/repo/root")
    assert "PYTHONPATH" not in env
    # Compat mode runs the server from a neutral dir that does NOT contain an
    # omnigent/ package (else CWD on sys.path[0] would shadow the old install).
    cwd = compat.compat_server_cwd()
    assert cwd is not None
    assert os.path.isdir(cwd)
    assert not os.path.exists(os.path.join(cwd, "omnigent"))
