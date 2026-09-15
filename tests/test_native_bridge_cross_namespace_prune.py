"""Native bridge orphan sweep must not reap live owners it cannot see.

``omnigent.native.native_bridge_common.prune_orphaned_dirs`` deletes a
per-session bridge dir when its ``owner.pid`` marker names a dead process. A
liveness probe is purely local (``os.kill(pid, 0)``), so when a bridge root is
shared across PID namespaces — a live session in one namespace, the startup
sweep running in another — the sweeper cannot see the live owner's PID and
must preserve the dir rather than delete bridge/config/hook state out from
under a running session. Ownership it cannot attribute to a provably-dead
local process (foreign, unreadable, or legacy bare-PID claims) must be left
untouched.

``test_prune_preserves_live_owner_in_real_foreign_pid_namespace`` exercises a
genuinely separate PID namespace via ``unshare`` and is skipped where the
kernel forbids one; the probe-patching tests model the same invisibility
portably (the marker read, liveness decision, and ``shutil.rmtree`` run for
real either way).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from omnigent.native import native_bridge_common


def _spawn_live_owner() -> subprocess.Popen[bytes]:
    """Start a genuinely-live process to stand in for a live session owner."""
    return subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])


def _terminate(owner: subprocess.Popen[bytes]) -> None:
    owner.terminate()
    try:
        owner.wait(timeout=10)
    except subprocess.TimeoutExpired:
        owner.kill()


def _foreign_pid_namespace_unavailable() -> str | None:
    """Return why a real second PID namespace cannot be created here, if it can't."""
    if not sys.platform.startswith("linux"):
        return "requires Linux PID namespaces"
    try:
        probe = subprocess.run(
            ["unshare", "--user", "--pid", "--fork", "true"],
            capture_output=True,
            timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return "unshare is unavailable"
    if probe.returncode != 0:
        return "kernel denies unprivileged PID namespaces"
    return None


def _pythonpath_env() -> dict[str, str]:
    """Propagate this process's import paths to subprocesses under test."""
    return {
        **os.environ,
        "PYTHONPATH": os.pathsep.join(entry for entry in sys.path if entry),
    }


def test_prune_preserves_live_owner_in_real_foreign_pid_namespace(
    tmp_path: Path,
) -> None:
    """A sweep in a real foreign PID namespace must preserve a live owner's dir.

    The owner process records its own ownership marker (as a harness's
    ``prepare_bridge_dir`` does) and stays alive while the sweep runs inside a
    freshly-unshared PID namespace, where the owner's PID resolves to nothing.
    On the buggy build the sweep deletes the live session's bridge state.
    """
    unavailable = _foreign_pid_namespace_unavailable()
    if unavailable:
        pytest.skip(unavailable)

    root = tmp_path / "bridge-root"
    live_dir = root / "live-native-session"
    live_dir.mkdir(parents=True)

    owner_code = (
        "import sys, time\n"
        "from pathlib import Path\n"
        "from omnigent.native import native_bridge_common\n"
        "live_dir = Path(sys.argv[1])\n"
        "native_bridge_common.write_owner_pid_marker(live_dir)\n"
        '(live_dir / "bridge.json").write_text(\n'
        '    \'{"active_session_id": "conv_live"}\', encoding="utf-8"\n'
        ")\n"
        'print("ready", flush=True)\n'
        "time.sleep(120)\n"
    )
    owner = subprocess.Popen(
        [sys.executable, "-c", owner_code, str(live_dir)],
        stdout=subprocess.PIPE,
        env=_pythonpath_env(),
    )
    try:
        assert owner.stdout is not None
        assert owner.stdout.readline().strip() == b"ready", "owner failed to start"

        sweep_code = (
            "import sys\n"
            "from pathlib import Path\n"
            "from omnigent.native import native_bridge_common\n"
            "print(native_bridge_common.prune_orphaned_dirs(Path(sys.argv[1])))\n"
        )
        sweep = subprocess.run(
            ["unshare", "--user", "--pid", "--fork", sys.executable, "-c", sweep_code, str(root)],
            capture_output=True,
            text=True,
            timeout=120,
            env=_pythonpath_env(),
        )

        assert sweep.returncode == 0, sweep.stderr
        assert owner.poll() is None, "owner exited unexpectedly during the sweep"
        assert sweep.stdout.strip() == "0", (
            "sweep in a foreign PID namespace reaped a live session's bridge dir"
        )
        assert (live_dir / "bridge.json").exists(), "live session's bridge.json was deleted"
    finally:
        _terminate(owner)


def test_prune_preserves_live_owner_invisible_in_sweeping_namespace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A live owner unseen in the sweeping namespace must not be reaped.

    Models a native session whose bridge dir sits under a bridge root shared
    across PID namespaces: the owner process is genuinely alive, but the sweep
    runs in a namespace where that PID resolves to nothing. Because the
    ``owner.pid`` marker is a bare PID with no namespace/boot identity (the
    legacy ownership format), the sweep cannot distinguish a foreign live
    owner from a locally-dead one and must preserve the dir rather than delete
    live bridge state.
    """
    root = tmp_path / "bridge-root"
    root.mkdir()

    owner = _spawn_live_owner()
    try:
        live_dir = root / "live-native-session"
        live_dir.mkdir()
        # Bare-PID marker == the legacy ownership format, naming a genuinely
        # live process. We record the live child's PID directly so the owner
        # stays alive independently of the sweeping code.
        (live_dir / native_bridge_common.OWNER_PID_FILENAME).write_text(
            str(owner.pid), encoding="utf-8"
        )
        # Bridge/config/hook state the running harness still needs to resume.
        bridge_json = live_dir / "bridge.json"
        bridge_json.write_text(
            '{"token": "fake-token", "active_session_id": "conv_live"}', encoding="utf-8"
        )
        (live_dir / "config.json").write_text('{"mcp": "..."}', encoding="utf-8")

        # Sanity: in the owner's own namespace the sweep leaves the dir alone.
        assert native_bridge_common.prune_orphaned_dirs(root) == 0
        assert live_dir.is_dir()
        assert owner.poll() is None

        # Model the sweeping namespace: the live owner's PID is not visible
        # here, so the local liveness probe reports it absent. This is exactly
        # what a foreign PID namespace (or a sweeper that cannot read its own
        # namespace) produces. prune imports _process_alive lazily from
        # inner.terminal, so patch it at the source.
        monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)

        pruned = native_bridge_common.prune_orphaned_dirs(root)

        # The owner is still running: deleting its bridge state is data loss.
        assert owner.poll() is None, "owner exited unexpectedly during the sweep"
        assert pruned == 0, (
            "sweep reaped a live session's bridge dir whose owner is merely "
            "invisible in the sweeping PID namespace"
        )
        assert live_dir.is_dir(), "live session's bridge dir was deleted"
        assert bridge_json.exists(), "live session's bridge.json was deleted"
    finally:
        _terminate(owner)


def test_prune_leaves_unmarked_dirs_when_owner_is_unresolvable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A dir with no attributable dead-local owner is never reaped.

    The conservative-direction invariant, stable on both the buggy and fixed
    builds: an unmarked bridge dir is left untouched even when the local
    liveness probe reports absent for every PID (as it does from a foreign
    sweeping namespace). This guards against a fix that becomes over-eager in
    the other direction.
    """
    root = tmp_path / "bridge-root"
    root.mkdir()
    unmarked_dir = root / "unmarked-native-session"
    unmarked_dir.mkdir()
    (unmarked_dir / "bridge.json").write_text('{"token": "keep"}', encoding="utf-8")

    monkeypatch.setattr("omnigent.inner.terminal._process_alive", lambda _pid: False)

    assert native_bridge_common.prune_orphaned_dirs(root) == 0
    assert unmarked_dir.is_dir()
    assert (unmarked_dir / "bridge.json").exists()
