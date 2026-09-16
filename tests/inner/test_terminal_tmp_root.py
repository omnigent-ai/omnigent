"""Terminal sockets remain usable with long or multibyte temporary paths."""

from __future__ import annotations

import os
import shutil
import stat
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.inner import terminal as terminal_mod
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec


def _use_temp_root(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    def mkdtemp(*, prefix: str, dir: Path | None = None) -> str:
        return tempfile.mkdtemp(prefix=prefix, dir=root if dir is None else dir)

    monkeypatch.setattr(
        terminal_mod,
        "tempfile",
        SimpleNamespace(gettempdir=lambda: str(root), mkdtemp=mkdtemp),
    )


@pytest.mark.parametrize("dirname", ["short", "x" * 100, "é" * 35])
def test_terminal_root_keeps_socket_within_portable_byte_limit(
    short_tmp_parent: Path, monkeypatch: pytest.MonkeyPatch, dirname: str
) -> None:
    root = short_tmp_parent / dirname
    root.mkdir()
    _use_temp_root(monkeypatch, root)
    monkeypatch.setattr(terminal_mod, "_require_supported_tmux", lambda: None)
    result = terminal_mod.create_terminal_instance(
        name="bash",
        session_key="test",
        spec=TerminalEnvSpec(command="sh", os_env=OSEnvSpec(cwd=str(root))),
    )
    instance = result.instance
    try:
        assert len(os.fsencode(instance.socket_path)) <= 103
        assert instance.socket_path.parent == instance.private_dir
        assert stat.S_IMODE(instance.private_dir.stat().st_mode) == 0o700
        assert instance.private_dir.parent == terminal_mod._terminals_tmp_root()
        if dirname == "short":
            assert instance.private_dir.parent == root
        else:
            assert instance.private_dir.parent == Path("/tmp")
    finally:
        shutil.rmtree(instance.private_dir)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires a real tmux binary")
@pytest.mark.asyncio
async def test_native_terminal_launches_under_long_temp_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    root = tmp_path / ("long-temp-root-" * 8)
    root.mkdir()
    _use_temp_root(monkeypatch, root)
    result = terminal_mod.create_terminal_instance(
        name="bash",
        session_key="long-tmpdir",
        spec=TerminalEnvSpec(
            command="sh",
            args=["-c", "sleep 60"],
            os_env=OSEnvSpec(cwd=str(tmp_path), sandbox=OSEnvSandboxSpec(type="none")),
        ),
    )
    instance = result.instance
    try:
        await instance.launch(cwd=result.cwd)
        assert await instance.is_alive()
        assert instance.socket_path.exists()
    finally:
        await instance.close()
