"""
macOS Seatbelt (``sandbox-exec``) sandbox backend.

Spawn-time wrapper that prepends ``sandbox-exec -f <profile-file>`` to
the helper argv, building a Sandbox Profile Language (SBPL) policy
from the resolved :class:`omnigent.inner.sandbox.SandboxPolicy`.
Opt-in via ``os_env.sandbox.type: darwin_seatbelt`` in YAML; selected
as the macOS platform default when ``sandbox-exec`` is on ``PATH``.

The profile is written to a mode-0600 tempfile in a parent-owned
tmpdir (NOT inside the helper's scratch view) and the file path is
passed to ``sandbox-exec -f`` instead of the profile being inlined
via ``-p``. This hides the profile contents from ``ps aux``: with
``-p`` the full SBPL — up to 256 KiB of cwd structure, dotfile mask
paths, and egress socket path — is visible to any same-host user via
``ps``; with ``-f`` only the file path appears.

Privileged capabilities are intentionally NOT granted in the SBPL
profile (security hardening over what other reference profiles
typically emit):

- ``mach-priv-host-port`` — not needed for the helper subprocess and
  is the primary lever for kernel-task IPC bypasses.
- ``iokit-open`` — not needed; would expose camera, microphone, GPU
  drivers, and every other IOKit user-client.
- Broad ``(allow file-read* file-write* (subpath "/dev"))`` — narrowed
  to ``(allow file-read* (subpath "/dev"))`` for read and per-literal
  write allows on ``/dev/null``, ``/dev/tty``, ``/dev/dtracehelper``,
  so the helper can't write through arbitrary device nodes.

This is the **macOS parity** for :mod:`omnigent.inner.bwrap_sandbox`.
The two backends share:

- The same :class:`SandboxPolicy` semantics (read roots, write roots,
  write files, allow-network, dotfile allowlist, env passthrough,
  egress relay/socket).
- The same cwd dotfile/escaping-symlink walker
  (:func:`omnigent.inner._cwd_scan.scan_cwd_mask_entries`).
- The same egress filtering pipeline: parent-side
  :class:`omnigent.inner.egress.proxy.EgressProxy`, Unix socket
  bridge in the scratch tmpdir, in-helper TCP→Unix relay via
  :func:`omnigent.inner.egress.relay.start_relay`, and
  ``HTTP_PROXY`` / ``SSL_CERT_FILE`` env injection.
- The same observable behaviour: writes outside cwd blocked, scratch
  tmpdir writable, hidden dotfiles inaccessible, env stripping,
  default-deny egress.

Default view inside the sandbox:

- ``/usr``, ``/System``, ``/Library``, ``/bin``, ``/sbin``,
  ``/private/etc``, plus a handful of dyld/timezone caches under
  ``/private/var`` are readable.
- The cwd is readable; writable iff ``write_paths`` includes ``"."``.
- The per-helper scratch tmpdir (added by the spawn site via
  :func:`omnigent.inner.sandbox.with_additional_write_roots`) is
  read-write and surfaced via ``$TMPDIR``.
- ``$HOME`` is hidden by the ``(deny default)`` baseline rather than
  an explicit subpath deny. Anything under ``$HOME`` not covered by
  cwd / scratch / read_paths / write_paths / write_files /
  interpreter-visibility is inaccessible — ``~/.aws/credentials``,
  ``~/.ssh/id_rsa``, ``~/.gnupg``, etc. all return EPERM.
- Top-level dotfiles / dotdirs anywhere under cwd are denied unless
  their basename is in :data:`_DEFAULT_CWD_ALLOW_HIDDEN` or the
  spec's ``cwd_allow_hidden``. ``.venv`` is allowed by default.
- The default network policy depends on egress and ``allow_network``:

  - egress active → ``network*`` denied except loopback to the
    in-helper relay (``127.0.0.1:<port>``) and the parent's Unix
    socket bridge (``literal "<socket>"``).
  - egress inactive, ``allow_network=false`` → ``network*`` denied
    by the default-deny rule (no allow rules emitted).
  - egress inactive, ``allow_network=true`` → ``(allow network*)``
    emitted so the helper sees the host's full network stack.

Known deltas from ``linux_bwrap`` (documented intentionally — see
:doc:`/designs/SANDBOXED_TOOL_EXECUTION` for the full rationale):

- **No PID / UTS / IPC namespace isolation.** macOS has no
  ``unshare(2)``; ``sandbox-exec`` only restricts capabilities at
  the syscall-policy level, not via process namespaces. The helper
  can ``ps`` and see other processes on the host (subject to TCC).
- **No seccomp denylist.** The shared k8s-derived baseline syscall
  blocks (``mount``, ``setns``, ``ptrace``, …) do not apply. SBPL
  blocks dangerous *capabilities* (``deny default`` + selective
  allows) rather than individual syscalls.
- **Masking is access-deny, not invisibility.** A masked file like
  ``.env`` is still visible to ``stat``/``readdir`` but read/write
  syscalls return ``EPERM``. The bwrap path bind-mounts
  ``/dev/null`` so the file appears empty. Scripts that branch on
  existence vs readability behave slightly differently.
- **SBPL deny-wins.** ``sandbox-exec`` evaluates deny rules as
  strictly winning over allow rules for the same operation on the
  same path, regardless of rule order or specificity. The bwrap
  semantics are different (mount-overlay layers compose). The
  profile generator avoids broad ``deny`` rules (e.g., a
  ``deny ... (subpath HOME)`` would override every cwd / venv /
  read_path allow under HOME) and instead relies on the global
  ``(deny default)`` plus narrow per-path denies for the dotfile
  mask, where deny-wins is precisely what we want.
- **AF_UNIX path-length cap.** macOS limits ``sun_path`` to ~104
  bytes (vs 108 on Linux). The default scratch path under
  ``/var/folders/.../T/omnigent-osenv-XXXXXX/.egress.sock`` is
  typically ~80 bytes — under the cap — but a custom ``$TMPDIR``
  in a deeply nested path could exceed it. The egress proxy will
  fail loud at bind time when that happens.
- **Profile size cap.** ``sandbox-exec`` has an undocumented profile
  size limit (~64 KB in practice). The
  ``cwd_hidden_scan_max_entries`` cap (default 50000) already
  bounds the worst case, and we additionally fail loud at spawn
  time when the emitted profile exceeds :data:`_MAX_PROFILE_BYTES`.
"""

from __future__ import annotations

import atexit
import contextlib
import logging
import os
import shutil
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

from ._cwd_scan import scan_cwd_mask_entries
from .datamodel import OSEnvSandboxSpec, OSEnvSpec
from .sandbox import (
    SandboxBackend,
    SandboxPolicy,
    register_backend,
)

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Hardening constants
# ---------------------------------------------------------------------------

# M6 (security): resolve ``sandbox-exec`` to its absolute path once at
# import time so the spawn call never goes through ``$PATH`` lookup at
# ``subprocess.Popen`` time. Otherwise an attacker who can mutate
# ``$PATH`` between the resolver's ``shutil.which`` check and the
# Popen call could substitute a malicious ``sandbox-exec`` earlier in
# the search path. ``/usr/bin/sandbox-exec`` is the canonical macOS
# location; we still consult ``shutil.which`` first to support unusual
# layouts (mocked tests, alternate macOS images). The literal fallback
# means the attribute always exists even on non-macOS hosts where the
# import would otherwise carry a ``None``.
_SANDBOX_EXEC_PATH: str = shutil.which("sandbox-exec") or "/usr/bin/sandbox-exec"

# H1/H2/H3 (security): paths that ``_ensure_executable_visible`` MUST
# refuse to widen to via ``_topmost_non_root_ancestor``. These are
# first-children-of-``/`` whose subtrees contain credentials, other
# users' homes, or sensitive system state; granting ``(subpath ...)``
# on any of them is a near-total bypass of the sandbox. ``/Users`` is
# the canonical macOS HOME parent; ``/private``, ``/var``, ``/etc``,
# ``/tmp`` are all root symlinks to ``/private/...`` whose subtrees
# contain logs, audit data, spool files, and runtime sockets.
_UNSAFE_WIDEN_ANCESTORS: frozenset[str] = frozenset(
    {
        "/Users",
        "/private",
        "/var",
        "/etc",
        "/tmp",
        "/Volumes",
        "/Network",
        "/Applications",
        "/home",  # historical macOS HOME on some setups
    }
)

# M7 (security): dotfile basenames that commonly carry credentials.
# When an operator places one of these in ``cwd_allow_hidden``, emit a
# warning so the choice is visible in logs and the operator can audit
# whether the agent actually needs it. Not a hard block — some agents
# legitimately need ``.aws`` or ``.netrc``; the warning makes the
# decision auditable rather than silent.
_SENSITIVE_HIDDEN_NAMES: frozenset[str] = frozenset(
    {
        ".aws",
        ".ssh",
        ".gnupg",
        ".gpg",
        ".kube",
        ".docker",
        ".npmrc",
        ".pypirc",
        ".netrc",
        ".env",
        ".env.local",
        ".env.production",
        ".pgpass",
        ".azure",
        ".gcloud",
        ".config",
        ".databrickscfg",
    }
)

# L5 (security): spec-supplied ``read_paths`` / ``write_paths`` entries
# that resolve to any of these paths grant near-unrestricted access to
# the host filesystem. Emit a warning so an over-broad spec is at
# least visible in logs. Not blocked because some legitimate agents
# do need ``read_paths: ["/"]`` (e.g. system-wide indexers).
_BROAD_GRANT_PATHS: frozenset[str] = frozenset(
    {
        "/",
        "/Users",
        "/private",
        "/var",
        "/etc",
        "/tmp",
        "/usr",
        "/Library",
        "/System",
        "/opt",
        "/Applications",
    }
)

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

# Top-level cwd dotfiles allowed through by default when the spec
# doesn't override ``cwd_allow_hidden``. Identical to the bwrap
# default so a user who ports a spec between platforms gets the same
# allowlist.
_DEFAULT_CWD_ALLOW_HIDDEN = (".venv",)

# S5 (security): per-user HOME subpaths denied by default UNLESS the
# operator explicitly opts in via a ``read_paths`` entry at-or-under
# one of them. macOS-only: ``$HOME/Library`` holds the bulk of
# non-dotfile-shaped credential / personal-data stores (browser
# cookies, Slack tokens, Docker keychain, Mail, Messages, app
# preferences with stored credentials) and so escapes the dotfile
# masker entirely. A spec author who writes ``read_paths: ["~/"]``
# (or any ancestor of ``$HOME/Library``) usually means "give the
# agent the workspace stuff under HOME"; they almost never mean
# "let the agent read every Chrome cookie and Slack session
# token on this Mac". Default-deny matches that intent.
#
# Suppression rule: a candidate ``$HOME/<subpath>`` is removed from
# the deny set when ANY ``read_paths`` entry equals it or lives
# under it. That way ``read_paths: ["~/Library/Logs"]`` (legitimate
# debug-log workload) suppresses the deny; ``read_paths:
# ["~/Library"]`` opts in for the whole tree (auditable in the
# spec); ``read_paths: ["~/"]`` does NOT suppress (the operator
# named the ancestor, not the candidate, so the default-deny
# stands).
#
# CAVEAT (documented for spec authors): SBPL deny-wins-over-allow
# is all-or-nothing at the subpath level. So when an operator
# writes BOTH ``~/Library/Logs`` AND ``~/`` in ``read_paths``, the
# narrow Logs grant suppresses the ``~/Library`` deny and the
# broad ``~/`` grant then exposes the rest of Library. To get
# "Logs only" semantics, the operator must drop the broad ``~/``
# grant and list only the specific subtrees they need (the
# dotfile masker keeps ``~/.aws`` etc. safe under any read grant).
#
# Linux gets an empty tuple — Linux puts everything credential-
# shaped under dotfiles (``~/.aws``, ``~/.config/gcloud``,
# ``~/.local/share/keyrings``) which the dotfile masker already
# catches under each ``read_paths`` root.
_SENSITIVE_HOME_SUBPATHS_DARWIN: tuple[str, ...] = ("Library",)

# Read-only system subtrees the helper needs to function (dyld,
# libSystem, Python, system Python stdlib, system CA bundle, …).
# Analogous to bwrap's _DEFAULT_RO_DIRS / _DEFAULT_ETC_*; the
# concrete paths differ because macOS lays things out differently
# (``/System`` for the OS, ``/Library`` for shared frameworks,
# ``/private/etc`` is the canonical path for ``/etc``).
#
# S1 (security): ``/private/var/folders`` is NOT in this list even
# though the per-user dyld closure cache + every helper's scratch
# tmpdir live under it. A broad ``(allow file-read* (subpath
# "/private/var/folders"))`` lets one helper read every OTHER
# concurrent same-user helper's scratch dir (mkdtemp 0700 doesn't
# protect against same-UID processes, only the sandbox does). The
# helper's own scratch is granted via the per-scratch subpath rule
# below; the per-user dyld closure cache is granted via
# :func:`_per_user_dyld_cache_subpath` so cross-helper isolation is
# preserved.
_DEFAULT_READ_SUBPATHS = (
    "/usr",
    "/System",
    "/Library",
    "/bin",
    "/sbin",
    "/opt",  # Homebrew (Apple Silicon) + most third-party installers
    "/private/etc",
    "/private/var/db/timezone",
    "/private/var/db/mds",
    "/private/var/db/dyld",
    "/dev",
)

# Individual file allows that ``subpath`` rules don't cover — root
# directory metadata, the resolv.conf-equivalent, and symlinks to
# ``/private/*`` that programs commonly stat.
_DEFAULT_READ_LITERALS = (
    "/",
    "/etc",
    "/var",
    "/tmp",
)

# Profile-size cap. ``sandbox-exec`` parses the profile with a
# finite-size scratch buffer; exceeding it fails the spawn with an
# opaque "policy compilation error". Cap at 256 KiB to leave headroom
# above the cwd-scan cap (50k entries × ~100 bytes/rule worst case)
# while staying well under the observed kernel ceiling.
_MAX_PROFILE_BYTES = 256 * 1024

# C1 (security): the egress relay port is now picked per-helper
# (random ephemeral, set on the policy by
# :meth:`_HelperProcessClient._start_egress_proxy_locked`) rather
# than hardcoded; the seatbelt profile generator reads
# ``policy.egress_relay_port`` to emit the localhost-only allow
# rules. No module-level constant is needed here.


# ---------------------------------------------------------------------------
# Backend
# ---------------------------------------------------------------------------


class SeatbeltSandboxBackend(SandboxBackend):
    """
    macOS Seatbelt sandbox backend.

    Resolves a :class:`SandboxPolicy` from an :class:`OSEnvSpec`,
    builds the ``sandbox-exec`` argv at spawn time via an inline SBPL
    profile (:meth:`wrap_launcher_argv`), and starts the egress relay
    inside the helper (:meth:`activate`) when L7 egress is configured.

    Stateless: a single shared instance is registered with the sandbox
    registry at module import time.
    """

    type_name = "darwin_seatbelt"

    def resolve(self, spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        """
        Build a :class:`SandboxPolicy` for the Seatbelt backend.

        Three resolver behaviours specific to this backend (matched to
        bwrap so a YAML port between platforms is value-preserving):

        - ``write_paths`` defaults to **empty** — cwd is read-only
          unless the spec sets ``write_paths: ["."]`` explicitly.
        - ``cwd_allow_hidden`` falls back to
          :data:`_DEFAULT_CWD_ALLOW_HIDDEN` (``[".venv"]``) when the
          spec doesn't declare one.
        - The ``sandbox-exec`` binary must be on ``PATH`` or the
          resolver fails loud with an install hint — no silent
          fallback to ``"none"``.

        :param spec: The agent's :class:`OSEnvSpec`. ``spec.sandbox``
            is read for backend tunables; the rest of the spec is
            unused by this backend.
        :param cwd: Effective working directory of the helper, e.g.
            the project root. Used to resolve relative entries in
            ``read_paths`` / ``write_paths`` / ``write_files``.
        :returns: A populated :class:`SandboxPolicy` ready to be
            consumed by :meth:`wrap_launcher_argv` and
            :meth:`activate`.
        :raises OSError: If the host is not macOS or the
            ``sandbox-exec`` binary cannot be located.
        """
        sandbox_spec = spec.sandbox or OSEnvSandboxSpec(type=self.type_name)

        if sys.platform != "darwin":
            raise OSError(
                "darwin_seatbelt sandbox is only available on macOS. "
                "Configure os_env.sandbox.type='linux_bwrap' on Linux "
                "or 'none' to disable sandboxing."
            )
        if shutil.which("sandbox-exec") is None:
            raise OSError(
                "darwin_seatbelt sandbox requires the 'sandbox-exec' binary on PATH. "
                "It ships with macOS by default at /usr/bin/sandbox-exec; if missing, "
                "verify your PATH includes /usr/bin or set os_env.sandbox.type to "
                "'none' to disable sandboxing."
            )

        read_roots: list[Path] | None = None
        if sandbox_spec.read_paths is not None:
            read_roots = [_resolve_root(cwd, root) for root in sandbox_spec.read_paths]

        # Seatbelt-specific default mirroring bwrap: cwd is RO unless
        # the spec opts in. Empty default honours the "no surprise
        # writes" contract — agents that need an editable project
        # tree opt in via ``write_paths: ["."]``.
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
        # M7 (security): warn when the spec opts into well-known
        # credential-bearing dotfiles. Not a hard block — operators
        # legitimately use ``cwd_allow_hidden: [".aws"]`` for agents
        # that read AWS profiles — but the choice should be visible
        # in logs for audit. Spec entries that match the canonical
        # default ``.venv`` are silent.
        for name in cwd_allow_hidden:
            if name in _SENSITIVE_HIDDEN_NAMES:
                _LOGGER.warning(
                    "darwin_seatbelt: cwd_allow_hidden grants the agent "
                    "access to %r under cwd; this dotfile commonly "
                    "carries credentials. Audit whether the agent "
                    "actually needs it.",
                    name,
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
        )

    def wrap_launcher_argv(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        cwd: Path,
        chdir: Path | None = None,
    ) -> list[str]:
        """
        Build the ``sandbox-exec`` argv that wraps *argv* under an
        SBPL profile loaded from a mode-0600 tempfile.

        Returns ``[<abs-sandbox-exec>, "-f", <profile-path>, *argv]``.
        The profile is generated by :func:`_build_profile` and
        written by :func:`_write_profile_to_tempfile`; see those
        functions for the rule layout and on-disk lifecycle.

        M5/M6 (security):

        - The profile is passed via ``-f <file>`` instead of
          ``-p <inline>`` so the contents (cwd structure, dotfile
          mask paths, egress socket path — up to 256 KiB) don't
          appear in ``ps aux`` output for any same-host user. With
          ``-p`` the full SBPL is in argv, visible to every user;
          with ``-f`` only the file path appears. The file itself
          is mode 0600 in a parent-owned tmpdir not reachable from
          the helper's sandbox view.
        - ``sandbox-exec`` is resolved to its absolute path at
          module import time (:data:`_SANDBOX_EXEC_PATH`) so the
          spawn call never goes through ``$PATH`` lookup at
          ``subprocess.Popen`` time, closing a small TOCTOU window
          where ``$PATH`` could change between resolver-time
          ``shutil.which`` and Popen-time resolution.

        Unlike bwrap, ``sandbox-exec`` has no ``--chdir`` equivalent
        — the *chdir* parameter is intentionally ignored here.
        The helper subprocess does its own ``os.chdir`` based on the
        ``cwd`` field in its JSON config (set by the parent's
        ``_HelperProcessClient`` to either the workspace or the
        scratch tmpdir when ``start_in_scratch`` is true). End state
        is identical to the bwrap path; only the moment of the
        chdir differs.

        :param argv: The unwrapped helper command, typically
            ``[sys.executable, "-m", "omnigent.inner.os_env",
            "helper", "<encoded>"]``. ``argv[0]`` is inspected to
            ensure the interpreter path is reachable inside the
            sandbox — when it lives outside the default RO subtrees
            and outside cwd (a typical pyenv / venv layout under
            ``$HOME``), extra ``allow file-read*`` rules are added so
            the kernel can open the binary after the HOME deny.
        :param policy: The :class:`SandboxPolicy` produced by
            :meth:`resolve`, possibly augmented by
            :func:`omnigent.inner.sandbox.with_additional_write_roots`
            (the parent adds the per-helper scratch tmpdir there
            before calling this).
        :param cwd: Workspace directory exposed to the helper. Always
            given file-read access regardless of *chdir* so the agent
            can reach project files via absolute paths even when the
            helper starts elsewhere.
        :param chdir: Ignored. Present for interface parity with
            :class:`SandboxBackend.wrap_launcher_argv`; the helper
            chdirs itself from its JSON config.
        :returns: A complete ``sandbox-exec`` argv ready for
            ``subprocess.Popen`` — never an empty list.
        :raises OSError: When the cwd-scan cap is hit and overflow is
            ``"error"``, when the emitted profile exceeds
            :data:`_MAX_PROFILE_BYTES`, or when the helper
            interpreter would require widening the sandbox to an
            unsafe ancestor (see :func:`_ensure_executable_visible`).
        """
        del chdir  # See docstring — Seatbelt has no --chdir analog.
        cwd_resolved = cwd.resolve(strict=False)
        extra_read_paths = _ensure_executable_visible(
            argv, cwd_resolved, policy_read_roots=policy.read_roots or []
        )
        profile = _build_profile(
            policy, cwd_resolved, extra_read_paths=extra_read_paths, argv=argv
        )
        if len(profile.encode("utf-8")) > _MAX_PROFILE_BYTES:
            raise OSError(
                f"darwin_seatbelt profile exceeds {_MAX_PROFILE_BYTES} bytes "
                f"({len(profile.encode('utf-8'))} bytes generated). "
                "Tune os_env.sandbox.cwd_hidden_scan_max_entries down so the "
                "dotfile mask emits fewer deny rules, or set "
                "os_env.sandbox.cwd_hidden_scan_overflow to 'warn' to accept "
                "a partial mask. sandbox-exec will reject larger profiles at "
                "spawn time with an opaque error; this check fails loud "
                "with the spec keys you can tune."
            )
        profile_path = _write_profile_to_tempfile(profile)
        return [_SANDBOX_EXEC_PATH, "-f", profile_path, *argv]

    def activate(self, policy: SandboxPolicy) -> None:
        """
        In-helper activation for the Seatbelt backend — start the
        egress relay if configured, otherwise no-op.

        Unlike :meth:`omnigent.inner.bwrap_sandbox.BwrapSandboxBackend.activate`,
        this method does **not** apply seccomp or ``PR_SET_NO_NEW_PRIVS``
        — neither primitive exists on macOS, and the sandbox-exec
        policy is already in force before this code runs (the kernel
        applied it pre-``execve``). The activate hook is reserved for
        the relay-startup side effect that bridges loopback TCP to
        the parent's Unix socket.

        :param policy: The :class:`SandboxPolicy` for this helper.
            Consulted for :attr:`SandboxPolicy.egress_relay_port` and
            :attr:`SandboxPolicy.egress_socket_path` to conditionally
            start the in-helper relay.
        """
        if policy.egress_relay_port is not None and policy.egress_socket_path is not None:
            from omnigent.inner.egress.relay import start_relay

            # macOS has no network-namespace primitive, so the relay
            # listens on the host's shared loopback. Two defenses
            # converge here:
            #
            #   1. Random ephemeral port per helper (picked by the
            #      parent in :func:`os_env._start_egress_proxy_locked`)
            #      so port-squat attackers can't pre-bind a known
            #      well-known number — they have to race every helper
            #      start.
            #
            #   2. Fail-loud bind contract in :func:`start_relay`: if
            #      the port is already taken (the attacker WON the
            #      race), this raises ``OSError`` and the helper
            #      aborts instead of silently forwarding its HTTP
            #      traffic to whatever is on the port.
            #
            # A previous revision also required clients to carry a
            # ``Proxy-Authorization`` token, but the parent had to
            # ship that token via ``Popen`` argv (visible to any
            # same-UID process through ``ps``), so it was strictly
            # weaker than the random-port + fail-loud-bind chain
            # above. The token was removed in favor of relying on
            # those guarantees.
            start_relay(
                policy.egress_relay_port,
                policy.egress_socket_path,
            )


# ---------------------------------------------------------------------------
# Profile generator
# ---------------------------------------------------------------------------


def _build_profile(
    policy: SandboxPolicy,
    cwd: Path,
    *,
    extra_read_paths: list[Path] | None = None,
    argv: Sequence[str] | None = None,
) -> str:
    """
    Build the SBPL profile text for *policy*.

    SBPL evaluation note: deny rules in ``sandbox-exec`` win over
    allow rules regardless of order or specificity once both match
    the same operation on the same path. That asymmetry is what
    drives this layout — we additively grant access with ``allow``
    rules, and rely on the global ``(deny default)`` to handle
    everything not explicitly allowed (including ``$HOME``). Per-path
    deny rules are reserved for the dotfile mask, where deny-wins
    is exactly what we want.

    The rule sections, in emission order:

    1. ``(version 1)`` + ``(deny default (with no-log))`` baseline.
    2. Process / mach / sysctl / iokit allows needed by libSystem.
    3. Read-only system roots (``/usr``, ``/System``, …).
    4. cwd read access + conditional cwd write access. ``$HOME`` is
       intentionally NOT explicitly denied — the default-deny
       handles it, and a blanket HOME deny would silently override
       the cwd / venv / read_paths allows when those paths live
       under HOME (the common case).
    5. Helper interpreter visibility (argv[0] parents when outside
       cwd and the default RO subtrees).
    6. Scratch tmpdir RW.
    7. Extra read roots, write roots, write files.
    8. Dotfile / escaping-symlink mask — per-path denies that win
       over the cwd allow exactly because deny beats allow in SBPL.
    9. Network rules.

    :param policy: The resolved :class:`SandboxPolicy`. Provides cwd
        writability, scratch tmpdir, read/write roots, dotfile
        allowlist, network knobs, and egress relay configuration.
    :param cwd: Workspace directory the helper was launched from,
        already resolved (no symlinks). Bound read-only at minimum.
    :param extra_read_paths: Additional directories that must be
        readable for ``argv[0]`` (the helper interpreter) to be
        exec'd inside the sandbox — typically a venv ``bin/`` and
        its parent when ``argv[0]`` lives under ``$HOME`` (which
        the explicit HOME deny would otherwise block). Emitted as
        ``(allow file-read* (subpath ...))`` AFTER the HOME deny so
        last-match-wins re-allows the interpreter.
    :returns: The SBPL profile text, ready to pass to
        ``sandbox-exec -p``. Always a non-empty string starting with
        ``(version 1)``.
    :raises OSError: When the cwd-scan cap is hit and the policy's
        overflow mode is ``"error"`` — propagated from the shared
        walker.
    """
    cwd_writable = any(_is_same_path(root, cwd) for root in policy.write_roots)
    scratch = _scratch_tmpdir(policy.write_roots)

    lines: list[str] = [
        "(version 1)",
        # ``(with no-log)`` on the default deny suppresses syslog
        # spam — under default-deny every uninteresting file probe
        # would otherwise log to /var/log/system.log, drowning real
        # sandbox events.
        "(deny default (with no-log))",
    ]

    # ----------------------------------------------------------------
    # Process / mach / sysctl / iokit — minimum for libSystem & Python
    # ----------------------------------------------------------------
    lines.extend(
        [
            "",
            ";; Process management — process-exec* is the wildcard form",
            ";; that covers process-exec + process-exec-interpreter. The",
            ";; non-wildcard ``process-exec`` requires per-binary path",
            ";; filters and rejects the helper interpreter under deny-default.",
            "(allow process-fork)",
            "(allow process-exec*)",
            "(allow process-info* (target self))",
            "(allow signal (target self))",
            "",
            ";; Mach / IPC / sysctl baseline. Verified empirically as the",
            ";; minimum needed for the Python helper to boot and run user",
            ";; code (subprocess, asyncio, http.client, ssl, socket).",
            ";;",
            ";; Intentionally NOT granted (hardening over reference SBPL",
            ";; profiles that ship with macOS):",
            ";;   - mach-priv-host-port: kernel-task IPC, not used by the",
            ";;     helper; common lever for sandbox-escape exploits.",
            ";;   - iokit-open: every IOKit driver including camera,",
            ";;     microphone, GPU; helper has no use for any of them.",
            ";;",
            ";; M3 (security note): mach-lookup and sysctl-read are",
            ";; granted broadly. Both are read-only / lookup-only",
            ";; surfaces with no narrowing facility usable from SBPL",
            ";; v1 — per-service (allow mach-lookup (global-name",
            ';; "com.apple.foo")) is SBPL v2, gated behind',
            ";; (version 2) and a private entitlement. libSystem's",
            ";; startup path consults ~40 Mach services (notifyd,",
            ";; opendirectoryd, securityd, configd, ...) and",
            ";; ~hundreds of sysctl names (kern.osversion, hw.ncpu,",
            ";; kern.maxfilesperproc, kern.boottime, ...) on every",
            ';; python3 -c "import sys" boot; per-service allowlist',
            ";; would need empirical enumeration on every macOS minor",
            ";; version or wholesale rely on Apple's reference SBPL",
            ";; profiles (which we intentionally do NOT inherit — see",
            ";; M1/M2 comments). Read-only sysctls and Mach service",
            ";; lookups are not privileged enough to enable the",
            ";; sandbox escapes that mach-priv-host-port / iokit-open",
            ";; enable, so the trade-off is acceptable. Operators",
            ";; needing tighter isolation should consider EXC handlers",
            ";; or codesigning-based entitlement gating outside the",
            ";; sandbox layer.",
            "(allow mach-lookup)",
            "(allow ipc-posix-shm)",
            "(allow ipc-posix-sem)",
            "(allow sysctl-read)",
            "(allow file-ioctl)",
            "",
            ";; /dev access. Read-only for the whole tree (device-node",
            ";; metadata, /dev/null content, /dev/urandom, /dev/fd/N for",
            ";; subprocess plumbing) but writes restricted to specific",
            ";; literals so the helper can't write through arbitrary",
            ";; character / block devices. ``/dev/tty`` is needed for",
            ";; the sys_os_shell tool; ``/dev/null`` is the universal",
            ";; output sink; ``/dev/dtracehelper`` is touched by some",
            ";; runtimes' tracing init.",
            '(allow file-read* (subpath "/dev"))',
            '(allow file-write* (literal "/dev/null"))',
            '(allow file-write* (literal "/dev/tty"))',
            '(allow file-write* (literal "/dev/dtracehelper"))',
        ]
    )

    # ----------------------------------------------------------------
    # Read-only system roots. Each ``subpath`` lives at depth 1 under
    # ``/`` (``/usr``, ``/System``, ``/opt``, …) or depth 2 under
    # ``/private/var/db/...``, so the only path components above them
    # are ``/`` (covered by the literal allow below) and ``/private``
    # (also a subpath, so traversal through it is implicit). No
    # additional ancestor-traversal allows are needed for the system
    # defaults; spec-supplied paths under ``$HOME`` DO need them,
    # see the ancestor-traversal block further down. The short
    # literal allows for ``/``, ``/etc``, ``/var``, ``/tmp`` below
    # grant ``stat`` access on those well-known symlinks / roots
    # that programs sometimes touch directly.
    # ----------------------------------------------------------------
    lines.append("")
    lines.append(";; Read-only system roots")
    for path in _DEFAULT_READ_SUBPATHS:
        lines.append(f"(allow file-read* (subpath {_quote(path)}))")
    for path in _DEFAULT_READ_LITERALS:
        lines.append(f"(allow file-read* (literal {_quote(path)}))")

    # S1 (security): narrow per-user dyld closure cache allow,
    # replacing the broad ``(subpath "/private/var/folders")`` that
    # used to be in :data:`_DEFAULT_READ_SUBPATHS`. See
    # :func:`_per_user_dyld_cache_subpath` for the rationale. Computed
    # once here and threaded through ``_collect_allowed_paths`` so
    # the ancestor walker emits the same set of ``file-read-metadata``
    # allows it would have for any other narrowly-granted subpath.
    dyld_cache = _per_user_dyld_cache_subpath()
    if dyld_cache is not None:
        lines.append(f"(allow file-read* (subpath {_quote(str(dyld_cache))}))")

    # ----------------------------------------------------------------
    # ``$HOME`` is intentionally NOT explicitly denied here. SBPL
    # treats deny as winning over any matching allow regardless of
    # rule order or specificity (verified empirically — see module
    # docstring under "Known deltas"), so a blanket
    # ``(deny ... (subpath HOME))`` would override the cwd / venv /
    # read_paths allows when those paths happen to live under HOME
    # (which is the common case, e.g. ``/Users/me/project``). The
    # default ``(deny default (with no-log))`` already handles
    # everything we don't explicitly allow — including paths under
    # HOME that aren't covered by cwd, scratch, read_roots,
    # write_roots, write_files, or the interpreter-visibility allow
    # set. Effective behaviour matches bwrap's "HOME is never
    # mounted": ``~/.aws/credentials``, ``~/.ssh/id_rsa`` etc. are
    # inaccessible unless the spec explicitly grants them.
    #
    # cwd read + conditional write
    # ----------------------------------------------------------------
    lines.append("")
    lines.append(";; Workspace (cwd)")
    lines.append(f"(allow file-read* (subpath {_quote(str(cwd))}))")
    if cwd_writable:
        lines.append(f"(allow file-write* (subpath {_quote(str(cwd))}))")

    # ----------------------------------------------------------------
    # Extra read paths for the helper interpreter (venv, pyenv, …).
    # Without these the helper interpreter located outside cwd and
    # outside the system RO subtrees (typical for venv / pyenv
    # layouts under ``$HOME``) would be unreadable under the
    # default-deny.
    # ----------------------------------------------------------------
    if extra_read_paths:
        lines.append("")
        lines.append(";; Helper interpreter visibility (argv[0] parents)")
        for path in extra_read_paths:
            lines.append(f"(allow file-read* (subpath {_quote(str(path))}))")

    # ----------------------------------------------------------------
    # Scratch tmpdir — always RW; surfaced via $TMPDIR for the helper.
    #
    # L2 (security): canonicalise the scratch path before emission so
    # the kernel's canonicalised match (e.g. ``/var/folders/...`` →
    # ``/private/var/folders/...``) never silently misses our allow
    # rule. The egress socket path (further down) already does this;
    # without canonicalisation here a custom ``$TMPDIR`` pointing
    # through a symlink would emit a rule the kernel doesn't see,
    # silently denying writes the spec author thought they granted.
    # ----------------------------------------------------------------
    if scratch is not None:
        canonical_scratch = str(scratch.resolve(strict=False))
        lines.append("")
        lines.append(";; Per-helper scratch tmpdir (RW)")
        lines.append(f"(allow file-read* (subpath {_quote(canonical_scratch)}))")
        lines.append(f"(allow file-write* (subpath {_quote(canonical_scratch)}))")

    # ----------------------------------------------------------------
    # Extra read roots
    # ----------------------------------------------------------------
    if policy.read_roots:
        lines.append("")
        lines.append(";; Spec-supplied extra read roots")
        for root in policy.read_roots:
            lines.append(f"(allow file-read* (subpath {_quote(str(root))}))")

    # ----------------------------------------------------------------
    # Extra write roots (excluding cwd which was handled above and
    # scratch which is always handled)
    # ----------------------------------------------------------------
    extra_write_roots = [
        root
        for root in policy.write_roots
        if not _is_same_path(root, cwd) and (scratch is None or not _is_same_path(root, scratch))
    ]
    if extra_write_roots:
        lines.append("")
        lines.append(";; Spec-supplied extra write roots")
        for root in extra_write_roots:
            lines.append(f"(allow file-read* (subpath {_quote(str(root))}))")
            lines.append(f"(allow file-write* (subpath {_quote(str(root))}))")

    # ----------------------------------------------------------------
    # Per-file write grants (bwrap does similar via --bind-try)
    # ----------------------------------------------------------------
    if policy.write_files:
        lines.append("")
        lines.append(";; Spec-supplied per-file write grants")
        for fpath in policy.write_files:
            lines.append(f"(allow file-read* (literal {_quote(str(fpath))}))")
            lines.append(f"(allow file-write* (literal {_quote(str(fpath))}))")

    # ----------------------------------------------------------------
    # Ancestor traversal allows.
    #
    # SBPL's ``(allow file-read* (subpath X))`` grants access to the
    # subtree rooted at ``X`` but NOT to ``X``'s strict ancestors.
    # macOS's ``realpath(3)`` (called by Python's interpreter startup
    # to canonicalise ``sys.executable``, by shells resolving ``cd``
    # targets, by ``open``+symlink follow, etc.) walks each path
    # component and ``lstat()`s it. When a component lives outside
    # the default RO subtrees and outside the path's own ``subpath``
    # allow — e.g. cwd ``/Users/me/proj`` whose ancestors are
    # ``/Users/me`` and ``/Users`` — the kernel denies the traversal
    # under deny-default and the program fails with EPERM.
    #
    # Fail mode if omitted: Python prints
    # ``python3: realpath: <cwd>/.venv/bin/: Operation not permitted``
    # and exits during ``Py_InitializeFromConfig``, before
    # ``omnigent.inner.os_env`` even starts. The bwrap backend
    # doesn't hit this because bind mounts implicitly expose the
    # entire mount-point path.
    #
    # Fix: emit ``(allow file-read-metadata (literal <ancestor>))``
    # for each strict ancestor of every spec-allowed path that isn't
    # already covered by a default subpath. ``file-read-metadata``
    # grants ``stat`` / ``lstat`` only — no directory listing, no
    # file content — so the leak is "this path exists" for a small
    # set of well-known parent directories (``/Users`` and one or
    # two intermediate dirs in the common case). Strictly narrower
    # than ``(allow file-read* (subpath /Users))``.
    # ----------------------------------------------------------------
    ancestor_literals = _ancestor_traversal_literals(
        allowed_paths=_collect_allowed_paths(
            cwd=cwd,
            scratch=scratch,
            extra_read_paths=extra_read_paths or [],
            policy=policy,
            dyld_cache=dyld_cache,
        ),
        covered_subpaths=[Path(p) for p in _DEFAULT_READ_SUBPATHS],
    )
    if ancestor_literals:
        lines.append("")
        lines.append(";; Ancestor traversal (stat-only) for realpath()/lstat() walks")
        for ancestor in ancestor_literals:
            lines.append(f"(allow file-read-metadata (literal {_quote(str(ancestor))}))")

    # ----------------------------------------------------------------
    # Dotfile / escaping-symlink mask — must come AFTER the allow
    # rules so each per-path deny is the last match.
    #
    # Two scopes are walked:
    #
    # 1. ``cwd`` — the agent's working directory, always covered.
    # 2. Every spec-supplied ``read_paths`` root that isn't already
    #    under ``cwd`` (S5: a ``read_paths: ["~/"]`` grant must NOT
    #    expose ``~/.aws/credentials`` just because dotfile masking
    #    used to stop at cwd).
    #
    # Per-path dedup runs across both passes so overlapping grants
    # don't emit the same deny twice.
    # ----------------------------------------------------------------
    safe_roots = _seatbelt_safe_roots(cwd, policy, argv=argv)
    seen_mask_paths: set[str] = set()
    mask_entries = scan_cwd_mask_entries(
        cwd,
        allow_hidden=(policy.cwd_allow_hidden if policy.cwd_allow_hidden is not None else []),
        safe_roots=safe_roots,
        max_entries=policy.cwd_hidden_scan_max_entries,
        overflow=policy.cwd_hidden_scan_overflow,
        logger_name=__name__,
    )
    for entry in mask_entries:
        seen_mask_paths.add(str(entry.path))
    mask_entries.extend(
        _scan_read_paths_mask_entries(
            policy,
            cwd,
            safe_roots,
            already_seen=seen_mask_paths,
        )
    )
    if mask_entries:
        lines.append("")
        lines.append(";; Dotfile / escaping-symlink mask (cwd + read_paths)")
        for entry in mask_entries:
            quoted = _quote(str(entry.path))
            if entry.kind == "dir":
                lines.append(f"(deny file-read* file-write* (subpath {quoted}))")
            else:
                lines.append(f"(deny file-read* file-write* (literal {quoted}))")

    # ----------------------------------------------------------------
    # S5 (security): per-user HOME subpaths denied by default.
    #
    # macOS-specific. Closes the gap where a broad ``read_paths``
    # grant (e.g. ``["~/"]``) would otherwise expose
    # ``$HOME/Library`` — browser cookies, Slack tokens, app
    # keychains, Messages history, etc. These aren't dotfile-shaped
    # so the dotfile masker doesn't catch them. The deny is
    # suppressed only when the operator explicitly named the
    # candidate (or a subtree under it) in ``read_paths``; see the
    # ``_SENSITIVE_HOME_SUBPATHS_DARWIN`` rationale for the full
    # suppression rule.
    # ----------------------------------------------------------------
    sensitive_denials = _sensitive_home_subpath_denials(policy)
    if sensitive_denials:
        lines.append("")
        lines.append(";; HOME-anchored sensitive subpath denials (S5 default-deny)")
        for denial in sensitive_denials:
            lines.append(f"(deny file-read* file-write* (subpath {_quote(str(denial))}))")

    # ----------------------------------------------------------------
    # Network rules
    # ----------------------------------------------------------------
    lines.append("")
    lines.append(";; Network policy")
    egress_active = policy.egress_relay_port is not None and policy.egress_socket_path is not None
    if egress_active:
        # Hard enforcement: deny all network (already covered by
        # (deny default)) except loopback to the relay and the
        # parent's Unix socket. ``allow_network`` is intentionally
        # ignored here — egress mode always overrides it, matching
        # the bwrap behaviour where ``--unshare-net`` is added
        # whenever egress is active regardless of ``allow_network``.
        socket_path = policy.egress_socket_path
        relay_port = policy.egress_relay_port
        lines.append(";; Egress active — loopback to relay + Unix socket to parent")
        # SBPL host syntax: ``(remote ip "HOST:PORT")`` and
        # ``(local ip "HOST:PORT")`` require ``HOST`` to be either
        # ``*`` or ``localhost`` — concrete IPs like ``127.0.0.1``
        # are rejected at profile-load time. ``localhost`` resolves
        # to the IPv4 + IPv6 loopback addresses inside the
        # sandbox-exec evaluator, which is exactly what the relay
        # listens on. The narrowness is preserved by the explicit
        # port match.
        #
        # Four allows are needed:
        # 1. The helper's relay binds to localhost:relay_port —
        #    needs ``network-bind`` permission on that local port.
        # 2. The relay's ``listen()`` + ``accept()`` of inbound
        #    loopback connections from in-helper HTTP clients —
        #    needs ``network-inbound`` on the same local port.
        #    Without this, bind succeeds but ``sock.listen()``
        #    fails with EPERM and the relay never serves.
        # 3. HTTP clients inside the helper connect to
        #    localhost:relay_port — needs ``network-outbound``.
        # 4. The relay then forwards to the parent's Unix socket
        #    via ``connect(2)`` on AF_UNIX — needs
        #    ``network-outbound`` matching the AF_UNIX address. SBPL
        #    syntax for that is ``(remote unix-socket (path-literal
        #    "<path>"))`` — NOT ``(literal "<path>")``, which is the
        #    file-system form. With the file-system form the kernel
        #    matches against the path-string for the socket file but
        #    against the AF_INET6/AF_INET6 outbound rule, so the
        #    AF_UNIX ``connect(2)`` falls through to the default
        #    deny, the relay's ``open_unix_connection`` raises, and
        #    the helper observes "Connection reset by peer" on its
        #    loopback socket. The path MUST be the realpath (e.g.
        #    ``/private/var/folders/...``) — the kernel canonicalises
        #    ``/var`` → ``/private/var`` before matching, and a rule
        #    written against the un-canonicalised path silently does
        #    not match (failing closed at the default-deny).
        canonical_socket = str(Path(socket_path).resolve(strict=False))
        lines.append(f'(allow network-bind (local ip "localhost:{relay_port}"))')
        lines.append(f'(allow network-inbound (local ip "localhost:{relay_port}"))')
        lines.append(f'(allow network-outbound (remote ip "localhost:{relay_port}"))')
        lines.append(
            "(allow network-outbound (remote unix-socket "
            f"(path-literal {_quote(canonical_socket)})))"
        )
    elif policy.allow_network:
        lines.append(";; allow_network=true — host network shared")
        lines.append("(allow network*)")
    else:
        lines.append(";; allow_network=false — covered by (deny default); no allow rules emitted")

    # ----------------------------------------------------------------
    # AF_UNIX control-socket denials.
    #
    # Even when the host network is shared (``allow_network=true`` emits
    # the broad ``(allow network*)`` above), specific pathname AF_UNIX
    # sockets must stay unreachable so a sandboxed pane cannot
    # ``connect(2)`` to an unsandboxed control-plane server (e.g. the
    # managed tmux control socket, whose ``run-shell`` would execute
    # outside the sandbox). Emitted LAST so the per-socket deny is the
    # final matching rule (SBPL is last-match-wins), overriding the
    # broad ``(allow network*)``. The path must be the realpath — the
    # kernel canonicalises (e.g. ``/var`` → ``/private/var``) before
    # matching, and an un-canonicalised rule silently fails to match.
    if policy.deny_unix_socket_paths:
        lines.append("")
        lines.append(";; AF_UNIX control-socket denials (last-match-wins)")
        for sock in policy.deny_unix_socket_paths:
            canonical = str(Path(sock).resolve(strict=False))
            lines.append(
                f"(deny network-outbound (remote unix-socket (path-literal {_quote(canonical)})))"
            )

    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _ensure_executable_visible(
    argv: list[str],
    cwd: Path,
    policy_read_roots: list[Path] | None = None,
) -> list[Path]:
    """
    Return extra read-subpath directories needed so ``argv[0]`` (the
    helper interpreter) is reachable inside the Seatbelt sandbox.

    Parallel to
    :func:`omnigent.inner.bwrap_sandbox._ensure_executable_visible`,
    but emits :class:`Path` directories to be added as
    ``(allow file-read* (subpath ...))`` rules rather than
    ``--ro-bind-try`` mounts. The kernel's ``execve`` reads the
    literal path first, then dereferences symlinks to the target
    binary — both paths plus all their dyld-loaded dylibs must be
    readable inside the sandbox.

    Why subpath, not literal-chain: empirically, when the kernel
    resolves a symlink target, it requires the parent chain of the
    target to be reachable via subpath rules. A chain of
    ``(literal "/opt") (literal "/opt/homebrew") ... (literal
    "...python3.12")`` does NOT permit exec of the resolved binary.
    Only ``(subpath ...)`` on a parent works. The smallest such
    parent that always works is the topmost non-default ancestor
    (the first child of ``/``).

    H1/H2/H3 (security): the topmost-ancestor scheme is intentionally
    DENIED for first-children-of-``/`` listed in
    :data:`_UNSAFE_WIDEN_ANCESTORS` (``/Users``, ``/private``,
    ``/var``, ``/etc``, ``/tmp``, ``/Volumes``, ``/Network``,
    ``/Applications``). Widening to any of those would silently grant
    read access to other users' home directories, credentials, logs,
    audit data, or system runtime state. When the helper interpreter
    lives under one of those (e.g. ``/Users/me/.pyenv/.../python``),
    this function attempts a tighter fallback via
    :func:`_interpreter_install_root` — granting only the
    self-contained CPython install directory (``<root>/bin/python*``
    + ``<root>/lib/python*``) rather than the broad ancestor. If the
    layout doesn't match the CPython install shape it raises
    :class:`OSError` with an actionable remediation list rather than
    silently widening.

    Typical cases:

    - ``/usr/bin/python3`` (literal == resolved): covered by the
      default ``/usr`` subpath; nothing extra emitted.
    - ``/opt/homebrew/.../python3.12``: covered by the default
      ``/opt`` subpath; nothing extra emitted.
    - ``./.venv/bin/python`` under cwd, symlinking to a Homebrew
      python: literal covered by cwd subpath; resolved covered by
      ``/opt`` default; nothing extra emitted.
    - ``./.venv/bin/python`` under cwd, symlinking to an uv-managed
      python under ``~/.local/share/uv/python/cpython-X.Y.Z-.../``:
      literal covered by cwd subpath; resolved would otherwise hit
      the ``/Users`` unsafe-widen guard, but
      :func:`_interpreter_install_root` detects the CPython install
      shape and emits a narrow
      ``(subpath ~/.local/share/uv/python/cpython-X.Y.Z-...)``
      instead. A WARNING is logged so the auto-widen is auditable.
    - ``/Users/me/.pyenv/versions/3.12/bin/python`` (pyenv): same
      narrow-fallback path — granted as
      ``(subpath /Users/me/.pyenv/versions/3.12)``.
    - ``/Users/me/random-script-that-isnt-python`` (no ``bin/`` +
      ``lib/python*`` shape): falls through to :class:`OSError`.
      Operator must switch to a covered interpreter or add a
      narrow ``read_paths`` entry covering only the interpreter
      tree.

    .. note::

       **Test-harness gotcha.** Earlier revisions of this function
       silently emitted ``[Path("/Users")]`` when the test runner's
       ``sys.executable`` lived outside ``tmp_path`` (a typical
       pytest layout where the runner's venv is under ``$HOME`` and
       cwd is under ``/private/var/folders``). That broad
       ``(subpath /Users)`` rule INCIDENTALLY satisfied the
       ``realpath()`` ancestor walk for *any* path under ``/Users``,
       masking missing-ancestor-traversal bugs from every test for
       the entire feature branch. The current implementation
       refuses to widen to ``/Users`` (and the other entries in
       :data:`_UNSAFE_WIDEN_ANCESTORS`) and only emits a narrow
       per-install-root subpath when the binary matches the
       canonical CPython install shape (see
       :func:`_interpreter_install_root`). Tests that need a
       homebrew-style venv install can still place the interpreter
       under cwd or under one of the default safe roots (``/opt``,
       ``/usr``); tests asserting the narrow-fallback path live
       beside the H1/H2/H3 regression suite in
       ``tests/inner/test_seatbelt_sandbox.py``.

    :param argv: Helper argv. The function inspects ``argv[0]``.
    :param cwd: Effective working directory; used to skip the allow
        when the interpreter already lives under cwd.
    :returns: Extra directory paths (possibly empty) to add as
        ``allow file-read* subpath`` rules. Never includes a path
        already covered by :data:`_DEFAULT_READ_SUBPATHS` or by
        *cwd*. At most two entries (one per literal/resolved exe
        when both are non-covered). When an entry would otherwise
        be a member of :data:`_UNSAFE_WIDEN_ANCESTORS`, the value
        returned is the tighter Python install root detected by
        :func:`_interpreter_install_root` (logged at WARNING for
        audit), or the function raises :class:`OSError` when no
        tighter root applies.
    :raises OSError: When the helper interpreter would require
        widening the sandbox to an entry in
        :data:`_UNSAFE_WIDEN_ANCESTORS` AND no narrower Python
        install root could be detected. The message includes the
        offending path and concrete remediation steps.
    """
    if not argv:
        return []
    exe_literal = Path(argv[0])
    exe_resolved = exe_literal.resolve(strict=False)
    covered_prefixes = [Path(p) for p in _DEFAULT_READ_SUBPATHS]
    covered_prefixes.append(cwd)
    # Spec-supplied ``read_paths`` (e.g. the repo root) make the
    # helper interpreter reachable when it lives under one of them —
    # no extra widening rule needed, and no unsafe-ancestor refusal
    # either. The caller resolved these via :func:`_resolve_root`
    # which already canonicalised + bounded them, so we can trust
    # them for the literal-prefix coverage check.
    if policy_read_roots:
        for root in policy_read_roots:
            covered_prefixes.append(Path(root))

    extras: list[Path] = []
    seen: set[Path] = set()

    def _add_topmost(exe: Path) -> None:
        # Check literal coverage (no symlink resolution) — the
        # kernel reads the path components as-given before
        # following any symlink. _is_within_literal compares
        # raw string prefixes, NOT resolved paths.
        if any(_is_within_literal(exe, root) for root in covered_prefixes):
            return
        topmost = _topmost_non_root_ancestor(exe)
        if topmost is None:
            return
        if any(_is_within_literal(topmost, root) for root in covered_prefixes):
            return
        if str(topmost) in _UNSAFE_WIDEN_ANCESTORS:
            # H1/H2/H3 (security): silently widening to ``/Users`` (or
            # any other entry in :data:`_UNSAFE_WIDEN_ANCESTORS`) would
            # expose every home directory / credential store on the box
            # and defeat the sandbox. Before refusing, try the tightest
            # safe fallback: when ``exe`` is the entry point of a
            # self-contained CPython install (``<root>/bin/python*``
            # with ``<root>/lib`` carrying a ``python*`` stdlib dir or
            # ``libpython*`` runtime), grant ``(subpath <root>)``
            # instead of the topmost ancestor. The install root holds
            # only the interpreter, its stdlib, and dyld-loaded dylibs
            # — none of the per-user credentials, ssh keys, or browser
            # state the topmost-ancestor grant would expose — and is
            # already implicitly trusted because the parent process is
            # already running ``exe``. This unblocks the common
            # ``uv run`` / ``pyenv`` / ``asdf`` layout
            # (``~/.local/share/uv/python/cpython-X.Y.Z-.../bin/python``
            # and friends) without re-introducing the broad
            # ``/Users`` widening that the H1/H2/H3 hardening
            # explicitly closed.
            install_root = _interpreter_install_root(exe)
            if install_root is not None and str(install_root) not in _UNSAFE_WIDEN_ANCESTORS:
                if any(_is_within_literal(install_root, root) for root in covered_prefixes):
                    return
                if install_root in seen:
                    return
                _LOGGER.warning(
                    "darwin_seatbelt: helper interpreter at %r resolves under "
                    "the unsafe ancestor %r; granting a narrow read-only "
                    "(subpath %r) on the detected Python install root "
                    "instead of widening to the ancestor. The install root "
                    "exposes only the interpreter, its stdlib, and dyld-"
                    "loaded dylibs — not other users' homes or credential "
                    "stores. Audit that this directory is owned exclusively "
                    "by the operator (no group/world write, no symlinks "
                    "outside the toolchain) before relying on this in "
                    "production.",
                    str(exe),
                    str(topmost),
                    str(install_root),
                )
                seen.add(install_root)
                extras.append(install_root)
                return
            raise OSError(
                f"darwin_seatbelt: helper interpreter at {str(exe)!r} would "
                f"require widening the sandbox read view to {str(topmost)!r}. "
                f"That grants the sandboxed helper read access to every "
                f"other user's home (or to system runtime state) and is a "
                f"sandbox-defeating widening. Auto-detection of a narrow "
                f"Python install root (``<root>/bin/python*`` + "
                f"``<root>/lib/python*`` or ``<root>/lib/libpython*``) "
                f"did not match the layout at this path. Remediate by "
                f"either: (1) using a Homebrew or system Python interpreter "
                f"(``/opt/homebrew/...`` or ``/usr/bin/python3``, both "
                f"covered by default RO subpaths); (2) placing the venv "
                f"under cwd (e.g. ``./.venv/bin/python``); or "
                f"(3) adding a narrower ``read_paths`` entry covering "
                f"only the interpreter tree (e.g. "
                f"``read_paths: ['~/.pyenv/versions/<ver>']``). The "
                f"sandbox refuses to silently grant ``(subpath {str(topmost)!r})``."
            )
        if topmost in seen:
            return
        seen.add(topmost)
        extras.append(topmost)

    _add_topmost(exe_literal)
    if exe_resolved != exe_literal:
        _add_topmost(exe_resolved)
    return extras


def _interpreter_install_root(exe: Path) -> Path | None:
    """
    Detect a self-contained CPython install root anchored at *exe*.

    Returns the directory two levels above *exe* — i.e. *exe*'s
    grand-parent — when it has the shape of a standalone CPython
    install:

    - ``exe.parent`` is named ``bin`` (the canonical ``bin/python*``
      layout used by every official CPython distribution: cpython.org
      installer, ``python-build-standalone`` artefacts shipped by uv
      and rye, pyenv ``versions/<ver>``, asdf ``installs/python/<ver>``,
      Homebrew Cellar python kegs, conda envs, system ``/usr``).
    - ``<root>/lib`` exists and is a real directory.
    - ``<root>/lib`` contains EITHER a directory whose name starts
      with ``python`` (the stdlib payload, e.g. ``lib/python3.12``)
      OR a file whose name starts with ``libpython`` and carries a
      shared-library extension (``.dylib`` on macOS, ``.so`` on
      Linux when this code runs against bwrap tests). Both markers
      are unique to CPython — an arbitrary HOME directory that
      happens to have ``bin/`` and ``lib/`` siblings (``~/.local/``
      itself, ``~/`` for users who symlink ``~/bin``, …) will fail
      the marker check and the caller falls through to OSError.

    The shape is intentionally tight so the auto-widen path can only
    grant access to actual Python toolchain directories, never to
    arbitrary HOME subtrees. The grant is on the install root
    (``<root>``) so the kernel can both ``exec`` ``bin/python*`` and
    ``dlopen`` everything under ``lib/`` (the runtime + stdlib
    extension modules) under the SBPL subpath rule.

    :param exe: Absolute path to a candidate interpreter binary, e.g.
        ``/Users/me/.local/share/uv/python/cpython-3.12-macos-aarch64-none/bin/python3.12``.
        Should already be resolved (the caller passes
        ``Path.resolve(strict=False)`` of ``argv[0]``); the function
        does not resolve again.
    :returns: The detected install root as a canonical absolute
        :class:`Path` (``resolve(strict=False)``-canonicalised so
        the kernel's match against our SBPL subpath rule lands
        cleanly), or ``None`` when the layout doesn't match.
    """
    if exe.parent.name != "bin":
        return None
    install_root = exe.parent.parent
    lib_dir = install_root / "lib"
    try:
        if not lib_dir.is_dir():
            return None
        for child in lib_dir.iterdir():
            name = child.name
            if name.startswith("python") and child.is_dir():
                return install_root.resolve(strict=False)
            if name.startswith("libpython") and (
                name.endswith((".dylib", ".so")) or ".dylib." in name or ".so." in name
            ):
                return install_root.resolve(strict=False)
    except OSError:
        return None
    return None


def _is_within_literal(path: Path, root: Path) -> bool:
    """
    Return whether *path* is *root* or a descendant of *root* using
    LITERAL (non-resolving) path-string-prefix comparison.

    Unlike :func:`_is_within`, this does NOT follow symlinks before
    comparing — the literal path string ``/Users/me/.venv/bin/python``
    is NOT considered "within" ``/opt`` even when ``.venv`` resolves
    to ``/opt/...``. The kernel needs read access on the literal
    path components (to read the symlink itself) BEFORE it can
    resolve the target, so this is the right comparison for
    deciding whether the literal exec path is reachable inside the
    sandbox.

    :param path: Candidate path, e.g. ``/Users/me/repo/.venv/bin/python``.
    :param root: Prefix path, e.g. ``/Users/me/repo``.
    :returns: ``True`` when *path* equals or lives under *root* as
        a raw path-string prefix.
    """
    try:
        # ``os.path.abspath`` normalises without resolving symlinks
        # (unlike Path.resolve which does follow them).
        compare_path = Path(os.path.abspath(str(path)))
        compare_root = Path(os.path.abspath(str(root)))
        compare_path.relative_to(compare_root)
        return True
    except ValueError:
        return False


def _topmost_non_root_ancestor(path: Path) -> Path | None:
    """
    Return the topmost ancestor of *path* that is a direct child of
    the filesystem root.

    Walks up *path* until the next parent is ``/`` itself, then
    returns the current step. E.g. ``/opt/homebrew/Cellar/python`` →
    ``/opt``; ``/Users/me/.pyenv/...`` → ``/Users``.

    :param path: Any absolute path. Should be resolved before
        calling (the function does not resolve).
    :returns: A first-child-of-``/`` directory, or ``None`` when
        *path* IS the root or has no meaningful ancestor.
    """
    root = Path(path.root) if path.is_absolute() else Path("/")
    current = path
    while current.parent != root and current.parent != current:
        current = current.parent
    if current in (root, path):
        return None
    return current


def _is_within(path: Path, root: Path) -> bool:
    """
    Return whether *path* is *root* or a descendant of *root*.

    Both paths are passed through ``resolve(strict=False)`` before
    comparison so symlinks pointing into safe roots count as
    "within" the safe root.

    :param path: Candidate path, e.g. ``/usr/bin/python3``.
    :param root: Prefix path, e.g. ``/usr``.
    :returns: ``True`` when *path* equals or lives under *root*
        after symlink-free resolution.
    """
    try:
        compare_path = path.resolve(strict=False)
        compare_root = root.resolve(strict=False)
        compare_path.relative_to(compare_root)
        return True
    except (ValueError, OSError):
        return False


def _collect_allowed_paths(
    *,
    cwd: Path,
    scratch: Path | None,
    extra_read_paths: Sequence[Path],
    policy: SandboxPolicy,
    dyld_cache: Path | None = None,
) -> list[Path]:
    """
    Return every absolute path the SBPL profile grants access to.

    Used as the input to :func:`_ancestor_traversal_literals` so it
    can compute the union of strict ancestors that need
    ``file-read-metadata`` to permit ``realpath()`` / ``lstat()``
    walks through them.

    For ``write_files`` we take each file's parent directory rather
    than the file itself — the file is reached via its parent, and
    the parent's own ancestors get pulled in naturally.

    :param cwd: The helper's resolved working directory.
    :param scratch: The per-helper scratch tmpdir, when present.
    :param extra_read_paths: Helper-interpreter visibility roots
        (venv ``bin/`` parents) computed by
        :func:`_executable_visibility_roots`.
    :param policy: The full resolved sandbox policy.
    :param dyld_cache: The per-user dyld closure cache subpath
        when emitted (see :func:`_per_user_dyld_cache_subpath`).
        Included so the walker emits ``file-read-metadata`` for
        its ancestors — without that, dyld's own ``realpath()``
        chain into the cache would EPERM on the first uncovered
        ancestor under deny-default.
    :returns: De-duplicated list of absolute paths to expose to the
        ancestor walker. Ordering is deterministic for reproducible
        profiles.
    """
    seen: set[str] = set()
    result: list[Path] = []
    candidates: list[Path] = [cwd]
    if scratch is not None:
        candidates.append(scratch)
    candidates.extend(extra_read_paths)
    if policy.read_roots is not None:
        candidates.extend(policy.read_roots)
    candidates.extend(policy.write_roots)
    candidates.extend(p.parent for p in policy.write_files)
    if dyld_cache is not None:
        candidates.append(dyld_cache)
    for path in candidates:
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        result.append(path)
    return result


def _ancestor_traversal_literals(
    *,
    allowed_paths: Sequence[Path],
    covered_subpaths: Sequence[Path],
) -> list[Path]:
    """
    Compute the set of ancestor directories that need a stat-only
    allow so ``realpath()`` walks from ``/`` to each allowed path
    succeed under deny-default.

    For each path in *allowed_paths*, walks its strict ancestors
    (excluding the path itself and excluding ``/``) and keeps every
    ancestor that is NOT contained inside any *covered_subpaths*
    entry. The result is de-duplicated and sorted shortest-first so
    the emitted SBPL block is stable across runs.

    Returning ``Path("/")`` is intentionally suppressed — the root
    is already in the default literal allow list, and re-emitting it
    would only bloat the profile.

    :param allowed_paths: Paths the profile grants subpath access to,
        typically cwd + scratch + read roots + write roots + extra
        exe-visibility roots + write-file parents. Each entry should
        be absolute.
    :param covered_subpaths: Subpath allows that already include
        traversal access for their own ancestors-by-containment.
        Typically the default RO subtrees (``/usr``, ``/System``,
        ``/opt``, ``/private``, …).
    :returns: Sorted list of unique absolute ancestor paths needing
        an explicit ``(allow file-read-metadata (literal ...))``
        entry. Empty list when every allowed path is already
        traversable via the system defaults.
    """
    needed: set[Path] = set()
    for path in allowed_paths:
        for ancestor in path.parents:
            if ancestor == Path(ancestor.root):
                # ``/`` already covered by ``_DEFAULT_READ_LITERALS``.
                break
            if any(_is_within_literal(ancestor, root) for root in covered_subpaths):
                # Already traversable via a default subpath allow.
                continue
            needed.add(ancestor)
    return sorted(needed, key=lambda p: (len(p.parts), str(p)))


def _sensitive_home_subpath_denials(policy: SandboxPolicy) -> list[Path]:
    """
    Return the list of ``$HOME/<subpath>`` paths that should be
    denied even when a broad ``read_paths`` grant covers them.

    See :data:`_SENSITIVE_HOME_SUBPATHS_DARWIN` for the
    motivation; this function applies the suppression rule (an
    explicit ``read_paths`` entry at-or-under a candidate clears
    that candidate from the deny set).

    Returned paths are absolute, ``~``-expanded, and
    ``resolve(strict=False)``-canonicalised so the kernel's
    canonical-match against our deny rule lands cleanly.

    :param policy: The active :class:`SandboxPolicy` — only
        ``read_roots`` is consulted (those are already
        absolute / canonical from :func:`_resolve_root`).
    :returns: Empty list when nothing should be denied (no
        ``$HOME`` resolvable, or every candidate is explicitly
        opted into via ``read_paths``).
    """
    try:
        home = Path(os.path.expanduser("~")).resolve(strict=False)
    except (OSError, RuntimeError):
        # No HOME on this box (unusual but possible in CI). With no
        # HOME there's nothing to anchor the denylist against.
        return []
    if not home.is_absolute() or str(home) in ("", "/"):
        return []
    candidates = [home / subpath for subpath in _SENSITIVE_HOME_SUBPATHS_DARWIN]
    read_roots = list(policy.read_roots or [])
    denials: list[Path] = []
    for candidate in candidates:
        if any(_is_within(root, candidate) for root in read_roots):
            # Operator explicitly opted in by naming the candidate
            # itself or a path under it. Trust the spec.
            continue
        denials.append(candidate)
    return denials


def _scan_read_paths_mask_entries(
    policy: SandboxPolicy,
    cwd: Path,
    safe_roots: list[Path],
    *,
    already_seen: set[str],
) -> list:  # list[MaskedEntry] — typed loosely to avoid the forward ref dance.
    """
    Walk every ``read_paths`` root the operator granted and identify
    dotfile / escaping-symlink entries to mask, using the same rules
    the cwd walker applies (see
    :func:`omnigent.inner._cwd_scan.scan_cwd_mask_entries`).

    Roots that are at-or-under ``cwd`` are skipped — the cwd walker
    already covered them. ``already_seen`` (a set of stringified
    paths from a prior call, typically the cwd scan's emitted
    entries) is updated in place so the caller can dedupe across
    overlapping grants without re-emitting the same SBPL / bwrap
    line twice.

    The walker's per-root entry cap and overflow behaviour come from
    ``policy.cwd_hidden_scan_max_entries`` /
    ``cwd_hidden_scan_overflow`` — same knobs as the cwd scan, so
    operators tune one place. A spec that grants ``read_paths:
    ["~/"]`` will likely trip the cap; the resulting ``OSError``
    points at the same tunables, and the operator can narrow the
    grant (which is the right answer almost every time — see the
    ``_SENSITIVE_HOME_SUBPATHS_DARWIN`` rationale).
    """
    from ._cwd_scan import scan_cwd_mask_entries  # local import — avoids module-load order tangles

    entries: list = []
    if not policy.read_roots:
        return entries
    allow_hidden = policy.cwd_allow_hidden if policy.cwd_allow_hidden is not None else []
    for root in policy.read_roots:
        # Skip roots fully covered by the cwd scan that already ran.
        # Comparing both ways: skip when root IS cwd, or when root
        # is under cwd (cwd ancestor of root → cwd scan walked it),
        # but NOT when cwd is under root (we still need to walk the
        # rest of root that's outside cwd).
        if _is_within(root, cwd):
            continue
        try:
            root_entries = scan_cwd_mask_entries(
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
        for entry in root_entries:
            key = str(entry.path)
            if key in already_seen:
                continue
            already_seen.add(key)
            entries.append(entry)
    return entries


def _seatbelt_safe_roots(
    cwd: Path, policy: SandboxPolicy, argv: Sequence[str] | None = None
) -> list[Path]:
    """
    Build the "already exposed" path set used by the shared cwd
    walker to decide which symlinks are escaping.

    Mirrors :func:`omnigent.inner.bwrap_sandbox._bwrap_safe_roots`
    but with the macOS-specific default read subtrees instead of the
    Linux ones. When *argv* is supplied, the literal-and-resolved
    parent / grandparent of ``argv[0]`` are also added so the
    dotfile walker doesn't flag the helper interpreter symlink as
    escaping (parity with the bwrap fix; macOS happens to ship
    ``/opt`` in :data:`_DEFAULT_READ_SUBPATHS` so most local installs
    are already covered, but this keeps the two backends symmetric).

    :param cwd: Resolved cwd path.
    :param policy: Sandbox policy whose read / write roots also
        count as safe.
    :param argv: Optional helper argv; when present, ``argv[0]``'s
        literal-and-resolved parent / grandparent are added so the
        walker doesn't mask the interpreter symlink.
    :returns: List of safe root paths the symlink-escape check uses.
    """
    safe_roots: list[Path] = [cwd]
    safe_roots.extend(Path(p) for p in _DEFAULT_READ_SUBPATHS)
    safe_roots.extend(Path(p) for p in _DEFAULT_READ_LITERALS)
    if policy.read_roots is not None:
        safe_roots.extend(policy.read_roots)
    safe_roots.extend(policy.write_roots)
    if argv:
        safe_roots.extend(_interpreter_safe_roots(argv))
    return safe_roots


def _interpreter_safe_roots(argv: Sequence[str]) -> list[Path]:
    """
    Compute the safe-root widening for the helper interpreter.

    Mirrors :func:`omnigent.inner.bwrap_sandbox._interpreter_safe_roots`:
    returns the literal-and-resolved parent and grandparent of
    ``argv[0]`` so the dotfile walker treats the helper interpreter
    (and the typical venv ``bin/`` + ``lib/`` siblings) as
    already-exposed.

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


def _resolve_root(cwd: Path, root: str) -> Path:
    """
    Resolve a spec-supplied path string against *cwd*, expanding
    ``~`` substitutions.

    H4 (security): ``$VAR`` expansion is **intentionally not
    performed**. The parent's environment carries arbitrary
    operator-controlled values (and credentials in well-known
    names); expanding ``$VAR`` from a spec field lets an attacker
    who can influence one of the spec author's env vars (e.g. via
    a CI environment poisoning) widen the sandbox boundary
    silently. A spec entry like ``read_paths: ["$AWS_PROFILE/data"]``
    would resolve to whatever ``AWS_PROFILE`` contains in the
    parent — including ``/`` if the var is empty (``""/data`` →
    ``/data``) or attacker-controlled. ``~`` (and ``~user``) are
    safe because they resolve via ``pwd`` and not via env.

    A warning is emitted at resolve time when the spec carries a
    literal ``$`` so spec authors who relied on the old behaviour
    see why their path didn't expand — keeps the trail visible
    rather than silently misbehaving.

    L5 (security): paths in :data:`_BROAD_GRANT_PATHS` emit a
    warning so over-broad spec entries are visible in logs. Not
    blocked because some legitimate agents need a wide grant.

    :param cwd: The agent's effective working directory; relative
        paths in the spec are resolved against it.
    :param root: The raw path string from the YAML spec, e.g.
        ``"."``, ``"src"``, ``"~/projects"``, or ``"/etc"``.
    :returns: An absolute, normalised :class:`Path` (without strict
        existence check — the caller may grant write access to a
        path that doesn't exist yet).
    """
    if "$" in root:
        _LOGGER.warning(
            "darwin_seatbelt: spec-supplied path %r contains '$' which is "
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
    resolved = path.resolve(strict=False)
    if str(resolved) in _BROAD_GRANT_PATHS:
        _LOGGER.warning(
            "darwin_seatbelt: spec-supplied path %r resolves to %r which "
            "grants near-unrestricted filesystem access. Audit whether "
            "the agent actually needs this breadth.",
            root,
            str(resolved),
        )
    return resolved


def _is_same_path(a: Path, b: Path) -> bool:
    """
    Return whether two paths reference the same filesystem location
    after symlink-free resolution.

    :param a: First path to compare.
    :param b: Second path to compare.
    :returns: ``True`` when both paths resolve to the same string.
    """
    return a.resolve(strict=False) == b.resolve(strict=False)


def _per_user_dyld_cache_subpath() -> Path | None:
    """
    Return the per-user dyld closure cache directory, when present.

    macOS keeps a per-user dyld optimisation cache at
    ``<per-user-folder>/C/com.apple.dyld/`` where ``<per-user-folder>``
    is ``Path(tempfile.gettempdir()).parent`` (e.g.
    ``/var/folders/zz/zyxvpxvq6csfxvn_n0000000000000``). dyld reads
    this on every process launch as an optimisation; it falls back
    to the system shared cache under ``/private/var/db/dyld`` when
    the per-user cache is missing or unreadable, so the helper still
    boots when this returns ``None`` — just a few hundred ms slower
    on the first import.

    S1 (security): this targeted subpath replaces the much broader
    ``/private/var/folders`` allow that used to live in
    :data:`_DEFAULT_READ_SUBPATHS`. The narrower path keeps dyld
    happy while denying cross-helper reads of other helpers' scratch
    dirs (which live under ``<per-user-folder>/T/omnigent-osenv-*``,
    NOT under ``/C/``). The OTHER subdirectories of
    ``<per-user-folder>/C/`` (Spotlight ``mds/``, WebKit caches, …)
    are deliberately NOT granted — they're per-user, not per-helper,
    but they can carry per-user-app-state that isn't relevant for a
    sandboxed agent.

    :returns: The dyld closure cache directory as a resolved
        absolute :class:`Path`, or ``None`` when the per-user
        folder can't be located (unusual ``$TMPDIR`` layout) or
        the dyld subdir doesn't exist (some macOS releases /
        configurations).
    """
    try:
        per_user_folder = Path(tempfile.gettempdir()).resolve(strict=False).parent
    except OSError:
        return None
    candidate = per_user_folder / "C" / "com.apple.dyld"
    if not candidate.exists():
        return None
    return candidate


def _scratch_tmpdir(write_roots: list[Path]) -> Path | None:
    """
    Return the first ``write_root`` that lives under the system tempdir.

    :func:`omnigent.inner.sandbox.create_private_tmpdir` returns a
    path under ``tempfile.gettempdir()`` (typically ``/var/folders/.../T``
    on macOS). The helper-spawn site adds it to ``write_roots`` via
    :func:`omnigent.inner.sandbox.with_additional_write_roots`, so
    we identify it here by checking which ``write_root`` is under
    the system tempdir prefix. Used to set RW access in the profile
    and to populate ``$TMPDIR`` for the helper.

    :param write_roots: The policy's resolved write roots, including
        any paths added by the spawn-site augmentation.
    :returns: The scratch tmpdir path, or ``None`` when the helper
        wasn't given one (e.g. tests that build a policy by hand).
    """
    import tempfile

    sys_tmp = Path(tempfile.gettempdir()).resolve(strict=False)
    for root in write_roots:
        try:
            resolved = root.resolve(strict=False)
            resolved.relative_to(sys_tmp)
            return root
        except (ValueError, OSError):
            continue
    return None


def _quote(s: str) -> str:
    """
    Quote a string for inclusion in an SBPL literal/subpath form.

    SBPL string literals are Scheme-style: enclosed in double quotes
    with backslash escapes for ``\\`` and ``"``. Paths on macOS
    rarely contain either, but a malicious or malformed cwd
    (``/tmp/proj"with"quotes/``) would otherwise produce an
    unparseable profile and a confusing ``sandbox-exec`` error.

    L1 (security): control characters (``\\x00``-``\\x1f``, ``\\x7f``)
    are rejected with :class:`ValueError`. They can't appear in any
    legitimate path on macOS (HFS+/APFS reject ``\\x00`` outright),
    but a path string carrying ``\\x0a`` would let an attacker who
    can shape paths inject extra SBPL forms after a newline. The
    backslash-escape pass below handles ``\\`` and ``"`` but does
    NOT escape control bytes, so we reject rather than try to encode
    them safely.

    :param s: The raw path string to embed.
    :returns: A double-quoted, escape-safe SBPL string literal,
        ready to drop into a ``(subpath ...)`` or ``(literal ...)``
        form.
    :raises ValueError: When *s* contains an ASCII control character
        (``\\x00``-``\\x1f`` or ``\\x7f``).
    """
    for ch in s:
        code = ord(ch)
        if code < 0x20 or code == 0x7F:
            raise ValueError(
                f"SBPL path string contains control character (0x{code:02x}); "
                f"refusing to emit. Path: {s!r}"
            )
    escaped = s.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


# ---------------------------------------------------------------------------
# Profile-file tempfile management (M5 hardening)
# ---------------------------------------------------------------------------

# Paths registered for cleanup at interpreter exit. Each
# :meth:`SeatbeltSandboxBackend.wrap_launcher_argv` call appends one
# entry; the atexit hook unlinks them all on shutdown. The list
# grows by ~1 path per helper restart (helpers are restarted on
# crash via ``_HelperProcessClient._request_locked``); typical agent
# runs add fewer than ten entries, so memory cost is negligible.
_PROFILE_CLEANUP_PATHS: list[str] = []
# Process-wide tempdir for profile files. Created lazily on first
# use. Mode 0700 so other users on the box can't list our profile
# filenames — defense-in-depth on top of the per-file 0600.
_PROFILE_TEMPDIR: str | None = None


def _ensure_profile_tempdir() -> str:
    """
    Return (lazily creating) the process-wide directory for SBPL
    profile tempfiles.

    The directory lives at
    ``<system tmpdir>/omnigent-seatbelt-profiles-XXXX`` with mode
    0700. Profile files are written here with mode 0600 — both
    layers are needed because some macOS configurations (e.g. an
    NFS-backed tmpdir, certain enterprise MDM profiles) override
    the umask and the per-file mode is the only reliable guard.

    The directory is intentionally OUTSIDE the helper's scratch
    tmpdir: the helper's sandbox profile grants it read/write
    access to its own scratch dir, so writing profiles there would
    let the agent ``cat`` them at runtime and learn the cwd-mask
    structure / egress socket path. A separate parent-only tmpdir
    keeps the profile contents invisible to the helper.

    :returns: Absolute path to the tempdir.
    """
    global _PROFILE_TEMPDIR
    if _PROFILE_TEMPDIR is None:
        _PROFILE_TEMPDIR = tempfile.mkdtemp(prefix="omnigent-seatbelt-profiles-")
        os.chmod(_PROFILE_TEMPDIR, 0o700)
    return _PROFILE_TEMPDIR


def _write_profile_to_tempfile(profile: str) -> str:
    """
    Write *profile* to a fresh mode-0600 tempfile under the
    parent-only profile tmpdir and register it for atexit cleanup.

    The file is created with ``tempfile.mkstemp`` (which opens with
    ``O_CREAT | O_EXCL`` so a same-user attacker can't race-replace
    the path between create and write) and immediately ``chmod``ed
    to 0600. The path is appended to :data:`_PROFILE_CLEANUP_PATHS`
    so :func:`_cleanup_profile_files` (registered via
    :func:`atexit.register`) unlinks it at interpreter shutdown.

    The file lives for the lifetime of the helper subprocess:
    ``sandbox-exec`` reads the profile during its own startup
    (synchronous before ``execve`` of the target), but we can't
    safely unlink it earlier because ``subprocess.Popen`` returns
    after fork but before the child has finished reading the
    profile, so an early unlink would race against the read. The
    atexit cleanup is the simplest reliable lifecycle.

    :param profile: The SBPL profile text from :func:`_build_profile`.
    :returns: Absolute path to the tempfile, ready to pass to
        ``sandbox-exec -f``.
    :raises OSError: When the file can't be created or written
        (e.g. tmpfs full, permission denied).
    """
    tempdir = _ensure_profile_tempdir()
    fd, path = tempfile.mkstemp(prefix="profile-", suffix=".sb", dir=tempdir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(profile)
        os.chmod(path, 0o600)
    except Exception:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    _PROFILE_CLEANUP_PATHS.append(path)
    return path


def _cleanup_profile_files() -> None:
    """
    atexit hook — unlink every registered profile tempfile and the
    enclosing tempdir.

    Best-effort: missing files (already cleaned up by something
    else) and permission errors are silently ignored. The helper
    subprocess has long since terminated by the time atexit runs
    in the parent, so unlinking the profile doesn't affect any
    running process.
    """
    while _PROFILE_CLEANUP_PATHS:
        path = _PROFILE_CLEANUP_PATHS.pop()
        with contextlib.suppress(OSError):
            os.unlink(path)
    global _PROFILE_TEMPDIR
    if _PROFILE_TEMPDIR is not None:
        with contextlib.suppress(OSError):
            os.rmdir(_PROFILE_TEMPDIR)
        _PROFILE_TEMPDIR = None


atexit.register(_cleanup_profile_files)


register_backend(SeatbeltSandboxBackend())
