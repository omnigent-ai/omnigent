"""The Pull Requests panel shows the PR a publishing command opened on a host no remote names.

Some checkouts push through a git remote helper (``<transport>::<address>``), so no
remote URL names the forge's host. Their gh signs in to the GitHub Enterprise host
only when a command names it, so ``gh auth status`` lists only github.com. A
publishing command pushes the branch and prints the PR it opened, as ``gh pr create``
does. gh's lookups by branch and by commit resolve against github.com and find
nothing, so the panel can only learn the PR from the URL the command printed.

The test owns its server and runner. git, git's ``ext::`` remote helper, the runner's
shell and the SPA run for real. A PATH-first ``gh`` stands in for the forge, and the
publishing commands are test-owned scripts. The agent publishes from the chat, and
the PR must appear in the panel without the user linking it.
"""

from __future__ import annotations

import json
import re
import subprocess
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.server_runner import server_runner
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.github._gh_shim import (
    create_session,
    make_checkout,
    open_pull_requests_tab,
    stack_env,
    write_gh_shim,
)

REPO = "acme/widgets"
PR_URL = f"https://ghe.example.com/{REPO}/pull/7"
PR_TITLE = "Publish the widget catalog"
BRANCH = "topic"
REPLY = "Published the branch for review."
ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'

# A git subcommand that pushes through the helper remote, then reports the PR it opened.
GIT_PUBLISH = f"""#!/bin/sh
set -e
echo git-publish >> "$(dirname "$0")/commands.log"
git push forge "HEAD:refs/heads/{BRANCH}"
echo
echo "Draft PR created: {PR_URL}"
"""

# A forge CLI that opens the PR for an already pushed branch and prints only its URL.
FORGE_PR = f"""#!/bin/sh
echo forge-pr >> "$(dirname "$0")/commands.log"
echo "{PR_URL}"
"""

# The agent's command, and whether the branch was pushed before the turn.
COMMANDS = {
    "git-subcommand": ("git publish", False),
    "pr-command": (f"forge-pr create --head {BRANCH}", True),
}


@dataclass(frozen=True)
class PublishSession:
    base_url: str
    session_id: str
    model: str
    workspace: Path
    forge: Path
    head: str
    binary: Path


def _git(*args: str) -> str:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {result.stderr.strip()}")
    return result.stdout.strip()


@pytest.fixture
def publish_session(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[PublishSession]:
    workspace = tmp_path / "checkout"
    make_checkout(workspace, branch=BRANCH, remote=f"https://github.com/{REPO}.git")
    forge = tmp_path / "forge.git"
    _git("init", "-q", "--bare", str(forge))
    # git's own ``ext::`` helper stands in for a forge's remote helper: its URL names no host.
    _git("-C", str(workspace), "config", "protocol.ext.allow", "always")
    _git("-C", str(workspace), "remote", "add", "forge", f"ext::git %s {forge}")
    head = _git("-C", str(workspace), "rev-parse", "HEAD")
    binary = tmp_path / "bin"
    pr = {
        "number": 7,
        "url": PR_URL,
        "title": PR_TITLE,
        "state": "OPEN",
        "isDraft": True,
        "author": {"login": "contributor"},
        "baseRefName": "main",
        "headRefName": BRANCH,
        "headRefOid": head,
        "baseRefOid": "b" * 40,
        "body": "Adds the widget catalog.",
        "comments": [],
        "statusCheckRollup": [],
    }
    write_gh_shim(binary, repo=REPO, pr=pr, found_by_branch=False)
    for name, script in (("git-publish", GIT_PUBLISH), ("forge-pr", FORGE_PR)):
        (binary / name).write_text(script)
        (binary / name).chmod(0o755)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    model = f"publish-{uuid.uuid4().hex[:8]}"
    env = stack_env(binary, runtime, mock_llm_server_url)
    with server_runner(runtime, base_env=env, workspace=workspace) as stack:
        stack.start_runner()
        session_id = create_session(stack.base_url, stack.runner_id, workspace, model=model)
        yield PublishSession(stack.base_url, session_id, model, workspace, forge, head, binary)


@pytest.mark.parametrize("shape", list(COMMANDS))
def test_published_pr_appears_without_linking(
    request: pytest.FixtureRequest,
    publish_session: PublishSession,
    mock_llm_server_url: str,
    shape: str,
) -> None:
    session = publish_session
    command, pushed_first = COMMANDS[shape]
    if pushed_first:
        _git("-C", str(session.workspace), "push", "-q", "forge", f"HEAD:refs/heads/{BRANCH}")
    page: Page = request.getfixturevalue("page")
    rail = open_pull_requests_tab(page, session.base_url, session.session_id)
    no_pr = rail.get_by_text(re.compile(rf"No open PR for\s*{BRANCH}"))
    expect(no_pr).to_be_visible(timeout=30_000)

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_publish",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            },
            {"text": REPLY},
        ],
        key=session.model,
    )
    set_fallback_mock_llm(mock_llm_server_url, session.model, "Done.")
    page.get_by_label("Message the agent").fill("Publish this branch for review.")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(ASSISTANT).last).to_contain_text(REPLY, timeout=60_000)
    # The command really ran: the forge holds the branch and the publishing script logged.
    assert _git("-C", str(session.forge), "rev-parse", f"refs/heads/{BRANCH}") == session.head
    script = "git-publish" if shape == "git-subcommand" else "forge-pr"
    assert (session.binary / "commands.log").read_text().splitlines() == [script]

    header = rail.get_by_role("link", name=f"{PR_TITLE} #7")
    expect(header).to_be_visible(timeout=30_000)
    expect(header).to_have_attribute("href", PR_URL)
    expect(no_pr).to_have_count(0)
    expect(page.get_by_test_id("composer-pr-link")).to_have_accessible_name("#7")
    info = httpx.get(
        f"{session.base_url}/v1/sessions/{session.session_id}/resources/github", timeout=30
    )
    info.raise_for_status()
    assert [pr["url"] for pr in info.json()["prs"]] == [PR_URL]
