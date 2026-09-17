"""Regression: the codex-native reaper must survive non-UTF-8 ``ps`` output.

macOS ``ps`` passes raw process argv bytes through, so a running app whose
argv is not valid UTF-8 (the reported trigger: UltraEdit) makes
``ps -axww -o pid=,pgid=,command=`` emit invalid UTF-8. The codex-native
launch path calls ``reap_codex_native_processes_for_state_dir`` first, which
runs that ``ps`` with ``text=True`` (strict UTF-8 decode) and dies with
``UnicodeDecodeError`` — so the codex terminal never starts.

Linux procps-ng sanitizes argv bytes to ``?``, so this shims ``ps`` on PATH
to emit the macOS-shaped byte stream (real listing + one raw invalid-UTF-8
line), then drives the real reaper. The crashing line must not carry
``app-server`` or a state dir, so a fixed reaper leaves it untouched and
simply returns 0.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from omnigent.harnesses.codex_native import process_registry as registry

# One macOS-shaped line whose command column carries raw invalid UTF-8,
# mimicking the reporter's UltraEdit process.
_RAW_NON_UTF8_PS_LINE = (
    b"  99999 99999 /Applications/UltraEdit.app/Contents/MacOS/UltraEdit --profile\xff\xfe\n"
)


def _install_non_utf8_ps_shim(shim_dir: Path, monkeypatch) -> None:
    """
    Put a ``ps`` on PATH that appends a raw non-UTF-8 line to real output.

    :param shim_dir: Directory prepended to PATH.
    :param monkeypatch: Pytest monkeypatch used to set PATH.
    """
    real_ps = shutil.which("ps")
    assert real_ps, "real `ps` not found on PATH"
    raw_line = shim_dir / "rawline.bin"
    raw_line.write_bytes(_RAW_NON_UTF8_PS_LINE)
    shim = shim_dir / "ps"
    shim.write_text(f'#!/bin/sh\n"{real_ps}" "$@"\ncat "{raw_line}"\n', encoding="utf-8")
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")


def test_reaper_survives_non_utf8_ps_output(tmp_path: Path, monkeypatch) -> None:
    """
    The reaper decodes non-UTF-8 ``ps`` output instead of crashing.

    Drives the real ``reap_codex_native_processes_for_state_dir`` while ``ps``
    emits invalid UTF-8 (macOS/UltraEdit condition). On the buggy code this
    raises ``UnicodeDecodeError`` (blocking codex-native launch); once decode
    is tolerant it returns 0 because the crafted line matches nothing.
    """
    if os.name != "posix":
        import pytest

        pytest.skip("reaper is a no-op off POSIX")

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _install_non_utf8_ps_shim(shim_dir, monkeypatch)

    state_dir = tmp_path / "deadbeefdeadbeefdeadbeefdeadbeef"
    reaped = registry.reap_codex_native_processes_for_state_dir(state_dir, grace_s=0.2)
    assert reaped == 0


def test_process_cmdline_ps_fallback_survives_non_utf8_output(tmp_path: Path, monkeypatch) -> None:
    """
    The ``ps -p`` cmdline fallback tolerates non-UTF-8 output too.

    ``_process_cmdline`` falls back to ``ps -p <pid>`` when ``/proc`` has no
    entry; the same strict decode crashed there. A nonexistent pid forces the
    fallback, and the shim makes even that ``ps`` emit invalid UTF-8: the
    lookup must decode it leniently instead of raising UnicodeDecodeError.
    """
    if os.name != "posix":
        import pytest

        pytest.skip("cmdline lookup uses POSIX ps")

    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    _install_non_utf8_ps_shim(shim_dir, monkeypatch)

    # Above Linux's default pid_max, so /proc/<pid>/cmdline cannot exist.
    cmdline = registry._process_cmdline(4_999_999)
    assert "UltraEdit" in cmdline
