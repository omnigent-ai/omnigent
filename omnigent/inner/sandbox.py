"""Sandbox interfaces, registry, and generic helpers."""

from __future__ import annotations

import base64
import json
import logging
import os
import shutil
import subprocess
import sys
import tempfile
from abc import ABC, abstractmethod
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeAlias, cast

from omnigent.runner.identity import RUNNER_AUTH_SECRET_ENV_VARS

from .datamodel import CredentialBrokerSpec, CredentialProxySpec, OSEnvSandboxSpec, OSEnvSpec

logger = logging.getLogger(__name__)

# Diagnostic: prepend ``strace -f -y -e trace=file`` to the spawned
# target so the next debug nightly captures every file-related syscall
# (and any EACCES sandbox denials) in the wrapper's stderr. Goes
# through the executor's tee.
_SANDBOX_STRACE_ENV = "OMNIGENT_SANDBOX_STRACE"

# Re-exec marker for ``run_launcher``: when set, the script is
# already running INSIDE a ``bwrap`` / ``sandbox-exec`` wrap and
# must NOT re-exec itself again. Set right before the re-exec call
# and inherited by the wrapped process.
_LAUNCHER_WRAPPED_ENV = "OMNIGENT_LAUNCHER_SPAWN_WRAPPED"

# Backends that need a spawn-time wrap (parent-side ``bwrap`` /
# ``sandbox-exec`` invocation) in addition to whatever in-process
# work ``activate_sandbox`` does. ``none`` is a no-op backend so it
# doesn't need a wrap.
_SPAWN_WRAP_BACKENDS = frozenset({"linux_bwrap", "darwin_seatbelt"})

# JSON-shaped payload passed across the parent/launcher boundary: the
# SandboxPolicy serialized via `to_jsonable()` plus whatever `json.loads`
# returns on the helper side.
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]


@dataclass
class SandboxPolicy:
    """
    Resolved sandbox policy serialized between the parent and helper.

    :param backend_type: The :attr:`SandboxBackend.type_name` of the
        backend that produced this policy, e.g. ``"linux_bwrap"``,
        ``"darwin_seatbelt"``, or ``"none"``.
    :param active: Whether the helper should run any sandbox activation
        for this policy. ``False`` for the ``"none"`` backend.
    :param read_roots: Per-spec read-only allow-list, or ``None`` when
        reads are unrestricted by the policy. The bwrap backend treats
        each entry as an extra ``--ro-bind-try`` mount on top of the
        hermetic root.
    :param write_roots: Directories the helper may write into, e.g.
        the cwd plus the per-helper scratch tmpdir.
    :param write_files: Per-file write grants for non-directory paths
        that can't live in :attr:`write_roots` (bwrap treats these as
        additional ``--bind-try`` mounts).
    :param allow_network: ``True`` to share the host network namespace,
        ``False`` to isolate (bwrap adds ``--unshare-net``).
    :param cwd_allow_hidden: List of dotfile / dotdir basenames that
        pass through the sandbox view at any depth under cwd. Only
        consumed by the bwrap backend today (it tmpfs-masks every
        dotfile whose basename is not in this list); other backends
        ignore the field. ``None`` means the policy carries no
        allowlist and the consuming backend applies its own default.
    :param cwd_hidden_scan_max_entries: Cap on entries the bwrap
        backend's recursive cwd walker visits. Ignored by other
        backends. Pair with :attr:`cwd_hidden_scan_overflow` to
        control behaviour when the cap is reached.
    :param cwd_hidden_scan_overflow: One of ``"error"``, ``"warn"``,
        or ``"unlimited"``. See :class:`OSEnvSandboxSpec` for the
        per-mode semantics.
    :param env_passthrough: User-declared environment-variable names
        the helper subprocess is allowed to inherit beyond the
        always-passed minimal default
        (:data:`omnigent.inner.os_env._DEFAULT_ENV_PASSTHROUGH`).
        Carried on the policy so every backend that spawns the
        helper applies the same allowlist; the parent-side
        :func:`omnigent.inner.os_env.build_helper_env` reads
        this and strips everything else from the helper's environment
        before ``subprocess.Popen``. ``None`` is treated identically
        to an empty list ("only the defaults pass through").
    :param spawn_env_allowlist: Exact environment-variable names the
        spawning executor deliberately included in the launcher
        subprocess's ``env=``. When set, :func:`run_launcher` prunes
        its inherited environment down to these names (plus internal
        launcher markers) before wrapping / exec-ing the target, so a
        spawn site that regresses to inheriting the full host
        environment cannot leak it into the sandbox.
        ``None`` (the default) skips the prune for spawn sites that
        haven't opted in.
    :param egress_relay_port: TCP port the in-namespace relay listens
        on (loopback). Set when ``egress_rules`` is active; the helper
        starts a relay daemon on this port at activation time. C1
        hardening: chosen as a random ephemeral port per helper by
        the parent rather than a hardcoded well-known port, so a
        port-squat attacker has to race every helper start instead
        of pre-binding a single known number.
    :param egress_socket_path: Absolute path (inside the namespace) to
        the Unix socket connecting to the parent's egress proxy. The
        relay forwards all traffic through this socket.
    :param deny_unix_socket_paths: Absolute paths to AF_UNIX (pathname)
        sockets the helper must NOT be able to ``connect(2)`` to, even
        when ``allow_network`` is true. Keeps a sandboxed pane from
        reaching back to an unsandboxed control-plane server over a
        socket that lives inside a bound write root (e.g. the managed
        tmux control socket). The bwrap backend overlays
        ``--bind-try /dev/null <path>`` so the path resolves to a
        character device, not a socket, and the connect fails; the
        seatbelt backend (whose default ``allow_network=true`` emits
        ``(allow network*)``, which would otherwise permit the AF_UNIX
        connect) emits an explicit last-match deny for each path.
        ``None`` is treated identically to an empty list, e.g.
        ``[Path("/tmp/omnigent-terminal-ab12/tmux.sock")]``.

    Historical note: a previous revision carried an
    ``egress_auth_token`` field shared between the parent (embedded
    as Basic auth in ``HTTP_PROXY``) and the relay (which validated
    a matching ``Proxy-Authorization`` header). That field was
    dropped — see :func:`omnigent.inner.os_env._HelperProcessClient.
    _start_egress_proxy_locked` for the rationale. The token used to
    leak through ``Popen`` argv (visible via ``ps`` to any same-UID
    process) and didn't add real protection given the relay's other
    bind-fails-loud guarantees.
    """

    backend_type: str
    active: bool
    read_roots: list[Path] | None
    write_roots: list[Path]
    write_files: list[Path]
    allow_network: bool
    cwd_allow_hidden: list[str] | None = None
    cwd_hidden_scan_max_entries: int = 50000
    cwd_hidden_scan_overflow: str = "warn"
    env_passthrough: list[str] | None = None
    spawn_env_allowlist: list[str] | None = None
    egress_relay_port: int | None = None
    egress_socket_path: str | None = None
    deny_unix_socket_paths: list[Path] | None = None
    # Parent-side only: the resolved credential-proxy policy. Read in
    # ``_HelperProcessClient._start_locked`` (parent) to mint synthetic
    # placeholders and proxy rewrite rules. Intentionally NOT carried in
    # ``to_jsonable`` / ``from_jsonable`` — the helper receives only the
    # non-secret synthetic payload over the config FD, and resolved
    # secrets never touch the policy that serialises into logs / dumps.
    credential_proxy: CredentialProxySpec | None = None
    # Parent-side only (like credential_proxy): read in
    # _HelperProcessClient._start_locked to start the broker + write shims.
    # Intentionally absent from to_jsonable/from_jsonable — the helper never
    # receives the policy; secrets never touch a serialised structure.
    credential_broker: CredentialBrokerSpec | None = None

    def to_jsonable(self) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {
            "backend_type": self.backend_type,
            "active": self.active,
            "read_roots": (
                [str(root) for root in self.read_roots] if self.read_roots is not None else None
            ),
            "write_roots": [str(root) for root in self.write_roots],
            "write_files": [str(path) for path in self.write_files],
            "allow_network": self.allow_network,
            "cwd_allow_hidden": (
                list(self.cwd_allow_hidden) if self.cwd_allow_hidden is not None else None
            ),
            "cwd_hidden_scan_max_entries": self.cwd_hidden_scan_max_entries,
            "cwd_hidden_scan_overflow": self.cwd_hidden_scan_overflow,
            "env_passthrough": (
                list(self.env_passthrough) if self.env_passthrough is not None else None
            ),
            "spawn_env_allowlist": (
                list(self.spawn_env_allowlist) if self.spawn_env_allowlist is not None else None
            ),
            "egress_relay_port": self.egress_relay_port,
            "egress_socket_path": self.egress_socket_path,
            "deny_unix_socket_paths": (
                [str(path) for path in self.deny_unix_socket_paths]
                if self.deny_unix_socket_paths is not None
                else None
            ),
        }
        return result

    @classmethod
    def from_jsonable(cls, data: dict[str, JsonValue]) -> SandboxPolicy:
        read_roots_data = data.get("read_roots")
        read_roots = None
        if isinstance(read_roots_data, list):
            read_roots = [Path(str(root)) for root in read_roots_data]
        write_roots_data = data.get("write_roots", [])
        write_files_data = data.get("write_files", [])
        if not isinstance(write_roots_data, list):
            write_roots_data = []
        if not isinstance(write_files_data, list):
            write_files_data = []
        cwd_allow_hidden_data = data.get("cwd_allow_hidden")
        cwd_allow_hidden: list[str] | None = None
        if isinstance(cwd_allow_hidden_data, list):
            cwd_allow_hidden = [str(name) for name in cwd_allow_hidden_data]
        # Narrow scan-cap fields defensively — ``data`` is a generic
        # JSON map that could carry any value at runtime even though
        # the spec parser already validated the source. The typed
        # field needs an int, so fall back to the dataclass default
        # when the encoded value isn't usable.
        max_entries_raw = data.get("cwd_hidden_scan_max_entries", 50000)
        max_entries = max_entries_raw if isinstance(max_entries_raw, int) else 50000
        overflow_raw = data.get("cwd_hidden_scan_overflow", "warn")
        overflow = overflow_raw if isinstance(overflow_raw, str) else "warn"
        env_passthrough_data = data.get("env_passthrough")
        env_passthrough: list[str] | None = None
        if isinstance(env_passthrough_data, list):
            env_passthrough = [str(name) for name in env_passthrough_data]
        spawn_env_allowlist_data = data.get("spawn_env_allowlist")
        spawn_env_allowlist: list[str] | None = None
        if isinstance(spawn_env_allowlist_data, list):
            spawn_env_allowlist = [str(name) for name in spawn_env_allowlist_data]
        egress_relay_port_raw = data.get("egress_relay_port")
        egress_relay_port: int | None = (
            int(egress_relay_port_raw) if isinstance(egress_relay_port_raw, (int, float)) else None
        )
        egress_socket_path_raw = data.get("egress_socket_path")
        egress_socket_path: str | None = (
            str(egress_socket_path_raw) if egress_socket_path_raw is not None else None
        )
        deny_unix_socket_paths_data = data.get("deny_unix_socket_paths")
        deny_unix_socket_paths: list[Path] | None = None
        if isinstance(deny_unix_socket_paths_data, list):
            deny_unix_socket_paths = [Path(str(path)) for path in deny_unix_socket_paths_data]
        return cls(
            backend_type=str(data.get("backend_type", "none")),
            active=bool(data.get("active", False)),
            read_roots=read_roots,
            write_roots=[Path(str(root)) for root in write_roots_data],
            write_files=[Path(str(path)) for path in write_files_data],
            allow_network=bool(data.get("allow_network", True)),
            cwd_allow_hidden=cwd_allow_hidden,
            cwd_hidden_scan_max_entries=max_entries,
            cwd_hidden_scan_overflow=overflow,
            env_passthrough=env_passthrough,
            spawn_env_allowlist=spawn_env_allowlist,
            egress_relay_port=egress_relay_port,
            egress_socket_path=egress_socket_path,
            deny_unix_socket_paths=deny_unix_socket_paths,
        )


class SandboxBackend(ABC):
    """
    Backend interface for host sandbox implementations.

    All built-in backends are **spawn-time** backends (e.g.
    ``linux_bwrap``, ``darwin_seatbelt``): they launch the helper
    subprocess *through* a sandbox launcher (bubblewrap, sandbox-exec,
    etc.) so the kernel sets up namespaces / mount views before the
    helper executes. They override :meth:`wrap_launcher_argv` to
    prepend the launcher and may additionally use :meth:`activate` for
    in-process hardening that runs after the launcher (e.g. seccomp
    filters layered on top of the namespace).

    The interface still permits **in-process** backends that apply
    syscalls to the current process inside :meth:`activate` and keep
    the no-op :meth:`wrap_launcher_argv` default, but none ship today.
    """

    type_name: str

    @abstractmethod
    def resolve(self, spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
        raise NotImplementedError

    @abstractmethod
    def activate(self, policy: SandboxPolicy) -> None:
        raise NotImplementedError

    def wrap_launcher_argv(
        self,
        argv: list[str],
        policy: SandboxPolicy,
        cwd: Path,
        chdir: Path | None = None,
        target: str | None = None,
    ) -> list[str]:
        """
        Wrap *argv* with whatever launcher the backend needs at spawn
        time, or return *argv* unchanged for backends that sandbox
        in-process from :meth:`activate`.

        Called by :class:`omnigent.inner.os_env._HelperProcessClient`
        on the parent side immediately before ``subprocess.Popen`` so
        the wrap can prepend a sandbox launcher (e.g. ``bwrap`` plus
        its mount/namespace flags). The default is a no-op so existing
        backends keep working without overriding anything.

        :param argv: The unwrapped command argv the parent intends to
            spawn, e.g. ``[sys.executable, "-m",
            "omnigent.inner.os_env", "helper", "<encoded>"]``.
        :param policy: The resolved :class:`SandboxPolicy` (the same
            object that will be passed to :meth:`activate` inside the
            helper). Backends that need read/write roots, the
            scratch tmpdir, or ``allow_network`` to shape their
            launcher args read them from here.
        :param cwd: Workspace directory the helper was launched from.
            Backends that bind-mount (bwrap) use this verbatim and
            should keep it exposed even when *chdir* differs.
        :param chdir: Optional separate ``--chdir`` target. When
            ``None``, the helper starts in *cwd*. When set (e.g. for
            ``OSEnvSpec.start_in_scratch``), the launcher chdirs
            there on entry. In-process backends may ignore this.
        :param target: Absolute path to the binary that the launcher
            will exec as its final target (e.g. the ``claude`` CLI).
            When set, the backend must ensure this path is reachable
            inside the sandbox namespace — for bwrap this means
            bind-mounting the target's directory chain just as it does
            for ``argv[0]`` (the Python interpreter). ``None`` when
            the target is already covered by the default mounts (e.g.
            ``/usr/bin/something``).
        :returns: The (possibly wrapped) argv. The default
            implementation returns *argv* unchanged.
        """
        del policy, cwd, chdir, target
        return argv


_BACKENDS: dict[str, SandboxBackend] = {}


def register_backend(backend: SandboxBackend) -> None:
    _BACKENDS[backend.type_name] = backend


def resolve_sandbox(spec: OSEnvSpec, cwd: Path) -> SandboxPolicy:
    sandbox_spec = spec.sandbox or _default_sandbox_for_platform()
    if sandbox_spec.type == "none":
        if (
            sandbox_spec.read_paths is not None
            or sandbox_spec.write_paths is not None
            or sandbox_spec.allow_network is False
        ):
            raise ValueError("sandbox type 'none' cannot restrict reads, writes, or network")
        return SandboxPolicy(
            backend_type="none",
            active=False,
            read_roots=None,
            write_roots=[],
            write_files=[],
            allow_network=True,
        )
    return _get_backend(sandbox_spec.type).resolve(spec, cwd)


def activate_sandbox(policy: SandboxPolicy) -> None:
    if not policy.active:
        return
    _get_backend(policy.backend_type).activate(policy)


def _clone_policy_with(
    policy: SandboxPolicy,
    *,
    read_roots: list[Path] | None,
    write_roots: list[Path],
    write_files: list[Path],
) -> SandboxPolicy:
    """
    Build a new :class:`SandboxPolicy` with the supplied root/file
    lists, copying the rest of the fields from *policy* unchanged.

    The ``with_additional_*`` helpers only ever change one of the
    three list fields; centralising the clone keeps every field on
    :class:`SandboxPolicy` (including newer additions like
    ``env_passthrough``) automatically preserved across all three
    helpers without each one redeclaring every constructor arg.
    """
    return SandboxPolicy(
        backend_type=policy.backend_type,
        active=policy.active,
        read_roots=read_roots,
        write_roots=write_roots,
        write_files=write_files,
        allow_network=policy.allow_network,
        cwd_allow_hidden=(
            list(policy.cwd_allow_hidden) if policy.cwd_allow_hidden is not None else None
        ),
        cwd_hidden_scan_max_entries=policy.cwd_hidden_scan_max_entries,
        cwd_hidden_scan_overflow=policy.cwd_hidden_scan_overflow,
        env_passthrough=(
            list(policy.env_passthrough) if policy.env_passthrough is not None else None
        ),
        spawn_env_allowlist=(
            list(policy.spawn_env_allowlist) if policy.spawn_env_allowlist is not None else None
        ),
        deny_unix_socket_paths=(
            list(policy.deny_unix_socket_paths)
            if policy.deny_unix_socket_paths is not None
            else None
        ),
        # Preserve the credential-proxy policy across clones: the
        # ``with_additional_*`` helpers run before the parent reads it in
        # ``_start_locked``, so dropping it here would silently disable
        # the feature.
        credential_proxy=policy.credential_proxy,
        credential_broker=policy.credential_broker,
        # Egress fields are intentionally NOT preserved here — the
        # ``with_additional_*`` helpers run BEFORE the egress proxy
        # starts, so the source policy never carries egress fields.
        # The egress fields are added later via ``dataclasses.replace``
        # in ``_HelperProcessClient._start_egress_proxy_locked``.
    )


def with_additional_write_roots(
    policy: SandboxPolicy,
    extra_roots: Sequence[Path],
) -> SandboxPolicy:
    write_roots = list(policy.write_roots)
    for root in extra_roots:
        resolved = root.resolve(strict=False)
        if all(existing != resolved for existing in write_roots):
            write_roots.append(resolved)
    return _clone_policy_with(
        policy,
        read_roots=list(policy.read_roots) if policy.read_roots is not None else None,
        write_roots=write_roots,
        write_files=list(policy.write_files),
    )


def with_additional_read_roots(
    policy: SandboxPolicy,
    extra_roots: Sequence[Path],
) -> SandboxPolicy:
    if policy.read_roots is None:
        return policy

    read_roots = list(policy.read_roots)
    for root in extra_roots:
        resolved = root.resolve(strict=False)
        if all(existing != resolved for existing in read_roots):
            read_roots.append(resolved)

    return _clone_policy_with(
        policy,
        read_roots=read_roots,
        write_roots=list(policy.write_roots),
        write_files=list(policy.write_files),
    )


def with_additional_write_files(
    policy: SandboxPolicy,
    extra_files: Sequence[Path],
) -> SandboxPolicy:
    write_files = list(policy.write_files)
    for path in extra_files:
        resolved = path.resolve(strict=False)
        if all(existing != resolved for existing in write_files):
            write_files.append(resolved)
    return _clone_policy_with(
        policy,
        read_roots=list(policy.read_roots) if policy.read_roots is not None else None,
        write_roots=list(policy.write_roots),
        write_files=write_files,
    )


def with_spawn_env_allowlist(
    policy: SandboxPolicy,
    names: Sequence[str] | None,
) -> SandboxPolicy:
    """
    Return a copy of *policy* with :attr:`SandboxPolicy.spawn_env_allowlist`
    set to *names*, so :func:`run_launcher` prunes its inherited
    environment to exactly that set before exec-ing the target.

    :param policy: The resolved policy to augment. Returned unchanged
        (same object) when *names* is ``None``.
    :param names: The environment-variable names the spawning executor
        deliberately passes in the launcher subprocess's ``env=``,
        e.g. ``[*clean_env, "PI_CODING_AGENT_DIR"]``. Deduplicated and
        sorted for a deterministic wire encoding.
    :returns: The augmented policy copy, or *policy* when *names* is
        ``None``.
    """
    if names is None:
        return policy
    return replace(policy, spawn_env_allowlist=sorted(set(names)))


def with_denied_unix_sockets(
    policy: SandboxPolicy,
    sockets: Sequence[Path],
) -> SandboxPolicy:
    """
    Return *policy* extended with AF_UNIX sockets the helper may not
    ``connect(2)`` to.

    See :attr:`SandboxPolicy.deny_unix_socket_paths`. Paths are resolved
    (non-strict, sockets may not exist yet at policy-build time) and
    de-duplicated. Called before the egress proxy is wired up, so the
    egress relay fields are still unset and preserved as-is.

    :param policy: The base policy to extend.
    :param sockets: Absolute (or resolvable) AF_UNIX socket paths to deny.
    :returns: A new :class:`SandboxPolicy` carrying the merged deny list.
    """
    denied = list(policy.deny_unix_socket_paths) if policy.deny_unix_socket_paths else []
    for sock in sockets:
        resolved = Path(sock).resolve(strict=False)
        if all(existing != resolved for existing in denied):
            denied.append(resolved)
    return replace(policy, deny_unix_socket_paths=denied)


def _prune_environ_to_spawn_allowlist(sandbox: SandboxPolicy) -> None:
    """
    Drop every variable not named in
    :attr:`SandboxPolicy.spawn_env_allowlist` from ``os.environ``.

    Defense in depth against host-env leakage: the spawning executor
    already passes a filtered ``env=`` to the launcher subprocess, so on
    the normal path this is a no-op — it only bites when a spawn site
    regresses to inheriting the full host environment. It runs before
    the spawn-time re-exec, so the wrap (``bwrap`` / ``sandbox-exec``)
    and the in-sandbox ``subprocess.run`` both inherit only the pruned
    set. ``--clearenv`` + ``--setenv`` on the bwrap argv was rejected
    for this purpose: argv values are world-readable via
    ``/proc/<pid>/cmdline``, the same channel that got
    ``egress_auth_token`` removed (see the class docstring).

    :param sandbox: The decoded launcher policy. No-op when its
        ``spawn_env_allowlist`` is ``None``. The internal launcher
        markers (:data:`_LAUNCHER_WRAPPED_ENV`,
        :data:`_SANDBOX_STRACE_ENV`) are always retained.
    """
    if sandbox.spawn_env_allowlist is None:
        return
    allowed = set(sandbox.spawn_env_allowlist) | {_LAUNCHER_WRAPPED_ENV, _SANDBOX_STRACE_ENV}
    for name in list(os.environ):
        if name not in allowed:
            del os.environ[name]


def create_private_tmpdir() -> Path:
    return Path(tempfile.mkdtemp(prefix="omnigent-osenv-"))


def set_temp_env(env: MutableMapping[str, str], tmpdir: Path) -> None:
    tmp_value = str(tmpdir)
    for key in ("TMPDIR", "TMP", "TEMP", "TEMPDIR"):
        env[key] = tmp_value


def cleanup_private_tmpdir(tmpdir: Path | None) -> None:
    if tmpdir is None:
        return
    shutil.rmtree(tmpdir, ignore_errors=True)


def run_launcher(encoded_sandbox: str, target_path: str, argv: list[str]) -> int:
    """
    Activate the sandbox and exec the wrapped target inside it.

    Runs in the launcher process (spawned by the parent spawn site, or
    re-exec'd under ``bwrap`` / ``sandbox-exec`` for spawn-time-wrap
    backends). Decodes the policy, optionally re-execs self under the
    backend wrap, activates the sandbox, then runs *target_path* with
    *argv* and returns its exit code.

    First strips the runner-auth secret from ``os.environ`` so it reaches
    neither the bwrap re-exec nor the final ``subprocess.run`` (both
    inherit ``os.environ``): the runner tunnel binding token must never
    reach the agent's target.

    :param encoded_sandbox: JSON-encoded :class:`SandboxPolicy`, as
        produced by ``_encode_json_arg(sandbox.to_jsonable())``.
    :param target_path: Absolute path to the wrapped executable to run
        inside the sandbox, e.g. ``"/usr/bin/python3"``.
    :param argv: Arguments passed to *target_path* (the launcher's own
        ``sys.argv[1:]``).
    :returns: The target process's exit code.
    :raises ValueError: If *encoded_sandbox* does not decode to a dict.
    """
    for secret_name in RUNNER_AUTH_SECRET_ENV_VARS:
        os.environ.pop(secret_name, None)

    decoded = _decode_json_arg(encoded_sandbox)
    if not isinstance(decoded, dict):
        raise ValueError("Invalid launcher sandbox payload")
    sandbox = SandboxPolicy.from_jsonable(decoded)

    # Before any wrap / exec so neither the re-exec'd launcher nor the
    # target can see env vars the spawner didn't deliberately pass.
    _prune_environ_to_spawn_allowlist(sandbox)

    # Spawn-time wrap re-exec for backends whose sandbox enforcement
    # is installed by the spawning process (``bwrap`` /
    # ``sandbox-exec``).
    # Previously every caller of ``create_exec_launcher`` outside
    # :class:`_HelperProcessClient` (notably the tmux terminal path)
    # skipped the wrap entirely — the launcher script ran in the
    # host namespace with no enforcement.
    # On macOS seatbelt and Linux bwrap, that collapsed to ZERO
    # sandboxing for the spawned target.
    #
    # We re-exec self under ``backend.wrap_launcher_argv``. The
    # wrapped invocation re-enters this function (via the launcher
    # script's ``__main__``) but the marker env var breaks the loop
    # and we proceed to the in-process ``activate_sandbox`` +
    # ``subprocess.run`` path. The wrap inherits HTTP_PROXY / CA
    # env vars set by the parent (terminal path) so the relay
    # daemon spun up by ``activate_sandbox`` is reachable.
    if (
        sandbox.active
        and sandbox.backend_type in _SPAWN_WRAP_BACKENDS
        and os.environ.get(_LAUNCHER_WRAPPED_ENV) != "1"
    ):
        backend = get_backend(sandbox.backend_type)
        # Re-invoke run_launcher via an INLINE python -c script
        # rather than re-running the launcher tempfile. Reason:
        # bwrap mounts ``/tmp`` as a fresh tmpfs, so the host's
        # ``/tmp/omnigent-sandbox-*.py`` written by
        # ``create_exec_launcher`` is invisible inside the wrap.
        # ``python -c '<inline>'`` doesn't need a script file in
        # the sandbox view — the inline string travels through
        # argv. Bwrap's ``_ensure_executable_visible`` already
        # ensures ``sys.executable`` is reachable. The project
        # root is added to sys.path inside the inline so
        # ``omnigent.inner.sandbox`` imports succeed even when
        # the cwd is outside the project tree (terminal case).
        project_root = repr(str(_project_root()))
        inline = (
            "import sys; "
            f"sys.path.insert(0, {project_root}); "
            "from omnigent.inner.sandbox import run_launcher; "
            f"raise SystemExit(run_launcher({encoded_sandbox!r}, "
            f"{target_path!r}, sys.argv[1:]))"
        )
        launcher_argv = [sys.executable, "-c", inline, *argv]
        wrapped = list(
            backend.wrap_launcher_argv(
                launcher_argv,
                sandbox,
                Path(os.getcwd()),
                target=target_path,
            )
        )
        os.environ[_LAUNCHER_WRAPPED_ENV] = "1"
        logger.info(
            "[omnigent-sandbox] spawn-time wrap re-exec backend=%s wrap_head=%s",
            sandbox.backend_type,
            wrapped[:3],
        )
        # ``os.execvp`` replaces the process; nothing after this
        # line runs unless the exec fails (which raises).
        os.execvp(wrapped[0], wrapped)

    tmpdir: Path | None = None
    if sandbox.active:
        tmpdir = create_private_tmpdir()
        sandbox = with_additional_write_roots(sandbox, [tmpdir])
        set_temp_env(os.environ, tmpdir)
    # Checkpoints around activate + spawn so a hang in either step is
    # visible in the wrapper's stderr (the wrapper template enables INFO).
    logger.info(
        "[omnigent-sandbox] activating backend=%s active=%s target=%s",
        sandbox.backend_type,
        sandbox.active,
        target_path,
    )
    target_argv: list[str] = [target_path, *argv]
    if os.environ.get(_SANDBOX_STRACE_ENV):
        strace_bin = shutil.which("strace")
        if strace_bin is None:
            logger.warning(
                "%s set but strace is not on PATH; running target unwrapped.",
                _SANDBOX_STRACE_ENV,
            )
        else:
            # ``-f`` follows forks, ``-y`` annotates FDs with paths,
            # ``trace=file`` keeps the trace focused on the syscalls
            # where sandbox denials surface (openat/stat/mkdir/etc).
            target_argv = [
                strace_bin,
                "-f",
                "-y",
                "-e",
                "trace=file",
                "--",
                *target_argv,
            ]
            logger.warning(
                "[omnigent-sandbox] strace active; wrapping target with %s",
                strace_bin,
            )
    try:
        activate_sandbox(sandbox)
        logger.info("[omnigent-sandbox] activated; spawning target=%s", target_path)
        completed = subprocess.run(target_argv)
        logger.info("[omnigent-sandbox] target exited rc=%s", completed.returncode)
        return int(completed.returncode)
    finally:
        cleanup_private_tmpdir(tmpdir)


def create_exec_launcher(target_path: str, sandbox: SandboxPolicy) -> str:
    encoded = _encode_json_arg(sandbox.to_jsonable())
    fd, path = tempfile.mkstemp(prefix="omnigent-sandbox-", suffix=".py")
    project_root = repr(str(_project_root()))
    encoded_literal = repr(encoded)
    target_literal = repr(target_path)
    # basicConfig so ``run_launcher``'s INFO records reach stderr.
    script = (
        f"#!{sys.executable}\n"
        "import logging\n"
        "import sys\n"
        f"sys.path.insert(0, {project_root})\n"
        "logging.basicConfig(level=logging.INFO, format='%(message)s', stream=sys.stderr)\n"
        "from omnigent.inner.sandbox import run_launcher\n"
        "if __name__ == '__main__':\n"
        f"    raise SystemExit(run_launcher({encoded_literal}, {target_literal}, sys.argv[1:]))\n"
    )
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(script)
    os.chmod(path, 0o755)
    return path


def get_backend(type_name: str) -> SandboxBackend:
    """
    Look up a registered :class:`SandboxBackend` by ``type_name``,
    importing the built-in backends on first call.

    Used by the parent-side spawn site
    (:meth:`omnigent.inner.os_env._HelperProcessClient._start_locked`)
    to call :meth:`SandboxBackend.wrap_launcher_argv` before
    ``subprocess.Popen``.

    :param type_name: Backend identifier matching
        :attr:`SandboxBackend.type_name`, e.g. ``"linux_bwrap"``,
        ``"darwin_seatbelt"``, or ``"none"``.
    :returns: The registered backend instance.
    :raises NotImplementedError: If no backend is registered under
        ``type_name``.
    """
    return _get_backend(type_name)


def _get_backend(type_name: str) -> SandboxBackend:
    _ensure_builtin_backends()
    try:
        return _BACKENDS[type_name]
    except KeyError as exc:
        raise NotImplementedError(f"sandbox type '{type_name}' is not implemented") from exc


def _ensure_builtin_backends() -> None:
    if "linux_bwrap" not in _BACKENDS:
        from . import bwrap_sandbox  # noqa: F401
    if "darwin_seatbelt" not in _BACKENDS:
        from . import seatbelt_sandbox  # noqa: F401


def _default_sandbox_for_platform() -> OSEnvSandboxSpec:
    """
    Pick the platform-preferred sandbox backend for the host OS.

    - **Linux**: ``linux_bwrap`` (mount/PID/UTS/IPC namespaces +
      seccomp via the ``bwrap`` binary).
    - **macOS**: ``darwin_seatbelt`` (SBPL via ``sandbox-exec``).

    This is a pure *platform* decision — it does **not** probe for the
    backend's binary. The default is resolved at spec **parse** time
    (e.g. :func:`omnigent.inner.loader._parse_os_env_sandbox_spec`
    and :func:`omnigent.spec.parser._parse_os_env_sandbox`) to fill
    in an absent ``type:``; parsing a YAML must not depend on whether
    the host that happens to be loading it has ``bwrap`` installed
    (CI nodes, dev laptops, and the runtime host can all differ).

    Fail-loud is preserved, but at the layer that actually matters:
    when the chosen backend is resolved at **run** time and its binary
    is missing, the backend raises (see
    :meth:`omnigent.inner.bwrap_sandbox.BwrapSandboxBackend.resolve`,
    which errors with an install hint when ``bwrap`` is not on
    ``PATH``). So an agent that omitted ``sandbox.type`` on a host with
    no usable mechanism still fails loudly rather than silently running
    unsandboxed — it just fails when the sandbox is built, not when the
    spec is parsed. The only explicit opt-out remains
    ``os_env.sandbox.type='none'``.

    Spec-self-containment is preserved: a YAML that explicitly
    declares ``sandbox.type: linux_bwrap`` still routes to the bwrap
    backend and errors loudly on macOS (the author asked for it). The
    default only fires when ``sandbox:`` is omitted or when the YAML's
    ``sandbox:`` block declares fields but not ``type:``.

    :returns: :class:`OSEnvSandboxSpec` with the OS-appropriate
        ``type``.
    :raises OSError: When the host platform has no sandbox backend at
        all (anything other than Linux or macOS). Set
        ``os_env.sandbox.type='none'`` explicitly to run without one.
    """
    if sys.platform.startswith("linux"):
        return OSEnvSandboxSpec(type="linux_bwrap")
    if sys.platform == "darwin":
        return OSEnvSandboxSpec(type="darwin_seatbelt")
    raise OSError(
        f"No sandbox backend is available on platform {sys.platform!r}. "
        "Set os_env.sandbox.type='none' explicitly to run without a sandbox."
    )


def _project_root() -> Path:
    # File lives at omnigent/inner/sandbox.py; climb two levels to the
    # repo root that hosts `omnigent/` as a package.
    return Path(__file__).resolve().parents[2]


def _encode_json_arg(value: JsonValue) -> str:
    raw = json.dumps(value, separators=(",", ":"), sort_keys=True).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii")


def _decode_json_arg(value: str) -> JsonValue:
    padded = value + "=" * (-len(value) % 4)
    raw = base64.urlsafe_b64decode(padded.encode("ascii"))
    return cast(JsonValue, json.loads(raw.decode("utf-8")))
