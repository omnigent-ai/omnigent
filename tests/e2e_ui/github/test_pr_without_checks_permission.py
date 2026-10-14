"""The Pull Requests panel resolves a PR whose checks the gh token cannot read.

GitHub refuses ``statusCheckRollup`` to a fine-grained personal access token
(``GraphQL: Resource not accessible by personal access token``) while every other
PR field, ``gh auth status``, ``gh repo view`` and the REST ``commits/{sha}/pulls``
lookup succeed. This test owns its server and runner so a PATH-shimmed ``gh``
can stand in for such a token; git, the runner and the SPA run normally. The PR
must still render, with the Checks area explaining why its check runs are missing.
"""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from dev.repro_env.runtime import isolated_env
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import open_right_rail

PR_URL = "https://github.com/example/project/pull/42"
PR_TITLE = "Teach the panel about token limits"
BRANCH = "local-topic"
CHECKS_REFUSED = (
    "GraphQL: Resource not accessible by personal access token "
    "(repository.pullRequests.nodes.0.statusCheckRollup.nodes.0.commit.statusCheckRollup)"
)

# Stands in for gh signed in with a fine-grained PAT: only a ``pr view`` that asks
# for statusCheckRollup fails, exactly as the real CLI does for such a token.
GH_SHIM = f"""import json, re, sys
from pathlib import Path

root = Path(__file__).parent
args = sys.argv[1:]
pr = {{
    "number": 42, "url": {PR_URL!r}, "title": {PR_TITLE!r}, "state": "OPEN",
    "isDraft": False, "author": {{"login": "contributor"}}, "baseRefName": "main",
    "headRefName": {BRANCH!r}, "headRefOid": "a" * 40, "baseRefOid": "b" * 40,
    "body": "Resolves even when checks are unreadable.", "comments": [],
    "statusCheckRollup": [{{
        "__typename": "CheckRun", "name": "unit", "status": "COMPLETED",
        "conclusion": "SUCCESS", "detailsUrl": "https://github.com/example/project/actions/runs/1",
    }}],
}}


def finish(rc, out="", err=""):
    with (root / "gh-calls.log").open("a") as log:
        print(json.dumps({{"args": args, "rc": rc}}), file=log)
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    sys.exit(rc)


if args[:2] == ["auth", "status"]:
    account = {{"login": "contributor", "active": True, "state": "success"}}
    if "--json" in args:
        finish(0, json.dumps({{"hosts": {{"github.com": [account]}}}}))
    finish(0, "github.com\\n  Logged in to github.com account contributor")
if args[:2] == ["repo", "view"]:
    finish(0, json.dumps({{"nameWithOwner": "example/project"}}))
if args[:3] == ["repo", "set-default", "--view"]:
    finish(0, "example/project")
if args[:2] == ["repo", "set-default"]:
    finish(0)
if args[:2] == ["pr", "view"]:
    fields = args[args.index("--json") + 1].split(",") if "--json" in args else []
    if "statusCheckRollup" in fields:
        finish(1, err={CHECKS_REFUSED!r})
    finish(0, json.dumps({{name: pr[name] for name in fields if name in pr}}))
if args[:2] == ["pr", "diff"]:
    finish(0, "")
if args[0] == "api" and re.fullmatch(
    r"repos/example/project/commits/[0-9a-f]{{40}}/pulls", args[1]
):
    row = {{"number": 42, "state": "open", "html_url": pr["url"],
           "base": {{"repo": {{"full_name": "example/project"}}}}}}
    finish(0, json.dumps([row]))
if args[0] == "api" and args[-1].startswith("repos/example/project/pulls/42/files"):
    finish(0, "[[]]" if "--slurp" in args else "[]")
finish(1, err="gh shim: unexpected command " + json.dumps(args))
"""


def write_gh_shim(binary: Path) -> Path:
    """Install the ``gh`` stand-in into ``binary`` and return its path."""
    binary.mkdir(parents=True, exist_ok=True)
    impl = binary / "gh_impl.py"
    impl.write_text(GH_SHIM)
    gh = binary / "gh"
    gh.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(impl)]) + ' "$@"\n')
    gh.chmod(0o755)
    return gh


def make_checkout(workspace: Path) -> None:
    """A one-commit checkout of ``example/project`` on the PR's head branch."""
    for args in (
        ["init", "-q", "-b", BRANCH, str(workspace)],
        [
            "-C",
            str(workspace),
            "-c",
            "user.name=Test",
            "-c",
            "user.email=test@example.com",
            "commit",
            "--allow-empty",
            "-qm",
            "Initial",
        ],
        # Avoid racing the startup cache probe against the test's real commit.
        ["-C", str(workspace), "config", "core.untrackedCache", "true"],
        [
            "-C",
            str(workspace),
            "remote",
            "add",
            "origin",
            "https://github.com/example/project.git",
        ],
    ):
        subprocess.run(["git", *args], check=True, capture_output=True)


def stack_env(binary: Path, runtime: Path, model_url: str) -> dict[str, str]:
    """Server/runner env: the shim first on PATH and no managed-sandbox marker."""
    env = isolated_env(dict(os.environ), runtime)
    env.pop("IS_SANDBOX", None)
    env.update(
        PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
        OPENAI_API_KEY="mock-key",
        OPENAI_BASE_URL=f"{model_url}/v1",
    )
    return env


def create_session(base_url: str, runner_id: str, workspace: Path) -> str:
    model = f"pr-checks-{uuid.uuid4().hex[:8]}"
    spec = f"""name: pr-checks
prompt: Run the requested shell command and report completion.
executor:
  model: {model}
  harness: openai-agents
os_env:
  type: caller_process
  cwd: {json.dumps(str(workspace))}
  sandbox:
    type: none
"""
    response = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        bundle_files({"pr-checks.yaml": spec.encode()}),
        metadata={"workspace": str(workspace)},
        timeout=30,
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=30)
    return session_id


@pytest.fixture
def pr_session(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[tuple[str, str, Path]]:
    binary = tmp_path / "bin"
    write_gh_shim(binary)
    workspace = tmp_path / "checkout"
    make_checkout(workspace)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = stack_env(binary, runtime, mock_llm_server_url)
    with server_runner(runtime, base_env=env, workspace=workspace) as stack:
        stack.start_runner()
        session_id = create_session(stack.base_url, stack.runner_id, workspace)
        yield stack.base_url, session_id, binary


def _open_pull_requests_tab(page: Page, base_url: str, session_id: str) -> Locator:
    page.add_init_script("window.localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()
    expect(rail.get_by_text("Loading pull requests…")).to_have_count(0, timeout=30_000)
    return rail


def _pr_header(rail: Locator) -> Locator:
    return rail.get_by_role("link", name=f"{PR_TITLE} #42")


def _checks_note(rail: Locator) -> Locator:
    return rail.get_by_text(re.compile(r"Checks can.t be read with this GitHub token"))


def test_branch_pr_resolves_when_checks_are_forbidden(
    request: pytest.FixtureRequest, pr_session: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, session_id, binary = pr_session
    page: Page = request.getfixturevalue("page")
    rail = _open_pull_requests_tab(page, base_url, session_id)
    no_pr = rail.get_by_text(re.compile(rf"No open PR for\s*{BRANCH}"))
    expect(_pr_header(rail).or_(no_pr)).to_be_visible(timeout=30_000)
    page.screenshot(path=str(tmp_path / "branch-pr.png"), animations="disabled")
    calls = [json.loads(line) for line in (binary / "gh-calls.log").read_text().splitlines()]
    assert any(c["args"][:2] == ["pr", "view"] and c["rc"] == 1 for c in calls), calls
    expect(no_pr).to_have_count(0)
    expect(_pr_header(rail)).to_be_visible()
    expect(_pr_header(rail)).to_have_attribute("href", PR_URL)
    expect(_checks_note(rail)).to_be_visible()


def test_linked_pr_resolves_when_checks_are_forbidden(
    request: pytest.FixtureRequest, pr_session: tuple[str, str, Path], tmp_path: Path
) -> None:
    base_url, session_id, binary = pr_session
    page: Page = request.getfixturevalue("page")
    rail = _open_pull_requests_tab(page, base_url, session_id)
    no_pr = rail.get_by_text(re.compile(rf"No open PR for\s*{BRANCH}"))
    expect(_pr_header(rail).or_(no_pr)).to_be_visible(timeout=30_000)
    rail.get_by_role("button", name="Link a PR", exact=True).click()
    rail.get_by_label("Pull request URL").fill(PR_URL)
    rail.get_by_role("button", name="Link", exact=True).click()
    picker = rail.get_by_role("combobox", name="Session pull request")
    expect(picker).to_contain_text("#42", timeout=30_000)
    unreachable = rail.get_by_text(re.compile(r"Can.t reach the upstream repo"))
    expect(_pr_header(rail).or_(unreachable)).to_be_visible(timeout=30_000)
    page.screenshot(path=str(tmp_path / "linked-pr.png"), animations="disabled")
    calls = [json.loads(line) for line in (binary / "gh-calls.log").read_text().splitlines()]
    assert any(c["args"][:3] == ["pr", "view", "42"] and c["rc"] == 1 for c in calls), calls
    expect(unreachable).to_have_count(0)
    expect(rail.get_by_role("combobox", name=re.compile(r"GitHub account"))).to_have_count(0)
    expect(_pr_header(rail)).to_be_visible()
    expect(_checks_note(rail)).to_be_visible()
