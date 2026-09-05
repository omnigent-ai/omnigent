"""E2E: a git-status failure surfaces its reason in the diff view, never a bare 502.

When ``git status`` cannot run in a session's workspace (dubious ownership /
corrupt repo), the runner's per-file diff endpoint
(``/resources/environments/{env}/diff/{path}``) answers
``500 {"error": {"code": "git_status_failed", "message": "<git reason>"}}``.
The server proxy must mirror that structured error to the client: the web hook
``useFileDiff.fetchFileDiff`` reads ``body.error?.message`` and falls back to
the bare status line (``"502 Bad Gateway"``) when the shape is lost, which is
exactly the regression this guards -- a proxy that collapses the runner error
into ``{"detail": ...}`` leaves the diff view showing
**"Failed to load: 502 Bad Gateway"** instead of the git reason.

This drives the REAL journey against a real git-repo workspace and the real
server diff endpoint (no request interception): the changed file is listed
while git is healthy, git is then broken on disk the way a broken deployment
leaves it, and the file's diff view is opened.
"""

from __future__ import annotations

import gzip
import io
import json
import re
import subprocess
import tarfile
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, open_right_rail

# A caller-process agent rooted at a real git repo, so the runner builds a
# GitFilesystemRegistry (the diff endpoint then shells out to ``git``). Model is
# a plain (non-databricks-) name so no provider auth is needed; no turn is run.
_AGENT_YAML_TEMPLATE = (
    "name: hello_world\n"
    "prompt: You are a friendly assistant.\n\n"
    "executor:\n"
    "  model: gpt-4o-mini\n"
    "  harness: openai-agents\n\n"
    "os_env:\n"
    "  type: caller_process\n"
    "  cwd: {cwd}\n"
    "  sandbox:\n"
    "    type: none\n"
)


def _git(cmd: list[str], cwd: Path) -> None:
    subprocess.run(["git", *cmd], cwd=str(cwd), check=True, capture_output=True)


def _build_bundle(agent_yaml: str) -> bytes:
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = agent_yaml.encode()
        info = tarfile.TarInfo(name="hello_world.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def git_workspace_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path]]:
    """Create a runner-bound session rooted at a real git repo with one changed file.

    Yields ``(base_url, session_id, repo_path)``. The repo has ``hello.py``
    committed and then modified, so it shows up in the session's changed-files
    list while git is healthy.
    """
    _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    repo = tmp_path_factory.mktemp("diff_view_repo")
    _git(["init"], repo)
    _git(["config", "user.email", "t@t"], repo)
    _git(["config", "user.name", "t"], repo)
    (repo / "hello.py").write_text("print('one')\n")
    _git(["add", "."], repo)
    _git(["commit", "-m", "init"], repo)
    # Modify the committed file so it appears as "modified" in the changes list.
    (repo / "hello.py").write_text("print('one')\nprint('two')\n")

    bundle = _build_bundle(_AGENT_YAML_TEMPLATE.format(cwd=repo))
    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({"workspace": str(repo)})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = create.json()["session_id"]
    httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    ).raise_for_status()

    try:
        yield (live_server, session_id, repo)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def test_diff_view_surfaces_git_reason_not_bare_502(
    page: Page,
    git_workspace_session: tuple[str, str, Path],
) -> None:
    """A git-status failure shows its reason in the diff view, never a bare 502."""
    base_url, session_id, repo = git_workspace_session

    page.goto(f"{base_url}/c/{session_id}")
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    # Healthy git: the changed file lists, so the diff (Δ) affordance is enabled.
    rail.get_by_role("tab", name=re.compile("^Changes")).click()
    file_btn = rail.get_by_role("button", name=re.compile(r"hello\.py")).filter(
        has_text="hello.py"
    )
    expect(file_btn.first).to_be_visible(timeout=30_000)

    # Break git the way a dubious-ownership / corrupt-repo deployment does:
    # ``git status`` now exits 128, which the runner reports as
    # git_status_failed (500). The already-loaded changed-files list stays
    # cached, so the diff affordance remains available.
    (repo / ".git" / "config").write_text("this is not valid git config [[[\n")

    # Open the file and switch to its diff view -- this fires the real
    # /diff request against the now-broken git.
    file_btn.first.click()
    file_viewer = rail.get_by_test_id("file-viewer")
    expect(file_viewer).to_be_visible(timeout=30_000)
    show_diff = file_viewer.get_by_role("button", name="Show diff")
    expect(show_diff).to_be_visible(timeout=30_000)
    show_diff.click()

    # The diff view settles on an error (after the client's runner-boot retries).
    error_line = file_viewer.get_by_text(re.compile(r"^Failed to load:"))
    expect(error_line).to_be_visible(timeout=45_000)

    # The panel must surface the actual git-status reason from the runner,
    # not the bare "502 Bad Gateway" a shape-discarding proxy produces.
    expect(error_line).to_contain_text("git status")
    expect(error_line).not_to_contain_text("502")
