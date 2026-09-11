"""A PR created in a mixed read/comment + create shell call is attributed.

Session PR tracking observes completed shell calls
(``omnigent/runner/pr_observer.py``) and surfaces the session's pull request
in the web UI: the workspace rail's GitHub tab picker and the composer status
line's ``#<pr>`` link. Reported bug: when one shell call combines an excluded
PR read/comment with PR creation (``gh pr comment 42 ... && gh pr create
...``), the observer only sees the command string and the combined stdout, so
it cannot attribute the printed PR URLs to the read versus the creation. The
merely-commented PR gets associated with the session and becomes its default
pull request, while the created PR is never established as the session's
created PR.

These tests drive the real journey end to end — a live server + runner
executes the agent's shell command for real (a PATH-stubbed ``gh`` prints
gh's canonical per-subcommand output: the comment URL for ``pr comment``, the
new PR URL for ``pr create``; no GitHub access is needed) — and assert the
*correct* behavior: the created PR, and only the created PR, is the session's
pull request. The mixed shape fails on the affected build (the picker names
the commented PR and offers it as an association); the create-alone control
passes, isolating the failure to mixed-call attribution rather than the
tracking pipeline.
"""

from __future__ import annotations

import gzip
import io
import json
import shutil
import subprocess
import tarfile
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    configure_mock_llm,
    open_right_rail,
    set_fallback_mock_llm,
)

_READ_PR_URL = "https://github.com/example/project/pull/42"
_CREATED_PR_URL = "https://github.com/example/project/pull/43"
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# The per-fixture model gives each test an isolated mock-LLM queue.
_AGENT_YAML = """\
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to review and open a
  pull request you run a shell command that does it, then confirm.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""

# Stands in for the real gh CLI on PATH: prints gh's canonical success output
# per subcommand — ``pr comment`` ends with the comment's URL on the read PR,
# ``pr create`` prints the new PR's URL — exactly what real successful calls
# show. The observer only ever sees the command string and this output.
_GH_STUB = f"""\
#!/bin/sh
if [ "$1" = "pr" ] && [ "$2" = "comment" ]; then
  echo "{_READ_PR_URL}#issuecomment-2001"
fi
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  echo "{_CREATED_PR_URL}"
fi
exit 0
"""

# The reported failing shape — an excluded PR read/comment sharing the shell
# call with the creation — plus the same creation alone as the pipeline
# control. The leading PATH export only makes the stubbed gh resolvable; it
# adds no gh clause.
_PR_CREATE = "gh pr create --title 'Example' --body 'Example'"
_COMMANDS = {
    "control-pr-create-alone": 'export PATH="{stub}:$PATH"; cd {worktree} && ' + _PR_CREATE,
    "comment-read-then-pr-create": (
        'export PATH="{stub}:$PATH"; cd {worktree} && '
        "gh pr comment 42 --body 'Reviewed.' && " + _PR_CREATE
    ),
}


def _agent_bundle(name: str, model: str, cwd: str) -> bytes:
    """Gzip-tar the agent YAML for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=cwd)
    buf = io.BytesIO()
    with (
        gzip.GzipFile(fileobj=buf, mode="wb", mtime=0) as gz,
        tarfile.open(fileobj=gz, mode="w") as tar,
    ):
        data = yaml_text.encode()
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


@pytest.fixture
def pr_probe_runner_id(
    live_server: str,
    runner_id: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Recover the shared runner after an earlier crash test in the shard."""
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        yield runner_id
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned.kill()
                respawned.wait(timeout=5)


@pytest.fixture
def pr_probe_session(
    live_server: str,
    pr_probe_runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str, Path, Path]]:
    """An isolated runner-bound session whose workspace can run the commands.

    The workspace holds a ``stub-bin/gh`` for PATH and a ``worktree`` git
    repo so the gh clauses run from a repository checkout, offline.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-pr-mixed-read-create-"))
    stub = ws / "stub-bin"
    stub.mkdir()
    (stub / "gh").write_text(_GH_STUB)
    (stub / "gh").chmod(0o755)
    worktree = ws / "worktree"
    _git("init", "-q", "-b", "topic", str(worktree))
    _git(
        "-C",
        str(worktree),
        "-c",
        "user.email=e2e@example.com",
        "-c",
        "user.name=e2e",
        "commit",
        "--allow-empty",
        "-q",
        "-m",
        "init",
    )

    name = f"pr_mixed_probe_{uuid.uuid4().hex[:8]}"
    model = f"pr-mixed-probe-{uuid.uuid4().hex[:8]}"
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={
            "bundle": (
                "agent.tar.gz",
                _agent_bundle(name, model, str(ws)),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    try:
        httpx.patch(
            f"{live_server}/v1/sessions/{session_id}",
            json={"runner_id": pr_probe_runner_id},
            timeout=10.0,
        ).raise_for_status()
        yield (live_server, session_id, model, stub, worktree)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


@pytest.mark.parametrize("shape", list(_COMMANDS), ids=list(_COMMANDS))
def test_created_pr_is_attributed(
    page: Page,
    pr_probe_session: tuple[str, str, str, Path, Path],
    mock_llm_server_url: str,
    shape: str,
) -> None:
    """The created PR — and only it — becomes the session's pull request."""
    base_url, session_id, model, stub, worktree = pr_probe_session
    command = _COMMANDS[shape].format(stub=stub, worktree=worktree)
    # Configure both responses together because reconfiguring a queue resets it.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_review_and_create_pr",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            },
            {"text": "Opened the pull request."},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, "Done.")

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Leave a review comment on PR 42, then open a pull request.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT).last).to_contain_text(
        "Opened the pull request.", timeout=60_000
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)

    # A fresh load reads the tracked-PR state the way a returning user does.
    page.reload()
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()
    picker = rail.get_by_role("combobox", name="Session pull request")
    # Wait for the tracked-PR state to render before pinning the identity, so
    # a failure below means wrong attribution rather than a slow load.
    expect(picker).to_contain_text("example/project #", timeout=30_000)
    # The created PR must be the session's pull request; on the affected
    # build the merely-commented PR takes its place here.
    expect(picker).to_have_text("example/project #43", timeout=5_000)
    # The commented PR must not be associated with the session at all.
    picker.click()
    expect(page.get_by_role("option", name="example/project #43")).to_be_visible(timeout=5_000)
    expect(page.get_by_role("option", name="example/project #42")).to_have_count(0)
    page.keyboard.press("Escape")
    # The composer status line links the created PR.
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#43")
