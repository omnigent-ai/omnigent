"""A PATH-first ``gh`` stand-in and the checkout, stack and session built around it.

The Pull Requests panel shells out to ``gh`` in the session workspace. Tests that
need a deterministic PR own their server and runner and put this shim first on
their PATH; git, the runner's PR resolution and the SPA all run normally.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import httpx
from playwright.sync_api import Locator, Page, expect

from dev.repro_env.runtime import isolated_env
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import open_right_rail

# Serves the PR from ``config.json`` beside it and logs every call to ``gh-calls.log``.
GH_SHIM = """import json, re, sys
from pathlib import Path

root = Path(__file__).parent
args = sys.argv[1:]
config = json.loads((root / "config.json").read_text())
pr = config["pr"]
repo = config["repo"]


def finish(rc, out="", err=""):
    with (root / "gh-calls.log").open("a") as log:
        print(json.dumps({"args": args, "rc": rc}), file=log)
    if out:
        print(out)
    if err:
        print(err, file=sys.stderr)
    sys.exit(rc)


if args[:2] == ["auth", "status"]:
    account = {"login": "contributor", "active": True, "state": "success"}
    if "--json" in args:
        finish(0, json.dumps({"hosts": {"github.com": [account]}}))
    finish(0, "github.com\\n  Logged in to github.com account contributor")
if args[:2] == ["repo", "view"]:
    finish(0, json.dumps({"nameWithOwner": repo}))
if args[:3] == ["repo", "set-default", "--view"]:
    finish(0, repo)
if args[:2] == ["repo", "set-default"]:
    finish(0)
if args[:2] == ["pr", "view"]:
    json_at = args.index("--json") if "--json" in args else -1
    fields = args[json_at + 1].split(",") if 0 <= json_at < len(args) - 1 else []
    if "statusCheckRollup" in fields and config["checks_error"]:
        finish(1, err=config["checks_error"])
    finish(0, json.dumps({name: pr[name] for name in fields if name in pr}))
if args[:2] == ["pr", "diff"]:
    finish(0, "")
if args[:1] == ["api"] and len(args) > 1 and re.fullmatch(
    f"repos/{re.escape(repo)}/commits/[0-9a-f]{{40}}/pulls", args[1]
):
    row = {"number": pr["number"], "state": "open", "html_url": pr["url"],
           "base": {"repo": {"full_name": repo}}}
    finish(0, json.dumps([row]))
if args[:1] == ["api"] and args[-1].startswith(f"repos/{repo}/pulls/{pr['number']}/files"):
    finish(0, "[[]]" if "--slurp" in args else "[]")
finish(1, err="gh shim: unexpected command " + json.dumps(args))
"""


def write_gh_shim(
    binary: Path, *, repo: str, pr: dict[str, Any], checks_error: str | None = None
) -> Path:
    """Install the ``gh`` stand-in into ``binary`` and return its path.

    ``pr`` is the ``gh pr view`` object for the branch's PR in ``repo``.
    ``checks_error`` makes a ``pr view`` that asks for ``statusCheckRollup`` fail
    with that stderr, as gh does for a token without the Checks permission.
    """
    binary.mkdir(parents=True, exist_ok=True)
    config = {"repo": repo, "pr": pr, "checks_error": checks_error}
    (binary / "config.json").write_text(json.dumps(config))
    (binary / "gh-calls.log").write_text("")
    impl = binary / "gh_impl.py"
    impl.write_text(GH_SHIM)
    gh = binary / "gh"
    gh.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(impl)]) + ' "$@"\n')
    gh.chmod(0o755)
    return gh


def make_checkout(workspace: Path, *, branch: str, remote: str) -> None:
    """A one-commit checkout on ``branch`` whose ``origin`` remote is ``remote``."""
    for args in (
        ["init", "-q", "-b", branch, str(workspace)],
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
        ["-C", str(workspace), "remote", "add", "origin", remote],
    ):
        result = subprocess.run(["git", *args], capture_output=True, text=True)
        if result.returncode != 0:
            raise RuntimeError(f"git {shlex.join(args)} failed: {result.stderr.strip()}")


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
    """A runner-bound single-agent session whose workspace is ``workspace``."""
    model = f"pr-checks-{uuid.uuid4().hex[:8]}"
    spec = f"""name: pr-checks
prompt: Run the requested shell command and report completion.
executor:
  model: {model}
  harness: openai-agents
os_env:
  type: caller_process
  cwd: .
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


def open_pull_requests_tab(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the session, select the Workspace rail's Pull Requests tab, return the rail."""
    page.add_init_script("window.localStorage.setItem('omnigent:default-workspace-panel', 'open')")
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="Pull Requests").click()
    expect(rail.get_by_text("Loading pull requests…")).to_have_count(0, timeout=30_000)
    return rail
