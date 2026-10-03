"""E2E: the GitHub tab resolves a cross-fork PR by commit identity.

Triangular workflow: the session branch is pushed to a fork (``origin`` ->
``acme/repo-dev``) under a remote branch name that differs from the local
checkout, and the PR is opened into the upstream repository (``upstream`` ->
``acme/repo``). The branch-name lookup (``gh pr view``) therefore misses, and
the commit-identity fallback must find the PR.

Which repo's ``commits/{sha}/pulls`` endpoint lists a PR varies across the
fork network (live github.com lists an open cross-fork PR on the fork's
endpoint, while a merged one appears on the base's too), so a lookup pinned to
any single repository misses PRs the other side would list. This scenario
emulates the worst case for a push-repo-only lookup — the fork's endpoint has
no association and only the upstream (base) endpoint lists the PR — so the
panel wrongly shows "No open PR" until the lookup also consults the checkout's
other remotes / the configured base repository.

Unlike the other GitHub-tab tests (which stub the ``/resources/github*``
endpoints with ``page.route``), this one exercises the REAL backend path: a
real workspace git checkout, the real server->runner resource proxy, and
``omnigent.runner.github_resource`` shelling out to a fake ``gh`` CLI that
emulates GitHub's fork-network responses (fork commit lookup empty, upstream
lists the open PR). Only GitHub's network side is faked; everything the user's
journey touches — the SPA, the server, the runner, ``git`` — is real. The
runner is spawned per-test so the fake ``gh`` can be put on its ``PATH``
without touching the shared ``live_server`` runner.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import secrets
import signal
import subprocess
import sys
import tarfile
import textwrap
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import open_right_rail

_REPO_ROOT = Path(__file__).resolve().parents[3]

_PR_NUMBER = 4242
_PR_TITLE = "Add cross-fork feature"
_FORK_NWO = "acme/repo-dev"
_UPSTREAM_NWO = "acme/repo"
_BRANCH = "omnigent/cross-fork-feature"

_RUNNER_ONLINE_TIMEOUT_S = 60.0
_RUNNER_POLL_INTERVAL_S = 0.5

# Fake ``gh`` emulating GitHub for the triangular workflow. Dispatches on the
# joined argv, mirroring how ``github_resource._gh`` invokes the CLI:
#  - ``pr view --json`` (branch-name lookup) fails — the branch was pushed
#    under a different remote name, so gh can't match it to a PR.
#  - the FORK's ``commits/{sha}/pulls`` returns ``[]`` — the worst case for a
#    push-repo-only lookup: only the upstream endpoint lists the PR.
#  - the UPSTREAM's ``commits/{sha}/pulls`` returns the open PR @PR@.
#  - ``pr view @PR@ -R @UPSTREAM@`` returns the PR's full JSON, and the
#    files/diff endpoints answer for it, so a fixed lookup renders fully.
_FAKE_GH_TEMPLATE = """\
#!/usr/bin/env bash
[ -n "$FAKE_GH_LOG" ] && echo "gh $*" >> "$FAKE_GH_LOG"
args="$*"
case "$args" in
  "auth status"*)
    echo '{"hosts":{"github.com":[{"login":"tester","active":true,"state":"success"}]}}'
    exit 0 ;;
  "repo set-default --view"*) exit 1 ;;
  "repo set-default"*) exit 0 ;;
  "repo view --json nameWithOwner"*)
    echo '{"nameWithOwner":"@FORK@"}'
    exit 0 ;;
  "pr view --json"*)
    echo "no pull requests found for branch" >&2
    exit 1 ;;
  "pr view @PR@"*)
    cat <<'EOF'
{"number": @PR@, "title": "@TITLE@", "state": "OPEN",
 "url": "https://github.com/@UPSTREAM@/pull/@PR@", "isDraft": false,
 "author": {"login": "octocat"}, "baseRefName": "main",
 "headRefName": "cross-fork-feature-remote", "statusCheckRollup": [],
 "body": "Adds the feature via a fork.", "comments": []}
EOF
    exit 0 ;;
  "pr diff @PR@"*)
    cat <<'EOF'
diff --git a/src/feature.py b/src/feature.py
index 0000000..4b825dc 100644
--- a/src/feature.py
+++ b/src/feature.py
@@ -0,0 +1 @@
+feature
EOF
    exit 0 ;;
  "api repos/@FORK@/commits/"*)
    echo "[]"
    exit 0 ;;
  "api repos/@UPSTREAM@/commits/"*)
    echo '[{"number": @PR@, "state": "open", "base": {"repo": {"full_name": "@UPSTREAM@"}}}]'
    exit 0 ;;
  "api --paginate "*"pulls/@PR@/files"*)
    echo '[{"filename": "src/feature.py", "status": "added", "additions": 1, "deletions": 0}]'
    exit 0 ;;
  *)
    echo "fake gh: unhandled: $args" >&2
    exit 1 ;;
esac
"""


def _fake_gh_script() -> str:
    """Render the fake ``gh`` script with the scenario's constants inlined."""
    return (
        _FAKE_GH_TEMPLATE.replace("@FORK@", _FORK_NWO)
        .replace("@UPSTREAM@", _UPSTREAM_NWO)
        .replace("@PR@", str(_PR_NUMBER))
        .replace("@TITLE@", _PR_TITLE)
    )


def _git(argv: list[str], cwd: Path) -> None:
    """Run git with a dummy identity so commits need no configured user."""
    env = {
        **os.environ,
        "GIT_AUTHOR_NAME": "Test",
        "GIT_AUTHOR_EMAIL": "test@example.com",
        "GIT_COMMITTER_NAME": "Test",
        "GIT_COMMITTER_EMAIL": "test@example.com",
    }
    subprocess.run(["git", *argv], cwd=cwd, check=True, capture_output=True, env=env)


def _make_triangular_repo(path: Path) -> Path:
    """A checkout with ``origin`` -> the push fork and ``upstream`` -> the base repo.

    The feature branch has no upstream tracking and no remote branch of its own
    name — the stacking-tool push shape the bug report describes — so the PR can
    only be resolved by pushed-commit identity.
    """
    path.mkdir(parents=True)
    _git(["init", "-q", "-b", "main"], path)
    (path / "README.md").write_text("base\n")
    _git(["add", "."], path)
    _git(["commit", "-qm", "base"], path)
    _git(["checkout", "-qb", _BRANCH], path)
    (path / "src").mkdir()
    (path / "src" / "feature.py").write_text("feature\n")
    _git(["add", "."], path)
    _git(["commit", "-qm", "feature"], path)
    _git(["remote", "add", "origin", f"https://github.com/{_FORK_NWO}.git"], path)
    _git(["remote", "add", "upstream", f"https://github.com/{_UPSTREAM_NWO}.git"], path)
    return path


def _spawn_fake_gh_runner(
    base_url: str,
    fake_gh_dir: Path,
    runner_tmp: Path,
    mock_llm_url: str,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn a per-test runner whose ``PATH`` resolves ``gh`` to the fake.

    Mirrors the conftest's runner-respawn plumbing (token-bound id + WS tunnel
    into the live server, accepted via the loopback fallback), but with the
    fake-``gh`` directory prepended to ``PATH`` so the GitHub resource layer's
    ``gh`` calls hit the emulation instead of the real CLI.
    """
    from omnigent.runner.identity import token_bound_runner_id

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    log_path = runner_tmp / "runner.log"

    env = {
        **os.environ,
        "PATH": f"{fake_gh_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "OPENAI_BASE_URL": f"{mock_llm_url}/v1",
        "OPENAI_API_KEY": "mock-key",
        "FAKE_GH_LOG": str(runner_tmp / "gh_calls.log"),
    }
    # A runner-wide workspace or leaked zygote/host plumbing would override the
    # agent spec's cwd (per compute_default_env_root) or hang the child; strip
    # them so the session's workspace is the triangular repo.
    for leaked in list(env):
        if leaked.startswith("OMNIGENT_RUNNER_ZYGOTE"):
            env.pop(leaked)
    for leaked in (
        "OMNIGENT_RUNNER_WORKSPACE",
        "OMNIGENT_RUNNER_ISOLATE_SESSION",
        "OMNIGENT_HOST_ID",
        "OMNIGENT_HOST_TOKEN",
        "OMNIGENT_HOST_NAME",
        "OMNIGENT_REMOTE_AUTH_TOKEN",
    ):
        env.pop(leaked, None)

    log_handle = open(log_path, "w")  # noqa: SIM115 — child holds a dup; closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # the child keeps its own dup of the fd

    deadline = time.monotonic() + _RUNNER_ONLINE_TIMEOUT_S
    last_error = "not polled yet"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            last_error = f"runner exited early with code {proc.returncode}"
            break
        try:
            resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
            if resp.status_code == 200 and resp.json().get("online") is True:
                return proc, runner_id, log_path
            last_error = f"runner status HTTP {resp.status_code}: {resp.text[:200]}"
        except httpx.HTTPError as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(_RUNNER_POLL_INTERVAL_S)

    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
    log_text = log_path.read_text() if log_path.exists() else ""
    raise RuntimeError(
        f"fake-gh runner did not come online within {_RUNNER_ONLINE_TIMEOUT_S:.0f}s "
        f"(last_error={last_error}).\nRunner log:\n{log_text[-3000:]}"
    )


def _create_probe_session(base_url: str, runner_id: str, repo_path: Path) -> str:
    """Create a session whose workspace is the triangular repo, bound to the runner.

    The agent spec pins ``os_env.cwd`` to the repo so the runner's GitHub
    resource routes resolve their workspace root there (the runner has no
    ``OMNIGENT_RUNNER_WORKSPACE``, so the spec's absolute cwd wins).
    """
    yaml_text = textwrap.dedent(
        f"""\
        name: github_fork_probe
        prompt: You answer questions about this workspace.

        executor:
          model: gpt-4o-mini
          harness: openai-agents

        os_env:
          type: caller_process
          cwd: {repo_path}
          sandbox:
            type: none
        """
    )
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname -> the omnigent compat translator (the spec
        # has no spec_version), matching the hello_world bundle in conftest.
        info = tarfile.TarInfo("github_fork_probe.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("github_fork_probe.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])

    patch = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch.raise_for_status()
    return session_id


@pytest.fixture
def cross_fork_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session in a triangular-fork checkout with a fake ``gh``.

    :returns: ``(base_url, session_id)``.
    """
    repo = _make_triangular_repo(tmp_path / "repo")
    fake_gh_dir = tmp_path / "fakegh"
    fake_gh_dir.mkdir()
    gh_path = fake_gh_dir / "gh"
    gh_path.write_text(_fake_gh_script())
    gh_path.chmod(0o755)

    proc, runner_id, _log = _spawn_fake_gh_runner(
        live_server, fake_gh_dir, tmp_path, mock_llm_server_url
    )
    session_id: str | None = None
    try:
        session_id = _create_probe_session(live_server, runner_id, repo)
        yield live_server, session_id
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        proc.send_signal(signal.SIGTERM)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


def test_github_tab_shows_cross_fork_pr(
    page: Page,
    cross_fork_session: tuple[str, str],
) -> None:
    """The GitHub tab renders the upstream PR found by commit identity.

    On the buggy build the commit-identity fallback asks only the push fork
    (whose ``commits/{sha}/pulls`` is empty in this scenario) and the panel
    falls to the "No open PR" empty state; the PR title/number never render.
    """
    base_url, session_id = cross_fork_session
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")

    github_tab = rail.get_by_role("tab", name="GitHub")
    expect(github_tab).to_be_visible(timeout=30_000)
    github_tab.click()

    # The cross-fork PR (open into upstream) must render as the panel header.
    expect(rail.get_by_text(_PR_TITLE)).to_be_visible(timeout=30_000)
    expect(rail.get_by_text(f"#{_PR_NUMBER}")).to_be_visible()
