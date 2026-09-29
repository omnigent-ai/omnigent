"""Config-to-checkout journey using real Git and generated workspace prep.

No cluster or LLM is required. The remote is a disposable local repository;
the credential broker is a loopback fixture exercising the production helper.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER
from omnigent.onboarding.sandboxes.kubernetes import (
    KubernetesSandboxLauncher,
    _render_workspace_prep_command,
)
from omnigent.onboarding.sandboxes.types import GitCloneOptions, RepoWorkspace
from omnigent.server.managed_hosts import _apply_git_clone_options, parse_sandbox_config


@pytest.mark.parametrize(
    ("policy", "branch"),
    [
        ({}, None),
        ({"depth": 2}, None),
        ({"depth": 2}, "main"),
        ({"depth": 2, "single_branch": False}, "main"),
        ({"depth": 2, "single_branch": True, "tags": False}, None),
        ({"filter": "blob:none", "single_branch": True}, None),
    ],
)
def test_clone_policy_checkout_recovery_and_retained_workspace(
    tmp_path: Path, policy: dict[str, object], branch: str | None
) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update(
        GIT_CONFIG_NOSYSTEM="1",
        GIT_CONFIG_GLOBAL=str(tmp_path / "gitconfig"),
        GIT_TERMINAL_PROMPT="0",
        GIT_AUTHOR_NAME="Test",
        GIT_AUTHOR_EMAIL="test@example.test",
        GIT_COMMITTER_NAME="Test",
        GIT_COMMITTER_EMAIL="test@example.test",
        IS_SANDBOX="1",
        PATH=str(Path(sys.executable).parent) + os.pathsep + env["PATH"],
    )
    templates = tmp_path / "templates"
    templates.mkdir()
    env["GIT_TEMPLATE_DIR"] = str(templates)
    env[HOST_TOKEN_ENV_VAR] = "fixture-host-token"

    def run(*args: str, cwd: Path = tmp_path, input: str | None = None) -> str:
        result = subprocess.run(
            args,
            cwd=cwd,
            env=env,
            input=input,
            text=True,
            capture_output=True,
            timeout=30,
            check=True,
        )
        return result.stdout.strip()

    source = tmp_path / "source"
    run("git", "init", "-b", "main", str(source))
    for revision in range(6):
        (source / "file.txt").write_text(f"revision {revision}\n")
        run("git", "add", ".", cwd=source)
        run("git", "commit", "-m", f"revision {revision}", cwd=source)
    run("git", "branch", "other", "HEAD~2", cwd=source)
    run("git", "tag", "v-tip", cwd=source)
    run("git", "config", "uploadpack.allowFilter", "true", cwd=source)
    old_blob = run("git", "rev-parse", "HEAD~5:file.txt", cwd=source)
    expected_head = run("git", "rev-parse", "HEAD", cwd=source)
    state = {"token": "fixture-owner-token"}
    requests: list[bool] = []

    class Broker(BaseHTTPRequestHandler):
        def log_message(self, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            valid = (
                self.path == "/v1/hosts/host-test/credentials/github"
                and self.headers.get(MANAGED_HOST_TOKEN_HEADER) == env[HOST_TOKEN_ENV_VAR]
            )
            requests.append(valid)
            self.send_response(200 if valid else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "connected": True,
                        "owner": "test@example.test",
                        "login": "test",
                        "username": "x-access-token",
                        "token": state["token"],
                    }
                ).encode()
            )

    server = ThreadingHTTPServer(("127.0.0.1", 0), Broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    url = f"http://127.0.0.1:{server.server_port}"
    try:
        deployment = parse_sandbox_config(
            {"provider": "agent_sandbox", "server_url": url, "git_clone": policy}
        )
        assert deployment is not None
        config = deployment.for_provider(None)
        assert config is not None
        repos = _apply_git_clone_options(
            KubernetesSandboxLauncher(),
            [RepoWorkspace(source.as_uri(), branch, "repo")],
            config.git_clone,
        )
        workspace = tmp_path / "workspace"

        def prepare(options: GitCloneOptions) -> None:
            command = _render_workspace_prep_command(
                str(workspace), [replace(repos[0], git_clone=options)], url, "host-test"
            )
            run("bash", "-c", command[2])

        prepare(config.git_clone)
        checkout = workspace / "repo"
        assert run("git", "rev-parse", "HEAD", cwd=checkout) == expected_head
        assert (checkout / "file.txt").read_text() == "revision 5\n"
        assert run("git", "rev-parse", "--is-shallow-repository", cwd=checkout) == (
            "true" if policy.get("depth") else "false"
        )
        refspec = run("git", "config", "remote.origin.fetch", cwd=checkout)
        assert refspec == (
            "+refs/heads/main:refs/remotes/origin/main"
            if policy.get("single_branch", branch is not None)
            else "+refs/heads/*:refs/remotes/origin/*"
        )
        assert run("git", "tag", cwd=checkout) == ("" if policy.get("tags") is False else "v-tip")

        if policy.get("filter"):
            assert run("git", "config", "remote.origin.promisor", cwd=checkout) == "true"
            missing = run("git", "rev-list", "--objects", "--all", "--missing=print", cwd=checkout)
            assert f"?{old_blob}" in missing.splitlines()
            assert run("git", "show", "HEAD~5:file.txt", cwd=checkout) == "revision 0"
        if policy.get("depth"):
            run("git", "fetch", "--deepen=2", "origin", cwd=checkout)
            assert run("git", "rev-parse", "HEAD~3", cwd=checkout)
            run("git", "fetch", "--unshallow", "origin", cwd=checkout)
            assert run("git", "rev-parse", "--is-shallow-repository", cwd=checkout) == "false"
            assert run("git", "rev-parse", "HEAD", cwd=checkout) == expected_head

        (checkout / "file.txt").write_text("user edit\n")
        (checkout / "untracked.txt").write_text("keep me\n")
        retained_config = (checkout / ".git/config").read_bytes()
        probe_count = len(requests)
        prepare(GitCloneOptions(depth=1, single_branch=False, tags=False))
        assert (checkout / ".git/config").read_bytes() == retained_config
        assert (checkout / "file.txt").read_text() == "user edit\n"
        assert (checkout / "untracked.txt").read_text() == "keep me\n"
        assert len(requests) == probe_count

        run(
            sys.executable,
            "-c",
            "from omnigent.git_credential_github import configure_host_git; "
            "import sys; configure_host_git(sys.argv[1], 'host-test')",
            url,
        )
        for token in ["fixture-owner-token", "fixture-rotated-token"]:
            state["token"] = token
            credential = run(
                "git", "credential", "fill", input="protocol=https\nhost=github.com\n\n"
            )
            assert f"password={token}" in credential.splitlines()
        assert requests and all(requests)
        assert state["token"] not in (checkout / ".git/config").read_text()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
