"""The GitHub panel keeps a session's picked PR when the user switches sessions.

The picker defaults to the most recently seen PR (after a merge, the merged one);
a pick must survive switching to another session and back. The journey runs on a
real server + runner + per-session PR registry. Only the GitHub CLI is replaced:
a ``gh`` stub first on the runner's PATH answers with metadata for two PRs
(#42 open, #7 merged), since CI has no GitHub repository whose PRs it can merge.
"""

from __future__ import annotations

import contextlib
import os
import re
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
    open_right_rail,
)

OPEN_PR = "https://github.com/example/project/pull/42"
MERGED_PR = "https://github.com/example/project/pull/7"
OPEN_LABEL = "example/project #42 — Add session switcher keyboard shortcuts"
MERGED_LABEL = "example/project #7 — Fix sidebar PR label truncation"
SESSION_A_TITLE = "Alpha — sidebar PR work"
SESSION_B_TITLE = "Beta — follow-up"

_HEALTH_TIMEOUT_S = 120.0

# Stands in for the GitHub CLI on the runner's PATH. Answers the read-only
# calls the GitHub panel makes (auth status, repo view, pr view/diff, api) with
# fixed metadata for the two PRs; everything else fails like an unknown PR.
GH_STUB = """\
#!{python}
import base64, json, os, re, sys, time

args = sys.argv[1:]
log = os.environ.get("OMNIGENT_E2E_GH_STUB_LOG")
if log:
    with open(log, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({{"at": time.time(), "argv": args}}) + "\\n")

PRS = {{
    42: {{
        "number": 42,
        "title": "Add session switcher keyboard shortcuts",
        "state": "OPEN",
        "url": "https://github.com/example/project/pull/42",
        "isDraft": False,
        "author": {{"login": "octocat"}},
        "baseRefName": "main",
        "headRefName": "feature/session-switcher",
        "headRefOid": "4242424242424242424242424242424242424242",
        "baseRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "statusCheckRollup": [
            {{"__typename": "CheckRun", "name": "unit", "status": "COMPLETED",
             "conclusion": "SUCCESS", "detailsUrl": None}}
        ],
        "body": "Adds keyboard shortcuts for switching between sessions.",
        "comments": [],
    }},
    7: {{
        "number": 7,
        "title": "Fix sidebar PR label truncation",
        "state": "MERGED",
        "url": "https://github.com/example/project/pull/7",
        "isDraft": False,
        "author": {{"login": "octocat"}},
        "baseRefName": "main",
        "headRefName": "fix/sidebar-truncation",
        "headRefOid": "0707070707070707070707070707070707070707",
        "baseRefOid": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
        "statusCheckRollup": [
            {{"__typename": "CheckRun", "name": "unit", "status": "COMPLETED",
             "conclusion": "SUCCESS", "detailsUrl": None}}
        ],
        "body": "Truncates long PR labels in the sidebar.",
        "comments": [],
    }},
}}


def emit(payload):
    print(json.dumps(payload))
    sys.exit(0)


def fail(message, code=1):
    print(message, file=sys.stderr)
    sys.exit(code)


def pr_after(keyword):
    index = args.index(keyword) + 1
    return int(args[index]) if index < len(args) and args[index].isdigit() else None


if args[:2] == ["auth", "status"]:
    if "--json" in args:
        emit({{"hosts": {{"github.com": [
            {{"login": "octocat", "active": True, "state": "success", "host": "github.com"}}
        ]}}}})
    sys.exit(0)
if args[:2] == ["repo", "view"]:
    emit({{"nameWithOwner": "example/project"}})
if args[:2] == ["pr", "view"]:
    number = pr_after("view")
    if number is None:
        fail("no pull requests found for branch")
    if number not in PRS:
        fail("GraphQL: Could not resolve to a PullRequest with the number of %d." % number)
    emit(PRS[number])
if args[:2] == ["pr", "diff"]:
    number = pr_after("diff")
    if number not in PRS:
        fail("no pull request found")
    print(
        "diff --git a/src/app.py b/src/app.py\\n--- a/src/app.py\\n+++ b/src/app.py\\n"
        "@@ -1 +1 @@\\n-old\\n+new from PR %d\\n" % number
    )
    sys.exit(0)
if args[:1] == ["api"]:
    endpoint = next(
        (a for a in reversed(args[1:]) if not a.startswith("-") and a != "github.com"), ""
    )
    files = re.search(r"/pulls/(\\d+)/files", endpoint)
    if files:
        rows = [{{"filename": "src/app.py", "status": "modified", "additions": 1, "deletions": 1}}]
        emit([rows] if "--slurp" in args else rows)
    single = re.search(r"/pulls/(\\d+)$", endpoint)
    if single and int(single.group(1)) in PRS:
        pr = PRS[int(single.group(1))]
        emit({{"head": {{"sha": pr["headRefOid"], "repo": {{"full_name": "example/project"}}}},
              "base": {{"sha": pr["baseRefOid"]}}}})
    if "/compare/" in endpoint:
        emit({{"merge_base_commit": {{"sha": PRS[42]["baseRefOid"]}}}})
    if "/contents/" in endpoint:
        emit({{"encoding": "base64", "content": base64.b64encode(b"print('hi')\\n").decode()}})
    fail("HTTP 404: Not Found")
fail("unsupported gh invocation in stub: %r" % (args,))
"""

_AGENT_YAML = """\
name: {name}
prompt: You are a deterministic test assistant.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""


def _git(*args: str) -> None:
    result = subprocess.run(["git", *args], capture_output=True, text=True)
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"git {' '.join(args)} failed: {detail}")


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


@pytest.fixture
def gh_stubbed_server(
    built_spa: None, mock_llm_server_url: str, tmp_path: Path
) -> Iterator[tuple[str, str]]:
    """Spawn a server + runner whose ``gh`` is the stub; yield ``(base_url, runner_id)``.

    The shared ``live_server`` runner inherits the test process PATH at spawn
    time, so a dedicated pair is the only way to put the stub first on PATH.
    ``OMNIGENT_DATA_DIR`` isolates the runner's per-session PR registry files.
    """
    import secrets

    from omnigent.runner.identity import token_bound_runner_id

    stub_dir = tmp_path / "gh-stub"
    stub_dir.mkdir()
    stub_py = stub_dir / "gh_stub.py"
    stub_py.write_text(GH_STUB.format(python=sys.executable))
    # A /bin/sh shim execs the interpreter by quoted path, tolerating spaces and
    # long venv paths that a bare shebang line can't handle on every runner.
    stub = stub_dir / "gh"
    stub.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{stub_py}" "$@"\n')
    stub.chmod(0o755)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    agent_yaml = tmp_path / "hello_world.yaml"
    agent_yaml.write_text(_TEST_AGENT_YAML)

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    shared_env = {
        **os.environ,
        "PATH": f"{stub_dir}{os.pathsep}{os.environ['PATH']}",
        "OMNIGENT_DATA_DIR": str(data_dir),
        "OMNIGENT_E2E_GH_STUB_LOG": str(tmp_path / "gh-stub.log"),
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "ANTHROPIC_API_KEY": "",
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
    }
    server_env = apply_server_env(
        {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}, _REPO_ROOT
    )
    runner_env = {
        **shared_env,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }
    server_log = (tmp_path / "server.log").open("w")
    runner_log = (tmp_path / "runner.log").open("w")
    server: subprocess.Popen[bytes] | None = None
    runner: subprocess.Popen[bytes] | None = None
    try:
        server = subprocess.Popen(
            [
                server_executable(),
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{tmp_path / 'test.db'}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
                "--agent",
                str(agent_yaml),
            ],
            env=server_env,
            cwd=compat_server_cwd(),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        runner = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log,
            stderr=subprocess.STDOUT,
        )
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if server.poll() is not None or runner.poll() is not None:
                last_error = f"server exit={server.poll()} runner exit={runner.poll()}"
                break
            try:
                if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online") is True:
                        break
                    last_error = f"runner status {status.status_code}: {status.text[:200]}"
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
        else:
            raise RuntimeError(
                f"gh-stubbed server/runner not ready within {_HEALTH_TIMEOUT_S:.0f}s "
                f"({last_error}); logs under {tmp_path}"
            )
        if server.poll() is not None or runner.poll() is not None:
            raise RuntimeError(
                f"gh-stubbed server/runner exited early ({last_error}); logs under {tmp_path}"
            )
        yield base_url, runner_id
    finally:
        _terminate(runner)
        _terminate(server)
        runner_log.close()
        server_log.close()


@pytest.fixture
def pr_sessions(
    gh_stubbed_server: tuple[str, str], tmp_path: Path
) -> Iterator[tuple[str, str, str]]:
    """Two titled sessions sharing one git workspace; yield ``(base_url, a, b)``."""
    base_url, runner_id = gh_stubbed_server
    workspace = tmp_path / "workspace"
    _git("init", "-q", "-b", "topic", str(workspace))
    _git(
        "-C",
        str(workspace),
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
    _git("init", "-q", "--bare", str(tmp_path / "origin.git"))
    _git("-C", str(workspace), "remote", "add", "origin", str(tmp_path / "origin.git"))

    session_ids: list[str] = []
    for title in (SESSION_A_TITLE, SESSION_B_TITLE):
        name = f"pr_switch_{uuid.uuid4().hex[:8]}"
        bundle = bundle_files(
            {f"{name}.yaml": _AGENT_YAML.format(name=name, cwd=workspace).encode()}
        )
        created = post_session_bundle(httpx.post, f"{base_url}/v1/sessions", bundle, timeout=30.0)
        created.raise_for_status()
        session_id = created.json()["session_id"]
        session_ids.append(session_id)
        bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}", json={"title": title}, timeout=10.0
        ).raise_for_status()
    try:
        yield base_url, session_ids[0], session_ids[1]
    finally:
        # Teardown stays best-effort: a delete that fails (e.g. the server died
        # mid-test) must not skip the remaining sessions or mask the real error.
        for session_id in session_ids:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def _open_github_tab(page: Page) -> None:
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    tab = rail.get_by_role("tab", name="GitHub")
    expect(tab).to_be_visible(timeout=30_000)
    if tab.get_attribute("aria-selected") != "true":
        tab.click()


def _link_pr(page: Page, url: str, label: str) -> None:
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Link a PR", exact=True).click()
    rail.get_by_role("textbox", name="Pull request URL").fill(url)
    rail.get_by_role("button", name="Link", exact=True).click()
    expect(rail.get_by_role("combobox", name="Session pull request")).to_have_text(
        label, timeout=30_000
    )


def _wait_github_loaded(page: Page) -> None:
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("combobox", name="Session pull request")).to_be_visible(timeout=30_000)
    expect(rail.get_by_text("Loading GitHub…", exact=True)).to_have_count(0, timeout=30_000)


def _hold(page: Page, ms: int) -> None:
    """Keep an asserted state on screen while the journey is being recorded."""
    if os.environ.get("OMNIGENT_E2E_RECORD_DIR"):
        page.wait_for_timeout(ms)


def test_selected_pr_survives_switching_sessions(
    request: pytest.FixtureRequest,
    pr_sessions: tuple[str, str, str],
    tmp_path: Path,
) -> None:
    base_url, session_a, session_b = pr_sessions
    page: Page = request.getfixturevalue("page")
    rail = page.get_by_role("complementary", name="Workspace")
    picker = rail.get_by_role("combobox", name="Session pull request")

    page.goto(f"{base_url}/c/{session_a}")
    _open_github_tab(page)
    _link_pr(page, OPEN_PR, OPEN_LABEL)
    _hold(page, 1_500)
    _link_pr(page, MERGED_PR, MERGED_LABEL)
    # The merged PR was seen last, so it is the session's default selection.
    expect(rail.get_by_label("Pull request status: Merged")).to_be_visible(timeout=30_000)
    page.screenshot(path=tmp_path / "1-session-a-default-merged.png", animations="disabled")
    _hold(page, 2_500)

    picker.click()
    page.get_by_role("option", name=OPEN_LABEL, exact=True).click()
    expect(picker).to_have_text(OPEN_LABEL)
    expect(rail.get_by_label("Pull request status: Open")).to_be_visible(timeout=30_000)
    page.screenshot(path=tmp_path / "2-session-a-picked-open.png", animations="disabled")
    _hold(page, 2_500)

    page.get_by_role("link", name=SESSION_B_TITLE, exact=True).click()
    expect(page).to_have_url(re.compile(rf"/c/{session_b}"))
    _open_github_tab(page)
    # Session B picks the *other* PR, so returning to A must still show A's open
    # pick; a shared (non per-session) store would leak B's merged pick instead.
    _link_pr(page, MERGED_PR, MERGED_LABEL)
    expect(picker).to_have_text(MERGED_LABEL)
    page.screenshot(path=tmp_path / "3-session-b-merged.png", animations="disabled")
    _hold(page, 2_000)

    page.get_by_role("link", name=SESSION_A_TITLE, exact=True).click()
    expect(page).to_have_url(re.compile(rf"/c/{session_a}"))
    _open_github_tab(page)
    _wait_github_loaded(page)
    _hold(page, 3_000)
    page.screenshot(path=tmp_path / "4-session-a-after-switch.png", animations="disabled")
    expect(picker).to_have_text(OPEN_LABEL, timeout=10_000)
    expect(rail.get_by_label("Pull request status: Open")).to_be_visible()
