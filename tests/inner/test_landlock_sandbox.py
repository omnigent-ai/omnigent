"""
Tests for the Linux Landlock sandbox backend.

Layers tested:

- **Resolver**: :meth:`LandlockSandboxBackend.resolve` produces the
  right :class:`SandboxPolicy` shape (RO-by-default cwd, ``read_paths``
  → ``read_roots`` with ``None`` meaning unrestricted, ``write_files``
  carried, ``allow_network`` carried, Linux-only gate).
- **Per-ABI access-mask clamping**: :func:`_fs_write_mask_for_abi`
  only sets REFER / TRUNCATE / IOCTL_DEV on kernels new enough.
- **Graceful degrade-open**: when Landlock is unavailable
  :meth:`LandlockSandboxBackend.activate` logs and returns without
  enforcing instead of crashing.
- **Real kernel enforcement**: a child process activates a
  ``linux_landlock`` policy and proves a write INSIDE the write root
  succeeds while a write OUTSIDE it is denied (and, in restricted-read
  mode, a read outside the read roots is denied). These run in a
  throwaway child because ``landlock_restrict_self`` is irreversible
  for the process, and skip cleanly when the kernel lacks Landlock.
"""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.landlock_sandbox import (
    _LANDLOCK_ACCESS_FS_IOCTL_DEV,
    _LANDLOCK_ACCESS_FS_MAKE_DIR,
    _LANDLOCK_ACCESS_FS_READ_DIR,
    _LANDLOCK_ACCESS_FS_READ_FILE,
    _LANDLOCK_ACCESS_FS_REFER,
    _LANDLOCK_ACCESS_FS_TRUNCATE,
    _LANDLOCK_ACCESS_FS_WRITE_FILE,
    LandlockSandboxBackend,
    _add_path_rule,
    _fs_file_mask_for_abi,
    _fs_write_mask_for_abi,
    detect_landlock_abi,
)
from omnigent.inner.sandbox import (
    SandboxPolicy,
    resolve_sandbox,
)


def _probe_abi() -> int | None:
    """ABI probe that never raises, for use in skip conditions."""
    try:
        return detect_landlock_abi()
    except OSError:
        return None


LANDLOCK_ABI = _probe_abi()
requires_landlock = pytest.mark.skipif(
    LANDLOCK_ABI is None,
    reason="kernel lacks Landlock (CONFIG_SECURITY_LANDLOCK disabled/absent)",
)
# resolve() hard-errors off Linux (see test_resolve_raises_on_non_linux), so the
# resolver-mapping tests below — which expect resolve() to succeed — only run on
# Linux. Mirrors the seatbelt suite's per-test macOS guard. The Landlock-absent
# kernel case is covered separately by requires_landlock.
requires_linux = pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="landlock resolver requires a Linux host",
)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def _make_backend() -> LandlockSandboxBackend:
    return LandlockSandboxBackend()


@requires_linux
def test_resolve_default_keeps_cwd_read_only() -> None:
    """
    ``write_paths`` omitted leaves ``write_roots`` empty (cwd RO) — the
    same "no surprise writes" default as the bwrap/seatbelt backends.
    """
    backend = _make_backend()
    spec = OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="linux_landlock"))
    policy = backend.resolve(spec, Path.cwd())
    assert policy.backend_type == "linux_landlock"
    assert policy.active is True
    assert policy.write_roots == []
    assert policy.write_files == []
    # read_paths unset → reads unrestricted.
    assert policy.read_roots is None


@requires_linux
def test_resolve_write_paths_dot_makes_cwd_writable() -> None:
    """``write_paths: ["."]`` resolves cwd into ``write_roots``."""
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_landlock", write_paths=["."]),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.write_roots == [Path.cwd().resolve(strict=False)]


@requires_linux
def test_resolve_read_paths_map_to_read_roots(tmp_path: Path) -> None:
    """``read_paths`` entries resolve into ``read_roots`` (reads restricted)."""
    backend = _make_backend()
    sub = tmp_path / "src"
    sub.mkdir()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_landlock", read_paths=[str(sub), "."]),
    )
    policy = backend.resolve(spec, tmp_path)
    assert policy.read_roots == [
        sub.resolve(strict=False),
        tmp_path.resolve(strict=False),
    ]


@requires_linux
def test_resolve_write_files_carried(tmp_path: Path) -> None:
    """``write_files`` entries resolve into ``write_files`` per-file grants."""
    backend = _make_backend()
    target = tmp_path / "config.json"
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_landlock", write_files=[str(target)]),
    )
    policy = backend.resolve(spec, tmp_path)
    assert policy.write_files == [target.resolve(strict=False)]


@requires_linux
def test_resolve_allow_network_carried() -> None:
    """``allow_network`` is carried onto the policy (parity field)."""
    backend = _make_backend()
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_landlock", allow_network=False),
    )
    policy = backend.resolve(spec, Path.cwd())
    assert policy.allow_network is False


def test_resolve_raises_on_non_linux() -> None:
    """The resolver hard-errors on non-Linux hosts (no Landlock there)."""
    backend = _make_backend()
    spec = OSEnvSpec(type="caller_process", sandbox=OSEnvSandboxSpec(type="linux_landlock"))
    with patch("omnigent.inner.landlock_sandbox.sys.platform", "darwin"):
        with pytest.raises(OSError, match="only available on Linux"):
            backend.resolve(spec, Path.cwd())


@requires_linux
def test_resolve_registered_in_builtin_backends() -> None:
    """
    ``os_env.sandbox.type: linux_landlock`` routes through the registry
    — :func:`resolve_sandbox` finds the backend via
    ``_ensure_builtin_backends`` without an explicit import.
    """
    spec = OSEnvSpec(
        type="caller_process",
        sandbox=OSEnvSandboxSpec(type="linux_landlock", write_paths=["."]),
    )
    policy = resolve_sandbox(spec, Path.cwd())
    assert policy.backend_type == "linux_landlock"


# ---------------------------------------------------------------------------
# Per-ABI access-mask clamping
# ---------------------------------------------------------------------------


def test_write_mask_clamps_refer_truncate_ioctl_per_abi() -> None:
    """
    REFER (ABI 2), TRUNCATE (ABI 3), IOCTL_DEV (ABI 5) are only set when
    the kernel ABI is new enough — passing an unknown bit to
    ``landlock_create_ruleset`` would fail with EINVAL.
    """
    abi1 = _fs_write_mask_for_abi(1)
    assert not (abi1 & _LANDLOCK_ACCESS_FS_REFER)
    assert not (abi1 & _LANDLOCK_ACCESS_FS_TRUNCATE)
    assert not (abi1 & _LANDLOCK_ACCESS_FS_IOCTL_DEV)

    abi2 = _fs_write_mask_for_abi(2)
    assert abi2 & _LANDLOCK_ACCESS_FS_REFER
    assert not (abi2 & _LANDLOCK_ACCESS_FS_TRUNCATE)

    abi3 = _fs_write_mask_for_abi(3)
    assert abi3 & _LANDLOCK_ACCESS_FS_TRUNCATE
    assert not (abi3 & _LANDLOCK_ACCESS_FS_IOCTL_DEV)

    abi5 = _fs_write_mask_for_abi(5)
    assert abi5 & _LANDLOCK_ACCESS_FS_IOCTL_DEV
    # Monotonic: a higher ABI never drops a bit a lower ABI had.
    assert abi1 & abi2 == abi1
    assert abi2 & abi3 == abi2
    assert abi3 & abi5 == abi3


def test_file_mask_excludes_directory_only_bits() -> None:
    """
    The file-applicable mask carries READ_FILE / WRITE_FILE (and
    ABI-gated TRUNCATE / IOCTL_DEV) but NONE of the directory-only rights
    (READ_DIR, MAKE_DIR, REFER, …). Handing a directory-only bit to
    ``landlock_add_rule`` on a regular-file parent_fd is the EINVAL that
    broke ``write_files`` — this guards the mask split that fixes it.
    """
    for abi in (1, 2, 3, 5):
        mask = _fs_file_mask_for_abi(abi)
        assert mask & _LANDLOCK_ACCESS_FS_READ_FILE
        assert mask & _LANDLOCK_ACCESS_FS_WRITE_FILE
        assert not (mask & _LANDLOCK_ACCESS_FS_READ_DIR), abi
        assert not (mask & _LANDLOCK_ACCESS_FS_MAKE_DIR), abi
        assert not (mask & _LANDLOCK_ACCESS_FS_REFER), abi
    assert not (_fs_file_mask_for_abi(2) & _LANDLOCK_ACCESS_FS_TRUNCATE)
    assert _fs_file_mask_for_abi(3) & _LANDLOCK_ACCESS_FS_TRUNCATE
    assert not (_fs_file_mask_for_abi(3) & _LANDLOCK_ACCESS_FS_IOCTL_DEV)
    assert _fs_file_mask_for_abi(5) & _LANDLOCK_ACCESS_FS_IOCTL_DEV


# ---------------------------------------------------------------------------
# _add_path_rule: skip missing paths, fail loud on everything else
# ---------------------------------------------------------------------------


def test_add_path_rule_skips_missing_path() -> None:
    """
    A non-existent granted path is skipped, not fatal — the spec may name
    a path that hasn't been created yet. Both ``ENOENT`` (the path
    itself) and ``ENOTDIR`` (an intermediate component isn't a directory)
    are skippable. No syscall is issued (the open fails first), so a stub
    libc is fine.
    """
    # ENOENT — the granted path does not exist.
    with patch("omnigent.inner.landlock_sandbox.os.open", side_effect=FileNotFoundError()):
        # Must not raise.
        _add_path_rule(object(), 445, 3, Path("/does/not/exist"), 0b110, 0b110)  # type: ignore[arg-type]
    # ENOTDIR — an intermediate path component is not a directory.
    with patch("omnigent.inner.landlock_sandbox.os.open", side_effect=NotADirectoryError()):
        # Must not raise.
        _add_path_rule(object(), 445, 3, Path("/etc/hosts/nope"), 0b110, 0b110)  # type: ignore[arg-type]


def test_add_path_rule_raises_on_non_skippable_oserror() -> None:
    """
    BLOCKING-2 contract: an existing-but-unopenable path
    (``PermissionError`` / EACCES, ELOOP, EMFILE, …) must RAISE rather
    than silently drop the requested allow rule. Swallowing it would
    leave the helper under a weaker-than-requested policy.
    """
    with patch(
        "omnigent.inner.landlock_sandbox.os.open",
        side_effect=PermissionError(13, "Permission denied"),
    ):
        with pytest.raises(PermissionError):
            _add_path_rule(object(), 445, 3, Path("/some/blocked/path"), 0b110, 0b110)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Graceful degrade-open
# ---------------------------------------------------------------------------


def test_activate_degrades_open_when_landlock_absent(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """
    When the ABI probe reports Landlock as unavailable,
    :meth:`activate` logs a warning and returns WITHOUT enforcing
    instead of raising — the documented degrade-open contract. Tested
    in-process (safe: nothing is restricted) by patching the probe.
    """
    backend = _make_backend()
    policy = SandboxPolicy(
        backend_type="linux_landlock",
        active=True,
        read_roots=None,
        write_roots=[tmp_path],
        write_files=[],
        allow_network=True,
    )
    with patch("omnigent.inner.landlock_sandbox.detect_landlock_abi", return_value=None):
        with caplog.at_level("WARNING"):
            backend.activate(policy)  # must not raise
    assert any("Landlock LSM is unavailable" in r.message for r in caplog.records)


# ---------------------------------------------------------------------------
# Real kernel enforcement (skips when Landlock is unavailable)
# ---------------------------------------------------------------------------


def _run_probe(body: str) -> dict[str, object]:
    """
    Run *body* in a fresh Python interpreter and return the JSON dict it
    prints on the last stdout line.

    ``landlock_restrict_self`` is irreversible for the process, so each
    enforcement probe runs in a throwaway subprocess (not the test
    process). A fresh interpreter — rather than ``os.fork`` — avoids the
    "fork of a multi-threaded process may deadlock" hazard when the
    suite runs under ``pytest-xdist`` workers. The repo root is put on
    ``sys.path`` so the child can ``import omnigent.*`` regardless of cwd.

    :param body: Python source that computes a ``result`` dict and is
        run after the import preamble. It must leave its findings in a
        local named ``result``.
    :returns: The decoded result dict from the child.
    """
    repo_root = str(Path(__file__).resolve().parents[2])
    script = textwrap.dedent(
        """
        import json, sys
        sys.path.insert(0, {repo_root!r})
        from pathlib import Path
        from omnigent.inner.datamodel import OSEnvSpec, OSEnvSandboxSpec
        from omnigent.inner.landlock_sandbox import LandlockSandboxBackend
        from omnigent.inner.sandbox import resolve_sandbox, activate_sandbox
        result = {{}}
        try:
        {body}
        except Exception as exc:  # surfaced to the parent assertion
            result["error"] = f"{{type(exc).__name__}}:{{exc}}"
        print("RESULT=" + json.dumps(result))
        """
    ).format(repo_root=repo_root, body=textwrap.indent(body, " " * 12))
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"probe exited rc={proc.returncode}\nstderr={proc.stderr}"
    line = next(ln for ln in reversed(proc.stdout.splitlines()) if ln.startswith("RESULT="))
    decoded: dict[str, object] = json.loads(line[len("RESULT=") :])
    return decoded


@requires_landlock
def test_write_outside_write_root_is_denied(tmp_path: Path) -> None:
    """
    THE key enforcement test: under a ``linux_landlock`` policy whose
    write root is a tmp dir, a write INSIDE the write root succeeds and
    a write OUTSIDE it fails with ``PermissionError`` (EACCES).

    Runs in a throwaway interpreter because ``landlock_restrict_self``
    is irreversible for the process.
    """
    write_dir = tmp_path / "writable"
    write_dir.mkdir()
    outside_dir = tmp_path / "outside"
    outside_dir.mkdir()
    # Prove the parent CAN write here, so a blocked child write is
    # unambiguously the sandbox's doing.
    (outside_dir / "marker").write_text("parent-can-write")

    body = f"""
        backend = LandlockSandboxBackend()
        spec = OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="linux_landlock", write_paths=[{str(write_dir)!r}]),
        )
        policy = backend.resolve(spec, Path({str(write_dir)!r}))
        backend.activate(policy)
        try:
            (Path({str(write_dir)!r}) / "inside.txt").write_text("ok")
            result["inside"] = "wrote"
        except OSError as exc:
            result["inside"] = f"FAILED:{{type(exc).__name__}}"
        try:
            (Path({str(outside_dir)!r}) / "pwned.txt").write_text("nope")
            result["outside"] = "WROTE"
        except PermissionError:
            result["outside"] = "blocked"
        except OSError as exc:
            result["outside"] = f"other:{{type(exc).__name__}}"
    """
    result = _run_probe(body)

    assert "error" not in result, f"child raised: {result.get('error')}"
    assert result["inside"] == "wrote", (
        f"write inside the write root should succeed, got {result['inside']!r}"
    )
    assert result["outside"] == "blocked", (
        f"write outside the write root should be denied with EACCES, got {result['outside']!r}"
    )
    assert not (outside_dir / "pwned.txt").exists(), (
        "host filesystem mutated outside the write root — Landlock did not enforce"
    )


@requires_landlock
def test_write_files_grants_existing_regular_file(tmp_path: Path) -> None:
    """
    Regression for the EINVAL-on-regular-file bug: a ``write_files``
    grant on an EXISTING regular file must let the helper write and
    truncate THAT file, while a sibling file in the same directory stays
    read-only.

    Before the fix ``write_files`` entries were handed the full directory
    mask (MAKE_DIR / REFER / …), so ``landlock_add_rule`` returned EINVAL
    on the regular file and activation aborted entirely — this test would
    have surfaced that as a child error. ``write_paths`` is intentionally
    left empty so the ONLY writable thing is the granted file.
    """
    work = tmp_path / "work"
    work.mkdir()
    target = work / "config.json"
    target.write_text("original-content")
    sibling = work / "sibling.txt"
    sibling.write_text("do-not-touch")

    body = f"""
        backend = LandlockSandboxBackend()
        spec = OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="linux_landlock", write_files=[{str(target)!r}]),
        )
        policy = backend.resolve(spec, Path({str(work)!r}))
        backend.activate(policy)
        # Truncating open ('w') exercises the TRUNCATE right too.
        try:
            with open({str(target)!r}, "w") as fh:
                fh.write("rewritten")
            result["target"] = "wrote"
        except OSError as exc:
            result["target"] = f"FAILED:{{type(exc).__name__}}"
        try:
            with open({str(sibling)!r}, "w") as fh:
                fh.write("pwned")
            result["sibling"] = "WROTE"
        except PermissionError:
            result["sibling"] = "blocked"
        except OSError as exc:
            result["sibling"] = f"other:{{type(exc).__name__}}"
    """
    result = _run_probe(body)

    assert "error" not in result, (
        f"activation failed for a write_files regular file — the EINVAL "
        f"regression is back: {result.get('error')}"
    )
    assert result["target"] == "wrote", (
        f"write to the granted file should succeed, got {result['target']!r}"
    )
    assert result["sibling"] == "blocked", (
        f"write to a sibling file (not granted) should be denied, got {result['sibling']!r}"
    )
    assert target.read_text() == "rewritten"
    assert sibling.read_text() == "do-not-touch"


@requires_landlock
def test_read_outside_read_root_is_denied(tmp_path: Path) -> None:
    """
    With ``read_paths`` set (restricted-read regime) a read inside the
    granted root succeeds while a read of a non-granted sibling is
    denied with ``PermissionError``.
    """
    read_dir = tmp_path / "readable"
    read_dir.mkdir()
    (read_dir / "ok.txt").write_text("visible")
    other_dir = tmp_path / "secret"
    other_dir.mkdir()
    (other_dir / "secret.txt").write_text("must-not-read")

    body = f"""
        backend = LandlockSandboxBackend()
        spec = OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="linux_landlock", read_paths=[{str(read_dir)!r}]),
        )
        policy = backend.resolve(spec, Path({str(read_dir)!r}))
        backend.activate(policy)
        try:
            result["granted"] = (Path({str(read_dir)!r}) / "ok.txt").read_text()
        except OSError as exc:
            result["granted"] = f"FAILED:{{type(exc).__name__}}"
        try:
            (Path({str(other_dir)!r}) / "secret.txt").read_text()
            result["other"] = "READ"
        except PermissionError:
            result["other"] = "blocked"
        except OSError as exc:
            result["other"] = f"other:{{type(exc).__name__}}"
    """
    result = _run_probe(body)

    assert "error" not in result, f"child raised: {result.get('error')}"
    assert result["granted"] == "visible", (
        f"read inside the granted read root should succeed, got {result['granted']!r}"
    )
    assert result["other"] == "blocked", (
        f"read of a non-granted path should be denied, got {result['other']!r}"
    )


@requires_landlock
def test_enforcement_via_resolve_and_activate_helpers(tmp_path: Path) -> None:
    """
    End-to-end through the public :func:`resolve_sandbox` /
    :func:`activate_sandbox` registry entry points (not just the backend
    object), proving ``os_env.sandbox.type: linux_landlock`` is wired
    into the dispatch path and enforces.
    """
    write_dir = tmp_path / "w"
    write_dir.mkdir()
    outside_dir = tmp_path / "o"
    outside_dir.mkdir()

    body = f"""
        spec = OSEnvSpec(
            type="caller_process",
            sandbox=OSEnvSandboxSpec(type="linux_landlock", write_paths=[{str(write_dir)!r}]),
        )
        policy = resolve_sandbox(spec, Path({str(write_dir)!r}))
        activate_sandbox(policy)
        try:
            (Path({str(outside_dir)!r}) / "x.txt").write_text("no")
            result["outside"] = "WROTE"
        except PermissionError:
            result["outside"] = "blocked"
    """
    result = _run_probe(body)
    assert "error" not in result, f"child raised: {result.get('error')}"
    assert result["outside"] == "blocked"
