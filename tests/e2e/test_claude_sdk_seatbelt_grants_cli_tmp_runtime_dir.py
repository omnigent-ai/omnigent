"""Regression guard: the claude-sdk seatbelt profile must cover the Claude
CLI's own ``/tmp/claude-<uid>`` runtime dir, or session launch dies at connect.

On macOS the claude-sdk harness wraps the Claude CLI in a ``darwin_seatbelt``
sandbox (``omnigent.inner.claude_sdk_executor._wrap_claude_cli`` /
``prepare_claude_cli_path`` → ``resolve_sandbox`` → ``_build_profile``). The
generated SBPL profile is ``(deny default)`` with narrow ``file-write*`` allows
for: the workspace, ``~/.claude/*``, ``~/.npm/_logs``, the per-helper scratch
tmpdir, and — via :func:`_claude_internal_write_roots` — a per-uid runtime dir
computed as ``Path(tempfile.gettempdir()) / f"claude-{uid}"``.

Claude Code 2.1.x, however, chooses its own per-uid runtime directory as
``join(CLAUDE_CODE_TMPDIR || os.tmpdir(), f"claude-{uid}")`` and, for its
``SANDBOX_RUNTIME`` child, hard-defaults ``TMPDIR`` to ``/tmp/claude`` when
neither ``CLAUDE_CODE_TMPDIR`` nor ``CLAUDE_TMPDIR`` is set. omnigent never sets
``CLAUDE_CODE_TMPDIR``. On a Mac whose ``tempfile.gettempdir()`` is a
``/var/folders/.../T`` path (the norm), the directory omnigent *grants* and the
directory the CLI *opens* (``/tmp/claude-<uid>``, kernel-canonical
``/private/tmp/claude-<uid>`` because ``/tmp`` is a symlink) diverge. Under
``(deny default)`` the CLI's ``open('/tmp/claude-<uid>')`` returns EPERM, exit 1,
and the session launch dies at connect:

    Claude SDK connect failed: Command failed with exit code 1 ...
    EPERM: operation not permitted, open '/tmp/claude-502'

This test drives the real product pipeline that builds the seatbelt profile and
asserts the CLI's actual runtime directory (``/tmp/claude-<uid>`` and its
macOS-canonical form) is write-granted. It fails on a build where no allow
covers that path, and passes once the grant either names ``/tmp/claude-<uid>``
or redirects the CLI into an already-granted root via ``CLAUDE_CODE_TMPDIR``.

The profile builder (``_build_profile``) is platform-independent — only the
resolver's ``sandbox-exec`` gate is macOS-only — so this regression guard runs
on Linux CI while faithfully modelling the macOS divergence by anchoring the
system tempdir at a non-``/tmp`` directory (as ``/var/folders/.../T`` is on a
Mac); ``tempfile.tempdir`` is patched rather than ``$TMPDIR`` because
``tempfile.gettempdir()`` caches its resolved value.

Usage::

    pytest tests/e2e/test_claude_sdk_seatbelt_grants_cli_tmp_runtime_dir.py -v
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import pytest

from omnigent.inner.claude_sdk_executor import (
    _claude_internal_write_files,
    _claude_internal_write_roots,
)
from omnigent.inner.sandbox import (
    SandboxPolicy,
    create_private_tmpdir,
    with_additional_read_roots,
    with_additional_write_files,
    with_additional_write_roots,
)
from omnigent.inner.seatbelt_sandbox import _build_profile

pytestmark = pytest.mark.skipif(
    not hasattr(os, "getuid"),
    reason="the Claude CLI's per-uid /tmp/claude-<uid> runtime dir is POSIX-only",
)

_WRITE_ALLOW_RE = re.compile(r'\(allow file-write\* \((subpath|literal) "((?:[^"\\]|\\.)*)"\)\)')


def _write_allows(profile: str) -> list[tuple[str, str]]:
    """Parse ``(allow file-write* (subpath|literal "..."))`` rules from an SBPL profile."""
    out: list[tuple[str, str]] = []
    for kind, raw in _WRITE_ALLOW_RE.findall(profile):
        out.append((kind, raw.replace('\\"', '"').replace("\\\\", "\\")))
    return out


def _kernel_write_allowed(profile: str, canonical_target: str) -> bool:
    """Model the macOS Sandbox kernel: does any file-write* allow cover *canonical_target*?

    ``literal`` matches the exact path; ``subpath`` matches the path itself or
    any descendant. Deny-default means "no covering allow" == EPERM on write.
    """
    for kind, path in _write_allows(profile):
        if kind == "literal" and canonical_target == path:
            return True
        if kind == "subpath" and (
            canonical_target == path or canonical_target.startswith(path.rstrip("/") + "/")
        ):
            return True
    return False


def _anchor_system_tempdir_off_tmp(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Model the affected Mac: make the system tempdir a non-``/tmp`` path.

    macOS resolves ``tempfile.gettempdir()`` to ``/var/folders/.../T``. Patch
    ``tempfile.tempdir`` (the cache ``gettempdir()`` reads) so both
    ``_claude_internal_write_roots`` and ``create_private_tmpdir`` anchor
    there, reproducing the divergence from the CLI's ``/tmp/claude-<uid>``.
    """
    fake_tmpdir = tmp_path / "var_folders" / "abc123" / "T"
    fake_tmpdir.mkdir(parents=True)
    monkeypatch.setattr(tempfile, "tempdir", str(fake_tmpdir))
    for key in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
        monkeypatch.setenv(key, str(fake_tmpdir))
    return fake_tmpdir


def _build_claude_sdk_seatbelt_profile(workspace: Path) -> str:
    """Reconstruct the seatbelt profile ``_wrap_claude_cli`` builds for the CLI.

    Mirrors ``omnigent.inner.claude_sdk_executor._wrap_claude_cli`` (base
    policy → ``_claude_internal_write_roots`` read+write grants →
    ``_claude_internal_write_files``) plus the ``run_launcher`` host pass that
    mints and grants the per-helper scratch tmpdir, then renders the SBPL via
    the real ``_build_profile``.
    """
    policy = SandboxPolicy(  # type: ignore[call-arg]
        backend_type="darwin_seatbelt",
        active=True,
        read_roots=None,
        write_roots=[workspace],
        write_files=[],
        allow_network=True,  # the CLI must reach the provider
        cwd_allow_hidden=None,
        egress_relay_port=None,
        egress_socket_path=None,
    )
    claude_roots = _claude_internal_write_roots()
    policy = with_additional_read_roots(policy, claude_roots)
    policy = with_additional_write_roots(policy, claude_roots)
    policy = with_additional_write_files(policy, _claude_internal_write_files())

    scratch = create_private_tmpdir()
    policy = with_additional_write_roots(policy, [scratch])

    return _build_profile(policy, workspace)


def test_seatbelt_profile_grants_claude_cli_runtime_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The generated seatbelt profile must let the Claude CLI open its runtime dir.

    Reproduces the failure: with the system tempdir at a non-``/tmp`` location (as
    ``/var/folders/.../T`` is on a Mac), omnigent grants ``$TMPDIR/claude-<uid>``
    but the CLI opens ``/tmp/claude-<uid>`` (its ``os.tmpdir()`` / hardcoded
    ``/tmp/claude`` default), which no allow covers → EPERM → connect fails.

    Asserts the CLI's actual runtime directory is write-granted. Fails on a
    build whose grants miss it; passes once ``/tmp/claude-<uid>`` is granted (or
    the CLI is redirected via ``CLAUDE_CODE_TMPDIR`` into a granted root).
    """
    _anchor_system_tempdir_off_tmp(tmp_path, monkeypatch)

    workspace = tmp_path / "workspace"
    workspace.mkdir()

    profile = _build_claude_sdk_seatbelt_profile(workspace.resolve())

    uid = os.getuid()
    # The CLI's hardcoded per-uid runtime dir, plus its macOS-canonical form
    # (/tmp is a symlink to /private/tmp on macOS, and the kernel matches the
    # canonical path). On the affected build NEITHER is covered.
    cli_runtime = f"/tmp/claude-{uid}"
    cli_runtime_canonical = f"/private/tmp/claude-{uid}"

    allowed_direct = _kernel_write_allowed(profile, cli_runtime)
    allowed_canonical = _kernel_write_allowed(profile, cli_runtime_canonical)

    assert allowed_direct or allowed_canonical, (
        "The claude-sdk seatbelt profile grants no file-write* allow "
        f"covering the Claude CLI's runtime dir ({cli_runtime!r} / "
        f"{cli_runtime_canonical!r}). Under (deny default) the CLI's "
        f"open({cli_runtime!r}) fails with EPERM (exit 1) and the session launch "
        "dies at connect. Emitted file-write* allows:\n  "
        + "\n  ".join(f"({k} {p!r})" for k, p in _write_allows(profile))
    )


def test_seatbelt_grant_diverges_from_cli_runtime_dir_on_macos_layout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Document the root-cause divergence the fix must close.

    ``_claude_internal_write_roots`` anchors the CLI runtime grant at
    ``tempfile.gettempdir()``. On a macOS ``/var/folders/.../T`` tempdir the
    granted dir is not under ``/tmp`` at all, while the CLI opens
    ``/tmp/claude-<uid>``. This asserts that observed divergence so a fix that
    merely renames the grant without covering ``/tmp/claude-<uid>`` still trips
    the primary guard above.
    """
    fake_tmpdir = _anchor_system_tempdir_off_tmp(tmp_path, monkeypatch)

    uid = os.getuid()
    granted = _claude_internal_write_roots()
    granted_claude_dir = next((p for p in granted if p.name == f"claude-{uid}"), None)
    assert granted_claude_dir is not None, (
        "expected _claude_internal_write_roots to include a claude-<uid> runtime dir"
    )
    # The tempdir-anchored grant lives under the (non-/tmp) system tempdir, NOT
    # at the /tmp/claude-<uid> the CLI actually opens — the divergence the extra
    # /tmp grant exists to close.
    assert str(granted_claude_dir).startswith(str(fake_tmpdir))
    assert str(granted_claude_dir) != f"/tmp/claude-{uid}"
