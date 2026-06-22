"""Tests for runner/filesystem security hardening.

Covers:
- session workspace directories are created with 0o700 permissions
- symlink escape blocked by ``_assert_within_cwd`` in os_env
- per-session workspace isolation when ``per_session_workspace=True``
- runner auth middleware rejects unauthenticated requests
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.os_env import _assert_within_cwd, _handle_helper_request
from omnigent.inner.sandbox import SandboxPolicy
from omnigent.runner.resource_registry import SessionResourceRegistry

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _inactive_sandbox() -> SandboxPolicy:
    """Return a sandbox policy with ``active=False`` (type=none).

    :returns: An inactive :class:`SandboxPolicy`.
    """
    return SandboxPolicy(
        backend_type="none",
        active=False,
        read_roots=None,
        write_roots=[],
        write_files=[],
        allow_network=True,
    )


def _agent_spec_sandbox_none(cwd: Path) -> SimpleNamespace:
    """Build a fake agent spec with ``sandbox.type="none"``.

    :param cwd: Working directory for the OS environment.
    :returns: Object exposing an ``os_env`` attribute.
    """
    cwd.mkdir(parents=True, exist_ok=True)
    return SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(cwd),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


def _agent_spec_default_cwd() -> SimpleNamespace:
    """Build a fake agent spec whose ``cwd`` is a placeholder.

    The registry will substitute its own ``default_cwd`` for
    ``"."``, which lets per-session-workspace logic engage.

    :returns: Object exposing an ``os_env`` attribute with
        ``cwd="."``.
    """
    return SimpleNamespace(
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


# ---------------------------------------------------------------------------
# workspace directory permissions
# ---------------------------------------------------------------------------


def test_session_workspace_created_with_0700(tmp_path: Path) -> None:
    """Workspace dirs created by the registry use mode 0700.

    :param tmp_path: Pytest-provided temporary directory.
    """
    registry = SessionResourceRegistry()
    # Patch the workspace root so it lands in tmp_path.
    ws_root = tmp_path / "ws"
    os.environ["OMNIGENT_RUNNER_OS_ENV_ROOT"] = str(ws_root)
    try:
        registry.resolve_environment(
            "sess_alice",
            "default",
            agent_spec=_agent_spec_sandbox_none(tmp_path / "spec_cwd"),
        )
        # The default_cwd fell through to _session_workspace because
        # runner_workspace is None.
        workspace_dir = ws_root / "sess_alice" / "workspace"
        assert workspace_dir.exists()
        mode = workspace_dir.stat().st_mode & 0o777
        assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"
    finally:
        os.environ.pop("OMNIGENT_RUNNER_OS_ENV_ROOT", None)


# ---------------------------------------------------------------------------
# symlink-resolving _assert_within_cwd
# ---------------------------------------------------------------------------


def test_assert_within_cwd_blocks_symlink_escape(tmp_path: Path) -> None:
    """A symlink pointing outside cwd is rejected.

    :param tmp_path: Pytest-provided temporary directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "secrets"
    outside.mkdir()
    secret_file = outside / "password.txt"
    secret_file.write_text("s3cr3t")

    link = workspace / "escape"
    link.symlink_to(outside / "password.txt")

    resolved = link.resolve()
    with pytest.raises(PermissionError, match="outside the environment root"):
        _assert_within_cwd(workspace, resolved)


def test_assert_within_cwd_allows_internal_symlink(tmp_path: Path) -> None:
    """A symlink that resolves within cwd is allowed.

    :param tmp_path: Pytest-provided temporary directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    real_file = workspace / "real.txt"
    real_file.write_text("ok")
    link = workspace / "alias.txt"
    link.symlink_to(real_file)

    resolved = link.resolve()
    # Should not raise.
    _assert_within_cwd(workspace, resolved)


def test_assert_within_cwd_blocks_dotdot_symlink(tmp_path: Path) -> None:
    """A symlink chain using ``..`` to escape is rejected.

    :param tmp_path: Pytest-provided temporary directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "sub").mkdir()
    link = workspace / "sub" / "escape"
    link.symlink_to("../../secrets")

    outside = tmp_path / "secrets"
    outside.mkdir()

    resolved = link.resolve()
    with pytest.raises(PermissionError, match="outside the environment root"):
        _assert_within_cwd(workspace, resolved)


def test_helper_read_blocks_symlink_escape(tmp_path: Path) -> None:
    """The read op in ``_handle_helper_request`` rejects symlink escapes.

    :param tmp_path: Pytest-provided temporary directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret data")
    link = workspace / "escape.txt"
    link.symlink_to(outside)

    result = _handle_helper_request(
        request={"op": "read", "path": "escape.txt"},
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=_inactive_sandbox(),
    )
    assert "error" in result
    assert "outside the environment root" in result["error"]


def test_helper_write_blocks_symlink_escape(tmp_path: Path) -> None:
    """The write op rejects symlink escapes.

    :param tmp_path: Pytest-provided temporary directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "target.txt"
    outside.write_text("original")
    link = workspace / "escape.txt"
    link.symlink_to(outside)

    result = _handle_helper_request(
        request={"op": "write", "path": "escape.txt", "content": "hacked"},
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=_inactive_sandbox(),
    )
    assert "error" in result
    assert "outside the environment root" in result["error"]
    # Verify the outside file was NOT modified.
    assert outside.read_text() == "original"


def test_helper_edit_blocks_symlink_escape(tmp_path: Path) -> None:
    """The edit op rejects symlink escapes.

    :param tmp_path: Pytest-provided temporary directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "target.txt"
    outside.write_text("original content")
    link = workspace / "escape.txt"
    link.symlink_to(outside)

    result = _handle_helper_request(
        request={
            "op": "edit",
            "path": "escape.txt",
            "oldText": "original",
            "newText": "hacked",
        },
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=_inactive_sandbox(),
    )
    assert "error" in result
    assert "outside the environment root" in result["error"]
    assert outside.read_text() == "original content"


def test_helper_read_allows_normal_relative_path(tmp_path: Path) -> None:
    """Normal relative paths within the workspace are allowed.

    :param tmp_path: Pytest-provided temporary directory.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "hello.txt").write_text("hello world")

    result = _handle_helper_request(
        request={"op": "read", "path": "hello.txt"},
        cwd=workspace,
        shell_path="/bin/sh",
        sandbox=_inactive_sandbox(),
    )
    assert "error" not in result
    assert result["content"] == "hello world"


# ---------------------------------------------------------------------------
# per-session workspace isolation
# ---------------------------------------------------------------------------


def test_per_session_workspace_isolation(tmp_path: Path) -> None:
    """Sessions get isolated subdirectories when per_session_workspace=True.

    :param tmp_path: Pytest-provided temporary directory.
    """
    shared_root = tmp_path / "shared"
    shared_root.mkdir()

    registry = SessionResourceRegistry(
        runner_workspace=shared_root,
        per_session_workspace=True,
    )
    env_alice = registry.resolve_environment(
        "sess_alice",
        "default",
        agent_spec=_agent_spec_default_cwd(),
    )
    env_bob = registry.resolve_environment(
        "sess_bob",
        "default",
        agent_spec=_agent_spec_default_cwd(),
    )
    assert str(env_alice.cwd) != str(env_bob.cwd)
    assert "sess_alice" in str(env_alice.cwd)
    assert "sess_bob" in str(env_bob.cwd)
    # Both should be under the shared root.
    assert str(env_alice.cwd).startswith(str(shared_root))
    assert str(env_bob.cwd).startswith(str(shared_root))


def test_shared_workspace_without_isolation(tmp_path: Path) -> None:
    """Without per_session_workspace, sessions share the runner workspace.

    :param tmp_path: Pytest-provided temporary directory.
    """
    shared_root = tmp_path / "shared"
    shared_root.mkdir()

    registry = SessionResourceRegistry(
        runner_workspace=shared_root,
        per_session_workspace=False,
    )
    env_alice = registry.resolve_environment(
        "sess_alice",
        "default",
        agent_spec=_agent_spec_default_cwd(),
    )
    env_bob = registry.resolve_environment(
        "sess_bob",
        "default",
        agent_spec=_agent_spec_default_cwd(),
    )
    # Both sessions should use the shared root.
    assert str(env_alice.cwd) == str(shared_root)
    assert str(env_bob.cwd) == str(shared_root)


def test_per_session_workspace_has_0700_permissions(tmp_path: Path) -> None:
    """Per-session subdirectories are created with mode 0700.

    :param tmp_path: Pytest-provided temporary directory.
    """
    shared_root = tmp_path / "shared"
    shared_root.mkdir()

    registry = SessionResourceRegistry(
        runner_workspace=shared_root,
        per_session_workspace=True,
    )
    registry.resolve_environment(
        "sess_alice",
        "default",
        agent_spec=_agent_spec_default_cwd(),
    )
    session_dir = shared_root / "sess_alice"
    assert session_dir.exists()
    mode = session_dir.stat().st_mode & 0o777
    assert mode == 0o700, f"Expected 0o700, got {oct(mode)}"


def test_compute_default_env_root_per_session(tmp_path: Path) -> None:
    """``compute_default_env_root`` returns per-session paths when enabled.

    :param tmp_path: Pytest-provided temporary directory.
    """
    shared_root = tmp_path / "shared"
    shared_root.mkdir()

    registry = SessionResourceRegistry(
        runner_workspace=shared_root,
        per_session_workspace=True,
    )
    root_alice = registry.compute_default_env_root("sess_alice", agent_spec=None)
    root_bob = registry.compute_default_env_root("sess_bob", agent_spec=None)
    assert root_alice != root_bob
    assert "sess_alice" in root_alice
    assert "sess_bob" in root_bob


def test_create_runner_app_propagates_per_session_workspace_false(
    tmp_path: Path,
) -> None:
    """``per_session_workspace=False`` lands sessions at the workspace root.

    :param tmp_path: Pytest-provided temporary directory.
    """
    from omnigent.runner.app import create_runner_app

    workspace = tmp_path / "project"
    workspace.mkdir()

    from tests.unit.runner.helpers import NullServerClient

    app = create_runner_app(
        runner_workspace=workspace,
        per_session_workspace=False,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    registry: SessionResourceRegistry = app.state.session_resource_registry
    root = registry.compute_default_env_root("sess_alice", agent_spec=None)
    # Exact equality — a per-session subdir would also startswith().
    assert root == str(workspace.resolve())


def test_create_runner_app_defaults_to_per_session_workspace_true(
    tmp_path: Path,
) -> None:
    """Default keeps per-session workspace isolation for shared-host runners.

    :param tmp_path: Pytest-provided temporary directory.
    """
    from omnigent.runner.app import create_runner_app

    workspace = tmp_path / "project"
    workspace.mkdir()

    from tests.unit.runner.helpers import NullServerClient

    app = create_runner_app(
        runner_workspace=workspace,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    registry: SessionResourceRegistry = app.state.session_resource_registry
    root_alice = registry.compute_default_env_root("sess_alice", agent_spec=None)
    root_bob = registry.compute_default_env_root("sess_bob", agent_spec=None)
    assert root_alice != root_bob


# ---------------------------------------------------------------------------
# runner auth middleware
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_runner_auth_rejects_no_token() -> None:
    """Requests without an auth header are rejected with 401."""
    from omnigent.runner.app import create_runner_app
    from tests.unit.runner.helpers import NullServerClient

    app = create_runner_app(
        auth_token="test-secret-token",
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://runner",
    ) as client:
        resp = await client.get("/v1/sessions")
        assert resp.status_code == 401


@pytest.mark.anyio
async def test_runner_auth_rejects_wrong_token() -> None:
    """Requests with the wrong token are rejected with 401."""
    from omnigent.runner.app import create_runner_app
    from tests.unit.runner.helpers import NullServerClient

    app = create_runner_app(
        auth_token="correct-token",
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://runner",
    ) as client:
        resp = await client.get(
            "/v1/sessions",
            headers={"Authorization": "Bearer wrong-token"},
        )
        assert resp.status_code == 401


@pytest.mark.anyio
async def test_runner_auth_accepts_correct_token() -> None:
    """Requests with the correct token pass through to the route."""
    from omnigent.runner.app import create_runner_app
    from tests.unit.runner.helpers import NullServerClient

    app = create_runner_app(
        auth_token="correct-token",
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://runner",
    ) as client:
        resp = await client.get(
            "/v1/sessions/nonexistent",
            headers={"Authorization": "Bearer correct-token"},
        )
        # Should pass auth (not 401) — actual status depends on
        # route logic (404 for missing session is fine).
        assert resp.status_code != 401


@pytest.mark.anyio
async def test_runner_auth_health_exempt() -> None:
    """GET /health succeeds without any auth token."""
    from omnigent.runner.app import create_runner_app
    from tests.unit.runner.helpers import NullServerClient

    app = create_runner_app(
        auth_token="some-secret",
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://runner",
    ) as client:
        resp = await client.get("/health")
        assert resp.status_code == 200


@pytest.mark.anyio
async def test_runner_no_auth_when_token_is_none() -> None:
    """When auth_token is None, no middleware is installed."""
    from omnigent.runner.app import create_runner_app
    from tests.unit.runner.helpers import NullServerClient

    app = create_runner_app(
        auth_token=None,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )

    from httpx import ASGITransport, AsyncClient

    async with AsyncClient(
        transport=ASGITransport(app=app),
        base_url="http://runner",
    ) as client:
        # /health should work without auth.
        resp = await client.get("/health")
        assert resp.status_code == 200
