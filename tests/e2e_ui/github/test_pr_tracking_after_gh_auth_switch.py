"""A PR created after a same-call ``gh auth switch`` is still tracked.

Session PR tracking observes completed shell calls on the runner
(``omnigent/runner/pr_observer.py``) and surfaces created PRs in the web UI:
the composer status line's ``#<pr>`` link and the workspace rail's GitHub
tab. Reported bug: when the successful ``gh pr create`` call also contains
an unrelated ``gh auth switch`` (with or without an intervening ``git
push``), the observer's mixed-operation guard rejects the whole call and
the created PR is never associated with the session.

These tests drive the real journey end to end — a live server + runner
executes the agent's shell command for real (a PATH-stubbed ``gh`` prints
gh's canonical PR-create success output, so no GitHub access is needed; the
observer only ever sees the command string and its output) — and assert the
*correct* behavior: the created PR appears in the UI. The two
``auth-switch`` shapes fail on the affected build; the control shape
passes, isolating the failure to the mixed-operation guard rather than the
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

_PR_URL = "https://github.com/example/project/pull/42"
_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# The per-fixture model gives each test an isolated mock-LLM queue.
_AGENT_YAML = """\
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to open a pull request
  you run a shell command that creates it, then confirm.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""

# Stands in for the real gh CLI on PATH: prints gh's canonical PR-create
# success output (the standalone PR URL) and succeeds for setup subcommands
# such as ``auth switch``, exactly what a real successful call shows.
_GH_STUB = f"""\
#!/bin/sh
if [ "$1" = "pr" ] && [ "$2" = "create" ]; then
  echo "{_PR_URL}"
fi
exit 0
"""

# The reported failing command shapes, plus the same command without the
# unrelated ``gh auth switch`` as the pipeline control. The leading PATH
# export only makes the stubbed gh resolvable; it adds no gh clause.
_PR_CREATE = "gh pr create --title 'Example' --body 'Example'"
_COMMANDS = {
    "control-pr-create-alone": 'export PATH="{stub}:$PATH"; cd {worktree} && ' + _PR_CREATE,
    "auth-switch-then-pr-create": (
        'export PATH="{stub}:$PATH"; gh auth switch --user example-user 2>/dev/null; '
        "cd {worktree} && " + _PR_CREATE
    ),
    "auth-switch-push-then-pr-create": (
        'export PATH="{stub}:$PATH"; gh auth switch --user example-user 2>/dev/null; '
        "cd {worktree} && git push -u origin topic && " + _PR_CREATE
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
    repo on branch ``topic`` with a local bare ``origin``, so the push
    shape runs offline.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-pr-auth-switch-"))
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
    _git("init", "-q", "--bare", str(ws / "origin.git"))
    _git("-C", str(worktree), "remote", "add", "origin", str(ws / "origin.git"))

    name = f"pr_track_probe_{uuid.uuid4().hex[:8]}"
    model = f"pr-track-probe-{uuid.uuid4().hex[:8]}"
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
def test_created_pr_is_tracked(
    page: Page,
    pr_probe_session: tuple[str, str, str, Path, Path],
    mock_llm_server_url: str,
    shape: str,
) -> None:
    """A successful ``gh pr create`` associates its PR with the session."""
    base_url, session_id, model, stub, worktree = pr_probe_session
    command = _COMMANDS[shape].format(stub=stub, worktree=worktree)
    # Configure both responses together because reconfiguring a queue resets it.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_create_pr",
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
    composer.fill("Open a pull request for this change.")
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
    # The created PR must be associated with the session: the GitHub tab's
    # session-PR picker names it, and the composer status line links it.
    picker = rail.get_by_role("combobox", name="Session pull request")
    expect(picker).to_have_text("example/project #42", timeout=30_000)
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#42")
