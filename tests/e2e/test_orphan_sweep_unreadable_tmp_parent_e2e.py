"""A real server must boot despite unreadable harness-sweep paths.

Mode 0333 blocks parent enumeration; mode 0000 blocks sibling inspection.
The sibling case also verifies that a readable dead orphan is removed.
These POSIX permission tests skip on Windows and when run as root."""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# A failing boot dies in a few seconds; a healthy boot serves /health well
# under this ceiling even on a contended CI box.
_HEALTH_TIMEOUT_S = 60.0
_POLL_INTERVAL_S = 0.2

pytestmark = [
    pytest.mark.skipif(
        sys.platform == "win32",
        reason="POSIX permission-bit staging; Windows ACLs need a different setup.",
    ),
    pytest.mark.skipif(
        hasattr(os, "geteuid") and os.geteuid() == 0,
        reason="root bypasses permission bits, so the unreadable staging is inert.",
    ),
]


def _find_free_port() -> int:
    """Pick a free TCP port for the server subprocess to bind."""
    s = socket.socket()
    s.bind(("", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def _server_env(tmp_parent: Path, home: Path) -> dict[str, str]:
    """Isolate server configuration and force imports from this worktree.

    :param tmp_parent: Harness root containing the staged permission failure.
    :param home: Scratch home for server state.
    :returns: Child environment with ambient runtime bindings removed."""
    env = {
        key: os.environ[key]
        for key in ("PATH", "TMPDIR", "LANG", "LC_ALL", "LC_CTYPE")
        if key in os.environ
    }
    env["HOME"] = str(home)
    env["PYTHONPATH"] = str(_REPO_ROOT)
    # Startup constructs an LLM client; a stub satisfies the env check.
    env["OPENAI_API_KEY"] = "stub-not-used"
    env["OPENAI_BASE_URL"] = "http://127.0.0.1:9/v1"
    env["OMNIGENT_HARNESS_TMP_PARENT"] = str(tmp_parent)
    return env


def _boot_server_and_wait_health(
    tmp_parent: Path, tmp_path: Path
) -> Iterator[tuple[subprocess.Popen[bytes], str, Path]]:
    """Boot the real CLI server and yield its health outcome with automatic teardown.

    :param tmp_parent: Staged harness root.
    :param tmp_path: Scratch directory for database, artifacts and logs.
    :yields: Process, healthy/exited/timeout outcome, and captured log path."""
    port = _find_free_port()
    artifact_dir = tmp_path / "artifacts"
    artifact_dir.mkdir()
    home_dir = tmp_path / "home"
    home_dir.mkdir()
    log_path = tmp_path / "server.log"
    log_handle = open(log_path, "w")  # noqa: SIM115 — subprocess holds the FD
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'e2e.db'}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=_server_env(tmp_parent, home_dir),
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    outcome = "timeout"
    try:
        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                outcome = "exited"
                break
            try:
                # trust_env=False: CI shells force an HTTP proxy that
                # intercepts even localhost, failing the probe spuriously.
                resp = httpx.get(
                    f"http://127.0.0.1:{port}/health",
                    timeout=2.0,
                    trust_env=False,
                )
                if resp.status_code == 200:
                    outcome = "healthy"
                    break
            except httpx.HTTPError:
                pass
            time.sleep(_POLL_INTERVAL_S)
        yield proc, outcome, log_path
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10.0)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5.0)
        log_handle.close()


def _assert_boot_survived(outcome: str, log_path: Path) -> None:
    """Require a healthy server, including its log on failure."""
    if outcome == "healthy":
        return
    log_text = log_path.read_text() if log_path.exists() else ""
    pytest.fail(
        "omnigent server boot did not survive the orphan sweep over an "
        f"unreadable harness tmp parent scope (outcome={outcome}). The sweep "
        "must warn and skip inaccessible entries, not abort startup.\n"
        f"Server log tail:\n{log_text[-4000:]}"
    )


def test_server_boot_survives_unlistable_tmp_parent(tmp_path: Path) -> None:
    """An unlistable but writable harness parent must not prevent server startup."""
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    (parent / "ap-foreign").mkdir(mode=0o700)
    parent.chmod(0o333)  # creatable but not listable
    boot = _boot_server_and_wait_health(parent, tmp_path)
    try:
        _proc, outcome, log_path = next(boot)
        _assert_boot_survived(outcome, log_path)
    finally:
        boot.close()
        # Restore modes so pytest's tmp_path cleanup can remove the tree.
        parent.chmod(0o700)


def test_server_boot_survives_inaccessible_ap_sibling(tmp_path: Path) -> None:
    """Skip an inaccessible sibling while still removing a readable dead orphan."""
    parent = tmp_path / "shared"
    parent.mkdir(mode=0o700)
    blocked = parent / "ap-foreign"
    blocked.mkdir(mode=0o700)
    (blocked / "AP_PID").write_text("999999", encoding="utf-8")
    blocked.chmod(0o000)  # inaccessible sibling: sentinel probes raise
    # A readable dead orphan: sorts after "ap-foreign" so the sweep hits the
    # blocked sibling first on ordered filesystems; PID 2**22+5 is above
    # every real pid_max default, so it is reliably not alive.
    dead = parent / "ap-orphan-dead"
    dead.mkdir(mode=0o700)
    (dead / "AP_PID").write_text(str(2**22 + 5), encoding="utf-8")
    boot = _boot_server_and_wait_health(parent, tmp_path)
    try:
        _proc, outcome, log_path = next(boot)
        _assert_boot_survived(outcome, log_path)
        # Best-effort contract's other half: the readable dead orphan was
        # cleaned even though a sibling was unreadable.
        assert not dead.exists(), (
            "readable dead orphan dir survived the sweep; skipping the "
            "unreadable sibling must not skip readable cleanup"
        )
        # The unreadable sibling itself is preserved (we cannot prove it is
        # dead), not clobbered.
        assert blocked.exists()
    finally:
        boot.close()
        blocked.chmod(0o700)
