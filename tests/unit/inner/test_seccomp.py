"""
Tests for :mod:`omnigent.inner._seccomp`.

The module owns two responsibilities and these tests cover both:

1. The libseccomp ctypes plumbing — that
   :func:`apply_seccomp_filter` actually loads a BPF program into the
   kernel and that argument-filtered rules (``SCMP_CMP_EQ``,
   ``SCMP_CMP_MASKED_EQ``) behave as expected against real syscalls.

2. The shared syscall denylist
   :data:`BASELINE_DENYLIST_SYSCALLS` — that the list stays aligned
   with the upstream Kubernetes / containerd ``RuntimeDefault``
   profile we derive it from, and that
   :func:`apply_baseline_denylist` actually engages the kernel.

Each test that installs a real seccomp filter does so inside a
forked child process so the BPF filter doesn't leak into pytest's
process and break the rest of the suite.
"""

from __future__ import annotations

import os
import platform
import sys

import pytest

from omnigent.inner._seccomp import (
    BASELINE_DENYLIST_SYSCALLS,
    _compat_arches_for_native,
)

# ---------------------------------------------------------------------------
# Baseline denylist content
# ---------------------------------------------------------------------------


# Subset of the Kubernetes / containerd ``RuntimeDefault`` profile we
# absolutely must keep blocking. Sourced by reading
# ``contrib/seccomp/seccomp_default.go`` and selecting the entries that
# are widely cited as "must-deny in any unprivileged Linux workload":
# kernel module loading, mount/pivot_root namespace primitives, kernel
# observability (BPF / perf_event_open), the keyring, kexec/reboot,
# the file-handle escape's open half, swap/quota DoS surface, and the
# CAP_SYS_TIME-gated time setters.
#
# Test failure here means a maintainer dropped one of these entries
# from :data:`BASELINE_DENYLIST_SYSCALLS`. Don't loosen this list to
# pass the test — re-read the upstream source first and only relax
# after a real audit.
_KUBERNETES_DEFAULT_HIGH_RISK = frozenset(
    {
        # Kernel module loading.
        "init_module",
        "finit_module",
        "delete_module",
        # Mount / namespace primitives.
        "mount",
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
        # Namespace creation.
        "unshare",
        "setns",
        # Kernel observability.
        "bpf",
        "perf_event_open",
        "userfaultfd",
        # File-handle escape (open half).
        "open_by_handle_at",
        # Kernel keyring.
        "add_key",
        "request_key",
        "keyctl",
        # Power / kernel control.
        "reboot",
        "kexec_load",
        "kexec_file_load",
        # Resource exhaustion.
        "swapon",
        "swapoff",
        "acct",
        "quotactl",
        # Time setters.
        "clock_settime",
        "settimeofday",
    }
)


# Syscalls the upstream profile *allows* but we deny anyway. Failure
# here means somebody quietly removed a deliberate hardening entry.
# These are documented in :data:`BASELINE_DENYLIST_SYSCALLS`'s module
# docstring under "Local additions beyond the upstream RuntimeDefault".
_LOCAL_ADDITIONS_BEYOND_K8S = frozenset(
    {
        "ptrace",
        "process_vm_readv",
        "process_vm_writev",
    }
)


# Syscalls every Python helper needs. If any of these end up in the
# baseline the agent helper can't even start. Acts as a tripwire
# against accidental over-blocking.
_MUST_NOT_BLOCK = frozenset(
    {
        "read",
        "write",
        "openat",
        "close",
        "mmap",
        "munmap",
        "brk",
        "exit",
        "exit_group",
        "rt_sigaction",
        "rt_sigprocmask",
        "fork",
        "execve",
        "wait4",
        "futex",
    }
)


def test_baseline_denylist_includes_kubernetes_high_risk_syscalls() -> None:
    """
    Every syscall in :data:`_KUBERNETES_DEFAULT_HIGH_RISK` must be in
    the baseline.

    The list was derived from the upstream
    ``contrib/seccomp/seccomp_default.go`` allowlist by selecting the
    entries that the upstream profile *blocks* (i.e. absent from the
    allowlist or only added under capabilities the helper never
    holds). Drift here means our baseline fell below the bar set by
    the most-deployed container runtime.
    """
    baseline = set(BASELINE_DENYLIST_SYSCALLS)
    missing = _KUBERNETES_DEFAULT_HIGH_RISK - baseline
    assert not missing, (
        "BASELINE_DENYLIST_SYSCALLS dropped these syscalls that the "
        "Kubernetes/containerd RuntimeDefault profile also blocks: "
        f"{sorted(missing)}. Re-read the upstream source before relaxing."
    )


def test_baseline_denylist_includes_local_hardening_additions() -> None:
    """
    Every syscall in :data:`_LOCAL_ADDITIONS_BEYOND_K8S` must be in
    the baseline.

    The upstream profile allows ``ptrace`` / ``process_vm_*`` on
    kernels >= 4.8; we deny outright because the agent helper has no
    legitimate ptrace use case and one of our backends has no PID
    namespace to bound the blast radius. Dropping any of these is a
    deliberate policy weakening that should never happen by accident.
    """
    baseline = set(BASELINE_DENYLIST_SYSCALLS)
    missing = _LOCAL_ADDITIONS_BEYOND_K8S - baseline
    assert not missing, (
        "BASELINE_DENYLIST_SYSCALLS dropped these intentional "
        "hardening additions: "
        f"{sorted(missing)}. See the module docstring on the constant."
    )


def test_baseline_denylist_does_not_block_essential_syscalls() -> None:
    """
    No syscall in :data:`_MUST_NOT_BLOCK` may appear in the baseline.

    These are syscalls a Python helper invokes during normal startup
    (interpreter init, signal handlers, fork/exec for subprocess
    tools). Blocking any of them turns the helper into a brick.
    """
    baseline = set(BASELINE_DENYLIST_SYSCALLS)
    leaked = _MUST_NOT_BLOCK & baseline
    assert not leaked, (
        "BASELINE_DENYLIST_SYSCALLS includes syscalls the helper "
        f"needs to run: {sorted(leaked)}. The baseline must be a "
        "denylist of administrative / dangerous syscalls only."
    )


def test_baseline_denylist_has_no_duplicates() -> None:
    """
    Each syscall name appears at most once in the baseline.

    Duplicates make the diff with upstream unreadable and add no
    security value (libseccomp would just register the same rule
    twice).
    """
    seen: dict[str, int] = {}
    for name in BASELINE_DENYLIST_SYSCALLS:
        seen[name] = seen.get(name, 0) + 1
    duplicates = {name: count for name, count in seen.items() if count > 1}
    assert not duplicates, f"Duplicate entries: {duplicates}"


# ---------------------------------------------------------------------------
# Behavioral: filters actually engage the kernel
# ---------------------------------------------------------------------------


def _run_in_child(probe: str) -> int:
    """
    Fork-exec a fresh Python with *probe* and return the child exit code.

    Forking-then-exec'ing isolates the seccomp filter to the child so
    the parent (pytest) keeps its full syscall surface. Communicating
    via exit code keeps the harness minimal — probes return 0 on
    success and non-zero with a recognizable code on failure.

    :param probe: Python source to run in the child.
    :returns: The child's exit code.
    """
    pid = os.fork()
    if pid == 0:
        os.execvp(sys.executable, [sys.executable, "-c", probe])
        os._exit(127)
    _, status = os.waitpid(pid, 0)
    if os.WIFEXITED(status):
        return os.WEXITSTATUS(status)
    return -1


def test_apply_baseline_denylist_blocks_ptrace_in_child() -> None:
    """
    :func:`apply_baseline_denylist` actually engages the kernel:
    ``ptrace`` returns ``EPERM`` after the filter loads.

    ``ptrace`` is a good behavioral smoke test because it's in the
    "local additions" group (so a regression in the entire deny
    pathway, not just one rule, would surface here) and it's safe to
    call with bogus arguments without side effects.

    Failure modes encoded in the child exit code:

    - 0: ptrace returned ``-1`` with ``EPERM`` (filter engaged).
    - 1: ptrace returned ``-1`` with a different errno (rule shape
      regressed but filter loaded).
    - 2: ptrace returned ``0`` (filter never engaged at all).
    """
    probe = (
        "import ctypes, errno, sys\n"
        "from omnigent.inner._seccomp import apply_baseline_denylist\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        # PR_SET_NO_NEW_PRIVS is required before seccomp_load for
        # unprivileged processes.
        "libc.prctl(38, 1, 0, 0, 0)\n"
        "apply_baseline_denylist()\n"
        "rc = libc.ptrace(0, 0, None, None)\n"
        "err = ctypes.get_errno()\n"
        "if rc == -1 and err == errno.EPERM:\n"
        "    sys.exit(0)\n"
        "elif rc == -1:\n"
        "    sys.exit(1)\n"
        "else:\n"
        "    sys.exit(2)\n"
    )
    rc = _run_in_child(probe)
    assert rc == 0, (
        "apply_baseline_denylist() did not produce EPERM from ptrace "
        f"(child exit code {rc}). Codes: 1 = wrong errno; 2 = filter "
        "never engaged."
    )


def test_apply_baseline_denylist_does_not_break_subprocess_basics() -> None:
    """
    A child that loads the baseline can still ``read``, ``write``,
    ``open``, ``execve`` — i.e. the baseline is genuinely a narrow
    denylist of administrative syscalls, not an over-zealous filter
    that breaks everyday I/O.

    Probe writes a small file to ``$TMPDIR``, reads it back, and
    re-execs ``/bin/true`` to exercise an extra exec path through the
    filter.
    """
    probe = (
        "import ctypes, os, sys, tempfile\n"
        "from omnigent.inner._seccomp import apply_baseline_denylist\n"
        "ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)\n"
        "apply_baseline_denylist()\n"
        "fd, path = tempfile.mkstemp()\n"
        "os.write(fd, b'hello'); os.close(fd)\n"
        "with open(path, 'rb') as f:\n"
        "    data = f.read()\n"
        "os.unlink(path)\n"
        "if data != b'hello':\n"
        "    sys.exit(2)\n"
        "pid = os.fork()\n"
        "if pid == 0:\n"
        "    os.execvp('/bin/true', ['/bin/true'])\n"
        "    os._exit(127)\n"
        "_, status = os.waitpid(pid, 0)\n"
        "sys.exit(0 if os.WIFEXITED(status) and os.WEXITSTATUS(status) == 0 else 3)\n"
    )
    rc = _run_in_child(probe)
    assert rc == 0, (
        "Baseline denylist broke a basic file-IO + fork/exec round-trip "
        f"(child exit {rc}). Codes: 2 = read-back mismatch; 3 = exec "
        "of /bin/true did not exit 0."
    )


def test_arg_filter_blocks_socket_family_only() -> None:
    """
    ``SCMP_CMP_EQ`` on ``socket(domain, ...)`` returns ``EPERM`` for
    the matching family while leaving other families open.

    Exercises :func:`apply_seccomp_filter`'s argument-filter path
    end-to-end against the kernel. The library itself doesn't ship
    any backend-specific socket policy — this test pins the rule
    *mechanics*; backend-specific allow/deny lists live in the
    backend modules and are tested there.
    """
    import socket as socket_module

    probe = (
        "import errno, json, socket, sys\n"
        "from omnigent.inner._seccomp import (\n"
        "    SCMP_CMP_EQ, SeccompArgFilter, SeccompRule,\n"
        "    apply_seccomp_filter, scmp_act_errno,\n"
        ")\n"
        "import ctypes\n"
        "ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)\n"
        f"NETLINK = {socket_module.AF_NETLINK}\n"
        "deny = scmp_act_errno(errno.EPERM)\n"
        "rules = [\n"
        "    SeccompRule(syscall='socket', action=deny,\n"
        "        arg_filters=(SeccompArgFilter(arg=0, op=SCMP_CMP_EQ,\n"
        "            datum_a=NETLINK),)),\n"
        "]\n"
        "apply_seccomp_filter(rules)\n"
        "results = {}\n"
        "for name, fam in [('AF_INET', socket.AF_INET),\n"
        "                  ('AF_NETLINK', socket.AF_NETLINK)]:\n"
        "    try:\n"
        "        s = socket.socket(fam, socket.SOCK_DGRAM, 0); s.close()\n"
        "        results[name] = 'opened'\n"
        "    except PermissionError:\n"
        "        results[name] = 'EPERM'\n"
        "    except OSError as e:\n"
        "        results[name] = f'err:{e.errno}'\n"
        "if results == {'AF_INET': 'opened', 'AF_NETLINK': 'EPERM'}:\n"
        "    sys.exit(0)\n"
        "print(json.dumps(results)); sys.exit(1)\n"
    )
    rc = _run_in_child(probe)
    assert rc == 0


def test_masked_eq_filter_blocks_clone_with_namespace_bit() -> None:
    """
    ``SCMP_CMP_MASKED_EQ`` on ``clone(flags, ...)`` returns ``EPERM``
    when the masked bit is set, while leaving plain ``fork()``-style
    ``clone`` calls untouched.

    Exercises :func:`apply_seccomp_filter`'s masked-equal arg-filter
    semantics end-to-end. If MASKED_EQ drifts, every per-bit
    namespace-escape rule any caller writes silently breaks.
    """
    probe = (
        "import ctypes, errno, os, sys\n"
        "from omnigent.inner._seccomp import (\n"
        "    SCMP_CMP_MASKED_EQ, SeccompArgFilter, SeccompRule,\n"
        "    apply_seccomp_filter, scmp_act_errno,\n"
        ")\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "libc.prctl(38, 1, 0, 0, 0)\n"
        "CLONE_NEWNET = 0x40000000\n"
        "deny = scmp_act_errno(errno.EPERM)\n"
        "apply_seccomp_filter([\n"
        "    SeccompRule(syscall='clone', action=deny,\n"
        "        arg_filters=(SeccompArgFilter(arg=0,\n"
        "            op=SCMP_CMP_MASKED_EQ,\n"
        "            datum_a=CLONE_NEWNET, datum_b=CLONE_NEWNET),)),\n"
        "])\n"
        "NR_CLONE = 56\n"
        "import signal\n"
        "rc = libc.syscall(NR_CLONE,\n"
        "    ctypes.c_ulong(CLONE_NEWNET | signal.SIGCHLD), 0, 0, 0, 0)\n"
        "err = ctypes.get_errno()\n"
        "if rc == -1 and err == errno.EPERM:\n"
        "    sys.exit(0)\n"
        "if rc > 0:\n"
        "    os.waitpid(rc, 0)\n"
        "sys.exit(1)\n"
    )
    rc = _run_in_child(probe)
    assert rc == 0


def test_unknown_syscall_silently_skipped() -> None:
    """
    A rule referencing a syscall name libseccomp can't resolve on
    this kernel must be silently dropped — that's how the helper
    accepts forward-compatible declarations like ``clone3`` on
    pre-5.3 kernels without erroring at filter-load time.

    The behavior is verified by trying to install a rule for a
    nonsensical syscall name and asserting the filter still loads
    successfully.
    """
    probe = (
        "import ctypes, errno, sys\n"
        "from omnigent.inner._seccomp import (\n"
        "    SeccompRule, apply_seccomp_filter, scmp_act_errno,\n"
        ")\n"
        "libc = ctypes.CDLL(None, use_errno=True)\n"
        "libc.prctl(38, 1, 0, 0, 0)\n"
        "deny = scmp_act_errno(errno.EPERM)\n"
        "apply_seccomp_filter([\n"
        "    SeccompRule(syscall='this_syscall_does_not_exist',\n"
        "        action=deny),\n"
        "])\n"
        "sys.exit(0)\n"
    )
    rc = _run_in_child(probe)
    assert rc == 0


# ---------------------------------------------------------------------------
# Multi-architecture coverage (the seccomp multi-arch bypass closure)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("machine", "expected"),
    [
        # x86_64 (the dominant Linux server arch) and its amd64 alias
        # both need i386 + x32 compat coverage. Without these, every
        # rule we install is silently bypassable via ``int $0x80``.
        ("x86_64", (b"x86", b"x32")),
        ("amd64", (b"x86", b"x32")),
        ("X86_64", (b"x86", b"x32")),
        # aarch64 hosts that ship 32-bit userspace need 32-bit ARM
        # compat. arm64 is Apple/Linux's preferred name for the same
        # arch (Linux kernel uses aarch64; Apple's clang reports
        # arm64).
        ("aarch64", (b"arm",)),
        ("arm64", (b"arm",)),
        # Native i386 / native armv7 / etc. have no narrower compat
        # ABI worth registering — the native init alone covers the
        # surface, so we return empty rather than asking libseccomp
        # to register an arch that doesn't exist.
        ("i686", ()),
        ("i386", ()),
        ("armv7l", ()),
        ("riscv64", ()),
        ("s390x", ()),
        ("ppc64le", ()),
        ("", ()),
    ],
)
def test_compat_arches_for_native(machine: str, expected: tuple[bytes, ...]) -> None:
    """
    :func:`_compat_arches_for_native` returns the right compat-arch
    set for each host machine name we expect to encounter.

    The mapping is small and well-defined; this test pins it so a
    refactor that drops or renames an entry surfaces immediately
    rather than silently regressing the multi-arch bypass closure.
    """
    assert _compat_arches_for_native(machine) == expected


@pytest.mark.skipif(
    platform.machine().lower() not in ("x86_64", "amd64"),
    reason="i386 ABI bypass test only meaningful on x86_64 hosts",
)
def test_seccomp_filter_applies_to_i386_compat_abi_on_x86_64() -> None:
    """
    A filter installed via :func:`apply_seccomp_filter` actually
    blocks syscalls issued through the i386 compat ABI (``int $0x80``)
    in addition to the native x86_64 ABI.

    This test pins the closure of the seccomp multi-architecture
    bypass: before :func:`_compat_arches_for_native` was wired in,
    every rule installed by :func:`apply_seccomp_filter` was silently
    bypassable on x86_64 by switching to a 32-bit syscall path.

    Probe shape: install a one-off filter that EPERMs ``getpid``
    (chosen because it's safe to call, normally always succeeds, and
    lets us distinguish "filter applied" from "syscall ran" by the
    return value), then issue ``getpid`` via 32-bit ``int $0x80``
    using a tiny shellcode page. Without the multi-arch fix the
    syscall returns the real PID (a positive integer); with the fix
    the kernel returns ``-EPERM``.

    Failure modes encoded in the child exit code:

    - 0: filter applied to i386 ABI (``-EPERM`` returned).
    - 2: bypass present — i386 ``getpid`` returned the real PID.
    - 3: kernel/system can't reach the shellcode (no IA32_EMULATION,
      W^X policy denied PROT_EXEC mmap, etc.) — skip rather than fail.
    """
    probe = (
        "import ctypes, errno, mmap, sys\n"
        "from omnigent.inner._seccomp import (\n"
        "    SeccompRule, apply_seccomp_filter, scmp_act_errno,\n"
        ")\n"
        "ctypes.CDLL(None, use_errno=True).prctl(38, 1, 0, 0, 0)\n"
        "apply_seccomp_filter([\n"
        "    SeccompRule(syscall='getpid', action=scmp_act_errno(errno.EPERM)),\n"
        "])\n"
        # i386 __NR_getpid = 20. Shellcode:
        #   mov  $20, %eax     (b8 14 00 00 00)
        #   int  $0x80         (cd 80)
        #   ret                (c3)
        # The bytes load eax with the i386 syscall number and trap into
        # the kernel via the 32-bit entry. ret pops back to ctypes which
        # returns whatever ended up in rax.
        "shellcode = bytes([0xb8, 0x14, 0x00, 0x00, 0x00,\n"
        "                   0xcd, 0x80,\n"
        "                   0xc3])\n"
        "try:\n"
        "    buf = mmap.mmap(-1, mmap.PAGESIZE,\n"
        "                    prot=mmap.PROT_READ | mmap.PROT_WRITE | mmap.PROT_EXEC)\n"
        "except (PermissionError, OSError):\n"
        "    sys.exit(3)\n"
        "buf.write(shellcode)\n"
        "addr = ctypes.addressof(ctypes.c_char.from_buffer(buf))\n"
        "fn = ctypes.CFUNCTYPE(ctypes.c_long)(addr)\n"
        "try:\n"
        "    result = fn()\n"
        "except OSError:\n"
        "    sys.exit(3)\n"
        "if result == -errno.EPERM:\n"
        "    sys.exit(0)\n"
        "if result > 0:\n"
        "    sys.exit(2)\n"
        "sys.exit(3)\n"
    )
    rc = _run_in_child(probe)
    if rc == 3:
        pytest.skip(
            "Kernel doesn't expose the i386 ABI on this host (no "
            "CONFIG_IA32_EMULATION, or W^X policy blocks PROT_EXEC "
            "mmap). The multi-arch wiring still applies but cannot be "
            "exercised end-to-end here."
        )
    assert rc == 0, (
        "i386 compat-ABI bypass detected: a getpid rule installed via "
        "apply_seccomp_filter did not engage when the syscall came in "
        f"via int $0x80 (child exit code {rc}). Code 2 means the "
        "syscall ran and returned the real PID; the seccomp filter "
        "was silently bypassed on the 32-bit ABI. This is the "
        "multi-arch footgun _compat_arches_for_native is meant to "
        "close — verify seccomp_arch_add is being called for both "
        "'x86' and 'x32'."
    )
