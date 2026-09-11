"""Tests for runner identity helpers."""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from omnigent.runner.identity import (
    RUNNER_AUTH_SECRET_ENV_VARS,
    RUNNER_CONNECT_MARKER_ENV_VAR,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    strip_runner_auth_secrets,
    token_bound_runner_id,
    touch_connect_marker,
)


def test_token_bound_runner_id_is_stable_and_token_scoped() -> None:
    """Token-bound ids are deterministic and differ across secret tokens.

    :returns: None.
    """
    first = token_bound_runner_id("tok-one")
    second = token_bound_runner_id("tok-two")

    assert first == token_bound_runner_id(" tok-one ")
    assert first.startswith("runner_token_")
    assert len(first) == len("runner_token_") + 32
    assert first != second
    assert "tok-one" not in first


def test_token_bound_runner_id_rejects_empty_token() -> None:
    """Missing token values fail loud instead of inventing a runner id.

    :returns: None.
    """
    with pytest.raises(RuntimeError, match="tunnel binding token must not be empty"):
        token_bound_runner_id("   ")


def test_strip_removes_every_registered_secret_name() -> None:
    """Every name in the registry is actually stripped.

    Guards the contract between the registry and the helper: if a name
    is ever added to ``RUNNER_AUTH_SECRET_ENV_VARS`` but the stripping
    logic stops covering it (or vice versa), this fails. Also pins that
    the binding token — the known control-plane secret — is registered,
    so a future edit that empties the set is caught.

    :returns: None.
    """
    assert RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR in RUNNER_AUTH_SECRET_ENV_VARS
    assert RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR in RUNNER_AUTH_SECRET_ENV_VARS

    seeded = {name: f"secret-{name}" for name in RUNNER_AUTH_SECRET_ENV_VARS}
    seeded["KEEP_ME"] = "keep"

    result = strip_runner_auth_secrets(seeded)

    assert set(result) == {"KEEP_ME"}
    assert result["KEEP_ME"] == "keep"


def test_strip_runner_auth_secrets_removes_token_and_keeps_rest() -> None:
    """The binding token is removed while every other var passes through.

    Asserts the exact surviving mapping (not just the token's absence)
    so a regression that also dropped legitimate vars (PATH, creds)
    would fail too.

    :returns: None.
    """
    source = {
        "PATH": "/usr/bin",
        "ANTHROPIC_API_KEY": "sk-keep-me",
        RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "bug-binding-token-secret",
    }

    result = strip_runner_auth_secrets(source)

    assert result == {"PATH": "/usr/bin", "ANTHROPIC_API_KEY": "sk-keep-me"}
    assert "bug-binding-token-secret" not in result.values()


def test_strip_runner_auth_secrets_does_not_mutate_input() -> None:
    """Stripping returns a fresh dict and leaves the caller's env intact.

    The runner process itself must retain the token in its own
    ``os.environ`` (it reuses it for request auth); only the child's
    copy is filtered. A mutating implementation would strip the token
    from the live runner environment.

    :returns: None.
    """
    source = {RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "tok", "HOME": "/home/x"}

    result = strip_runner_auth_secrets(source)

    assert source == {RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR: "tok", "HOME": "/home/x"}
    assert result == {"HOME": "/home/x"}
    assert result is not source


def test_importing_identity_does_not_pull_in_fastapi() -> None:
    """``import omnigent.runner.identity`` stays free of the FastAPI stack.

    The helper is imported at every runner→child spawn boundary —
    including the sandbox launcher, which re-execs a fresh interpreter
    per spawn. The runner package ``__init__`` resolves
    ``create_runner_app`` lazily (PEP 562) precisely so this stdlib-only
    submodule import does not drag in ``runner.app`` and ~0.5s of
    FastAPI import on that hot path. If someone reinstates an eager
    ``from omnigent.runner.app import create_runner_app`` in the package
    ``__init__``, this fails.

    Runs in a fresh subprocess so an unrelated test in the same session
    can't pre-import FastAPI and mask the regression.

    :returns: None.
    """
    probe = (
        "import sys\n"
        "import omnigent.runner.identity\n"
        "assert 'fastapi' not in sys.modules, 'fastapi loaded via identity import'\n"
        "assert 'omnigent.runner.app' not in sys.modules, "
        "'runner.app loaded via identity import'\n"
    )
    # Hand the child the same import roots as this process so it resolves
    # ``omnigent`` to the code under test (worktree or installed package).
    child_env = {**os.environ, "PYTHONPATH": os.pathsep.join(p for p in sys.path if p)}

    result = subprocess.run(
        [sys.executable, "-c", probe],
        env=child_env,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, (
        f"identity import pulled in the FastAPI stack (lazy runner "
        f"package __init__ regressed). stderr:\n{result.stderr}"
    )


def test_touch_connect_marker_creates_the_stamped_file(tmp_path) -> None:
    """The runner records its first tunnel connect at the host-stamped path.

    The launching host arms a connect-deadline watchdog on this file; a
    missing touch turns every healthy launch into a false never-connected
    ERROR on the host.

    :param tmp_path: Pytest temp dir.
    :returns: None.
    """
    marker = tmp_path / "runner-abc.connected"

    touch_connect_marker({RUNNER_CONNECT_MARKER_ENV_VAR: str(marker)})

    assert marker.exists()
    # Reconnects re-touch the same file without erroring.
    touch_connect_marker({RUNNER_CONNECT_MARKER_ENV_VAR: str(marker)})
    assert marker.exists()


def test_touch_connect_marker_noop_without_env(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A CLI-local runner (no host, no marker env) touches nothing.

    :param tmp_path: Pytest temp dir.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.delenv(RUNNER_CONNECT_MARKER_ENV_VAR, raising=False)
    monkeypatch.chdir(tmp_path)

    touch_connect_marker()

    assert list(tmp_path.iterdir()) == []


def test_touch_connect_marker_survives_unwritable_path(tmp_path) -> None:
    """A failed touch is logged, never raised — the tunnel connect must win.

    :param tmp_path: Pytest temp dir.
    :returns: None.
    """
    marker = tmp_path / "missing-dir" / "runner.connected"

    touch_connect_marker({RUNNER_CONNECT_MARKER_ENV_VAR: str(marker)})

    assert not marker.exists()
