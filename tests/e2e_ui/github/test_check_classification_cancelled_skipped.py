"""E2E: cancelled/skipped GitHub checks must not be miscounted as failed/passed.

The GitHub rail's Summary tab renders check pills from the runner's
``_classify_check`` (``omnigent/runner/github_resource.py``), which shells out
to ``gh pr view --json ... statusCheckRollup``. A ``CANCELLED`` CheckRun must
not be bucketed as *failing* nor a ``SKIPPED`` one as *passing*; each gets its
own pill, excluded from the passed and failed counts.

This drives the real journey end to end: a self-spawned server + runner whose
PATH carries a stubbed ``gh`` printing gh's canonical ``pr view --json`` rollup
(one success, one failure, one cancelled, one skipped, plus a legacy status
context) executes the *real* classification, then Playwright opens the Summary
tab and reads the pills.
"""

from __future__ import annotations

import gzip
import io
import json
import os
import re
import secrets
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import apply_server_env, compat_server_cwd, server_executable
from tests.e2e_ui.conftest import open_right_rail

_REPO_ROOT = Path(__file__).resolve().parents[3]
_BUILD_OUTPUT = _REPO_ROOT / "omnigent" / "server" / "static" / "web-ui"
_HEALTH_TIMEOUT_S = 60.0
_POLL_S = 0.5

# Minimal agent whose workspace is the git repo the GitHub routes read from.
# No turn ever runs, so the model/harness are never exercised.
_AGENT_YAML = """\
name: {name}
prompt: |
  Deterministic test assistant.

executor:
  model: {model}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""

# Stands in for the gh CLI on the runner's PATH. Only ``gh pr view --json`` is
# meaningful to the Summary route; it prints one CheckRun per bucket the ticket
# cares about. Every other gh subcommand no-ops so the resource resolver's
# incidental probes stay harmless.
_PR_VIEW_JSON = json.dumps(
    {
        "number": 7738,
        "title": "Opt-in per-session SSE event log files",
        "state": "OPEN",
        "url": "https://github.com/example/project/pull/7738",
        "isDraft": False,
        "author": {"login": "octocat"},
        "baseRefName": "main",
        "headRefName": "feature/sse-log",
        "statusCheckRollup": [
            {
                "__typename": "CheckRun",
                "name": "unit",
                "status": "COMPLETED",
                "conclusion": "SUCCESS",
                "detailsUrl": "https://github.com/example/project/runs/1",
            },
            {
                "__typename": "CheckRun",
                "name": "e2e",
                "status": "COMPLETED",
                "conclusion": "FAILURE",
                "detailsUrl": "https://github.com/example/project/runs/2",
            },
            {
                "__typename": "CheckRun",
                "name": "deploy",
                "status": "COMPLETED",
                "conclusion": "CANCELLED",
                "detailsUrl": "https://github.com/example/project/runs/3",
            },
            {
                "__typename": "CheckRun",
                "name": "docs",
                "status": "COMPLETED",
                "conclusion": "SKIPPED",
                "detailsUrl": "https://github.com/example/project/runs/4",
            },
            {
                "__typename": "StatusContext",
                "context": "legacy-status",
                "state": "SUCCESS",
                "targetUrl": "https://github.com/example/project/statuses/5",
            },
        ],
        "body": "## Summary\n\nExample PR with mixed check outcomes.",
        "comments": [],
    }
)

_GH_STUB = f"""\
#!/bin/sh
if [ "$1" = "pr" ] && [ "$2" = "view" ]; then
  cat <<'OMNIGENT_GH_JSON'
{_PR_VIEW_JSON}
OMNIGENT_GH_JSON
  exit 0
fi
exit 0
"""


def _git(*args: str) -> None:
    subprocess.run(["git", *args], check=True, capture_output=True)


def _agent_bundle(name: str, model: str, cwd: str) -> bytes:
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


def _free_port() -> int:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


# Ambient runner/host env leaks send a spawned runner down the zygote-fork path
# where it blocks on control FDs it doesn't have; strip them so the child boots
# clean (see dev/recording-lanes.md).
_LEAKED_ENV = (
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "OMNIGENT_RUNNER_PARENT_PID",
    "OMNIGENT_RUNNER_ISOLATE_SESSION",
    "OMNIGENT_RUNNER_WORKSPACE",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
    "OMNIGENT_HOST_NAME",
    "RUNNER_SERVER_URL",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
)


def _clean_env() -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_RUNNER_ZYGOTE")}
    for key in _LEAKED_ENV:
        env.pop(key, None)
    return env


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
def github_checks_session() -> Iterator[tuple[str, str]]:
    """A live server + runner (stubbed ``gh`` on PATH) bound to a git workspace."""
    from omnigent.runner.identity import token_bound_runner_id

    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-gh-checks-"))
    stub_bin = ws / "stub-bin"
    stub_bin.mkdir()
    (stub_bin / "gh").write_text(_GH_STUB)
    (stub_bin / "gh").chmod(0o755)
    worktree = ws / "worktree"
    _git("init", "-q", "-b", "feature/sse-log", str(worktree))
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

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = ws / "server.db"
    artifact_dir = ws / "artifacts"
    artifact_dir.mkdir()
    server_log = ws / "server.log"
    runner_log = ws / "runner.log"

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    # No OMNIGENT_RUNNER_TUNNEL_TOKEN on the server -> it accepts any runner whose
    # id matches token_bound_runner_id(its binding token), which this runner is.
    server_env = _clean_env()
    server_env["OMNIGENT_WEB_UI_DIST"] = str(_BUILD_OUTPUT)
    apply_server_env(server_env, _REPO_ROOT)

    runner_env = _clean_env()
    runner_env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}"
    runner_env["OMNIGENT_RUNNER_ID"] = runner_id
    runner_env["OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN"] = binding_token
    runner_env["OMNIGENT_RUNNER_PARENT_PID"] = str(os.getpid())
    runner_env["RUNNER_SERVER_URL"] = base_url
    runner_env["PATH"] = f"{stub_bin}{os.pathsep}{runner_env.get('PATH', '')}"
    runner_env.setdefault("OPENAI_API_KEY", "mock-key")

    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    server_handle = open(server_log, "w")  # noqa: SIM115
    runner_handle = open(runner_log, "w")  # noqa: SIM115
    session_id = ""
    try:
        server_proc = subprocess.Popen(
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
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(artifact_dir),
            ],
            env=server_env,
            cwd=compat_server_cwd(),
            stdout=server_handle,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        last = "not polled"
        while time.monotonic() < deadline:
            if server_proc.poll() is not None:
                raise RuntimeError(f"server exited early ({server_proc.returncode})")
            if runner_proc.poll() is not None:
                raise RuntimeError(
                    f"runner exited early ({runner_proc.returncode}); "
                    f"log:\n{runner_log.read_text()[-2000:]}"
                )
            try:
                health = httpx.get(f"{base_url}/health", timeout=2)
                if health.status_code == 200:
                    status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online") is True:
                        online = True
                        break
                    last = f"runner status {status.status_code}: {status.text[:150]}"
            except httpx.HTTPError as exc:
                last = f"{type(exc).__name__}: {exc}"
            time.sleep(_POLL_S)
        if not online:
            raise RuntimeError(
                f"server+runner not ready within {_HEALTH_TIMEOUT_S:.0f}s ({last}); "
                f"server log:\n{server_log.read_text()[-2000:]}"
            )

        name = f"gh_checks_{secrets.token_hex(4)}"
        model = f"gh-checks-{secrets.token_hex(4)}"
        create = httpx.post(
            f"{base_url}/v1/sessions",
            data={"metadata": json.dumps({})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    _agent_bundle(name, model, str(worktree)),
                    "application/gzip",
                )
            },
            timeout=30.0,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json={"runner_id": runner_id},
            timeout=10.0,
        ).raise_for_status()

        yield base_url, session_id
    finally:
        if session_id:
            try:
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
            except httpx.HTTPError:
                pass
        _terminate(runner_proc)
        _terminate(server_proc)
        runner_handle.close()
        server_handle.close()
        shutil.rmtree(ws, ignore_errors=True)


def test_cancelled_and_skipped_checks_are_not_counted_as_failed_or_passed(
    page: Page,
    github_checks_session: tuple[str, str],
) -> None:
    base_url, session_id = github_checks_session
    page.goto(f"{base_url}/c/{session_id}")

    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("tab", name="GitHub").click()

    expect(rail.get_by_text("Checks")).to_be_visible(timeout=30_000)

    # Only genuine successes count as passed (unit + legacy-status = 2); the
    # skipped "docs" job must not inflate it to 3.
    passed = rail.get_by_role("button", name=re.compile(r"\bpassed\b"))
    expect(passed).to_be_visible()
    expect(passed).to_have_text(re.compile(r"\b2\s*passed\b"))

    # Only genuine failures count as failed (e2e = 1); the cancelled "deploy"
    # job must not inflate it to 2.
    failed = rail.get_by_role("button", name=re.compile(r"\bfailed\b"))
    expect(failed).to_be_visible()
    expect(failed).to_have_text(re.compile(r"\b1\s*failed\b"))

    # Cancelled and skipped surface as their own pills with the job on hover.
    cancelled = rail.get_by_role("button", name=re.compile(r"\bcancelled\b"))
    expect(cancelled).to_be_visible()
    expect(cancelled).to_have_text(re.compile(r"\b1\s*cancelled\b"))
    cancelled.hover()
    expect(page.get_by_text("deploy", exact=True)).to_be_visible()

    skipped = rail.get_by_role("button", name=re.compile(r"\bskipped\b"))
    expect(skipped).to_be_visible()
    expect(skipped).to_have_text(re.compile(r"\b1\s*skipped\b"))
    skipped.hover()
    expect(page.get_by_text("docs", exact=True)).to_be_visible()
