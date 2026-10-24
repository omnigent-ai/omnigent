"""Session-level span attributes resolved by the runner."""

from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest

from omnigent.runner import session_telemetry


@pytest.fixture(autouse=True)
def _capture_content(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", True)


def test_session_metadata_attributes_omit_missing_values_and_internal_labels() -> None:
    session = {
        "title": "Fix login",
        "labels": {"team": "web", "priority": 2, "omnigent.ui": "web"},
        "project_id": "proj_1",
        "git_branch": "",
        "agent_name": None,
    }

    attributes = session_telemetry.session_metadata_attributes(
        session, project_name="Web", git_branch="feature/login"
    )

    assert attributes == {
        "omnigent.session.title": "Fix login",
        "omnigent.session.labels.team": "web",
        "omnigent.session.labels.priority": "2",
        "omnigent.project.id": "proj_1",
        "omnigent.project.name": "Web",
        "omnigent.git.branch": "feature/login",
    }


def test_session_title_needs_content_capture(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("omnigent.runtime.telemetry._capture_content", False)

    attributes = session_telemetry.session_metadata_attributes(
        {"title": "Fix login for alice@example.com", "labels": {"team": "web"}}
    )

    assert attributes == {"omnigent.session.labels.team": "web"}


def test_local_session_attributes_omit_the_user(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OMNIGENT_USER_ID", "alice@example.com")
    monkeypatch.setattr(session_telemetry, "git_remote_url", lambda _: "https://git/acme/app")

    attributes = session_telemetry.local_session_attributes("/repo")

    assert attributes.pop("omnigent.host.name")
    assert attributes == {
        "omnigent.workspace": "/repo",
        "omnigent.git.remote": "https://git/acme/app",
    }


@pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")
def test_git_lookups_read_the_repo_and_tolerate_failures(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ignore the developer's url.<base>.insteadOf rewrites.
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    repo = tmp_path / "repo"
    (repo / "pkg").mkdir(parents=True)

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-q", "-b", "main")
    git("remote", "add", "origin", "git@github.com:acme/app.git")
    git("-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "--allow-empty", "-m", "x")
    workspace = str(repo / "pkg")

    # The SSH user and any https credentials are dropped from the remote.
    assert session_telemetry.git_remote_url(workspace) == "github.com:acme/app.git"
    git("remote", "set-url", "origin", "https://alice:s3cr3t-pw@github.com/acme/app.git")
    assert session_telemetry.git_remote_url(workspace) == "https://github.com/acme/app.git"
    assert session_telemetry.git_branch(workspace) == "main"
    git("checkout", "-q", "--detach")
    assert session_telemetry.git_branch(workspace) is None
    missing = str(tmp_path / "missing")
    assert session_telemetry.git_remote_url(missing) is None
    assert session_telemetry.git_branch(missing) is None


def _client(
    handler: Callable[[httpx.Request], httpx.Response | Awaitable[httpx.Response]],
) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://server")


@pytest.mark.asyncio
async def test_resolve_session_attributes_reads_server_metadata_each_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(session_telemetry, "git_branch", lambda _: "feature/login")
    session: dict[str, object] = {
        "title": "Untitled",
        "agent_name": "coder",
        "llm_model": "gpt-5.5",
    }
    bodies = {"/v1/sessions/conv_1": session, "/v1/projects/proj_1": {"name": "Web"}}
    local = {"omnigent.host.name": "dev"}
    requested: list[str] = []
    project_names: dict[str, str] = {}

    def serve(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        assert request.extensions["timeout"]["read"] == session_telemetry._SERVER_TIMEOUT_S
        return httpx.Response(200, json=bodies[request.url.path])

    async def resolve() -> dict[str, str]:
        return await session_telemetry.resolve_session_attributes(
            client,
            "conv_1",
            workspace="/repo",
            local=local,
            project_names=project_names,
            configured_model="gpt-5.5",
        )

    async with _client(serve) as client:
        first = await resolve()
        # The harness reports the model it resolved, and the user picks an effort.
        session.update(
            title="Fix login",
            labels={"team": "web"},
            project_id="proj_1",
            llm_model="gpt-5.5-2026-09",
            reasoning_effort="high",
        )
        second = await resolve()
        third = await resolve()

    assert first == {
        "omnigent.host.name": "dev",
        "omnigent.session.title": "Untitled",
        "omnigent.git.branch": "feature/login",
        "omnigent.agent.name": "coder",
        "omnigent.agent.model": "gpt-5.5",
        "omnigent.session.model": "gpt-5.5",
        "omnigent.session.model.source": "resolved_default",
    }
    assert second == {
        **first,
        "omnigent.session.title": "Fix login",
        "omnigent.session.labels.team": "web",
        "omnigent.project.id": "proj_1",
        "omnigent.project.name": "Web",
        "omnigent.session.model": "gpt-5.5-2026-09",
        "omnigent.session.model.source": "observed",
        "omnigent.session.reasoning_effort": "high",
    }
    # The session is re-read every call; the project name is cached by id.
    assert third == second
    assert requested.count("/v1/sessions/conv_1") == 3
    assert requested.count("/v1/projects/proj_1") == 1
    assert project_names == {"proj_1": "Web"}


@pytest.mark.parametrize(
    ("session", "configured_model", "expected"),
    [
        pytest.param({"llm_model": "opus[1m]"}, None, ("opus[1m]", "observed"), id="reported"),
        pytest.param(
            {"llm_model": "gpt-5.5", "model_override": "gpt-5.4-mini"},
            "gpt-5.5",
            ("gpt-5.4-mini", "requested"),
            id="override-unreported",
        ),
        pytest.param(
            {"llm_model": "gpt-5.4-mini", "model_override": "gpt-5.4-mini"},
            "gpt-5.5",
            ("gpt-5.4-mini", "observed"),
            id="override-reported",
        ),
        pytest.param({}, "gpt-5.5", ("gpt-5.5", "resolved_default"), id="configured-only"),
        pytest.param({}, None, None, id="unknown"),
    ],
)
def test_session_model_tells_reports_from_requests_and_defaults(
    session: dict[str, object],
    configured_model: str | None,
    expected: tuple[str, str] | None,
) -> None:
    assert session_telemetry.session_model(session, configured_model) == expected


@pytest.mark.asyncio
async def test_resolve_session_attributes_gives_up_when_the_server_stalls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A stalled read ends within the budget with the local attributes only."""
    monkeypatch.setattr(session_telemetry, "_RESOLVE_TIMEOUT_S", 0.05)

    async def stall(_request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(5)
        return httpx.Response(200, json={"title": "late"})

    async with _client(stall) as client:
        attributes = await asyncio.wait_for(
            session_telemetry.resolve_session_attributes(
                client, "conv_1", workspace=None, local={"omnigent.host.name": "dev"}
            ),
            timeout=2,
        )

    assert attributes == {"omnigent.host.name": "dev"}


@pytest.mark.asyncio
async def test_resolve_session_attributes_survives_server_failure() -> None:
    def fail(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("server down", request=request)

    async with _client(fail) as client:
        attributes = await session_telemetry.resolve_session_attributes(
            client, "conv_1", workspace=None, local={"omnigent.host.name": "dev"}
        )

    assert attributes == {"omnigent.host.name": "dev"}
