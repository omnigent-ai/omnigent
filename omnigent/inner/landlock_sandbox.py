"""
Linux Landlock sandbox backend.

In-process filesystem-confinement backend built on the Landlock LSM
(``CONFIG_SECURITY_LANDLOCK``). Unlike :mod:`omnigent.inner.bwrap_sandbox`,
Landlock needs **no** mount operations, **no** namespaces, and **no**
privileges — the unprivileged process simply asks the kernel to restrict
its own (and its descendants') filesystem access rights via three
syscalls. That makes it the backend of choice inside a hardened /
unprivileged container where ``bwrap`` cannot run: a container whose
seccomp / AppArmor profile denies ``unshare(CLONE_NEWUSER)`` and the
mount family (the syscalls bubblewrap depends on) will still let a
process call ``landlock_restrict_self`` and confine itself.

Opt-in only via ``os_env.sandbox.type: linux_landlock`` in YAML — it is
**not** a platform default (the Linux default stays ``linux_bwrap``; see
:func:`omnigent.inner.sandbox._default_sandbox_for_platform`).

In-process backend contract
---------------------------
This backend is an *in-process* :class:`SandboxBackend`: it does all of
its enforcement inside :meth:`LandlockSandboxBackend.activate`, which
runs in the helper/launcher process via
:func:`omnigent.inner.sandbox.run_launcher`. It keeps the no-op
:meth:`SandboxBackend.wrap_launcher_argv` default and is deliberately
NOT a member of :data:`omnigent.inner.sandbox._SPAWN_WRAP_BACKENDS` —
there is no parent-side launcher to prepend. Landlock policy is a
process credential that is inherited across ``fork``/``execve`` (and
cannot be relaxed once ``PR_SET_NO_NEW_PRIVS`` is set), so confining the
launcher confines every command it spawns.

Enforcement model
-----------------
Landlock works by *handling* a set of filesystem access rights at the
ruleset level and then *granting* a subset of those rights to specific
filesystem hierarchies (``LANDLOCK_RULE_PATH_BENEATH`` rules). Any
handled right exercised on a path that no rule grants is denied with
``EACCES`` / ``EPERM``. Rights that are not handled are not restricted at
all. We exploit that to express write-confinement cheaply:

- **Write roots** are directories and get the full handled mask (read +
  write classes), so the helper can create, modify, delete, and traverse
  inside them.
- **Write files** are regular files and get only the *file-applicable*
  subset (``EXECUTE | READ_FILE | WRITE_FILE`` plus ABI-gated
  ``TRUNCATE`` / ``IOCTL_DEV``). Handing a directory-only right
  (``READ_DIR``, ``MAKE_*``, ``REMOVE_*``, ``REFER``) to
  ``landlock_add_rule`` on a regular-file ``parent_fd`` fails with
  ``EINVAL``. ``_add_path_rule`` also ``fstat``s every ``parent_fd`` and
  clamps any non-directory to the file mask as defense in depth.
- **Read roots** (when the spec restricts reads) get the read-class
  rights only.
- Everything else is denied for whatever class is handled.

Two regimes, mirroring the bwrap/seatbelt ``read_paths`` contract:

- ``read_paths`` unset (``read_roots is None``) → reads are
  **unrestricted**: the ruleset handles only the write-class rights, so
  no read is ever denied while writes are confined to the write roots.
- ``read_paths`` set → reads are confined too: the ruleset additionally
  handles the read-class rights and only the read/write roots are
  reachable for reads.

Per-ABI access-mask clamping
---------------------------
The set of ``LANDLOCK_ACCESS_FS_*`` bits has grown over kernel releases
(``REFER`` in ABI 2, ``TRUNCATE`` in ABI 3, ``IOCTL_DEV`` in ABI 5).
Passing a bit the running kernel does not understand in
``handled_access_fs`` makes ``landlock_create_ruleset`` fail with
``EINVAL``, so the handled mask is built **per detected ABI** — we only
ever hand the kernel bits it knows about.

Graceful degrade-open when Landlock is absent
---------------------------------------------
``landlock_create_ruleset(NULL, 0, LANDLOCK_CREATE_RULESET_VERSION)``
returns the supported ABI version, or fails with ``ENOSYS`` (kernel too
old / Landlock not compiled in) or ``EOPNOTSUPP`` (compiled in but
disabled at boot, e.g. ``lsm=`` without ``landlock``). When Landlock is
unavailable :meth:`LandlockSandboxBackend.activate` logs a clear warning
and returns **without enforcing** rather than crashing the helper. This
"degrade open" is a deliberate availability-over-security trade-off for
the not-supported case: the alternative (hard-failing every spawn) would
make the backend unusable on mixed fleets where only some kernels carry
Landlock. Operators who require hard enforcement should pin a backend on
a known-good kernel. A *real* failure mid-setup (e.g. ``EINVAL`` from a
malformed rule, an unexpected ``landlock_restrict_self`` error) is NOT
swallowed — those raise so the helper aborts rather than running with a
half-applied policy.

Known deltas from ``linux_bwrap`` (documented intentionally)
-----------------------------------------------------------
- **No mount-based ``/proc`` / ``/dev`` / ``/tmp`` isolation.** Landlock
  governs filesystem *access rights* only; it performs no mounts and
  builds no hermetic view. The helper sees the host's real ``/proc``,
  ``/dev``, ``/tmp``, etc. (subject to the access mask). bwrap replaces
  those with fresh private instances.
- **Weaker credential read-masking.** bwrap overlays ``/dev/null`` /
  tmpfs to carve deny-holes for dotfiles (``~/.aws`` etc.) underneath an
  allowed read root. Landlock has no "subtract a subtree once a parent
  is allowed" primitive — a ``PATH_BENEATH`` grant on a directory covers
  the whole subtree and cannot be punched through. So the dotfile /
  escaping-symlink masker is not applied here; with unrestricted reads
  every readable path stays readable.
- **No network / namespace isolation.** ``allow_network`` is carried on
  the policy for interface parity but is not enforced (Landlock ABI 4+
  can restrict TCP bind/connect, but that is out of scope here). There
  is likewise no PID/UTS/IPC namespace isolation and no seccomp
  denylist — those are bwrap-only.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import platform
import stat
import sys
from pathlib import Path

from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .sandbox import (
    SandboxBackend,
    SandboxPolicy,
    register_backend,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Syscall numbers
# ---------------------------------------------------------------------------

# Landlock syscall numbers. They are identical on x86_64 and aarch64
# (aarch64 follows the asm-generic table and x86_64 was assigned the
# same values when Landlock landed in 5.13). Other architectures are
# intentionally absent — see :func:`_landlock_syscall_numbers`.
_LANDLOCK_SYSCALLS_BY_ARCH: dict[str, tuple[int, int, int]] = {
    # (create_ruleset, add_rule, restrict_self)
    "x86_64": (444, 445, 446),
    "aarch64": (444, 445, 446),
}

_PR_SET_NO_NEW_PRIVS = 38

# ---------------------------------------------------------------------------
# Landlock ABI constants
# ---------------------------------------------------------------------------

# Passed as the ``flags`` arg of ``landlock_create_ruleset`` to ask the
# kernel for the supported ABI version instead of creating a ruleset.
_LANDLOCK_CREATE_RULESET_VERSION = 1 << 0

# ``enum landlock_rule_type`` — only PATH_BENEATH exists for filesystem
# rules (NET_PORT was added in ABI 4 and is not used here).
_LANDLOCK_RULE_PATH_BENEATH = 1

# ``LANDLOCK_ACCESS_FS_*`` bits from ``include/uapi/linux/landlock.h``.
# Defined explicitly (rather than via a header lookup) so the activation
# path has zero import-time dependencies beyond ctypes.
_LANDLOCK_ACCESS_FS_EXECUTE = 1 << 0
_LANDLOCK_ACCESS_FS_WRITE_FILE = 1 << 1
_LANDLOCK_ACCESS_FS_READ_FILE = 1 << 2
_LANDLOCK_ACCESS_FS_READ_DIR = 1 << 3
_LANDLOCK_ACCESS_FS_REMOVE_DIR = 1 << 4
_LANDLOCK_ACCESS_FS_REMOVE_FILE = 1 << 5
_LANDLOCK_ACCESS_FS_MAKE_CHAR = 1 << 6
_LANDLOCK_ACCESS_FS_MAKE_DIR = 1 << 7
_LANDLOCK_ACCESS_FS_MAKE_REG = 1 << 8
_LANDLOCK_ACCESS_FS_MAKE_SOCK = 1 << 9
_LANDLOCK_ACCESS_FS_MAKE_FIFO = 1 << 10
_LANDLOCK_ACCESS_FS_MAKE_BLOCK = 1 << 11
_LANDLOCK_ACCESS_FS_MAKE_SYM = 1 << 12
_LANDLOCK_ACCESS_FS_REFER = 1 << 13  # ABI >= 2
_LANDLOCK_ACCESS_FS_TRUNCATE = 1 << 14  # ABI >= 3
_LANDLOCK_ACCESS_FS_IOCTL_DEV = 1 << 15  # ABI >= 5

# Read-class rights: opening files for read, listing/traversing dirs,
# and executing. Granted to read roots and (implicitly, via the full
# mask) to write roots.
_FS_READ_BASE = (
    _LANDLOCK_ACCESS_FS_EXECUTE | _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_READ_DIR
)

# Write-class rights available in ABI 1 (the file/dir creation + removal
# + write family). REFER/TRUNCATE/IOCTL_DEV are layered on per ABI in
# :func:`_fs_write_mask_for_abi`.
_FS_WRITE_BASE = (
    _LANDLOCK_ACCESS_FS_WRITE_FILE
    | _LANDLOCK_ACCESS_FS_REMOVE_DIR
    | _LANDLOCK_ACCESS_FS_REMOVE_FILE
    | _LANDLOCK_ACCESS_FS_MAKE_CHAR
    | _LANDLOCK_ACCESS_FS_MAKE_DIR
    | _LANDLOCK_ACCESS_FS_MAKE_REG
    | _LANDLOCK_ACCESS_FS_MAKE_SOCK
    | _LANDLOCK_ACCESS_FS_MAKE_FIFO
    | _LANDLOCK_ACCESS_FS_MAKE_BLOCK
    | _LANDLOCK_ACCESS_FS_MAKE_SYM
)

# File-applicable rights for ABI 1 — the subset of access rights the
# kernel accepts on a ``PATH_BENEATH`` rule whose ``parent_fd`` is a
# *regular file* (not a directory). Passing any directory-only right
# (READ_DIR, the MAKE_*/REMOVE_* family, REFER) for a file makes
# ``landlock_add_rule`` fail with EINVAL (see man landlock_add_rule), so
# per-file grants (``write_files``) and file-shaped read/write roots are
# clamped to this set. TRUNCATE (ABI 3) and IOCTL_DEV (ABI 5) are layered
# on per ABI in :func:`_fs_file_mask_for_abi`. EXECUTE is ABI 1 and
# always applicable to files.
_FS_FILE_BASE = (
    _LANDLOCK_ACCESS_FS_EXECUTE | _LANDLOCK_ACCESS_FS_READ_FILE | _LANDLOCK_ACCESS_FS_WRITE_FILE
)

# O_PATH | O_CLOEXEC for opening rule parent directories. O_PATH yields a
# bare fd referencing the path (no read/exec permission needed, no
# content access) — exactly what ``landlock_add_rule`` wants for
# ``path_beneath_attr.parent_fd``. O_CLOEXEC keeps the fd from leaking
# into the eventual target exec.
_O_PATH = 0o10000000
_O_CLOEXEC = 0o2000000


# ---------------------------------------------------------------------------
# ctypes structs
# ---------------------------------------------------------------------------


class _LandlockRulesetAttr(ctypes.Structure):
    """
    ``struct landlock_ruleset_attr`` (uapi).

    Carries the full current-ABI layout (``handled_access_net`` added in
    ABI 4, ``scoped`` in ABI 6). On older kernels the kernel's
    ``copy_struct_from_user`` zero-checks the trailing bytes, so passing
    the full struct with ``handled_access_net = scoped = 0`` is accepted
    everywhere — we only ever set ``handled_access_fs``.
    """

    _fields_ = (
        ("handled_access_fs", ctypes.c_uint64),
        ("handled_access_net", ctypes.c_uint64),
        ("scoped", ctypes.c_uint64),
    )


class _LandlockPathBeneathAttr(ctypes.Structure):
    """
    ``struct landlock_path_beneath_attr`` (uapi) — ``__packed``.

    ``allowed_access`` (u64) must be a subset of the ruleset's
    ``handled_access_fs`` or ``landlock_add_rule`` fails with ``EINVAL``.
    ``parent_fd`` (s32) is an ``O_PATH`` fd to the hierarchy root.
    """

    _pack_ = 1
    _fields_ = (
        ("allowed_access", ctypes.c_uint64),
        ("parent_fd", ctypes.c_int32),
    )


# ---------------------------------------------------------------------------
# ABI detection + mask helpers
# ---------------------------------------------------------------------------


def _landlock_syscall_numbers() -> tuple[int, int, int] | None:
    """
    Return ``(create_ruleset, add_rule, restrict_self)`` syscall numbers
    for the current architecture, or ``None`` when unsupported.

    :returns: The three Landlock syscall numbers, or ``None`` on an
        architecture this backend doesn't carry numbers for (treated as
        a degrade-open condition by :func:`detect_landlock_abi`).
    """
    return _LANDLOCK_SYSCALLS_BY_ARCH.get(platform.machine())


def detect_landlock_abi() -> int | None:
    """
    Probe the running kernel's Landlock ABI version.

    Calls ``landlock_create_ruleset(NULL, 0,
    LANDLOCK_CREATE_RULESET_VERSION)`` which returns the supported ABI
    (an integer ``>= 1``) without creating a ruleset, or fails with
    ``ENOSYS`` / ``EOPNOTSUPP`` when Landlock is absent or disabled.

    :returns: The ABI version (``>= 1``) when Landlock is available, or
        ``None`` when it is absent/disabled or the architecture is
        unsupported. Used both by :meth:`LandlockSandboxBackend.activate`
        (to decide whether to enforce) and by the test suite (to skip
        cleanly on non-Landlock kernels).
    :raises OSError: On an unexpected errno from the probe syscall
        (anything other than ``ENOSYS`` / ``EOPNOTSUPP``).
    """
    if not sys.platform.startswith("linux"):
        return None
    numbers = _landlock_syscall_numbers()
    if numbers is None:
        return None
    create_ruleset = numbers[0]

    libc = ctypes.CDLL(None, use_errno=True)
    ctypes.set_errno(0)
    rc = int(libc.syscall(create_ruleset, None, 0, _LANDLOCK_CREATE_RULESET_VERSION))
    if rc < 0:
        err = ctypes.get_errno()
        if err in (errno.ENOSYS, errno.EOPNOTSUPP):
            return None
        raise OSError(err, f"landlock_create_ruleset(version probe) failed: {os.strerror(err)}")
    return rc


def _fs_read_mask_for_abi(abi: int) -> int:
    """
    Read-class access rights supported by *abi*.

    No read-class bits were added after ABI 1, so this is constant for
    every Landlock kernel; the parameter is kept for symmetry with
    :func:`_fs_write_mask_for_abi` and to document the per-ABI contract.

    :param abi: Detected Landlock ABI version (``>= 1``).
    :returns: The read-class ``LANDLOCK_ACCESS_FS_*`` mask.
    """
    del abi
    return _FS_READ_BASE


def _fs_write_mask_for_abi(abi: int) -> int:
    """
    Write-class access rights supported by *abi*, clamped to the bits
    the running kernel understands.

    REFER (ABI 2), TRUNCATE (ABI 3), and IOCTL_DEV (ABI 5) are layered on
    only when the kernel is new enough; passing an unknown bit to
    ``landlock_create_ruleset`` would fail with ``EINVAL``.

    :param abi: Detected Landlock ABI version (``>= 1``).
    :returns: The write-class ``LANDLOCK_ACCESS_FS_*`` mask for *abi*.
    """
    mask = _FS_WRITE_BASE
    if abi >= 2:
        mask |= _LANDLOCK_ACCESS_FS_REFER
    if abi >= 3:
        mask |= _LANDLOCK_ACCESS_FS_TRUNCATE
    if abi >= 5:
        mask |= _LANDLOCK_ACCESS_FS_IOCTL_DEV
    # Intentional ABI-scoped decision: we only OR in the bits this module
    # knows about (up to IOCTL_DEV / ABI 5). Filesystem rights added in
    # ABIs newer than what this code defines — and on kernels reporting an
    # ABI higher than 6 — are simply left UNHANDLED, i.e. unrestricted,
    # rather than enforced. ``handled_access_fs`` is clamped to the
    # detected ABI either way (passing an unknown bit fails with EINVAL),
    # so the floor is "we never crash on a new kernel" and the ceiling is
    # "we enforce exactly the rights we understand". Picking up newer
    # rights is a deliberate follow-up, not an automatic widening.
    return mask


def _fs_file_mask_for_abi(abi: int) -> int:
    """
    File-applicable access rights supported by *abi*.

    The subset of rights the kernel accepts on a ``PATH_BENEATH`` rule
    whose ``parent_fd`` is a regular file. Used for per-file grants
    (``write_files``) and as the defense-in-depth clamp in
    :func:`_add_path_rule` for any non-directory path: directory-only
    rights (READ_DIR, MAKE_*/REMOVE_*, REFER) would make
    ``landlock_add_rule`` fail with ``EINVAL`` on a file.

    TRUNCATE (ABI 3) and IOCTL_DEV (ABI 5) are file-applicable and layered
    on per ABI; the directory-creation/removal family is deliberately
    excluded.

    :param abi: Detected Landlock ABI version (``>= 1``).
    :returns: The file-applicable ``LANDLOCK_ACCESS_FS_*`` mask for *abi*.
    """
    mask = _FS_FILE_BASE
    if abi >= 3:
        mask |= _LANDLOCK_ACCESS_FS_TRUNCATE
    if abi >= 5:
        mask |= _LANDLOCK_ACCESS_FS_IOCTL_DEV
    return mask


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class LandlockSandboxBackend(SandboxBackend):
    """
    Landlock-based in-process filesystem-confinement backend.

    Resolves a :class:`SandboxPolicy` from an :class:`OSEnvSpec`
    (:meth:`resolve`) and applies a Landlock ruleset to the current
    process — and therefore every command it later spawns — inside the
    helper (:meth:`activate`). Keeps the no-op
    :meth:`SandboxBackend.wrap_launcher_argv` (there is no spawn-time
    launcher).

    Stateless: a single shared instance is registered with the sandbox
    registry at module import time.
    """

    type_name = "linux_landlock"

    def resolve(self, spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        """
        Build a :class:`SandboxPolicy` for the Landlock backend.

        Mirrors the bwrap/seatbelt resolve contract so a YAML spec ports
        between backends value-for-value:

        - ``read_paths`` → ``read_roots`` (``None`` means reads are
          unrestricted).
        - ``write_paths`` defaults to **empty** — cwd is read-only unless
          the spec sets ``write_paths: ["."]`` explicitly.
        - ``write_files`` → per-file write grants.
        - ``allow_network`` is carried (not enforced — see the module
          docstring).

        Unlike bwrap/seatbelt there is no external binary to probe for;
        availability is detected at :meth:`activate` time and degrades
        open. The only hard requirement at resolve time is a Linux host.

        :param spec: The agent's :class:`OSEnvSpec`. Only ``spec.sandbox``
            is consulted.
        :param cwd: Effective working directory; relative entries in
            ``read_paths`` / ``write_paths`` / ``write_files`` resolve
            against it.
        :returns: A populated :class:`SandboxPolicy` with
            ``backend_type=self.type_name`` and ``active=True``.
        :raises OSError: If the host is not Linux.
        """
        sandbox_spec = spec.sandbox or OSEnvSandboxSpec(type=self.type_name)

        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise OSError(
                "linux_landlock sandbox is only available on Linux. "
                "Configure os_env.sandbox.type='none' on other OSes."
            )

        read_roots: list[Path] | None = None
        if sandbox_spec.read_paths is not None:
            read_roots = [_resolve_root(cwd, root) for root in sandbox_spec.read_paths]

        # Landlock-specific default mirroring bwrap: cwd is RO unless the
        # spec opts in via ``write_paths: ["."]``. Empty default honours
        # the "no surprise writes" contract.
        write_paths_config = (
            sandbox_spec.write_paths if sandbox_spec.write_paths is not None else []
        )
        write_roots = [_resolve_root(cwd, root) for root in write_paths_config]

        write_files: list[Path] = []
        if sandbox_spec.write_files is not None:
            write_files.extend(_resolve_root(cwd, path) for path in sandbox_spec.write_files)

        return SandboxPolicy(
            backend_type=self.type_name,
            active=True,
            read_roots=read_roots,
            write_roots=write_roots,
            write_files=write_files,
            allow_network=sandbox_spec.allow_network,
            env_passthrough=(
                list(sandbox_spec.env_passthrough)
                if sandbox_spec.env_passthrough is not None
                else None
            ),
        )

    def activate(self, policy: SandboxPolicy) -> None:
        """
        Apply a Landlock ruleset to the current process in-place.

        Steps:

        1. Probe the Landlock ABI. If unavailable (``ENOSYS`` /
           ``EOPNOTSUPP`` / unsupported arch) log a warning and return
           WITHOUT enforcing — the documented degrade-open behaviour.
        2. Build the handled access-rights mask, clamped to the detected
           ABI. The write-class is always handled; the read-class is
           handled only when ``read_roots`` restricts reads.
        3. Create the ruleset, add one ``PATH_BENEATH`` rule per
           write/read root and write file. Directory roots (write/read
           roots) get the full handled / read mask; ``write_files`` get
           the **file-applicable** subset only — passing a directory-only
           right (MAKE_DIR, READ_DIR, REFER, …) on a regular-file
           ``parent_fd`` makes ``landlock_add_rule`` fail with EINVAL.
           :func:`_add_path_rule` additionally ``fstat``s every parent_fd
           and clamps non-directories to the file mask as defense in
           depth. Finally ``PR_SET_NO_NEW_PRIVS`` +
           ``landlock_restrict_self``.

        :param policy: The resolved :class:`SandboxPolicy`. Consulted for
            ``read_roots`` (regime selector), ``write_roots``, and
            ``write_files``.
        :raises OSError: On a *real* Landlock failure (ruleset creation,
            rule add, restrict-self, or no-new-privs). The absent /
            disabled case does NOT raise — it degrades open.
        """
        abi = detect_landlock_abi()
        if abi is None:
            # Degrade open: Landlock is not available on this kernel /
            # arch. Hard-failing here would make every spawn on a
            # non-Landlock host fail; the operator opted into this
            # backend knowing it is best-effort where the LSM is absent.
            _LOGGER.warning(
                "linux_landlock: Landlock LSM is unavailable on this kernel "
                "(arch=%s); proceeding WITHOUT filesystem confinement. To "
                "enforce, run on a kernel with CONFIG_SECURITY_LANDLOCK "
                "enabled (and 'landlock' in the active LSM list), or pin a "
                "different os_env.sandbox.type.",
                platform.machine(),
            )
            return

        numbers = _landlock_syscall_numbers()
        # detect_landlock_abi already returned non-None, so the arch is
        # known; this is belt-and-suspenders for the type checker.
        if numbers is None:  # pragma: no cover - unreachable given abi check
            return
        _create_ruleset, add_rule, restrict_self = numbers

        read_mask = _fs_read_mask_for_abi(abi)
        write_mask = _fs_write_mask_for_abi(abi)
        file_mask = _fs_file_mask_for_abi(abi)
        restrict_reads = policy.read_roots is not None

        # Handled mask: always confine writes; confine reads only when the
        # spec restricts them. Rights NOT handled are left unrestricted by
        # Landlock, which is how unrestricted-reads mode keeps every read
        # working while writes stay confined.
        handled = write_mask | (read_mask if restrict_reads else 0)

        libc = ctypes.CDLL(None, use_errno=True)

        ruleset_attr = _LandlockRulesetAttr()
        ruleset_attr.handled_access_fs = handled
        ctypes.set_errno(0)
        ruleset_fd = int(
            libc.syscall(
                numbers[0],
                ctypes.byref(ruleset_attr),
                ctypes.sizeof(ruleset_attr),
                0,
            )
        )
        if ruleset_fd < 0:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_create_ruleset failed: {os.strerror(err)}")

        try:
            # Write roots are directories: grant the entire handled mask
            # (write class + read class when reads are restricted).
            for path in policy.write_roots:
                _add_path_rule(libc, add_rule, ruleset_fd, path, handled, file_mask)

            # Write files are regular files: grant only the file-applicable
            # WRITE+READ subset (intersected with the handled mask). Passing
            # the full handled mask here — which carries directory-only bits
            # like MAKE_REG / REFER — makes landlock_add_rule return EINVAL
            # on a regular-file parent_fd and aborts activation.
            for path in policy.write_files:
                _add_path_rule(libc, add_rule, ruleset_fd, path, file_mask & handled, file_mask)

            # Read roots: grant the read class only (intersected with the
            # handled mask). Skipped entirely when reads are unrestricted
            # — there is nothing to re-allow because reads aren't handled.
            if restrict_reads:
                for path in policy.read_roots or []:
                    _add_path_rule(
                        libc, add_rule, ruleset_fd, path, read_mask & handled, file_mask
                    )

            _set_no_new_privs(libc)

            ctypes.set_errno(0)
            rc = int(libc.syscall(restrict_self, ruleset_fd, 0))
            if rc != 0:
                err = ctypes.get_errno()
                raise OSError(err, f"landlock_restrict_self failed: {os.strerror(err)}")
        finally:
            os.close(ruleset_fd)

        _LOGGER.info(
            "[omnigent-sandbox] landlock active abi=%s write_roots=%d write_files=%d "
            "read_roots=%s",
            abi,
            len(policy.write_roots),
            len(policy.write_files),
            "unrestricted" if not restrict_reads else len(policy.read_roots or []),
        )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _add_path_rule(
    libc: ctypes.CDLL,
    add_rule_nr: int,
    ruleset_fd: int,
    path: Path,
    allowed_access: int,
    file_mask: int,
) -> None:
    """
    Add one ``LANDLOCK_RULE_PATH_BENEATH`` rule granting *allowed_access*
    to the hierarchy rooted at *path*.

    Opens *path* with ``O_PATH | O_CLOEXEC`` for the rule's
    ``parent_fd``, then ``fstat``s it: if the path is NOT a directory the
    requested *allowed_access* is intersected with *file_mask* before the
    syscall, so a directory-only right never reaches the kernel for a
    regular file (which would fail with ``EINVAL``). This makes the call
    correct regardless of which policy list the path came from.

    Skippable failures vs fail-loud:

    - A **non-existent** path (``ENOENT``) — or an ``ENOTDIR`` where an
      intermediate path component isn't a directory — is logged and
      skipped: a spec may grant a path that hasn't been created yet, and
      that is not a security-relevant policy loss.
    - **Any other** ``OSError`` (``EACCES``, ``ELOOP``, ``EMFILE``, an
      unexpected ``landlock_add_rule`` errno, …) is RAISED. Silently
      dropping a requested allow rule would leave the helper running
      under a weaker-than-requested policy — a fail-closed contract, kept
      deliberately distinct from the Landlock-absent degrade-open path in
      :meth:`LandlockSandboxBackend.activate`.

    :param libc: The shared ``CDLL(None)`` handle.
    :param add_rule_nr: ``landlock_add_rule`` syscall number for the arch.
    :param ruleset_fd: Open ruleset fd from ``landlock_create_ruleset``.
    :param path: Filesystem hierarchy root to grant access to.
    :param allowed_access: Subset of the ruleset's handled mask to grant.
    :param file_mask: File-applicable mask (:func:`_fs_file_mask_for_abi`)
        used to clamp *allowed_access* when *path* is not a directory.
    :raises OSError: When opening or adding the rule fails for any reason
        other than a non-existent path (fail-closed).
    """
    if allowed_access == 0:
        return
    try:
        parent_fd = os.open(str(path), _O_PATH | _O_CLOEXEC)
    except (FileNotFoundError, NotADirectoryError) as exc:
        # ENOENT / ENOTDIR — the granted path (or an intermediate
        # component) doesn't exist. Skippable: the spec may name a path
        # that hasn't been created yet. NOT a silent policy loss for a
        # path that exists but we couldn't open.
        _LOGGER.info(
            "linux_landlock: skipping rule for %s (path does not exist: %s)",
            path,
            exc,
        )
        return

    try:
        # Clamp to file-applicable rights when the parent is a regular
        # file (or anything non-directory): directory-only bits on a file
        # parent_fd make landlock_add_rule fail with EINVAL.
        st = os.fstat(parent_fd)
        if not stat.S_ISDIR(st.st_mode):
            allowed_access &= file_mask
            if allowed_access == 0:
                return
        path_attr = _LandlockPathBeneathAttr()
        path_attr.allowed_access = allowed_access
        path_attr.parent_fd = parent_fd
        ctypes.set_errno(0)
        rc = int(
            libc.syscall(
                add_rule_nr,
                ruleset_fd,
                _LANDLOCK_RULE_PATH_BENEATH,
                ctypes.byref(path_attr),
                0,
            )
        )
        if rc != 0:
            err = ctypes.get_errno()
            raise OSError(err, f"landlock_add_rule({path}) failed: {os.strerror(err)}")
    finally:
        os.close(parent_fd)


def _set_no_new_privs(libc: ctypes.CDLL) -> None:
    """
    Set ``PR_SET_NO_NEW_PRIVS`` on the current process.

    Mandatory before ``landlock_restrict_self`` for an unprivileged
    process: the kernel refuses to install a Landlock ruleset without it
    (otherwise a setuid exec could drop the confinement).

    :param libc: The shared ``CDLL(None)`` handle.
    :raises OSError: If ``prctl`` returns non-zero.
    """
    ctypes.set_errno(0)
    rc = int(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0))
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(err)}")


def _resolve_root(cwd: Path, root: str) -> Path:
    """
    Resolve a spec-supplied path string against *cwd*, expanding only
    ``~`` (NOT ``$VAR``) and normalising relative entries.

    Identical hardening to
    :func:`omnigent.inner.bwrap_sandbox._resolve_root`: ``$VAR`` is
    intentionally NOT expanded so the parent environment can't be used as
    a sandbox-widening lever (an attacker who shapes ``$HOME`` /
    ``$LOG_DIR`` could otherwise rewrite a narrow grant into ``/``). A
    ``$`` in a spec path is warned about so over-broad expansions stand
    out in logs.

    :param cwd: The agent's effective working directory; relative paths
        resolve against it.
    :param root: The raw path string from the YAML spec.
    :returns: An absolute, normalised :class:`Path` (no strict existence
        check — a granted path may not exist yet).
    """
    if "$" in root:
        _LOGGER.warning(
            "linux_landlock: spec-supplied path %r contains '$' which is "
            "not expanded against the parent environment (security "
            "hardening). Use literal paths or ~ instead.",
            root,
        )
    expanded = os.path.expanduser(root)
    path = Path(expanded)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


register_backend(LandlockSandboxBackend())
