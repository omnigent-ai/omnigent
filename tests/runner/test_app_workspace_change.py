"""Tests for the runner's ``workspace_change`` session event.

The handler repoints the session's working directory onto a browsed
folder. The server EDIT-gates relative wire paths on the promise that
they stay inside the environment root, so the runner must enforce that
promise exactly like the browse flow does: traversal and resolved
escapes rejected, with the unconfined reach widening reserved for the
(owner-gated) absolute form. The target must also be an existing
directory — persisting a file or a nonexistent path would leave every
later turn and new shell failing to ``cd``.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.runner import create_runner_app
from omnigent.runner.resource_registry import SessionResourceRegistry
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient


def _app_rooted_at(root: Path) -> FastAPI:
    """A runner app whose sessions run unconfined with *root* as env root."""
    spec = AgentSpec(
        spec_version=1,
        name="workdir-probe",
        executor=ExecutorSpec(type="omnigent", config={}),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(root),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    return create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )


async def _post_workspace_change(app: FastAPI, workspace: str) -> httpx.Response:
    """Create a session on *app* and POST it a ``workspace_change`` event."""
    session_id = uuid.uuid4().hex
    async with _runner_client(app) as client:
        create = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": uuid.uuid4().hex},
        )
        assert create.status_code == 201, create.text
        return await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "workspace_change", "workspace": workspace},
        )


async def test_relative_traversal_is_rejected_even_when_unconfined(tmp_path: Path) -> None:
    """A relative ``..`` path must not escape the env root.

    The server EDIT-gates relative paths precisely because they are
    supposed to stay inside the env root; on an unconfined runner a
    traversal that reaches ``resolve_browse_target`` is admitted anywhere
    on the filesystem, letting a non-owner editor repoint the session's
    cwd to an arbitrary directory.
    """
    resp = await _post_workspace_change(_app_rooted_at(tmp_path), "../../../../../../etc")
    assert resp.status_code == 400, resp.text


async def test_relative_symlink_escape_is_rejected(tmp_path: Path) -> None:
    """A relative path resolving outside the root via a symlink is refused.

    String-level ``..`` rejection is not enough — containment must be
    checked on the RESOLVED path, the same as the browse flow.
    """
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)

    resp = await _post_workspace_change(_app_rooted_at(root), "link")
    assert resp.status_code == 400, resp.text


async def test_leading_whitespace_does_not_make_a_path_absolute(tmp_path: Path) -> None:
    """``" /etc"`` is EDIT-gated as relative at the server, so the runner
    must not strip it into an absolute path and admit it."""
    resp = await _post_workspace_change(_app_rooted_at(tmp_path), " /etc")
    assert resp.status_code == 400, resp.text


async def test_nonexistent_directory_is_rejected(tmp_path: Path) -> None:
    """A path that does not exist must not become the session's workdir."""
    resp = await _post_workspace_change(_app_rooted_at(tmp_path), "missing")
    assert resp.status_code == 400, resp.text


async def test_file_target_is_rejected(tmp_path: Path) -> None:
    """A regular file must not become the session's workdir."""
    (tmp_path / "notes.txt").write_text("not a directory\n")
    resp = await _post_workspace_change(_app_rooted_at(tmp_path), "notes.txt")
    assert resp.status_code == 400, resp.text


async def test_subdirectory_resolves_to_its_absolute_path(tmp_path: Path) -> None:
    """The happy path: a relative subfolder resolves under the env root."""
    (tmp_path / "sub").mkdir()
    resp = await _post_workspace_change(_app_rooted_at(tmp_path), "sub")
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace"] == str((tmp_path / "sub").resolve())


async def test_empty_location_resolves_to_the_env_root(tmp_path: Path) -> None:
    """``""`` is the wire form for the env root itself."""
    resp = await _post_workspace_change(_app_rooted_at(tmp_path), "")
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace"] == str(tmp_path.resolve())


async def test_new_terminal_opens_in_the_changed_workspace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A shell created after ``workspace_change`` roots at the new workdir.

    The feature's promise is that new shells open in the browsed folder, so
    terminal creation must read the live session workspace — rooting at the
    static default env root would leave every new shell in the old directory.
    """
    (tmp_path / "sub").mkdir()
    app = _app_rooted_at(tmp_path)

    captured: dict[str, Any] = {}

    async def _capture_launch(self: SessionResourceRegistry, **kwargs: Any) -> Any:
        captured.update(kwargs)
        raise RuntimeError("probe: launch captured")

    monkeypatch.setattr(SessionResourceRegistry, "launch_auxiliary_terminal", _capture_launch)

    session_id = uuid.uuid4().hex
    async with _runner_client(app) as client:
        create = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": uuid.uuid4().hex},
        )
        assert create.status_code == 201, create.text
        change = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "workspace_change", "workspace": "sub"},
        )
        assert change.status_code == 200, change.text
        resp = await client.post(
            f"/v1/sessions/{session_id}/resources/terminals",
            json={"terminal": "probe-shell", "session_key": "aux1"},
        )
        # The capturing fake aborts the launch; the assertion below is about
        # what the route asked the registry to launch, not the launch result.
        assert resp.status_code == 500, resp.text

    env_spec = captured["spec"]
    assert env_spec.os_env.cwd == str((tmp_path / "sub").resolve()), (
        f"New terminal must root at the changed workspace, got {env_spec.os_env.cwd!r}"
    )
    assert captured["cwd_override"] is None


async def test_absolute_target_outside_the_agent_boundary_is_rejected(tmp_path: Path) -> None:
    """A pinned agent cwd bounds online changes exactly like create/offline.

    Browse reach (here: unconfined, so the whole filesystem) is a viewing
    grant; the working directory must stay inside the agent's pinned
    ``os_env.cwd``. The refusal must also leave the live session workdir
    untouched, so the previously persisted workspace keeps applying.
    """
    root = tmp_path / "boundary"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    app = _app_rooted_at(root)
    session_id = uuid.uuid4().hex
    async with _runner_client(app) as client:
        create = await client.post(
            "/v1/sessions",
            json={"session_id": session_id, "agent_id": uuid.uuid4().hex},
        )
        assert create.status_code == 201, create.text
        refused = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "workspace_change", "workspace": str(outside)},
        )
        assert refused.status_code == 400, refused.text
        assert "boundary" in refused.json()["detail"]
        # The refused change left no live override: an empty-location reset
        # still lands on the environment root, not the outside target.
        reset = await client.post(
            f"/v1/sessions/{session_id}/events",
            json={"type": "workspace_change", "workspace": ""},
        )
        assert reset.status_code == 200, reset.text
        assert reset.json()["workspace"] == str(root.resolve())


async def test_absolute_target_inside_the_agent_boundary_is_accepted(tmp_path: Path) -> None:
    """An absolute target under the pinned agent cwd still resolves."""
    root = tmp_path / "boundary"
    sub = root / "sub"
    sub.mkdir(parents=True)
    resp = await _post_workspace_change(_app_rooted_at(root), str(sub))
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace"] == str(sub.resolve())


async def test_relative_agent_cwd_places_no_workdir_boundary(tmp_path: Path) -> None:
    """Agents without a pinned cwd keep the unconfined browse-anywhere reach."""
    target = tmp_path / "anywhere"
    target.mkdir()
    spec = AgentSpec(
        spec_version=1,
        name="workdir-probe-unbounded",
        executor=ExecutorSpec(type="omnigent", config={}),
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=".",
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )

    async def _resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
        del agent_id, session_id
        return spec

    pm = _FakeProcessManager(_ScriptedHarnessClient([]))
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_resolver,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    resp = await _post_workspace_change(app, str(target))
    assert resp.status_code == 200, resp.text
    assert resp.json()["workspace"] == str(target.resolve())
