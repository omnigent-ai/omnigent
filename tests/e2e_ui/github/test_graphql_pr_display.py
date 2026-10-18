"""Session PR discovery and GraphQL creation populate both PR surfaces.

This test owns its server and runner so their gh executable can be replaced
without touching another environment. Git and the product run normally;
only GitHub and the model are deterministic fixtures.
"""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from dev.repro_env.runtime import isolated_env
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_URL = "https://github.com/example/project/pull/42"
_TITLE = "Show the session pull request"
_QUERY = """mutation CreatePullRequest($repositoryId: ID!, $headRepositoryId: ID!) {
  createPullRequest(input: {
    repositoryId: $repositoryId, headRepositoryId: $headRepositoryId,
    baseRefName: "main", headRefName: "contributor/topic", title: "A change"
  }) { pullRequest { number url title isDraft } }
}"""
_GH = """import json, re, sys
from pathlib import Path

root = Path(__file__).parent
args = sys.argv[1:]

def unexpected_api():
    with (root / "unexpected-api").open("a") as log:
        print(json.dumps(args), file=log)
    sys.exit(1)

pr = {
    "number": 42, "url": "https://github.com/example/project/pull/42",
    "title": "Show the session pull request", "state": "OPEN", "isDraft": True,
    "author": {"login": "contributor"}, "baseRefName": "main",
    "headRefName": "contributor/topic", "headRefOid": "a" * 40,
    "baseRefOid": "b" * 40, "statusCheckRollup": [], "comments": [],
    "body": "Created from the active session checkout.",
}
if args[:2] == ["api", "graphql"]:
    if args[-2:] != ["--jq", ".data.createPullRequest.pullRequest"] or not any(
        arg.startswith("query=") and "createPullRequest(" in arg for arg in args
    ):
        unexpected_api()
    (root / "created").touch()
    print(json.dumps(pr))
elif args[:2] == ["pr", "view"]:
    explicit = "-R" in args and args[args.index("-R") + 1] == "github.com/example/project"
    if (explicit and (root / "created").exists()) or (root / "discover").exists():
        print(json.dumps(pr))
    else:
        sys.exit(1)
elif args[:2] == ["repo", "view"]:
    print(json.dumps({"nameWithOwner": "example/project-dev"}))
elif args[:2] == ["auth", "status"]:
    account = {"login": "contributor", "active": True, "state": "success"}
    print(json.dumps({"hosts": {"github.com": [account]}}))
elif len(args) == 2 and args[0] == "api" and re.fullmatch(
    r"repos/example/project-dev/commits/[0-9a-f]{40}/pulls", args[1]
):
    print("[]")
elif args == [
    "api", "--hostname", "github.com", "--paginate", "--slurp",
    "repos/example/project/pulls/42/files?per_page=100",
]:
    print("[[]]")
elif args[0] == "api":
    unexpected_api()
else:
    sys.exit(1)
"""


@pytest.fixture
def pr_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[tuple[str, str, str, Path]]:
    binary = tmp_path / "bin"
    binary.mkdir()
    impl = binary / "gh_impl.py"
    impl.write_text(_GH)
    gh = binary / "gh"
    gh.write_text("#!/bin/sh\nexec " + shlex.join([sys.executable, str(impl)]) + ' "$@"\n')
    gh.chmod(0o755)
    workspace = tmp_path / "checkout"
    for args in (
        ["init", "-q", "-b", "local-topic", str(workspace)],
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
            "https://github.com/example/project-dev.git",
        ],
    ):
        subprocess.run(["git", *args], check=True, capture_output=True)
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    env = isolated_env(dict(os.environ), runtime)
    env.update(
        PATH=f"{binary}{os.pathsep}{os.environ['PATH']}",
        OPENAI_API_KEY="mock-key",
        OPENAI_BASE_URL=f"{mock_llm_server_url}/v1",
    )
    model = f"pr-display-{uuid.uuid4().hex[:8]}"
    with server_runner(runtime, base_env=env, workspace=workspace) as stack:
        stack.start_runner()
        base_url = stack.base_url
        spec = f"""name: pr-display
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
            bundle_files({"pr-display.yaml": spec.encode()}),
            metadata={"workspace": str(workspace)},
            timeout=30,
        )
        response.raise_for_status()
        session_id = response.json()["session_id"]
        bind_session_runner(httpx.patch, base_url, session_id, stack.runner_id, timeout=30)
        yield base_url, session_id, model, binary


@pytest.mark.parametrize(
    "existing", [False, True], ids=["created-after-start", "existing-branch-pr"]
)
def test_pr_appears_in_composer_and_workspace(
    page: Page,
    pr_session: tuple[str, str, str, Path],
    mock_llm_server_url: str,
    tmp_path: Path,
    existing: bool,
) -> None:
    base_url, session_id, model, binary = pr_session
    if existing:
        (binary / "discover").touch()
        (binary / "created").touch()
    else:
        info = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/github", timeout=30)
        info.raise_for_status()
        assert info.json()["prs"] == []

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)
    indicator = page.get_by_test_id("composer-pr-link")
    if not existing:
        expect(indicator).to_have_count(0)
        shell = shlex.join(
            [
                str(binary / "gh"),
                "api",
                "graphql",
                "-f",
                f"query={_QUERY}",
                "--jq",
                ".data.createPullRequest.pullRequest",
            ]
        )
        commit = shlex.join(
            [
                "git",
                "-c",
                "user.name=Test",
                "-c",
                "user.email=test@example.com",
                "commit",
                "--allow-empty",
                "-m",
                "A change",
            ]
        )
        shell = f"{commit} && {shell}"
        configure_mock_llm(
            mock_llm_server_url,
            [
                {
                    "tool_calls": [
                        {
                            "call_id": "create-pr",
                            "name": "sys_os_shell",
                            "arguments": json.dumps({"command": shell}),
                        }
                    ]
                },
                {"text": "Created the draft pull request."},
            ],
            key=model,
        )
        set_fallback_mock_llm(mock_llm_server_url, model, "Done.")
        composer.fill("Create a draft pull request for this change.")
        page.get_by_role("button", name="Send", exact=True).click()
        expect(page.locator('[data-role="assistant"]').last).to_contain_text(
            "Created the draft pull request.", timeout=60_000
        )
        expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=60_000)

    # Keep the page mounted: turn completion must invalidate the initial empty result.
    expect(indicator).to_have_accessible_name("#42", timeout=15_000)
    indicator.click()
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("tab", name="Pull Requests")).to_have_attribute(
        "aria-selected", "true"
    )
    expect(rail.get_by_role("combobox", name="Session pull request")).to_contain_text(_TITLE)
    expect(rail.get_by_role("link", name=f"{_TITLE} #42")).to_have_attribute("href", _URL)
    expect(rail.get_by_text("Created from the active session checkout.")).to_be_visible()
    page.screenshot(path=str(tmp_path / "pr-displayed.png"), animations="disabled")
    response = httpx.get(f"{base_url}/v1/sessions/{session_id}/resources/github", timeout=30)
    response.raise_for_status()
    info = response.json()
    assert info["selected_pr_url"] == _URL
    assert info["repo"]["name_with_owner"] == "example/project"
    assert info["pr"]["head_ref"] == "contributor/topic"
    assert info["prs"][0]["relationship"] == ("inferred" if existing else "created")
    unexpected_api = binary / "unexpected-api"
    assert not unexpected_api.exists(), unexpected_api.read_text()
