"""
Shared libseccomp filter loader and baseline syscall denylist.

This module owns two responsibilities so sandbox backends can stay thin:

1. **libseccomp ctypes plumbing.** Library loading, context lifecycle,
   rule addition, BPF program load. Backends declare rules as
   :class:`SeccompRule` instances and call :func:`apply_seccomp_filter`;
   they never touch the C ABI directly.

2. **A single shared syscall denylist** (:data:`BASELINE_DENYLIST_SYSCALLS`)
   exposed via :func:`apply_baseline_denylist`. Every sandbox backend
   that runs the helper as an unprivileged Linux process applies this
   list. The list is derived from the Kubernetes / containerd
   ``RuntimeDefault`` seccomp profile so we get the same kernel-attack-
   surface reduction the most-deployed container runtime in the world
   has been validated against — see the docstring on
   :data:`BASELINE_DENYLIST_SYSCALLS` for the upstream-vs-local diff.

Backends that need additional hardening (e.g. argument-filtered
``clone(CLONE_NEW*)`` blocks, socket-family allowlists) layer their
own :func:`apply_seccomp_filter` calls on top — the kernel ANDs
filters, so additive policies compose cleanly.

Multi-architecture coverage: every filter installed by
:func:`apply_seccomp_filter` covers the native ABI plus the relevant
compat ABIs for the host (i386 + x32 on x86_64, 32-bit ARM on
aarch64). The compat archs are enumerated by
:func:`_compat_arches_for_native` and registered via
``seccomp_arch_add`` immediately after ``seccomp_init``. Without
this an attacker on x86_64 could bypass every rule by issuing the
syscall via ``int $0x80`` — the classic seccomp multi-arch
footgun the OCI/k8s ``RuntimeDefault`` profile spends ~30 lines
defending against.

Architecture note on syscall arg ordering: this module passes each
:class:`SeccompArgFilter` ``arg`` index straight through to libseccomp,
which maps it to the kernel's syscall ABI register slot (``rdi``..``r9``
on x86_64, ``x0``..``x5`` on aarch64). For the syscalls we filter today
(``clone``, ``socket``, ``unshare``, ``setns``), arg 0 is the policy
input we care about on both x86_64 and aarch64. ``clone`` has a different
arg order on s390x (``child_stack`` is arg 0); we don't ship there today,
and a port would need to extend this module with an architecture-aware
arg map.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import errno
import platform
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import ClassVar

# libseccomp action codes. ``SCMP_ACT_ALLOW`` is the default action our
# callers use for the filter context; ``SCMP_ACT_ERRNO(N)`` returns errno
# ``N`` from the filtered syscall.
SCMP_ACT_ALLOW: int = 0x7FFF0000
_SCMP_ACT_ERRNO_BASE: int = 0x00050000

# libseccomp comparison ops mirroring ``enum scmp_compare`` from
# ``<seccomp.h>``. Exposed as module-level constants so callers don't have
# to remember the numeric values; tests use them too.
SCMP_CMP_NE: int = 1
SCMP_CMP_LT: int = 2
SCMP_CMP_LE: int = 3
SCMP_CMP_EQ: int = 4
SCMP_CMP_GE: int = 5
SCMP_CMP_GT: int = 6
SCMP_CMP_MASKED_EQ: int = 7


def scmp_act_errno(error_code: int) -> int:
    """
    Build the ``SCMP_ACT_ERRNO`` action code that returns ``error_code``
    from a filtered syscall.

    :param error_code: POSIX errno value to return when the rule matches,
        e.g. ``errno.EPERM``.
    :returns: The 32-bit action code accepted by libseccomp APIs (e.g.
        :func:`apply_seccomp_filter`).
    """
    return _SCMP_ACT_ERRNO_BASE | (error_code & 0x0000FFFF)


@dataclass(frozen=True)
class SeccompArgFilter:
    """
    Single argument comparator for a :class:`SeccompRule`.

    libseccomp matches the rule only when ALL of a rule's arg filters
    evaluate to true (per-rule AND). For "any of these arg patterns
    matches" semantics, add multiple :class:`SeccompRule` entries — one
    per pattern — pointing at the same syscall and action.

    :param arg: Zero-indexed syscall argument number, 0-5. Maps to the
        kernel's syscall ABI register for the host architecture, e.g.
        arg 0 is ``rdi`` on x86_64 and ``x0`` on aarch64.
    :param op: One of the ``SCMP_CMP_*`` constants exposed by this
        module, e.g. :data:`SCMP_CMP_EQ` or :data:`SCMP_CMP_MASKED_EQ`.
    :param datum_a: First comparison value. For most ops this is THE
        value compared against the syscall arg. For
        :data:`SCMP_CMP_MASKED_EQ` this is the bitmask applied to the
        arg before comparing against ``datum_b``.
    :param datum_b: Second comparison value. Used only by
        :data:`SCMP_CMP_MASKED_EQ` (the value the masked arg must equal).
        Ignored otherwise; defaults to ``0``.
    """

    arg: int
    op: int
    datum_a: int
    datum_b: int = 0


@dataclass(frozen=True)
class SeccompRule:
    """
    One seccomp filter rule.

    A rule carries the syscall to gate, the action to take when the rule
    matches, and an optional list of argument comparators. All arg
    comparators must match for the rule to fire (libseccomp's per-rule
    AND semantics).

    :param syscall: Syscall name as understood by
        ``seccomp_syscall_resolve_name``, e.g. ``"socket"`` or
        ``"clone"``. Names that don't resolve on the host kernel
        (e.g. ``"clone3"`` on a pre-5.3 kernel) are silently skipped
        by :func:`apply_seccomp_filter` — see that function's
        docstring for the rationale.
    :param action: Action code returned to the kernel when the rule
        matches, e.g. ``scmp_act_errno(errno.EPERM)``.
    :param arg_filters: Argument comparators (see
        :class:`SeccompArgFilter`). Empty tuple means "match any args"
        (gates the syscall unconditionally).
    """

    syscall: str
    action: int
    arg_filters: tuple[SeccompArgFilter, ...] = field(default_factory=tuple)


class _ScmpArgCmp(ctypes.Structure):
    """
    ctypes layout matching libseccomp's ``struct scmp_arg_cmp``.

    The struct is ``{unsigned int arg; enum scmp_compare op;
    scmp_datum_t datumA; scmp_datum_t datumB;}`` from ``<seccomp.h>``.
    With natural alignment that's 4 + 4 + 8 + 8 = 24 bytes; ctypes
    inserts the necessary padding so this layout is ABI-compatible
    with libseccomp on both x86_64 and aarch64.
    """

    _fields_: ClassVar[list[tuple[str, type]]] = [
        ("arg", ctypes.c_uint),
        ("op", ctypes.c_int),
        ("datum_a", ctypes.c_uint64),
        ("datum_b", ctypes.c_uint64),
    ]


def _compat_arches_for_native(machine: str) -> tuple[bytes, ...]:
    """
    Return the libseccomp arch tokens to register beyond the native ABI.

    libseccomp's ``seccomp_init`` only installs the filter for the
    architecture the process is running as. On any host where the
    kernel still services syscalls from a *different* architecture's
    ABI (the i386 ``int $0x80`` / ``sysenter`` path on x86_64, the
    x32 ABI on x86_64, the 32-bit ARM compat path on aarch64), every
    rule we install silently does nothing for syscalls coming in via
    that other ABI — the classic seccomp multi-architecture bypass
    that the OCI/k8s ``RuntimeDefault`` profile spends ~30 lines
    defending against.

    This helper enumerates the compat ABIs to add for the host the
    helper is running on so :func:`apply_seccomp_filter` can call
    ``seccomp_arch_add`` for each one. The returned names are the
    ASCII tokens libseccomp's ``seccomp_arch_resolve_name`` accepts.

    :param machine: Output of :func:`platform.machine`, e.g.
        ``"x86_64"`` or ``"aarch64"``. Compared case-insensitively.
    :returns: Tuple of arch-name byte strings. Empty when the native
        arch has no narrower compat ABI worth covering (e.g. the host
        is already i386 or 32-bit ARM, so there's nothing further to
        register).
    """
    normalized = machine.lower()
    if normalized in ("x86_64", "amd64"):
        # Linux on x86_64 with CONFIG_IA32_EMULATION (default on every
        # mainstream distro) lets unprivileged processes issue 32-bit
        # syscalls via ``int $0x80``. The x32 ABI uses 32-bit pointers
        # but 64-bit registers and shares syscall numbers with x86_64
        # plus a high bit; libseccomp treats it as a separate arch.
        return (b"x86", b"x32")
    if normalized in ("aarch64", "arm64"):
        # 32-bit ARM userspace can run on aarch64 kernels with the
        # COMPAT layer. Distros that ship 32-bit packages (Raspberry
        # Pi OS, some Debian flavors) leave the compat path enabled.
        return (b"arm",)
    # Native i386, native armv7, riscv64, s390x, ppc64le, etc.: no
    # narrower compat ABI on this machine, so the native init alone
    # covers the surface.
    return ()


def apply_seccomp_filter(
    rules: Sequence[SeccompRule],
    *,
    default_action: int = SCMP_ACT_ALLOW,
) -> None:
    """
    Install a seccomp BPF filter built from *rules* on the current process.

    The function is a one-shot: it builds a fresh libseccomp context with
    *default_action*, registers every relevant compat architecture (see
    :func:`_compat_arches_for_native`), adds every rule, calls
    ``seccomp_load`` to commit the filter to the kernel, and releases
    the context. Subsequent calls install additional independent filters
    layered on top of the first one (the kernel ANDs them).

    Multi-architecture coverage: in addition to the native ABI that
    ``seccomp_init`` registers automatically, this function calls
    ``seccomp_arch_add`` for each compat ABI returned by
    :func:`_compat_arches_for_native`. On x86_64 that means rules also
    apply to syscalls issued via the i386 (``int $0x80``) and x32
    paths; on aarch64 it adds 32-bit ARM. Without this every rule
    here is silently bypassable on x86_64 by emitting a 32-bit
    syscall, which is the canonical seccomp multi-arch footgun.

    Rules whose syscall name doesn't resolve on the host kernel are
    skipped — this matches libseccomp's own forgiving "use whatever's
    on the kernel" stance and lets callers list forward-compatible
    syscalls (e.g. ``clone3``) without a hard dependency on a kernel
    version. Callers who care that a specific rule landed must verify
    it via runtime probes (see ``tests/inner/test_bwrap_sandbox.py``).

    :param rules: Rules to install. Order is not significant — libseccomp
        builds a single decision tree.
    :param default_action: Action returned for syscalls no rule matches.
        Defaults to :data:`SCMP_ACT_ALLOW` so the filter is a denylist
        layered on top of the otherwise-permissive baseline. Pass
        :func:`scmp_act_errno` ``(errno.EPERM)`` for an allowlist instead.
    :raises OSError: If libseccomp cannot be loaded, the context cannot
        be initialized, a compat-arch token cannot be resolved, a rule
        fails to add for a non-skip reason, or ``seccomp_load`` fails.
    """
    lib = _load_libseccomp()
    ctx = lib.seccomp_init(ctypes.c_uint32(default_action))
    if not ctx:
        raise OSError(errno.ENOMEM, "seccomp_init failed")
    try:
        for arch_name in _compat_arches_for_native(platform.machine()):
            arch_token = lib.seccomp_arch_resolve_name(arch_name)
            if arch_token == 0:
                raise OSError(
                    errno.ENOTSUP,
                    f"seccomp_arch_resolve_name({arch_name.decode('ascii')!r}) "
                    f"returned 0 — libseccomp does not recognize this compat "
                    f"ABI. Upgrade libseccomp or file a bug; the sandbox "
                    f"cannot guarantee syscall filtering across all ABIs "
                    f"without this architecture registered.",
                )
            rc = lib.seccomp_arch_add(ctx, ctypes.c_uint32(arch_token))
            # ``-EEXIST`` means the arch was already in the filter
            # (typically the native arch returned by the helper, which
            # ``seccomp_init`` already registered). Treat as success.
            if rc != 0 and rc != -errno.EEXIST:
                raise OSError(
                    -rc,
                    f"seccomp_arch_add({arch_name.decode('ascii')!r}) failed (rc={rc})",
                )
        for rule in rules:
            syscall_nr = lib.seccomp_syscall_resolve_name(rule.syscall.encode("ascii"))
            if syscall_nr < 0:
                # Unknown to this kernel — caller declared a forward-
                # compatible rule (e.g. ``clone3`` on pre-5.3 kernels).
                # Skip rather than erroring, mirroring libseccomp's
                # own permissive design.
                continue
            if rule.arg_filters:
                arg_array_t = _ScmpArgCmp * len(rule.arg_filters)
                arg_array = arg_array_t(
                    *(
                        _ScmpArgCmp(
                            arg=ctypes.c_uint(f.arg),
                            op=ctypes.c_int(f.op),
                            datum_a=ctypes.c_uint64(f.datum_a),
                            datum_b=ctypes.c_uint64(f.datum_b),
                        )
                        for f in rule.arg_filters
                    )
                )
                rc = lib.seccomp_rule_add_array(
                    ctx,
                    ctypes.c_uint32(rule.action),
                    ctypes.c_int(syscall_nr),
                    ctypes.c_uint(len(rule.arg_filters)),
                    arg_array,
                )
            else:
                rc = lib.seccomp_rule_add(
                    ctx,
                    ctypes.c_uint32(rule.action),
                    ctypes.c_int(syscall_nr),
                    ctypes.c_uint(0),
                )
            if rc != 0:
                raise OSError(
                    -rc,
                    f"seccomp_rule_add failed for {rule.syscall} (rc={rc})",
                )

        rc = lib.seccomp_load(ctx)
        if rc != 0:
            raise OSError(-rc, f"seccomp_load failed (rc={rc})")
    finally:
        lib.seccomp_release(ctx)


def _load_libseccomp() -> ctypes.CDLL:
    """
    Load libseccomp via ctypes and configure all symbol signatures used
    by :func:`apply_seccomp_filter`.

    Centralised here so the two backends don't redeclare the same
    argtypes/restype contracts and silently drift on libseccomp ABI
    changes. Callers that only need :func:`apply_seccomp_filter` should
    not import this directly.

    :returns: The loaded :class:`ctypes.CDLL` with all needed symbols
        configured.
    :raises OSError: If libseccomp cannot be located on the system.
    """
    lib_name = ctypes.util.find_library("seccomp") or "libseccomp.so.2"
    lib = ctypes.CDLL(lib_name, use_errno=True)

    lib.seccomp_init.argtypes = [ctypes.c_uint32]
    lib.seccomp_init.restype = ctypes.c_void_p

    lib.seccomp_release.argtypes = [ctypes.c_void_p]
    lib.seccomp_release.restype = None

    lib.seccomp_syscall_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_syscall_resolve_name.restype = ctypes.c_int

    lib.seccomp_rule_add.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
    ]
    lib.seccomp_rule_add.restype = ctypes.c_int

    lib.seccomp_rule_add_array.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_int,
        ctypes.c_uint,
        ctypes.POINTER(_ScmpArgCmp),
    ]
    lib.seccomp_rule_add_array.restype = ctypes.c_int

    lib.seccomp_load.argtypes = [ctypes.c_void_p]
    lib.seccomp_load.restype = ctypes.c_int

    # Multi-architecture wiring. ``seccomp_init`` only registers the
    # native ABI; without an explicit ``seccomp_arch_add`` per compat
    # ABI, every rule we install silently fails to fire when a syscall
    # comes in via a non-native architecture (the classic seccomp
    # multi-arch footgun — see :func:`_compat_arches_for_native`).
    lib.seccomp_arch_resolve_name.argtypes = [ctypes.c_char_p]
    lib.seccomp_arch_resolve_name.restype = ctypes.c_uint32

    lib.seccomp_arch_add.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    lib.seccomp_arch_add.restype = ctypes.c_int

    return lib


# ---------------------------------------------------------------------------
# Shared baseline syscall denylist (Kubernetes / containerd RuntimeDefault)
# ---------------------------------------------------------------------------

# Syscalls that the upstream Kubernetes / containerd ``RuntimeDefault``
# seccomp profile rejects for an unprivileged container (no extra
# capabilities granted). Source of truth:
# https://github.com/containerd/containerd/blob/main/contrib/seccomp/seccomp_default.go
#
# That profile is an *allowlist* (default action = ENOSYS, with a
# curated allow set). We re-express it as a denylist (default action =
# ALLOW with explicit ``EPERM`` rules) so the two sandbox backends can
# layer additional rules on top without having to maintain an exhaustive
# allowlist of every syscall a Python helper might ever issue.
#
# Maintenance protocol when upstream changes:
#   1. Re-fetch ``contrib/seccomp/seccomp_default.go``.
#   2. Diff its allowlist + capability-gated additions against this list.
#   3. Add any newly-blocked syscall to the appropriate group below.
#   4. Update ``tests/inner/test_seccomp.py``'s critical-syscall list to
#      include any new high-risk additions.
#
# Two intentional deviations from the upstream profile:
#
# - ``ptrace`` / ``process_vm_readv`` / ``process_vm_writev``:
#     upstream allows these on kernels >= 4.8 because container
#     runtimes bound their effect via PID namespacing + the
#     ``ptrace_scope`` sysctl. We deny outright. Backends that wrap
#     the helper in a fresh PID namespace gain nothing legitimate
#     from these syscalls, and backends that share the host PID
#     namespace would otherwise let the helper ``ptrace`` arbitrary
#     host processes the user owns. Either way: zero legitimate
#     use, real escape vector — block.
#
# - ``name_to_handle_at`` is allowed (upstream also allows it). The
#     dangerous half of the file-handle-escape pair —
#     ``open_by_handle_at`` — stays blocked, which neutralises the
#     attack on its own.
#
# Grouped by kernel subsystem so additions are easy to audit.
BASELINE_DENYLIST_SYSCALLS: tuple[str, ...] = (
    # Kernel module loading and introspection (legacy + current).
    "init_module",
    "finit_module",
    "delete_module",
    "create_module",
    "query_module",
    "get_kernel_syms",
    # Filesystem mount / namespace primitives. New mount API (fsopen,
    # fsmount, fsconfig, fspick, move_mount, open_tree, mount_setattr)
    # plus the legacy mount/umount/pivot_root/chroot family.
    "mount",
    "umount",
    "umount2",
    "pivot_root",
    "chroot",
    "open_tree",
    "move_mount",
    "fsopen",
    "fsconfig",
    "fsmount",
    "fspick",
    "mount_setattr",
    # File-handle escape vector (the "open" half — name resolution
    # alone is harmless and stays allowed).
    "open_by_handle_at",
    # Legacy / dangerous filesystem syscalls.
    "nfsservctl",
    "sysfs",
    "_sysctl",
    "ustat",
    "uselib",
    "quotactl",
    "quotactl_fd",
    # Namespace creation / joining. Note: the bwrap backend layers an
    # argument-filtered ``clone(CLONE_NEW*)`` rule on top to also block
    # nested namespace creation via ``clone()``; that rule is not in
    # this baseline because some legitimate libc paths call ``clone``
    # without namespace flags and we want this baseline to be safe to
    # apply unconditionally.
    "unshare",
    "setns",
    # Kernel observability / tracing with privesc CVE history.
    "bpf",
    "perf_event_open",
    "userfaultfd",
    "kcmp",
    "lookup_dcookie",
    "fanotify_init",
    # System time / clock manipulation. Read-side syscalls
    # (``clock_gettime``, ``clock_getres``, ``adjtimex``,
    # ``clock_adjtime``) stay allowed; only the setters are blocked.
    "clock_settime",
    "clock_settime64",
    "settimeofday",
    "stime",
    # Power, kernel control, and the kernel ring buffer.
    "reboot",
    "kexec_load",
    "kexec_file_load",
    "syslog",
    # Resource exhaustion / DoS surface.
    "swapon",
    "swapoff",
    "acct",
    # NUMA memory policy.
    "mbind",
    "migrate_pages",
    "move_pages",
    "set_mempolicy",
    "get_mempolicy",
    "set_mempolicy_home_node",
    # I/O port access (x86) and legacy 8086 emulation.
    "ioperm",
    "iopl",
    "vm86",
    "vm86old",
    # Hostname / domain name (CAP_SYS_ADMIN-gated upstream).
    "sethostname",
    "setdomainname",
    # TTY hangup (CAP_SYS_TTY_CONFIG-gated upstream).
    "vhangup",
    # Cross-process file-descriptor and madvise control
    # (CAP_SYS_PTRACE-gated upstream; we block since the helper has
    # no legitimate cross-process use case).
    "pidfd_getfd",
    "process_madvise",
    # Kernel keyring (token-theft surface).
    "add_key",
    "request_key",
    "keyctl",
    # ----- Local additions beyond the upstream RuntimeDefault -----
    # See module-level rationale above. The agent helper has no
    # legitimate ptrace use, and not every backend that consumes
    # this baseline gives the helper a fresh PID namespace, so we
    # deny outright.
    "ptrace",
    "process_vm_readv",
    "process_vm_writev",
)


def apply_baseline_denylist() -> None:
    """
    Install :data:`BASELINE_DENYLIST_SYSCALLS` as ``EPERM`` rules on the
    current process via :func:`apply_seccomp_filter`.

    The default action stays :data:`SCMP_ACT_ALLOW`, so the filter is a
    pure denylist that callers can layer additional rules on top of
    (the kernel ANDs subsequent ``apply_seccomp_filter`` calls).

    Caller contract: ``PR_SET_NO_NEW_PRIVS`` must already be set on the
    current process. Without it the kernel rejects ``seccomp_load``
    unless the caller has ``CAP_SYS_ADMIN``, and we don't run with that.
    Both shipped backends call the appropriate ``prctl`` before invoking
    this helper.

    :raises OSError: Propagated from :func:`apply_seccomp_filter` if
        libseccomp can't load, the filter context can't be built, or
        the kernel rejects the load.
    """
    deny = scmp_act_errno(errno.EPERM)
    rules = [SeccompRule(syscall=name, action=deny) for name in BASELINE_DENYLIST_SYSCALLS]
    apply_seccomp_filter(rules)
