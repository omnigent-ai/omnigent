"""Unknown terminal ownership must not cause cleanup of a live tmux server."""

from __future__ import annotations

import asyncio
import contextlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

import omnigent.inner.terminal as terminal_mod
from omnigent.inner.terminal import TerminalInstance
from omnigent.native import owner_claim


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires a real tmux binary")
@pytest.mark.parametrize("ownership", ["legacy", "foreign", "unreadable", "dead-local"])
@pytest.mark.asyncio
async def test_orphan_sweep_uses_resolvable_ownership(
    monkeypatch: pytest.MonkeyPatch, ownership: str
) -> None:
    """Run the production sweep against an isolated, live terminal socket."""
    # Keep the tmux socket below the Unix socket path-length limit.
    with tempfile.TemporaryDirectory(prefix="og-sweep-") as scratch_name:
        scratch = Path(scratch_name)
        monkeypatch.setattr(terminal_mod, "_terminals_tmp_root", lambda: scratch)
        instance_dir = scratch / "omnigent-terminal-live"
        instance_dir.mkdir()
        socket_path = instance_dir / "tmux.sock"
        instance = TerminalInstance(
            name="claude",
            session_key="main",
            socket_path=socket_path,
            private_dir=instance_dir,
            command="sh",
            args=["-c", "sleep 600"],
            keep_alive_after_exit=True,
        )

        def reachable() -> bool:
            return (
                subprocess.run(
                    ["tmux", "-S", str(socket_path), "has-session", "-t", instance.tmux_target],
                    capture_output=True,
                    timeout=5,
                ).returncode
                == 0
            )

        try:
            await instance.launch(cwd=instance_dir)
            async with asyncio.timeout(5):
                while not reachable():
                    await asyncio.sleep(0.02)

            child = subprocess.Popen(["sh", "-c", "exit 0"])
            child.wait(timeout=5)
            assert not terminal_mod._process_alive(child.pid)
            marker = str(child.pid)
            if ownership != "legacy":
                namespace = (
                    "pid:[foreign]"
                    if ownership == "foreign"
                    else owner_claim.current_pid_namespace()
                )
                marker += f"\npid_ns={namespace}\nboot={owner_claim.current_boot_id() or ''}\n"
            if ownership == "unreadable":
                monkeypatch.setattr(owner_claim, "current_pid_namespace", lambda: None)
            (instance_dir / "owner.pid").write_text(marker, encoding="utf-8")

            if ownership == "dead-local":
                assert terminal_mod.reap_orphaned_terminals() == 1
                assert not reachable()
                assert not instance_dir.exists()
            else:
                assert terminal_mod.reap_orphaned_terminals() == 0
                assert reachable()
                assert instance_dir.exists()
        finally:
            with contextlib.suppress(Exception):
                await instance.close()
