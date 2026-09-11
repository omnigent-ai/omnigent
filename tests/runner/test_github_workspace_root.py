"""The GitHub resource reads git state from the session's stored workspace.

A session bound to a workspace on a shared runner (``metadata.workspace``)
keeps its git checkout somewhere the runner-wide default root knows nothing
about. The ``/resources/github`` routes must resolve that stored workspace —
the same per-session value the native terminal launches honor — or the branch
they report describes the wrong directory (the composer's branch chip then
shows "Not a git repository" for a session sitting on a real branch).
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.runner import create_runner_app
from omnigent.runner import github_resource as github
from tests.runner.helpers import NullServerClient

_BRANCH = "feature/session-workspace"


def _git_repo_on_branch(path: Path, branch: str) -> Path:
    """Create a git repo at *path* with one commit, checked out on *branch*."""
    path.mkdir()
    run = lambda *args: subprocess.run(  # noqa: E731 — local shorthand
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    )
    run("init")
    run("config", "user.email", "runner-test@example.com")
    run("config", "user.name", "Runner Test")
    (path / "README.md").write_text("github workspace root\n")
    run("add", "README.md")
    run("commit", "-m", "initial commit")
    run("checkout", "-b", branch)
    return path


class _WorkspaceServerClient(NullServerClient):
    """Server stub whose session snapshot carries a stored workspace."""

    def __init__(self, workspace: str) -> None:
        self._workspace = workspace

    class _SessionResponse:
        status_code = 200

        def __init__(self, workspace: str) -> None:
            self._workspace = workspace

        def json(self) -> dict[str, Any]:
            return {"workspace": self._workspace, "agent_id": "agent_test", "created_at": 0}

        def raise_for_status(self) -> None:
            """No-op: stub always succeeds."""

    async def get(self, url: str, **kwargs: Any) -> Any:
        if re.fullmatch(r"/v1/sessions/[^/]+", url):
            return self._SessionResponse(self._workspace)
        return await super().get(url, **kwargs)


@pytest.fixture(autouse=True)
def _isolated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Keep the PR registry out of the developer's real data dir, and skip the
    # gh enhancement layer so no network/auth probes run.
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setattr(github.shutil, "which", lambda _: None)


async def _fetch_github_info(app: Any, session_id: str) -> dict[str, Any]:
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://runner"
    ) as client:
        response = await client.get(f"/v1/sessions/{session_id}/resources/github")
    assert response.status_code == 200, response.text
    return response.json()


async def test_github_info_reads_the_sessions_stored_workspace(tmp_path: Path) -> None:
    # The runner-wide root is NOT a git repo; only the session's stored
    # workspace is. Reporting available=False here means the wrong root
    # was inspected.
    repo = _git_repo_on_branch(tmp_path / "session-checkout", _BRANCH)
    runner_root = tmp_path / "runner-root"
    runner_root.mkdir()
    app = create_runner_app(
        runner_workspace=runner_root,
        server_client=_WorkspaceServerClient(str(repo)),  # type: ignore[arg-type]
    )
    info = await _fetch_github_info(app, "session-with-workspace")
    assert info["available"] is True, info
    assert info["branch"] == _BRANCH


async def test_github_info_falls_back_to_the_runner_workspace(tmp_path: Path) -> None:
    # No stored workspace on the session (empty snapshot) — the pre-existing
    # runner-workspace resolution must keep working.
    runner_repo = _git_repo_on_branch(tmp_path / "runner-checkout", "runner-default")
    app = create_runner_app(
        runner_workspace=runner_repo,
        per_session_workspace=False,
        server_client=NullServerClient(),  # type: ignore[arg-type]
    )
    info = await _fetch_github_info(app, "session-without-workspace")
    assert info["available"] is True, info
    assert info["branch"] == "runner-default"
