"""Native terminals must start even when ``$TMPDIR`` is a deeply nested path."""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

from omnigent.inner import terminal as terminal_mod
from omnigent.inner.datamodel import OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import create_terminal_instance

# Longest socket path that binds on every supported platform: macOS caps
# ``sun_path`` at 104 bytes including the NUL terminator (Linux allows 108).
_MAX_SOCKET_PATH_BYTES = 103


def _use_tmpdir(monkeypatch: pytest.MonkeyPatch, tmpdir: Path) -> None:
    """Point ``tempfile`` at ``tmpdir`` the way a runner's ``$TMPDIR`` does."""
    monkeypatch.setenv("TMPDIR", str(tmpdir))
    # gettempdir() caches its first result; drop it so the new value is read.
    monkeypatch.setattr(tempfile, "tempdir", None)


def _long_tmpdir(base: Path, segment: str = "deeply-nested-temporary-directory") -> Path:
    """Build a writable temporary directory whose path is over 120 bytes."""
    nested = base
    while len(os.fsencode(nested)) < 120:
        nested = nested / segment
    nested.mkdir(parents=True, exist_ok=True)
    return nested


def _bash_terminal_spec(cwd: Path) -> TerminalEnvSpec:
    return TerminalEnvSpec(
        command="bash",
        args=["--noprofile", "--norc"],
        os_env=OSEnvSpec(type="caller_process", cwd=str(cwd)),
    )


@pytest.mark.parametrize(
    "segment",
    ["deeply-nested-temporary-directory", "répertoire-temporaire-imbriqué"],
    ids=["ascii", "multibyte"],
)
def test_socket_path_stays_within_af_unix_limit_under_long_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, segment: str
) -> None:
    """A long ``$TMPDIR`` yields a socket path that fits ``sun_path`` in bytes,
    inside the instance dir, under the parent ``reap_orphaned_terminals`` scans."""
    monkeypatch.setattr(terminal_mod, "_require_supported_tmux", lambda: None)
    _use_tmpdir(monkeypatch, _long_tmpdir(tmp_path / "cfg", segment))

    result = create_terminal_instance(
        name="bash", session_key="s1", spec=_bash_terminal_spec(tmp_path)
    )
    instance = result.instance
    try:
        socket_bytes = len(os.fsencode(instance.socket_path))
        assert socket_bytes <= _MAX_SOCKET_PATH_BYTES, (
            f"tmux socket path is {socket_bytes} bytes, over the AF_UNIX limit "
            f"of {_MAX_SOCKET_PATH_BYTES}: {instance.socket_path}"
        )
        assert instance.socket_path.parent == instance.private_dir
        assert instance.private_dir.parent == terminal_mod._terminals_tmp_root()
    finally:
        shutil.rmtree(instance.private_dir, ignore_errors=True)


def test_short_tmpdir_keeps_configured_parent(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A short configured ``$TMPDIR`` keeps the private dir under that parent."""
    monkeypatch.setattr(terminal_mod, "_require_supported_tmux", lambda: None)
    short_tmpdir = Path(tempfile.mkdtemp(prefix="omni-short-", dir="/tmp"))
    _use_tmpdir(monkeypatch, short_tmpdir)

    result = create_terminal_instance(
        name="bash", session_key="s1", spec=_bash_terminal_spec(tmp_path)
    )
    try:
        assert result.instance.private_dir.parent == short_tmpdir
    finally:
        shutil.rmtree(result.instance.private_dir, ignore_errors=True)
        shutil.rmtree(short_tmpdir, ignore_errors=True)


@pytest.mark.skipif(shutil.which("tmux") is None, reason="requires a real tmux binary")
@pytest.mark.asyncio
async def test_native_terminal_launches_under_long_tmpdir(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A real tmux server and the clipboard bridge both bind under a long ``$TMPDIR``."""
    _use_tmpdir(monkeypatch, _long_tmpdir(tmp_path / "cfg"))

    result = create_terminal_instance(
        name="bash", session_key="s1", spec=_bash_terminal_spec(tmp_path)
    )
    instance = result.instance
    try:
        await instance.launch(cwd=result.cwd)
        assert await instance.is_alive()
    finally:
        await instance.close()
        shutil.rmtree(instance.private_dir, ignore_errors=True)
