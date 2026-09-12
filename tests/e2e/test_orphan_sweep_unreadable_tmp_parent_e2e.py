"""
End-to-end regression: the boot-time orphan sweep must not abort
``omnigent server`` startup when the harness tmp parent (or an entry
inside it) is unreadable.

``HarnessProcessManager._sweep_orphans()`` documents best-effort cleanup:
an inaccessible path should log and skip rather than abort runner boot.
Without ``OSError`` guards on the metadata operations around the sentinel
read — ``self._tmp_parent.exists()`` / ``iterdir()`` and ``child.is_dir()``
/ ``sentinel.exists()`` — a ``PermissionError`` propagates straight out of
``start()``, which the server lifespan awaits, so uvicorn logs
"Application startup failed. Exiting." and the whole ``omnigent server``
process dies.

The user journey guarded here (both facets):

1. operator points ``OMNIGENT_HARNESS_TMP_PARENT`` at a shared directory
   whose contents they cannot fully read (another Unix user's entries, a
   broken ACL, a filesystem race);
2. operator runs ``omnigent server``;
3. boot crashes with ``PermissionError`` from ``_sweep_orphans`` instead
   of warning, skipping the unreadable scope, and serving.

Two staged filesystem shapes cover the two unguarded surfaces:

- **unlistable parent** — the parent is mode ``0o333`` (creatable but not
  listable, exactly what a non-owner sees on a ``0o700``-style shared
  parent); ``iterdir()`` raises.
- **inaccessible child** — the parent is listable but contains a mode
  ``0o000`` ``ap-*`` sibling; ``(child / "AP_PID").exists()`` raises.

Each test boots a REAL ``omnigent server`` subprocess through the CLI (the
same command a user runs) and asserts the server reaches ``/health``.
Without the sweep's ``OSError`` guards both tests fail with the process
exiting early and the ``PermissionError`` traceback in its log. The
child-facet test additionally asserts that a readable dead orphan sibling
still gets swept, guarding the "readable dead-orphan cleanup remains
unchanged" clause of the expected behavior.

Permission-bit staging is meaningless as root or on Windows, hence the
skips.
"""

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
    """
    Environment for the ``omnigent server`` subprocess.

    Points the harness tmp parent at the staged directory, forces imports
    from this worktree, and strips every ambient ``OMNIGENT_*`` var so a
    test run that is itself hosted inside an Omnigent runner doesn't leak
    zygote fds, tunnel tokens, or log/config/data paths into the child
    server. ``HOME`` is redirected to a scratch dir so the child's default
    config and log locations stay test-local.

    :param tmp_parent: Value for ``OMNIGENT_HARNESS_TMP_PARENT``.
    :param home: Scratch ``HOME`` for the child server.
    :returns: The subprocess environment mapping.
    """
    env = {k: v for k, v in os.environ.items() if not k.startswith("OMNIGENT_")}
    env.pop("RUNNER_SERVER_URL", None)
    for var in ("DATABRICKS_TOKEN", "ANTHROPIC_API_KEY", "CODEX", "CLAUDE_CODE"):
        env.pop(var, None)
    env["HOME"] = str(home)
    env["PYTHONPATH"] = f"{_REPO_ROOT}{os.pathsep}{env.get('PYTHONPATH', '')}"
    # Startup constructs an LLM client; a stub satisfies the env check.
    env["OPENAI_API_KEY"] = "stub-not-used"
    env["OMNIGENT_HARNESS_TMP_PARENT"] = str(tmp_parent)
    return env


def _boot_server_and_wait_health(
    tmp_parent: Path, tmp_path: Path
) -> Iterator[tuple[subprocess.Popen[bytes], str, Path]]:
    """
    Start ``omnigent server`` with *tmp_parent* configured and wait for
    ``/health``.

    Yields once with ``(proc, outcome, log_path)`` where *outcome* is
    ``"healthy"``, ``"exited"`` (the startup abort this bug produces), or
    ``"timeout"``. Generator form so callers get teardown via ``finally``.

    :param tmp_parent: The staged harness tmp parent.
    :param tmp_path: Per-test scratch dir for db/artifacts/log.
    """
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
    """
    Fail with the server log's abort evidence when boot did not survive.

    :param outcome: ``"healthy"`` / ``"exited"`` / ``"timeout"`` from
        :func:`_boot_server_and_wait_health`.
    :param log_path: The captured server stdout/stderr log.
    """
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
    """
    Facet 1 — parent enumeration: an ``OMNIGENT_HARNESS_TMP_PARENT`` that
    exists but cannot be listed (mode ``0o333``, the non-owner view of a
    shared parent) must not abort ``omnigent server`` startup.

    Without the guard, ``_sweep_orphans``'s ``self._tmp_parent.iterdir()``
    raises ``PermissionError``, the server lifespan aborts, and the
    process exits before ever serving ``/health``.
    """
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
    """
    Facet 2 — child inspection: a listable parent containing a mode
    ``0o000`` ``ap-*`` sibling (another user's instance dir, a broken ACL)
    must not abort startup, and a READABLE dead orphan alongside it must
    still get swept.

    Without the guard, ``(child / "AP_PID").exists()`` raises
    ``PermissionError`` on the inaccessible sibling and boot dies before
    the readable orphan is even considered.
    """
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
