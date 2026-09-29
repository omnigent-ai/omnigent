"""E2E test: runner startup with unreadable ancestor Git metadata.

A user's writable workspace can sit below a directory whose ``.git``
metadata they cannot read (for example a root-owned deploy checkout
whose ``.git`` is mode 000). Optional Git discovery in
:func:`create_filesystem_registry` must fall back to plain file-change
tracking instead of aborting runner initialization, so the runner still
comes online and sessions can start from that workspace.

The server and runner are spawned the same way the CLI launches them
(``omnigent.runner._entry`` with ``OMNIGENT_RUNNER_WORKSPACE`` pointing
at the workspace), mirroring ``test_non_git_changed_files_e2e.py``.

Usage::

    pytest tests/e2e/test_inaccessible_git_metadata_e2e.py -v
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest

from tests.e2e.conftest import find_free_port
from tests.e2e.helpers import HEALTH_TIMEOUT_S, POLL_INTERVAL_S

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Maximum seconds for the runner to come online once the server is healthy.
_RUNNER_ONLINE_TIMEOUT_S: float = 60.0

pytestmark = pytest.mark.skipif(
    os.name != "posix" or os.geteuid() == 0,
    reason="needs POSIX file permissions the current user cannot bypass",
)


@dataclass
class _RunnerUnderTest:
    """Handles the test needs to observe the spawned runner."""

    base_url: str
    runner_id: str
    proc: subprocess.Popen[bytes]
    log_path: Path


def _log_tail(path: Path, lines: int = 40) -> str:
    if not path.exists():
        return "<no log>"
    return "\n".join(path.read_text(errors="replace").splitlines()[-lines:])


@pytest.fixture()
def denied_git_workspace(tmp_path: Path) -> Iterator[Path]:
    """A writable workspace below an ancestor whose ``.git`` is unreadable.

    Layout: ``deploy-checkout/.git`` holds a minimal repository shape
    (``HEAD``, ``objects/``, ``refs/``) and is then made mode 000, as a
    checkout owned by another user would appear. The workspace
    ``deploy-checkout/project`` itself stays fully accessible.

    :returns: Path to the writable workspace directory.
    """
    checkout = tmp_path / "deploy-checkout"
    git_dir = checkout / ".git"
    (git_dir / "objects").mkdir(parents=True)
    (git_dir / "refs").mkdir()
    (git_dir / "HEAD").write_text("ref: refs/heads/main\n")
    workspace = checkout / "project"
    workspace.mkdir()
    (workspace / "notes.txt").write_text("user file\n")
    os.chmod(git_dir, 0o000)
    try:
        yield workspace
    finally:
        # Restore permissions so pytest can clean up tmp_path.
        os.chmod(git_dir, 0o700)


@pytest.fixture()
def runner_under_test(
    tmp_path: Path,
    denied_git_workspace: Path,
) -> Iterator[_RunnerUnderTest]:
    """Spawn a real server plus a CLI-style runner bound to the workspace.

    The server is started in a neutral directory; the runner is started
    exactly the way ``_start_cli_runner_process`` launches it, with
    ``OMNIGENT_RUNNER_WORKSPACE`` set to *denied_git_workspace*.

    :returns: Handles for the spawned runner and the server base URL.
    """
    from omnigent.runner.identity import token_bound_runner_id

    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    port = find_free_port()
    base_url = f"http://localhost:{port}"

    server_cwd = tmp_path / "server-cwd"
    server_cwd.mkdir()
    db_path = tmp_path / "e2e.db"
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    server_log = tmp_path / "server.log"
    runner_log = tmp_path / "runner.log"

    env: dict[str, str] = {
        **os.environ,
        "OPENAI_API_KEY": "mock-key",
        # Absolute sdks entries: the subprocesses run outside the checkout,
        # where relative PYTHONPATH entries and cwd-based imports don't resolve.
        "PYTHONPATH": os.pathsep.join(
            [
                str(_REPO_ROOT),
                str(_REPO_ROOT / "sdks" / "python-client"),
                str(_REPO_ROOT / "sdks" / "ui"),
                os.environ.get("PYTHONPATH", ""),
            ]
        ),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }

    server_fh = open(server_log, "w")  # noqa: SIM115
    server_proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token},
        cwd=str(server_cwd),
        stdout=server_fh,
        stderr=subprocess.STDOUT,
    )

    runner_fh = open(runner_log, "w")  # noqa: SIM115
    runner_proc = subprocess.Popen(
        [sys.executable, "-P", "-m", "omnigent.runner._entry"],
        env={
            **env,
            "OMNIGENT_RUNNER_ID": runner_id,
            "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
            "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
            "RUNNER_SERVER_URL": base_url,
            "OMNIGENT_RUNNER_WORKSPACE": str(denied_git_workspace),
        },
        cwd=str(denied_git_workspace),
        stdout=runner_fh,
        stderr=subprocess.STDOUT,
    )

    health_deadline = time.time() + HEALTH_TIMEOUT_S
    server_healthy = False
    while time.time() < health_deadline:
        try:
            if httpx.get(f"{base_url}/health", timeout=2).status_code == 200:
                server_healthy = True
                break
        except httpx.HTTPError:
            pass
        time.sleep(POLL_INTERVAL_S)

    try:
        if not server_healthy:
            raise RuntimeError(
                f"Server did not become healthy within {HEALTH_TIMEOUT_S}s.\n"
                f"Server log: {_log_tail(server_log)}"
            )
        yield _RunnerUnderTest(
            base_url=base_url,
            runner_id=runner_id,
            proc=runner_proc,
            log_path=runner_log,
        )
    finally:
        for proc in (runner_proc, server_proc):
            if proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc, grace in ((runner_proc, 5), (server_proc, 10)):
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        runner_fh.close()
        server_fh.close()


def test_runner_online_despite_unreadable_ancestor_git_metadata(
    runner_under_test: _RunnerUnderTest,
) -> None:
    """The runner must come online, not abort on the unreadable ancestor .git."""
    rut = runner_under_test
    deadline = time.time() + _RUNNER_ONLINE_TIMEOUT_S
    while time.time() < deadline:
        if rut.proc.poll() is not None:
            pytest.fail(
                "runner aborted during initialization (exit code "
                f"{rut.proc.returncode}) instead of falling back to plain "
                "file-change tracking for a workspace below unreadable "
                f"ancestor git metadata.\nRunner log:\n{_log_tail(rut.log_path)}"
            )
        try:
            resp = httpx.get(
                f"{rut.base_url}/v1/runners/{rut.runner_id}/status",
                timeout=2,
            )
            if resp.status_code == 200 and resp.json().get("online") is True:
                return
        except httpx.HTTPError:
            pass
        time.sleep(POLL_INTERVAL_S)
    pytest.fail(
        f"runner did not come online within {_RUNNER_ONLINE_TIMEOUT_S}s.\n"
        f"Runner log:\n{_log_tail(rut.log_path)}"
    )
