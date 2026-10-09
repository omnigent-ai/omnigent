"""Checks that the claude-sdk seatbelt profile write-grants the Claude CLI's fixed
``/tmp``-anchored runtime dir when the system temp dir differs from it (macOS:
``/var/folders/.../T``). Runs on Linux with a matcher that canonicalises ``/tmp``
and ``/var`` like the macOS kernel does before comparing ``subpath`` rules.
"""

from __future__ import annotations

# Warm the darwin-branching stdlib import before ``sys.platform`` is patched to
# "darwin" below, so nothing re-imports it through the macOS-only branch.
import ctypes.util  # noqa: F401
import os
import re
import tempfile
from pathlib import Path
from unittest import mock

import pytest

import omnigent.inner.seatbelt_sandbox as sb
from omnigent._platform import stable_user_id
from omnigent.inner.claude_sdk_executor import (
    _claude_internal_write_files,
    _claude_internal_write_roots,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.sandbox import (
    create_private_tmpdir,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
)
from omnigent.inner.seatbelt_sandbox import SeatbeltSandboxBackend, _build_profile

pytestmark = pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="the Claude CLI's per-uid /tmp/claude-<uid> runtime dir is POSIX-only",
)

_WRITE_ALLOW_RE = re.compile(r'\(allow file-write\* \((subpath|literal) "((?:[^"\\]|\\.)*)"\)\)')


def _macos_canonical(path: str) -> str:
    """``/tmp``, ``/var`` and ``/etc`` are symlinks into ``/private`` on macOS."""
    for prefix in ("/tmp", "/var", "/etc"):
        if path == prefix or path.startswith(prefix + "/"):
            return "/private" + path
    return path


def _write_allows(profile: str) -> list[tuple[str, str]]:
    return [
        (kind, raw.replace('\\"', '"').replace("\\\\", "\\"))
        for kind, raw in _WRITE_ALLOW_RE.findall(profile)
    ]


def _kernel_write_allowed(profile: str, target: str) -> bool:
    """Model the deny-default kernel: does any ``file-write*`` allow cover *target*?"""
    canonical_target = _macos_canonical(target)
    for kind, path in _write_allows(profile):
        canonical = _macos_canonical(path).rstrip("/")
        if canonical_target == canonical:
            return True
        if kind == "subpath" and canonical_target.startswith(canonical + "/"):
            return True
    return False


@pytest.fixture
def macos_shaped_tmpdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``tempfile.gettempdir()`` a per-user ``/var/folders``-style dir."""
    per_user_tmp = tmp_path / "var" / "folders" / "zz" / "zzabc123def" / "T"
    per_user_tmp.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(per_user_tmp))
    for key in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
        monkeypatch.setenv(key, str(per_user_tmp))
    return per_user_tmp


def test_seatbelt_profile_grants_claude_cli_tmp_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, macos_shaped_tmpdir: Path
) -> None:
    """Resolve the ``darwin_seatbelt`` policy as ``prepare_claude_cli_path`` does,
    add the CLI grants plus the launcher's scratch tmpdir, and check the rendered
    profile covers the CLI's runtime dir."""
    # Keep the ~/.claude and ~/.npm grants the helper creates out of the real home,
    # and stand in for the CLI's /tmp root with a test-owned dir.
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    cli_tmp_root = tmp_path / "tmp"
    cli_tmp_root.mkdir()
    cli_runtime_dir = cli_tmp_root / f"claude-{stable_user_id()}"

    cwd = (tmp_path / "workspace").resolve()
    cwd.mkdir()
    spec = OSEnvSpec(
        type="caller_process",
        cwd=str(cwd),
        sandbox=OSEnvSandboxSpec(type="darwin_seatbelt", write_paths=["."], allow_network=True),
    )
    with (
        mock.patch.object(sb.sys, "platform", "darwin"),
        mock.patch.object(sb.shutil, "which", return_value="/usr/bin/sandbox-exec"),
    ):
        sandbox = SeatbeltSandboxBackend().resolve(spec, cwd)

    with mock.patch(
        "omnigent.inner.claude_sdk_executor._CLAUDE_CLI_TMP_ROOT", cli_tmp_root, create=True
    ):
        claude_roots = _claude_internal_write_roots()
    sandbox = with_additional_read_roots(sandbox, claude_roots)
    sandbox = with_additional_write_roots(sandbox, claude_roots)
    sandbox = with_additional_write_files(sandbox, _claude_internal_write_files())
    sandbox = with_additional_write_roots(sandbox, [create_private_tmpdir()])

    profile = _build_profile(sandbox, cwd)

    assert _kernel_write_allowed(profile, str(cli_runtime_dir)), (
        f"seatbelt profile does not write-grant the Claude CLI runtime dir {cli_runtime_dir}; "
        "the CLI's open() there is denied (EPERM) and the Claude SDK connect fails.\n"
        "file-write* allows:\n  " + "\n  ".join(f"({k} {p!r})" for k, p in _write_allows(profile))
    )
