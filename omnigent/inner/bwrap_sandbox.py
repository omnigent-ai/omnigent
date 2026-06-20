"""
Linux Bubblewrap sandbox backend.

Builds a hermetic mount-namespace view via the ``bwrap`` launcher and
layers a hardened seccomp profile on top once the helper has exec'd
inside the namespace. Selected as the Linux platform default when
the ``bwrap`` binary is on ``PATH`` (see
:func:`omnigent.inner.sandbox._default_sandbox_for_platform`); spec
authors can also pin it explicitly via ``os_env.sandbox.type:
linux_bwrap`` in YAML.

Default view inside the sandbox:

- ``/usr``, ``/lib*``, ``/bin``, ``/sbin`` mounted read-only.
- A small allow-list of ``/etc`` files needed for libc / DNS / TLS
  (``/etc/resolv.conf``, ``/etc/hosts``, ``/etc/nsswitch.conf``,
  ``/etc/passwd``, ``/etc/group``, ``/etc/localtime``,
  ``/etc/ld.so.cache``, ``/etc/ld.so.conf``,
  ``/etc/ld.so.conf.d``, ``/etc/ssl``, ``/etc/ca-certificates``,
  ``/etc/pki``) bound read-only via ``--ro-bind-try``.
- Fresh ``/proc``, ``/dev``, and ``/tmp``.
- Cwd bind-mounted read-only by default; explicit
  ``write_paths: ["."]`` flips it to read-write. Top-level
  dotfiles / dotdirs in cwd are tmpfs-masked unless their name is
  in :data:`_DEFAULT_CWD_ALLOW_HIDDEN` (``.venv`` by default) or
  :attr:`OSEnvSandboxSpec.cwd_allow_hidden`.
- The per-helper scratch tmpdir created by
  :func:`omnigent.inner.sandbox.create_private_tmpdir` is
  bind-mounted read-write and surfaced via ``$TMPDIR``.
- ``$HOME`` is **never** mounted. ``/root``, ``/var``, and host
  dotfiles never leak.

Hardened seccomp profile applied by :meth:`BwrapSandboxBackend.activate`
inside the helper, on top of ``PR_SET_NO_NEW_PRIVS``:

1. :data:`omnigent.inner._seccomp.BASELINE_DENYLIST_SYSCALLS` — the
   shared k8s/containerd-derived denylist (mount/pivot_root family,
   kernel-module loaders, BPF, kernel keyring, time-setters,
   namespace primitives, plus our own ``ptrace``-family hardening).
2. Argument-filtered ``clone(flags, ...)``: any ``CLONE_NEW*`` bit
   set in arg 0 returns ``EPERM``. ``fork()`` /
   ``pthread_create()`` keep working (they don't set ``CLONE_NEW*``).
3. ``clone3`` denied with ``ENOSYS`` (not ``EPERM``) — the
   ``struct clone_args`` flags live in user memory that seccomp
   can't dereference, so we can't arg-filter ``clone3``. We return
   ``ENOSYS`` because glibc's ``clone_internal`` only falls back to
   the legacy ``clone`` syscall on ``ENOSYS``; returning ``EPERM``
   here makes ``pthread_create`` fail outright on Ubuntu 22.04 +
   glibc 2.34+ (which use ``clone3`` for thread creation), which
   would break the in-helper egress relay thread. Same trade-off
   Flatpak / Snap / Firejail accept.
4. Argument-filtered ``socket(domain, ...)``: an allowlist of
   ``AF_UNIX`` (1), ``AF_INET`` (2), ``AF_INET6`` (10). Every other
   socket family is denied with ``EPERM``. This is future-proof —
   new kernel socket families are blocked by default without code
   changes.
"""

from __future__ import annotations

import ctypes
import errno
import logging
import os
import shutil
import sys
from collections.abc import Sequence
from pathlib import Path

from ._cwd_scan import scan_cwd_mask_entries
from ._seccomp import (
    SCMP_CMP_EQ,
    SCMP_CMP_GE,
    SCMP_CMP_MASKED_EQ,
    SeccompArgFilter,
    SeccompRule,
    apply_baseline_denylist,
    apply_seccomp_filter,
    scmp_act_errno,
)
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .sandbox import (
    SandboxBackend,
    SandboxPolicy,
    register_backend,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_PR_SET_NO_NEW_PRIVS = 38

# Top-level cwd dotfiles allowed through by default when the spec
# doesn't override ``cwd_allow_hidden``. ``.venv`` is whitelisted so the
# common Python project layout (per the project's CLAUDE.md guidance to
# use ``.venv`` with ``uv``) keeps working out of the box.
_DEFAULT_CWD_ALLOW_HIDDEN = (".venv",)

# Read-only directory binds that make the standard Linux toolchain
# usable inside the sandbox (libc, dynamic linker, system Python and
# friends). Each is emitted as ``--ro-bind-try`` so missing entries
# (e.g. /sbin on systemd merged-/usr distros) don't fail the spawn.
_DEFAULT_RO_DIRS = (
    "/usr",
    "/lib",
    "/lib64",
    "/lib32",
    "/bin",
    "/sbin",
)

# Read-only file binds for the minimal /etc files libc, DNS, the
# dynamic linker, and TLS clients reach for. Bind individual files
# (not /etc/) so /etc/shadow, /etc/sudoers, /etc/cron*, etc. are
# never visible inside the sandbox.
_DEFAULT_ETC_FILES = (
    "/etc/resolv.conf",
    "/etc/hosts",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
    "/etc/ld.so.cache",
    "/etc/ld.so.conf",
)

# Read-only directory binds for the multi-file /etc subtrees — linker
# search paths and TLS trust stores. Bound as directories so adding /
# updating cert bundles on the host stays transparent inside the
# sandbox.
_DEFAULT_ETC_DIRS = (
    "/etc/ld.so.conf.d",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/pki",
)

# Linux ``CLONE_NEW*`` flag bits. Any of these set in arg 0 of
# ``clone()`` would create a fresh namespace — the canonical user-
# namespace privilege-escalation pattern out of bwrap-style sandboxes.
# Defined here (instead of relying on a header import) because Python
# doesn't expose them in ``os`` and we don't want a libc lookup at
# activation time.
_CLONE_NEWNS = 0x00020000
_CLONE_NEWCGROUP = 0x02000000
_CLONE_NEWUTS = 0x04000000
_CLONE_NEWIPC = 0x08000000
_CLONE_NEWUSER = 0x10000000
_CLONE_NEWPID = 0x20000000
_CLONE_NEWNET = 0x40000000

_CLONE_NEW_FLAG_BITS = (
    _CLONE_NEWNS,
    _CLONE_NEWCGROUP,
    _CLONE_NEWUTS,
    _CLONE_NEWIPC,
    _CLONE_NEWUSER,
    _CLONE_NEWPID,
    _CLONE_NEWNET,
)

# Socket families the helper is allowed to create. Everything else is
# denied with EPERM. Numeric values are inlined so the activation path
# never imports ``socket`` (which on import opens netlink sockets on
# some platforms — pointless work inside a hardened helper).
#
# References: ``include/linux/socket.h`` in the Linux source tree.
_ALLOWED_SOCKET_FAMILIES = (
    1,  # AF_UNIX  — local IPC
    2,  # AF_INET  — IPv4
    10,  # AF_INET6 — IPv6
)


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class BwrapSandboxBackend(SandboxBackend):
    """
    Bubblewrap-based sandbox backend.

    Resolves a :class:`SandboxPolicy` from an :class:`OSEnvSpec`,
    builds the ``bwrap`` argv at spawn time
    (:meth:`wrap_launcher_argv`), and applies the hardened seccomp
    profile inside the helper subprocess once it has exec'd
    (:meth:`activate`).

    Stateless: a single shared instance is registered with the
    sandbox registry at module import time.
    """

    type_name = "linux_bwrap"

    def resolve(self, spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        """
        Build a :class:`SandboxPolicy` for the bwrap backend.

        Three resolver behaviors specific to this backend:

        - ``write_paths`` defaults to **empty** — cwd is read-only
          unless the spec sets ``write_paths: ["."]`` explicitly.
        - ``cwd_allow_hidden`` falls back to
          :data:`_DEFAULT_CWD_ALLOW_HIDDEN` when the spec doesn't
          declare one.
        - The bwrap binary must be on ``PATH`` or the resolver fails
          loud with an install hint — no silent fallback.

        :param spec: The agent's :class:`OSEnvSpec`. ``spec.sandbox``
            is read for backend tunables; the rest of the spec is
            unused by this backend.
        :param cwd: Effective working directory of the helper, e.g.
            the project root. Used to resolve relative entries in
            ``read_paths`` / ``write_paths`` / ``write_files``.
        :returns: A populated :class:`SandboxPolicy` ready to be
            consumed by :meth:`wrap_launcher_argv` and
            :meth:`activate`.
        :raises OSError: If the host is not Linux or the ``bwrap``
            binary cannot be located.
        """
        sandbox_spec = spec.sandbox or OSEnvSandboxSpec(type=self.type_name)

        if os.name != "posix" or not sys.platform.startswith("linux"):
            raise OSError(
                "linux_bwrap sandbox is only available on Linux. "
                "Configure os_env.sandbox.type='none' on other OSes."
            )
        if shutil.which("bwrap") is None:
            raise OSError(
                "linux_bwrap sandbox requires the 'bwrap' binary on PATH. "
                "Install bubblewrap (e.g. `apt install bubblewrap` or "
                "`dnf install bubblewrap`), or set os_env.sandbox.type to "
                "'none' to disable sandboxing."
            )

        read_roots: list[Path] | None = None
        if sandbox_spec.read_paths is not None:
            read_roots = [_resolve_root(cwd, root) for root in sandbox_spec.read_paths]

        # Bwrap-specific default: cwd is RO unless the spec opts in.
        # Empty default honors the "no surprise writes" contract from
        # the design plan — agents that need an editable project tree
        # opt in via ``write_paths: ["."]``.
        write_paths_config = (
            sandbox_spec.write_paths if sandbox_spec.write_paths is not None else []
        )
        write_roots = [_resolve_root(cwd, root) for root in write_paths_config]

        write_files: list[Path] = []
        if sandbox_spec.write_files is not None:
            write_files.extend(_resolve_root(cwd, path) for path in sandbox_spec.write_files)

        cwd_allow_hidden = (
            list(sandbox_spec.cwd_allow_hidden)
            if sandbox_spec.cwd_allow_hidden is not None
            else list(_DEFAULT_CWD_ALLOW_HIDDEN)
        )

        return SandboxPolicy(
            backend_type=self.type_name,
            active=True,
            read_roots=read_roots,
            write_roots=write_roots,
            write_files=write_files,
            allow_network=sandbox_spec.allow_network,
            cwd_allow_hidden=cwd_allow_hidden,
            cwd_hidden_scan_max_entries=sandbox_spec.cwd_hidden_scan_max_entries,
            cwd_hidden_scan_overflow=sandbox_spec.cwd_hidden_scan_overflow,
            env_passthrough=(
                list(sandbox_spec.env_passthrough)
                if sandbox_spec.env_passthrough is not None
                else None
            ),
            credential_proxy=sandbox_spec.credential_proxy,
        )

    def wrap_launcher_argv(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        cwd: Path,
        chdir: Path | None = None,
        target: str | None = None,
    ) -> list[str]:
        """
        Build the ``bwrap`` argv that wraps *argv* with the hermetic
        sandbox view described in this module's docstring.

        :param argv: The unwrapped helper command, typically
            ``[sys.executable, "-m", "omnigent.inner.os_env",
            "helper", "<encoded>"]``.
        :param policy: The :class:`SandboxPolicy` produced by
            :meth:`resolve`, possibly augmented by
            :func:`omnigent.inner.sandbox.with_additional_write_roots`
            (the parent adds the per-helper scratch tmpdir there
            before calling this).
        :param cwd: Workspace directory exposed to the helper. Bound
            read-only (or read-write if a ``write_root`` matches it)
            at its real absolute path, and walked for the dotfile /
            escaping-symlink mask. Always exposed regardless of
            *chdir* so the agent can reach project files via absolute
            paths even when the helper starts elsewhere.
        :param chdir: Optional separate target for ``--chdir``. When
            ``None`` (the default), the helper starts in *cwd*. When
            set — typically to the per-helper scratch tmpdir for
            ``OSEnvSpec.start_in_scratch`` — the helper starts there
            instead while *cwd* stays bound for reads.
        :param target: Absolute path to the binary that the launcher
            will exec as its final target after the re-exec (e.g. the
            ``claude`` CLI installed under ``node_modules/.bin/``).
            When set and the path lives outside the default mounts,
            :func:`_ensure_executable_visible` is called for it so
            its directory chain is bind-mounted into the namespace —
            the same treatment ``argv[0]`` (the Python interpreter)
            already receives.  ``None`` when the target is already
            reachable via the standard mounts.
        :returns: A complete ``bwrap`` argv ready for
            ``subprocess.Popen`` — never an empty list.

        Environment containment is deliberately NOT done here with
        ``--clearenv`` / ``--setenv``: values embedded in the bwrap
        argv are world-readable via ``/proc/<pid>/cmdline`` for the
        sandbox's lifetime, which would leak spec-granted secrets to
        every host process. The child instead inherits the spawner's
        already-filtered environment, enforced by the
        ``SandboxPolicy.spawn_env_allowlist`` prune in
        :func:`omnigent.inner.sandbox.run_launcher`.
        """
        cwd_resolved = cwd.resolve(strict=False)
        chdir_target = chdir.resolve(strict=False) if chdir is not None else cwd_resolved
        bwrap_args: list[str] = ["bwrap"]

        for path in _DEFAULT_RO_DIRS:
            bwrap_args += ["--ro-bind-try", path, path]
        for path in _DEFAULT_ETC_FILES:
            bwrap_args += ["--ro-bind-try", path, path]
        for path in _DEFAULT_ETC_DIRS:
            bwrap_args += ["--ro-bind-try", path, path]

        # /proc, /dev (filtered by bwrap to a safe minimal device set),
        # and a private /tmp so the agent's writes there don't pollute
        # the host.
        bwrap_args += [
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",
        ]

        # Make sure argv[0] (the helper interpreter) is reachable inside
        # the sandbox even when it lives outside the default mounts —
        # e.g. a pyenv install under ``$HOME/.pyenv``.
        bwrap_args += _ensure_executable_visible(argv, cwd_resolved)

        # Make sure the final target binary (e.g. the claude CLI at
        # node_modules/.bin/claude) is also reachable.  The launcher
        # re-execs itself into the bwrap namespace and then runs the
        # target via subprocess.run — without this bind the target's
        # directory is invisible inside the namespace and the exec
        # fails with FileNotFoundError.
        if target is not None:
            bwrap_args += _ensure_executable_visible([target], cwd_resolved)

        # cwd bind: writable iff a write_root resolves to cwd.
        cwd_writable = any(_is_same_path(root, cwd_resolved) for root in policy.write_roots)
        bwrap_args += [
            "--bind" if cwd_writable else "--ro-bind",
            str(cwd_resolved),
            str(cwd_resolved),
        ]

        # Additional write roots — typically the per-helper scratch
        # tmpdir. We skip cwd here because it was bound above.
        for root in policy.write_roots:
            if _is_same_path(root, cwd_resolved):
                continue
            bwrap_args += ["--bind-try", str(root), str(root)]

        # Per-file write grants. ``--bind-try`` with a file source
        # creates a file-to-file bind mount; the parent dir must
        # already exist inside the sandbox view (typically because the
        # caller also added the parent to read_paths or write_paths).
        for fpath in policy.write_files:
            bwrap_args += ["--bind-try", str(fpath), str(fpath)]

        # Extra read roots beyond the default toolchain bind set.
        if policy.read_roots is not None:
            for root in policy.read_roots:
                bwrap_args += ["--ro-bind-try", str(root), str(root)]

        # Mask dotfiles anywhere under cwd OR under any ``read_paths``
        # root that aren't on the allowlist, plus any symlink (at any
        # depth) whose target escapes the sandbox mount set
        # (host-relative dereference defense). The walker prunes at
        # masked dot-directories so ``.git/objects`` etc. don't count
        # toward the cap.
        #
        # Mask emission MUST come AFTER the cwd / write_paths /
        # read_paths binds above: bwrap mount semantics layer later
        # mounts ON TOP of earlier ones, so a ``--tmpfs <root>/.aws``
        # emitted before ``--ro-bind-try <root>`` would be shadowed
        # by the read_paths bind and the helper would silently see
        # the host's ``.aws/`` content. Emitting the mask last keeps
        # the deny-wins guarantee on Linux regardless of how many
        # broader binds the policy stacks underneath.
        bwrap_args.extend(
            _dotfile_and_symlink_mask_args(
                cwd_resolved,
                policy.cwd_allow_hidden if policy.cwd_allow_hidden is not None else [],
                policy,
                argv=argv,
            )
        )

        # AF_UNIX control-socket masks. A denied socket
        # (e.g. the managed tmux control socket) lives inside a bound
        # write root — the instance ``private_dir`` — so the helper can
        # otherwise reach it and ``connect(2)`` to the unsandboxed tmux
        # server, whose ``run-shell`` would execute outside the sandbox.
        # Overlay ``/dev/null`` onto each socket path so the helper sees
        # a character device, not a socket, and ``connect`` fails. The
        # host (which manages the server) keeps the real socket — the
        # mask only exists inside the helper's mount namespace. Emitted
        # AFTER the write-root binds so it is the last mount and wins
        # (same deny-wins ordering as the dotfile mask above).
        for sock in policy.deny_unix_socket_paths or []:
            bwrap_args += ["--bind-try", "/dev/null", str(sock)]

        # Set $TMPDIR (and friends) to the first write_root that lives
        # under the system temp dir — the parent's
        # ``set_temp_env`` already pointed the host env at the same
        # path, but bwrap doesn't carry parent env vars across
        # ``--unshare-*`` boundaries reliably, so re-emit them.
        scratch = _scratch_tmpdir(policy.write_roots)
        if scratch is not None:
            for env_var in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
                bwrap_args += ["--setenv", env_var, str(scratch)]

        # Isolate the network namespace when networking is disabled OR
        # when egress rules are active (hard enforcement — the helper
        # has no direct internet; only the relay->proxy path works).
        if not policy.allow_network or policy.egress_relay_port is not None:
            bwrap_args.append("--unshare-net")

        # Namespacing + lifecycle hardening.
        # - ``--unshare-pid``: the helper sees only its own process tree.
        # - ``--unshare-uts``: hostname/domain isolation.
        # - ``--unshare-ipc``: isolate SysV IPC and POSIX message queues.
        # - ``--die-with-parent``: kernel kills the helper if the parent
        #   crashes (no orphan helpers left running on the host).
        # - ``--new-session``: run the helper in its own session so a
        #   misbehaving signal handler can't reach the parent's TTY.
        bwrap_args += [
            "--unshare-pid",
            "--unshare-uts",
            "--unshare-ipc",
            "--die-with-parent",
            "--new-session",
            "--chdir",
            str(chdir_target),
            "--",
        ]
        bwrap_args.extend(argv)
        return bwrap_args

    def activate(self, policy: SandboxPolicy) -> None:
        """
        Apply the in-helper hardening: ``PR_SET_NO_NEW_PRIVS`` plus
        the two seccomp filters described at the top of this module.

        Bwrap already sets ``PR_SET_NO_NEW_PRIVS`` for the helper
        (it's how non-setuid bwrap installs run unprivileged user
        namespaces), but we re-set it here for defense in depth — the
        prctl is idempotent and cheap, and an explicit call documents
        the seccomp prerequisite.

        Two seccomp filters are loaded back-to-back (the kernel ANDs
        them):

        1. The shared k8s-derived baseline via
           :func:`omnigent.inner._seccomp.apply_baseline_denylist`.
        2. The bwrap-specific argument-filtered hardening built by
           :func:`_bwrap_extra_seccomp_rules` (clone-with-CLONE_NEW*
           mask, clone3 deny, socket family allowlist).

        After seccomp, if the policy carries egress relay config, a
        TCP-to-Unix relay daemon is started in a background thread so
        that HTTP clients routed via ``HTTP_PROXY`` can reach the
        parent's egress proxy through the bind-mounted Unix socket.

        :param policy: The :class:`SandboxPolicy` for this helper.
            Consulted for :attr:`egress_relay_port` and
            :attr:`egress_socket_path` to conditionally start the
            in-namespace relay.
        """
        _set_no_new_privs()
        apply_baseline_denylist()
        apply_seccomp_filter(_bwrap_extra_seccomp_rules())

        if policy.egress_relay_port is not None and policy.egress_socket_path is not None:
            from omnigent.inner.egress.relay import start_relay

            # Under bwrap the helper runs in its own network namespace
            # so port collisions with host processes are impossible —
            # the bind always succeeds and the relay listens on a
            # private loopback. We still rely on
            # :func:`start_relay`'s fail-loud bind contract so that
            # any unexpected condition (e.g. seccomp denying socket)
            # aborts the helper rather than running with no egress
            # enforcement.
            start_relay(
                policy.egress_relay_port,
                policy.egress_socket_path,
            )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _bwrap_extra_seccomp_rules() -> list[SeccompRule]:
    """
    Build the bwrap-specific argument-filtered seccomp rules layered
    on top of :func:`omnigent.inner._seccomp.apply_baseline_denylist`.

    Three rule families, all returning ``EPERM`` against the default
    ``SCMP_ACT_ALLOW``:

    1. ``clone(flags, ...)`` denied when any ``CLONE_NEW*`` bit is set
       (one rule per bit, matched with ``SCMP_CMP_MASKED_EQ``).
       ``fork()`` and ``pthread_create()`` keep working — they don't
       set ``CLONE_NEW*``.
    2. ``clone3`` denied with ``ENOSYS``. ``struct clone_args`` lives
       in user memory that seccomp can't dereference, so we can't
       arg-filter it. We return ``ENOSYS`` (not ``EPERM``) because
       glibc's ``clone_internal`` only falls back to legacy ``clone``
       on ``ENOSYS`` — returning ``EPERM`` propagates up and breaks
       ``pthread_create`` on glibc 2.34+ (Ubuntu 22.04+), which uses
       ``clone3`` for thread creation. The fallback to legacy
       ``clone`` is still caught by rule 1 if the spawn carries a
       ``CLONE_NEW*`` bit, so namespace-creation is still blocked.
    3. ``socket(domain, ...)`` denied for all families NOT in
       :data:`_ALLOWED_SOCKET_FAMILIES`. Uses range-based deny rules:
       deny ``AF_UNSPEC`` (0), deny families 3–9 (gap between
       ``AF_INET`` and ``AF_INET6``), and deny ``>= 11`` via
       ``SCMP_CMP_GE`` — catching all current and future families
       above ``AF_INET6`` without code changes.

    These rules are bwrap-specific because (a) bwrap already set up a
    fresh PID/UTS/IPC namespace so blocking nested namespace creation
    is a true defense-in-depth layer rather than a workload break,
    and (b) the bwrap helper has no legitimate use for raw netlink
    or packet sockets. The shared baseline does not include them so
    other backends can adopt the baseline without inheriting this
    extra restriction.

    :returns: The flattened list of :class:`SeccompRule` ready for
        :func:`apply_seccomp_filter`.
    """
    deny = scmp_act_errno(errno.EPERM)
    # ``clone3`` is denied with ``ENOSYS`` (not ``EPERM``) so glibc's
    # ``clone_internal`` falls back to the legacy ``clone`` syscall,
    # which IS arg-filtered above. Returning ``EPERM`` here would
    # break ``pthread_create`` outright on glibc 2.34+ (Ubuntu
    # 22.04+ uses ``clone3`` for thread creation, and the glibc
    # fallback path triggers only on ``ENOSYS``). The in-helper
    # egress relay thread depends on ``pthread_create`` succeeding.
    deny_clone3 = scmp_act_errno(errno.ENOSYS)
    rules: list[SeccompRule] = []

    for bit in _CLONE_NEW_FLAG_BITS:
        rules.append(
            SeccompRule(
                syscall="clone",
                action=deny,
                arg_filters=(
                    SeccompArgFilter(
                        arg=0,
                        op=SCMP_CMP_MASKED_EQ,
                        datum_a=bit,
                        datum_b=bit,
                    ),
                ),
            )
        )

    rules.append(SeccompRule(syscall="clone3", action=deny_clone3))

    # Socket allowlist: deny everything NOT in _ALLOWED_SOCKET_FAMILIES.
    # We can't use a blanket deny + ALLOW holes (libseccomp rejects
    # ALLOW rules when default_action is ALLOW). Instead we deny the
    # complement using ranges:
    #   - AF_UNSPEC (0): denied individually
    #   - Families 3..9: denied individually (gap between AF_INET and AF_INET6)
    #   - Families >= 11: denied via SCMP_CMP_GE (catches all future families)
    _AF_UNSPEC = 0
    _FIRST_GAP_START = 3  # first family between AF_INET(2) and AF_INET6(10)
    _FIRST_GAP_END = 9
    _ABOVE_INET6 = 11  # first family after AF_INET6(10)

    rules.append(
        SeccompRule(
            syscall="socket",
            action=deny,
            arg_filters=(SeccompArgFilter(arg=0, op=SCMP_CMP_EQ, datum_a=_AF_UNSPEC),),
        )
    )
    for family in range(_FIRST_GAP_START, _FIRST_GAP_END + 1):
        rules.append(
            SeccompRule(
                syscall="socket",
                action=deny,
                arg_filters=(SeccompArgFilter(arg=0, op=SCMP_CMP_EQ, datum_a=family),),
            )
        )
    rules.append(
        SeccompRule(
            syscall="socket",
            action=deny,
            arg_filters=(SeccompArgFilter(arg=0, op=SCMP_CMP_GE, datum_a=_ABOVE_INET6),),
        )
    )

    return rules


def _set_no_new_privs() -> None:
    """
    Set ``PR_SET_NO_NEW_PRIVS`` on the current process via libc
    ``prctl``.

    Idempotent — bwrap already does this for the helper, but applying
    it explicitly here documents the seccomp prerequisite (the kernel
    refuses ``seccomp_load`` without ``CAP_SYS_ADMIN`` unless
    ``NO_NEW_PRIVS`` is set).

    :raises OSError: If ``prctl`` returns non-zero.
    """
    libc = ctypes.CDLL(None, use_errno=True)
    rc = libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0)
    if rc != 0:
        err = ctypes.get_errno()
        raise OSError(err, f"prctl(PR_SET_NO_NEW_PRIVS) failed: {os.strerror(err)}")


def _resolve_root(cwd: Path, root: str) -> Path:
    """
    Resolve a spec-supplied path string against *cwd*, expanding
    only ``~`` (NOT ``$VAR``) and normalising relative entries.

    H4 (security): ``os.path.expandvars`` is intentionally NOT
    applied to the spec string. Expanding environment variables
    against the parent's env at resolve-time turned the env into a
    sandbox-widening lever — an attacker who can influence
    ``$HOME`` / ``$XDG_DATA_HOME`` / ``$LOG_DIR`` (via a parent
    shell, MCP server, supervisor agent, or unaudited spec
    templating) could rewrite ``read_paths: ['$LOG_DIR/audit']``
    into ``read_paths: ['/']`` and silently widen the sandbox to
    the whole filesystem. Spec authors should write literal paths
    or ``~`` and let the operator audit them; ``$`` in a spec is
    now warned about so over-broad expansions stand out in logs.
    See :data:`omnigent.inner.seatbelt_sandbox._resolve_root`
    for the parallel hardening on the macOS backend.

    :param cwd: The agent's effective working directory; relative
        paths in the spec are resolved against it.
    :param root: The raw path string from the YAML spec, e.g.
        ``"."``, ``"src"``, ``"~/projects"``, or ``"/etc"``.
    :returns: An absolute, normalized :class:`Path` (without strict
        existence check — the caller may bind a path that doesn't
        exist yet).
    """
    if "$" in root:
        _LOGGER.warning(
            "linux_bwrap: spec-supplied path %r contains '$' which is "
            "no longer expanded against the parent environment (security "
            "hardening — env-var expansion was a sandbox-widening lever "
            "when an attacker could shape the parent's env). Use literal "
            "paths or ~ instead.",
            root,
        )
    expanded = os.path.expanduser(root)
    path = Path(expanded)
    if not path.is_absolute():
        path = cwd / path
    return path.resolve(strict=False)


def _is_same_path(a: Path, b: Path) -> bool:
    """
    Return whether two paths reference the same filesystem location
    after symlink-free resolution.

    :param a: First path to compare.
    :param b: Second path to compare.
    :returns: ``True`` when both paths resolve to the same string.
    """
    return a.resolve(strict=False) == b.resolve(strict=False)


def _ensure_executable_visible(argv: list[str], cwd: Path) -> list[str]:
    """
    Return any extra ``--ro-bind-try`` args needed so ``argv[0]`` (the
    helper interpreter) is reachable inside the sandbox.

    Bwrap exec's ``argv[0]`` literally — it does NOT follow symlinks on
    the host before exec — so every name in the path the kernel will
    traverse must be visible inside the sandbox. This walks the symlink
    chain at ``argv[0]`` hop-by-hop. At each hop, the literal path's
    parent and grandparent are bound at their literal names, with
    sources pointing at the fully-resolved real directories. That
    handles intermediate directory-symlinks: e.g. uv installs a
    version-floating symlink ``cpython-3.12 -> cpython-3.12.13``; the
    kernel traverses the literal ``cpython-3.12`` name, so we mount the
    real ``cpython-3.12.13`` contents there.

    Common cases:

    - ``/usr/bin/python3`` (literal == resolved): covered by
      ``--ro-bind-try /usr``; nothing extra emitted.
    - ``./.venv/bin/python`` inside cwd, symlinking into ``/usr``:
      covered by the cwd bind (literal) plus ``/usr`` (resolved);
      nothing extra emitted.
    - ``/home/me/omnigent/.venv/bin/python`` while the helper
      runs in ``/tmp/scratch`` (uv-managed Python with intermediate
      ``cpython-3.12`` symlink): binds the venv bin/lib roots at the
      literal paths and the uv-python install dir at its literal
      name, with the real ``cpython-3.12.13`` mounted there.
    - Pyenv-style ``$HOME/.pyenv/versions/3.12/bin/python``: same
      walk produces the binds needed whether or not the
      ``versions/3.12`` component is itself a symlink.

    :param argv: Helper argv. The function inspects ``argv[0]``.
    :param cwd: Effective working directory; used to skip binds for
        paths already covered by the cwd mount.
    :returns: Extra bwrap args (possibly empty) to insert before the
        cwd bind. Never includes a destination already covered by
        :data:`_DEFAULT_RO_DIRS` or by *cwd*.
    """
    if not argv:
        return []

    covered_prefixes = [Path(p) for p in _DEFAULT_RO_DIRS]
    covered_prefixes.append(cwd)

    extra: list[str] = []
    seen_dest: set[Path] = set()

    def _emit(src: Path, dst: Path) -> None:
        if dst in seen_dest:
            return
        # The destination is the literal path bwrap/the kernel will
        # traverse. Skip when that literal lives under a default mount.
        if any(_is_within(dst, root, resolve=False) for root in covered_prefixes):
            return
        seen_dest.add(dst)
        extra.extend(["--ro-bind-try", str(src), str(dst)])

    def _emit_parent_pair(literal: Path) -> None:
        """Bind ``literal``'s parent and grandparent at their literal
        paths, sourcing from each path's realpath so intermediate
        directory-symlinks resolve correctly inside the sandbox."""
        parent_literal = literal.parent
        parent_real = Path(os.path.realpath(str(parent_literal)))
        _emit(parent_real, parent_literal)
        gp_literal = parent_literal.parent
        if gp_literal != parent_literal:
            gp_real = Path(os.path.realpath(str(gp_literal)))
            _emit(gp_real, gp_literal)

    # Walk the symlink chain hop-by-hop until either a regular file is
    # reached or the visited-set catches a cycle. The 40-hop cap matches
    # Linux's MAXSYMLINKS / ELOOP threshold.
    visited: set[Path] = set()
    current = Path(os.path.abspath(str(Path(argv[0]))))
    for _ in range(40):
        if current in visited:
            break
        visited.add(current)
        _emit_parent_pair(current)
        try:
            if not current.is_symlink():
                break
            link = os.readlink(str(current))
        except OSError:
            break
        if link.startswith("/"):
            current = Path(link)
        else:
            current = Path(os.path.normpath(str(current.parent / link)))

    return extra


def _is_within(path: Path, root: Path, *, resolve: bool = True) -> bool:
    """
    Return whether *path* is *root* or a descendant of *root*.

    By default both paths are passed through ``resolve(strict=False)``
    before comparison, so symlinks under cwd that point into safe
    roots (e.g. ``./.venv/bin/python`` → ``/usr/bin/python3.12``)
    count as "within" the safe root for safety checks.

    Set *resolve* to ``False`` for path-string-prefix checks where
    symlink dereferencing would mask the literal path's location —
    e.g. when verifying that ``argv[0]`` (the literal exec path that
    bwrap will pass to ``execvp``) is reachable inside the sandbox.

    :param path: Candidate path, e.g. ``/usr/bin/python3``.
    :param root: Prefix path, e.g. ``/usr``.
    :param resolve: Whether to resolve symlinks before comparison.
    :returns: ``True`` when *path* equals or lives under *root* under
        the chosen resolution mode.
    """
    if resolve:
        compare_path = path.resolve(strict=False)
        compare_root = root.resolve(strict=False)
    else:
        # Use absolute() to normalise '..' / cwd-relative components
        # without following symlinks. Drop trailing slashes by
        # normalising both sides through pathlib.
        compare_path = Path(os.path.abspath(str(path)))
        compare_root = Path(os.path.abspath(str(root)))
    try:
        compare_path.relative_to(compare_root)
        return True
    except ValueError:
        return False


def _bwrap_safe_roots(
    cwd: Path, policy: SandboxPolicy, argv: Sequence[str] | None = None
) -> list[Path]:
    """
    Build the "already exposed" path set used by the cwd walker to
    decide which symlinks are escaping.

    Includes the bwrap-specific default mounts (``/usr``, ``/lib*``,
    ``/bin``, ``/sbin``, ``/proc``, ``/dev``, ``/tmp``), the cwd, the
    policy's read/write roots, AND — when *argv* is supplied — the
    literal-and-resolved parent (and grandparent) of ``argv[0]`` so
    the dotfile walker doesn't mask the helper interpreter symlink.

    Without the interpreter widening, a ``read_paths`` grant covering
    a workspace whose venv resolves into a non-default install root
    (e.g. ``/opt/hostedtoolcache/Python/.../bin/python3.12`` on
    GitHub-hosted Linux runners, where ``/opt`` is NOT in
    :data:`_DEFAULT_RO_DIRS`) would have the S5 read_paths scan flag
    ``.venv/bin/python`` as an escaping symlink and emit a
    ``--bind /dev/null`` mask over it — making the helper unspawnable
    with the bwrap error
    ``Can't create file at .venv/bin/python: No such file or directory``.
    Including the resolved interpreter parents keeps the scanner
    consistent with what :func:`_ensure_executable_visible` will
    already bind in for the helper.

    Backends that expose a different default mount set (e.g.
    ``darwin_seatbelt``) build their own list with the same shape.

    :param cwd: Resolved cwd path.
    :param policy: Sandbox policy whose read/write roots also count
        as safe.
    :param argv: Optional helper argv; when present, ``argv[0]``'s
        literal-and-resolved parent / grandparent are added so the
        walker doesn't mask the interpreter symlink.
    :returns: List of safe root paths.
    """
    safe_roots: list[Path] = [cwd]
    safe_roots.extend(Path(p) for p in _DEFAULT_RO_DIRS)
    safe_roots.append(Path("/proc"))
    safe_roots.append(Path("/dev"))
    safe_roots.append(Path("/tmp"))
    if policy.read_roots is not None:
        safe_roots.extend(policy.read_roots)
    safe_roots.extend(policy.write_roots)
    if argv:
        safe_roots.extend(_interpreter_safe_roots(argv))
    return safe_roots


def _interpreter_safe_roots(argv: Sequence[str]) -> list[Path]:
    """
    Compute the safe-root widening that mirrors
    :func:`_ensure_executable_visible`.

    Returns the literal-and-resolved parent and grandparent of
    ``argv[0]`` so the dotfile walker treats the helper interpreter
    (and the typical venv ``bin/`` + ``lib/`` siblings) as
    already-exposed, regardless of whether the resolved Python sits
    under a non-default mount root like ``/opt/hostedtoolcache`` on
    GitHub Actions Linux runners.

    :param argv: Helper argv. Only ``argv[0]`` is inspected.
    :returns: List of safe root paths derived from the interpreter
        location. Empty when *argv* is empty.
    """
    if not argv:
        return []
    exe_literal = Path(argv[0])
    exe_resolved = exe_literal.resolve(strict=False)
    candidates: set[Path] = set()
    for exe in (exe_literal, exe_resolved):
        parent = exe.parent
        if str(parent) not in {"", "/"}:
            candidates.add(parent)
        grandparent = parent.parent
        if str(grandparent) not in {"", "/"}:
            candidates.add(grandparent)
    return sorted(candidates, key=str)


def _dotfile_and_symlink_mask_args(
    cwd: Path,
    allow_hidden: list[str],
    policy: SandboxPolicy,
    argv: Sequence[str] | None = None,
) -> list[str]:
    """
    Build the bwrap mount args needed to mask dotfile / escaping
    entries the helper must not see, across BOTH cwd and every
    spec-supplied ``read_paths`` root.

    Thin emitter over :func:`omnigent.inner._cwd_scan.scan_cwd_mask_entries`:
    the shared walker decides *which* paths to mask (dotfiles by
    basename + escaping symlinks); this function maps each
    :class:`MaskedEntry` to the bwrap mount triple that hides it.

    Mask shape depends on entry kind because bwrap mount targets are
    type-checked at the kernel level:

    - **Directories** are masked with ``--tmpfs <path>`` — the agent
      sees an empty in-memory directory.
    - **Files / symlinks / sockets** are masked with
      ``--bind-try /dev/null <path>`` — the agent sees what looks like
      an empty file (reads return EOF, writes are discarded).
      ``/dev/null`` is always present inside the sandbox via the
      ``--dev /dev`` mount. ``--bind-try`` (rather than ``--bind``)
      tolerates TOCTOU races: transient files (e.g. ``.coverage.*``)
      may vanish between the scan and the ``bwrap`` exec; the
      ``-try`` variant silently skips the mount instead of aborting.

    S5 (security): the walk covers each ``read_paths`` root in
    addition to ``cwd`` so a broad grant like ``read_paths: ["~/"]``
    does NOT leave ``~/.aws``, ``~/.ssh``, ``~/.config/gcloud`` etc.
    readable just because the dotfile masker used to be cwd-only.
    Roots that live under ``cwd`` are skipped — the cwd pass already
    covered them. Per-path dedup runs across both passes.

    See :mod:`omnigent.inner._cwd_scan` for the masking-decision
    semantics, walk-cap behaviour, and the
    ``cwd_hidden_scan_max_entries`` / ``cwd_hidden_scan_overflow``
    tuning knobs. Each root is walked under the same cap; a
    ``read_paths: ["~/"]`` grant will almost always trip it and the
    resulting :class:`OSError` points at the same tunables (the
    right answer in almost every case is to narrow the grant).

    :param cwd: Effective working directory of the helper, already
        resolved (no symlinks).
    :param allow_hidden: Dotfile / dotdir basenames that pass through
        unmasked at any depth, e.g. ``[".venv"]``.
    :param policy: Sandbox policy. Provides the read/write roots that
        contribute to the symlink-escape safe set and the
        ``cwd_hidden_scan_max_entries`` / ``cwd_hidden_scan_overflow``
        knobs.
    :returns: Flat list of bwrap argv tokens (alternating
        ``--tmpfs <path>`` and ``--bind-try /dev/null <path>`` triples).
        Empty when nothing worth masking was found.
    :raises OSError: When the entry cap is reached and the policy's
        overflow mode is ``"error"``.
    """
    safe_roots = _bwrap_safe_roots(cwd, policy, argv=argv)
    seen_mask_paths: set[str] = set()
    entries = scan_cwd_mask_entries(
        cwd,
        allow_hidden=allow_hidden,
        safe_roots=safe_roots,
        max_entries=policy.cwd_hidden_scan_max_entries,
        overflow=policy.cwd_hidden_scan_overflow,
        logger_name=__name__,
    )
    for entry in entries:
        seen_mask_paths.add(str(entry.path))
    # Extend the mask to every read_paths root that the cwd scan
    # didn't already cover (skip roots that live under cwd — those
    # were walked in the cwd pass).
    for root in policy.read_roots or []:
        if _is_within(root, cwd):
            continue
        try:
            extra = scan_cwd_mask_entries(
                root,
                allow_hidden=allow_hidden,
                safe_roots=safe_roots,
                max_entries=policy.cwd_hidden_scan_max_entries,
                overflow=policy.cwd_hidden_scan_overflow,
                logger_name=__name__,
                scope_label="read_paths",
            )
        except OSError as err:
            # Re-raise with read_paths-specific advice, forwarding the
            # walker's own message verbatim — it already names the
            # overflowed root (passed as the walk's scope) and the
            # unfinished directories, so we don't want to drop that
            # detail by rewriting the text from scratch.
            raise OSError(
                f"dotfile mask scan overflowed while walking read_paths root "
                f"{root}. Narrow the grant or tune the scan limits. {err}"
            ) from err
        for entry in extra:
            key = str(entry.path)
            if key in seen_mask_paths:
                continue
            seen_mask_paths.add(key)
            entries.append(entry)
    args: list[str] = []
    for entry in entries:
        # Re-stat just before emitting: a mask overlays onto an EXISTING
        # path, so a transient dotfile (e.g. coverage.py's `.coverage.*`)
        # that vanished since the scan would force bwrap to create the
        # mountpoint inside the ro-bound cwd and abort. Skipping a gone
        # target is safe — persistent host dotfiles still exist and are
        # still masked.
        if not _path_exists_lstat(entry.path):
            continue
        if entry.kind == "dir":
            args.extend(["--tmpfs", str(entry.path)])
        else:
            args.extend(["--bind-try", "/dev/null", str(entry.path)])
    return args


def _path_exists_lstat(path: Path) -> bool:
    """
    Return whether *path* exists without following a final symlink.

    ``lstat`` (not ``stat``) so a dangling / escaping symlink still
    counts as present and gets masked.

    :param path: Candidate mask target.
    :returns: ``True`` when the path still exists (including broken
        symlinks), ``False`` when it has been removed.
    """
    try:
        os.lstat(path)
    except OSError:
        return False
    return True


def _scratch_tmpdir(write_roots: list[Path]) -> Path | None:
    """
    Return the first ``write_root`` that lives under the system tempdir.

    :func:`omnigent.inner.sandbox.create_private_tmpdir` returns a
    path under ``tempfile.gettempdir()`` (typically ``/tmp/...``).
    The helper-spawn site adds it to ``write_roots`` via
    :func:`omnigent.inner.sandbox.with_additional_write_roots`, so
    we identify it here by checking which ``write_root`` is under
    the system tempdir prefix. Used to set ``$TMPDIR`` etc. inside
    the sandbox.

    :param write_roots: The policy's resolved write roots, including
        any paths added by the spawn-site augmentation.
    :returns: The scratch tmpdir path, or ``None`` when the helper
        wasn't given one (e.g. tests that build a policy by hand).
    """
    import tempfile

    sys_tmp = Path(tempfile.gettempdir()).resolve(strict=False)
    for root in write_roots:
        try:
            if _is_within(root, sys_tmp):
                return root
        except OSError:
            continue
    return None


register_backend(BwrapSandboxBackend())
