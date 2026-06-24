"""Central, dependency-light platform flags and OS-portability helpers.

omnigent grew up on Linux/macOS and bakes a number of POSIX assumptions into
process management, shells, and user identity. This module is the single place
that answers "which OS are we on?" and provides the small portable primitives
that the rest of the package uses instead of branching on :data:`os.name`
ad hoc.

Keep this module import-cheap and free of heavy/optional dependencies: it is
imported very early (and on Windows it must import before any POSIX-only module
would otherwise crash), so it must never pull in ``fcntl``/``termios``/``pty``
or anything platform-specific at module top level.
"""

from __future__ import annotations

import getpass
import hashlib
import os
import sys
from pathlib import Path

#: True on native Windows (cmd/PowerShell), i.e. ``os.name == "nt"``. This is
#: *not* true under WSL, where Python reports a Linux platform.
IS_WINDOWS = os.name == "nt"
#: True on any POSIX host (Linux, macOS, BSD, WSL).
IS_POSIX = os.name == "posix"
#: True on Linux specifically (the only platform with bwrap + seccomp).
IS_LINUX = sys.platform.startswith("linux")
#: True on macOS specifically (the seatbelt sandbox platform).
IS_DARWIN = sys.platform == "darwin"

#: Non-sensitive Windows environment variables that a spawned omnigent
#: subprocess needs to function, for env-passthrough allowlists that otherwise
#: assume POSIX names. Python uppercases env keys on Windows, so these match
#: ``os.environ`` as stored; they are absent on POSIX, so including them in an
#: allowlist is a no-op there (only present vars pass through).
#:
#: - ``SYSTEMROOT`` is MANDATORY: Winsock loads its providers from
#:   ``%SystemRoot%\system32\mswsock.dll``, so a child without it dies at
#:   ``import asyncio`` with ``WinError 10106`` (WSAEPROVIDERFAILEDINIT).
#: - ``USERPROFILE`` / ``HOMEDRIVE`` / ``HOMEPATH`` let ``Path.home()`` /
#:   ``expanduser("~")`` resolve (the Windows analog of POSIX ``HOME``).
#: - ``APPDATA`` / ``LOCALAPPDATA`` are where Windows apps (keyring, pip, npm,
#:   …) keep per-user config and cache.
#: - The rest let a Windows process and shell resolve binaries normally.
#:
#: All are path/identity constants, not credentials — consistent with POSIX
#: ``HOME``/``PATH`` already being allowed.
WINDOWS_ENV_PASSTHROUGH: tuple[str, ...] = (
    "SYSTEMROOT",
    "SYSTEMDRIVE",
    "WINDIR",
    "COMSPEC",
    "PATHEXT",
    "NUMBER_OF_PROCESSORS",
    "PROCESSOR_ARCHITECTURE",
    "PROCESSOR_IDENTIFIER",
    "PROCESSOR_LEVEL",
    "PROCESSOR_REVISION",
    "USERPROFILE",
    "HOMEDRIVE",
    "HOMEPATH",
    "APPDATA",
    "LOCALAPPDATA",
)


def default_shell_argv(command: str) -> list[str]:
    """
    Build the argv to run ``command`` through the host's default shell.

    On POSIX this mirrors the long-standing behavior: prefer ``bash`` with
    ``--noprofile --norc`` (skip user rc files for a predictable environment),
    falling back to ``sh -c``. On Windows there is no ``/bin/sh``; route through
    ``cmd.exe`` (``%COMSPEC%``) with ``/c``.

    :param command: The shell command string to execute.
    :returns: An argv list suitable for :func:`subprocess.Popen` (no
        ``shell=True`` needed).
    """
    if IS_WINDOWS:
        comspec = os.environ.get("COMSPEC", "cmd.exe")
        return [comspec, "/c", command]
    import shutil

    bash = shutil.which("bash")
    if bash:
        return [bash, "--noprofile", "--norc", "-c", command]
    sh = shutil.which("sh") or "/bin/sh"
    return [sh, "-c", command]


def stable_user_id() -> str:
    """
    A stable, filesystem-safe token identifying the current OS user.

    Used to namespace per-user scratch directories (e.g.
    ``omnigent-<id>`` / ``claude-<id>`` under the temp dir). On POSIX this is
    the numeric uid, matching historical behavior. Windows has no ``getuid``;
    derive a short hex digest from the login name so the value is stable across
    runs and safe to embed in a path.

    The digest is for path namespacing only — not security — so ``getuser``'s
    value never needs to be recoverable or collision-proof against an
    adversary; it just needs to be stable and filesystem-safe. SHA-256 with
    ``usedforsecurity=False`` documents that intent (and avoids flagging SHA-1).

    :returns: A short string with no path separators or shell-special chars.
    """
    if IS_POSIX and hasattr(os, "getuid"):
        return str(os.getuid())
    try:
        name = getpass.getuser()
    except (OSError, KeyError, ModuleNotFoundError):
        name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return hashlib.sha256(name.encode("utf-8"), usedforsecurity=False).hexdigest()[:12]


def resolve_repo_symlink(path: Path) -> Path:
    """
    Follow a Git symlink that a no-symlink Windows checkout left as a text file.

    On Windows with ``core.symlinks=false`` (the default when Developer Mode is
    off and Git was not run elevated), Git materializes a repository symlink as a
    *regular file* whose entire content is the link target — e.g. the checked-out
    ``omnigent/resources/examples/polly`` is a 23-byte file containing
    ``../../../examples/polly`` rather than a link to that directory. Code that
    expects to open the linked directory then reads this stub instead (the
    symptom: ``expected YAML mapping at top level, got str``).

    Detect that exact shape — a small, single-line regular file whose content,
    resolved relative to the stub's parent, names an existing path — and return
    the real target. Everything else (real directories, real symlinks, genuine
    single-file specs, multi-line or unresolvable content) is returned
    unchanged. No-op off Windows, where the symlink is followed natively.

    :param path: The path as resolved from packaged resources.
    :returns: The dereferenced target on Windows when *path* is a Git-symlink
        stub, otherwise *path* unchanged.
    """
    if not IS_WINDOWS:
        return path
    try:
        if not path.is_file() or path.stat().st_size > 4096:
            return path
        body = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return path
    target = body.strip()
    if not target or "\n" in target:
        return path
    candidate = path.parent / target
    if candidate.exists():
        return candidate.resolve()
    return path
