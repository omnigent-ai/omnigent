"""OS environment abstraction and helper process transport."""

from __future__ import annotations

import atexit
import base64
import codecs
import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from abc import ABC, abstractmethod
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, TypeAlias, cast
from urllib.parse import urlparse, urlunparse

from omnigent._platform import IS_WINDOWS, WINDOWS_ENV_PASSTHROUGH
from omnigent.runner.identity import (
    OMNIGENT_SESSION_ENV_VAR,
    strip_runner_auth_secrets,
)

from .async_utils import run_sync_on_thread
from .credential_proxy import (
    CredentialProxyRuntime,
    CredentialRewriteRule,
    prepare_credential_proxy_runtime,
)
from .datamodel import CredentialProxySpec, OSEnvSpec
from .sandbox import (
    ContainmentHandle,
    SandboxPolicy,
    activate_sandbox,
    cleanup_private_tmpdir,
    create_private_tmpdir,
    get_backend,
    resolve_sandbox,
    set_temp_env,
    with_additional_write_roots,
)
from .sandbox import (
    run_launcher as _run_launcher,
)

# Any JSON-shaped leaf — used for the encode/decode serializer helpers that
# mirror the pattern in ``omnigent/sandbox.py`` and ``omnigent/uc_tools.py``.
JsonValue: TypeAlias = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

# Result dict returned by ``read`` / ``write`` / ``edit`` / ``shell`` and the
# corresponding ``_*_impl`` helpers. Keys vary by op (content/offset/total_lines
# for read; bytes_written/created for write; stdout/stderr/exit_code for shell;
# error/ok markers for all of them) and values mix str/int/bool, so we expose
# an opaque JSON-shaped dict at this boundary rather than enumerating every
# shape as a TypedDict tree.
OpResult: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# JSON-RPC payload exchanged between the parent ``_HelperProcessClient`` and
# the sandboxed helper subprocess. Carries an ``op`` discriminator plus
# op-specific fields (path/content/command/timeout/edits); treated as an
# opaque JSON object at this boundary with runtime validation in
# ``_handle_helper_request``.
OpRequest: TypeAlias = dict[str, Any]  # type: ignore[explicit-any]

# A single ``edit`` list entry — an {oldText, newText} pair of strings.
EditEntry: TypeAlias = dict[str, str]


# Environment variables every helper subprocess inherits unconditionally.
# Names that any reasonable Python program or POSIX shell expects to find
# in its environment regardless of who is running it. Adding to this list
# is permissible; removing requires justification because removal can
# break tools the agent runs via ``sys_os_shell``.
#
# Deliberately EXCLUDED from this default and from the prefix list below
# (the omissions are the security work this list is doing — see also
# :data:`_DEFAULT_ENV_PASSTHROUGH_PREFIXES`):
#
# - ``LD_PRELOAD`` / ``LD_LIBRARY_PATH``: arbitrary library injection
#   into every binary the helper execs.
# - ``PYTHONSTARTUP``: arbitrary Python file evaluated at interpreter
#   startup — a code-execution vector for any sandboxed Python helper.
# - ``BASH_ENV`` / ``ENV``: arbitrary file sourced by bash / sh on
#   non-interactive startup.
# - ``PROMPT_COMMAND``: arbitrary command run by bash before each prompt.
# - ``CDPATH``: changes the resolution of relative paths in shell ``cd``.
# - ``SSH_AUTH_SOCK``: the user's running ssh-agent socket — a
#   credential surface masquerading as a path.
# - ``DBUS_SESSION_BUS_ADDRESS``: lets the helper talk to the user's
#   D-Bus session.
# - ``XDG_RUNTIME_DIR``: per-session socket directory (Wayland, ssh-
#   agent, gpg-agent, etc.).
# - ``PYTHONPATH``: deliberately set by :func:`build_helper_env` to
#   include the project root; passing through the parent's value would
#   let any ambient ``PYTHONPATH`` shadow our setting.
# - ``TMPDIR`` / ``TMP`` / ``TEMP`` / ``TEMPDIR``: set explicitly by
#   :func:`set_temp_env` to point at the per-helper scratch tmpdir.
# - All credential families: ``AWS_*``, ``GITHUB_TOKEN``,
#   ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``, ``DATABRICKS_TOKEN``,
#   ``GOOGLE_APPLICATION_CREDENTIALS``, ``VAULT_TOKEN``, ``KUBECONFIG``,
#   ``OP_SERVICE_ACCOUNT_TOKEN``, … User opts in per-spec via
#   ``OSEnvSandboxSpec.env_passthrough``.
_DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = (
    # Shell + binary discovery.
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "PWD",
    # Terminal capabilities and timezone.
    "TERM",
    "TZ",
    # POSIX locale (the LC_* family is matched via prefix below, this
    # is just the catch-all base name).
    "LANG",
    "LANGUAGE",
    # Python interpreter knobs that don't change security posture.
    "PYTHONHASHSEED",
    "PYTHONIOENCODING",
    "PYTHONUNBUFFERED",
    "PYTHONDONTWRITEBYTECODE",
    "PYTHONFAULTHANDLER",
    # Omnigent session marker: always pass the "inside Omnigent" marker
    # through so an agent's sandboxed shell can detect the session, the
    # way CLAUDE_CODE / CODEX are visible in their agents' shells. Set on
    # the runner via runner.identity.OMNIGENT_SESSION_ENV_VAR.
    OMNIGENT_SESSION_ENV_VAR,
    # Windows system / profile constants (SYSTEMROOT is mandatory for Winsock,
    # USERPROFILE for Path.home(), etc.); a no-op on POSIX. See _platform.
    *WINDOWS_ENV_PASSTHROUGH,
)


# Prefix-matched names that join the default passthrough. ``LC_*`` is
# open-ended (LC_ALL, LC_MESSAGES, LC_TIME, LC_NUMERIC, LC_COLLATE, …)
# and locale categories are routinely set per-distribution, so we accept
# the whole family rather than enumerating every variant.
_DEFAULT_ENV_PASSTHROUGH_PREFIXES: tuple[str, ...] = ("LC_",)

# Maximum characters returned per field in a single tool output. Large outputs
# (e.g. ``cat`` on a multi-MB log file) saturate the context window and cause
# the LLM to fail with a context-length-exceeded error. The limit applies
# independently to stdout and stderr — each may be up to this size.
_MAX_TOOL_OUTPUT_CHARS = 100_000

# Default line limit for sys_os_read when the caller does not specify one.
# Without a cap an agent can ask to read an unbounded file and saturate the
# context window, causing the next LLM call to fail with a context-length
# error. 2 000 lines matches the convention used by most coding tools.
_DEFAULT_READ_LIMIT = 2_000


def build_helper_env(
    parent_env: Mapping[str, str],
    sandbox: SandboxPolicy,
) -> dict[str, str]:
    """
    Build the environment dict for the OS-env helper subprocess.

    The parent process inherits the user's full shell environment, which
    typically carries credentials in well-known names (``AWS_*``,
    ``GITHUB_TOKEN``, ``OPENAI_API_KEY``, ``ANTHROPIC_API_KEY``,
    ``DATABRICKS_TOKEN``, ``KUBECONFIG``, ``GOOGLE_APPLICATION_CREDENTIALS``,
    ``VAULT_TOKEN``, ``SSH_AUTH_SOCK``, …). Passing all of that to a
    sandboxed helper undoes the filesystem masking that hides
    ``~/.aws/credentials`` and friends — the helper would just call
    ``sys_os_shell("env")`` to enumerate every secret and ``curl`` it
    out over the (default-shared) network namespace.

    The strategy is deny-by-default with two narrow allowances:

    1. :data:`_DEFAULT_ENV_PASSTHROUGH` — names every Python helper or
       POSIX shell reasonably expects (``PATH``, ``HOME``, locale,
       ``TERM``, etc.). These pass through always.
    2. ``sandbox.env_passthrough`` — names the spec author explicitly
       declared the helper needs. Use this to grant specific secrets
       the agent legitimately uses, e.g. ``["AWS_PROFILE",
       "GITHUB_TOKEN"]``.

    All other names from *parent_env* are dropped before the helper
    sees them. Spec authors who want the previous "inherit everything"
    behavior can pass ``sandbox.type: none`` (which opts out of every
    sandboxing protection, env filtering included).

    Both branches always strip the runner-auth secret
    (:data:`~omnigent.runner.identity.RUNNER_AUTH_SECRET_ENV_VARS`): the
    helper runs the agent's tool payload, which must never see the tunnel
    binding token.

    :param parent_env: The parent process's environment (typically
        ``os.environ``). Read-only — the function does not mutate it.
    :param sandbox: The resolved :class:`SandboxPolicy` for this helper.
        ``policy.env_passthrough`` is the per-spec extra allowlist on top
        of the default. When ``policy.active`` is ``False`` (e.g.
        ``sandbox.type: none``), the parent env passes through unchanged
        except the always-removed runner-auth secrets.
    :returns: A fresh dict containing only the allowed env vars (minus
        runner-auth secrets), ready to hand to ``subprocess.Popen``'s
        ``env=`` argument. Callers typically follow up with
        ``set_temp_env`` and an explicit ``PYTHONPATH`` write so those
        values take precedence over anything the parent might have set.
    """
    if not sandbox.active:
        # Opted out of sandboxing (incl. env filtering): mirror parent
        # env, but still drop the runner-auth secret — opting out of the
        # sandbox must not also hand the agent the binding token.
        return strip_runner_auth_secrets(parent_env)

    allowed = set(_DEFAULT_ENV_PASSTHROUGH)
    if sandbox.env_passthrough is not None:
        allowed.update(sandbox.env_passthrough)
    prefixes = _DEFAULT_ENV_PASSTHROUGH_PREFIXES

    env: dict[str, str] = {}
    for name, value in parent_env.items():
        if name in allowed or any(name.startswith(prefix) for prefix in prefixes):
            env[name] = value
    # The default allowlist already excludes the runner-auth secrets,
    # but strip again so a spec author can't re-admit one by naming it
    # in ``sandbox.env_passthrough``.
    return strip_runner_auth_secrets(env)


def _build_credential_proxy_parent_env(
    *,
    helper_env: Mapping[str, str],
    parent_env: Mapping[str, str],
    spec: CredentialProxySpec,
) -> dict[str, str]:
    """
    Build the parent-side env used to resolve credential-proxy sources.

    ``file:`` / ``command:`` sources run against the same filtered
    baseline the sandbox helper gets (so a ``command`` source can't
    enumerate the parent's full secret-bearing environment), with the
    one narrow addition that ``env:`` sources need: their referenced
    variable, lifted from the real parent environment.

    :param helper_env: The filtered helper environment from
        :func:`build_helper_env`.
    :param parent_env: The real parent process environment (typically
        ``os.environ``).
    :param spec: The credential-proxy policy whose ``env:`` source names
        are lifted from *parent_env*.
    :returns: An env map suitable for source resolution.
    """
    resolved = dict(helper_env)
    for entry in spec.entries:
        if entry.source.kind == "env" and entry.source.env:
            value = parent_env.get(entry.source.env)
            if value is not None:
                resolved[entry.source.env] = value
    return resolved


@dataclass
class OSEnvironment(ABC):
    """Base OS environment interface."""

    spec: OSEnvSpec
    cwd: Path

    @abstractmethod
    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
    ) -> OpResult:
        raise NotImplementedError

    @abstractmethod
    async def write(self, path: str, content: str) -> OpResult:
        raise NotImplementedError

    @abstractmethod
    async def edit(
        self,
        path: str,
        *,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: Sequence[EditEntry] | None = None,
    ) -> OpResult:
        raise NotImplementedError

    @abstractmethod
    async def shell(
        self,
        command: str,
        timeout: int | None = None,
        max_output: int | None = None,
    ) -> OpResult:
        raise NotImplementedError

    def close(self) -> None:  # noqa: B027 — optional override hook; default is a no-op
        """Release any process or file resources held by the environment.

        Subclasses override this when they hold state (subprocesses, file
        handles, etc.). The default is a no-op so stateless environments
        don't need to implement it.
        """


class _HelperProcessClient:
    """JSON-line RPC client for the sandboxed OS helper process."""

    def __init__(
        self,
        *,
        cwd: Path,
        shell_path: str,
        sandbox: SandboxPolicy,
        start_in_scratch: bool = False,
        egress_rules: list[str] | None = None,
        egress_allow_private_destinations: bool = False,
    ) -> None:
        self.cwd = cwd
        self.shell_path = shell_path
        self.sandbox = sandbox
        self.start_in_scratch = start_in_scratch
        self._egress_rules = egress_rules
        self._egress_allow_private_destinations = egress_allow_private_destinations
        # S4 (security): per-helper Proxy-Authorization token,
        # generated in :meth:`_start_egress_proxy_locked` and read by
        # the config-FD writer in :meth:`_start_locked`. ``None``
        # before the proxy is started (and when ``egress_rules`` is
        # empty — there's no proxy to authenticate against). Never
        # exposed on the policy, never put on ``Popen(env=...)``.
        self._egress_auth_token: str | None = None
        # S4 (security): introspection hook — the random relay port
        # this client gave its helper. Stored ONLY for in-process
        # callers (tests, debug tools) that need to assert against
        # the live listener; the policy already carries it for the
        # helper itself. Cleared in :meth:`_stop_egress_proxy_locked`.
        self._egress_relay_port: int | None = None
        self._proc: subprocess.Popen[str] | None = None
        # Parent-held containment handle from the sandbox backend's
        # post_spawn hook (e.g. a Windows Job Object). Closed in
        # ``_stop_locked`` to tear down the helper's process tree.
        self._sandbox_handle: ContainmentHandle | None = None
        self._tmpdir: Path | None = None
        self._egress_proxy: Any | None = None  # EgressProxy when active
        self._egress_loop: Any | None = None  # asyncio event loop for proxy
        self._egress_thread: threading.Thread | None = None
        # Controller handle for unified start/stop. The legacy
        # ``_egress_proxy`` / ``_egress_loop`` / ``_egress_thread``
        # mirrors are kept for back-compat with any tooling that
        # introspects them, but the lifecycle is driven through
        # the handle when present.
        self._egress_handle: Any | None = None  # EgressProxyHandle
        self._lock = threading.Lock()
        self._closed = False
        atexit.register(self.close)

    def request(self, payload: OpRequest) -> OpResult:
        with self._lock:
            return self._request_locked(payload, allow_retry=True)

    def close(self) -> None:
        with self._lock:
            self._closed = True
            self._stop_locked()

    def _request_locked(
        self,
        payload: OpRequest,
        *,
        allow_retry: bool,
    ) -> OpResult:
        if self._closed:
            return {"error": "OS environment helper is closed"}

        self._ensure_started_locked()
        assert self._proc is not None
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None

        try:
            self._proc.stdin.write(json.dumps(payload) + "\n")
            self._proc.stdin.flush()
            line = self._proc.stdout.readline()
            if not line:
                raise RuntimeError(self._helper_exit_detail_locked())
            result = json.loads(line)
            if isinstance(result, dict):
                return result
            return {"error": f"Helper returned non-object response: {result!r}"}
        except Exception as exc:  # noqa: BLE001 — helper IO failures are retried or surfaced via error dict
            self._stop_locked()
            if allow_retry and not self._closed:
                self._ensure_started_locked()
                return self._request_locked(payload, allow_retry=False)
            return {"error": f"os_env helper failed: {exc}"}

    def _ensure_started_locked(self) -> None:
        if self._proc is not None and self._proc.poll() is None:
            return
        self._start_locked()

    def _start_locked(self) -> None:
        sandbox = self.sandbox
        env = build_helper_env(os.environ, sandbox)
        project_root = str(_project_root())
        existing_pythonpath = env.get("PYTHONPATH")
        env["PYTHONPATH"] = (
            f"{project_root}{os.pathsep}{existing_pythonpath}"
            if existing_pythonpath
            else project_root
        )

        helper_cwd = self.cwd
        credential_runtime: CredentialProxyRuntime | None = None
        if sandbox.active:
            self._tmpdir = create_private_tmpdir()
            sandbox = with_additional_write_roots(sandbox, [self._tmpdir])
            set_temp_env(env, self._tmpdir)
            if self.start_in_scratch:
                helper_cwd = self._tmpdir
                env["PWD"] = str(self._tmpdir)
            if sandbox.credential_proxy is not None:
                # Resolve real secrets in the parent. Real secrets stay
                # here and are attached to outbound requests by the egress
                # proxy (swap-on-access). Only entries that opted into
                # ``inject_env`` contribute a synthetic placeholder, merged
                # into the helper env below; nothing else crosses into the
                # sandbox.
                credential_parent_env = _build_credential_proxy_parent_env(
                    helper_env=env,
                    parent_env=os.environ,
                    spec=sandbox.credential_proxy,
                )
                credential_runtime = prepare_credential_proxy_runtime(
                    sandbox.credential_proxy,
                    parent_env=credential_parent_env,
                )
                env.update(credential_runtime.helper_env_updates)

        # Start L7 egress proxy if rules are configured. The proxy
        # listens on a Unix socket in the scratch tmpdir; the helper
        # starts a relay inside the network namespace that bridges
        # loopback TCP to this socket.
        if self._egress_rules and self._tmpdir is not None:
            sandbox = self._start_egress_proxy_locked(
                sandbox,
                env,
                credential_rewrites=(
                    credential_runtime.rewrites if credential_runtime is not None else None
                ),
            )

        config: dict[str, JsonValue] = {
            "cwd": str(helper_cwd),
            "shell_path": self.shell_path,
            "sandbox": sandbox.to_jsonable(),
        }
        # S4 (security): include the per-helper Proxy-Authorization
        # token IF egress is active. Delivered ONLY via the pipe FD,
        # not via the policy (which serialises to logs/dumps) and not
        # via env (visible to ``ps -E``). The helper picks it up in
        # :func:`_run_helper` and injects it into its own
        # ``HTTP_PROXY`` / ``HTTPS_PROXY`` via in-process
        # ``os.environ`` mutation — that mutation is invisible to
        # ``ps -E`` because the kernel only snapshots envp at
        # execve time, not on later libc mutations.
        if self._egress_auth_token is not None:
            config["egress_auth_token"] = self._egress_auth_token
        # S3 (security): deliver the config via an inherited pipe fd
        # instead of base64-encoding it onto argv. The legacy argv
        # form put the policy bytes on the helper's command line,
        # which any same-UID process could read via
        # ``/proc/<pid>/cmdline`` or ``ps -ww``. That's irrelevant
        # for current fields (paths/booleans), but it would have
        # leaked the per-helper egress auth token while it existed
        # — and it's a footgun for any future field that carries a
        # secret. Argv is a global side-channel; an inherited fd
        # is private to the parent/child pair.
        config_bytes = json.dumps(config, separators=(",", ":"), sort_keys=True).encode("utf-8")
        # Deliver the config off-band — not on argv (readable via
        # ``/proc/<pid>/cmdline`` / ``ps -ww``) and not in env (readable via
        # ``ps -E``) — because it may carry the per-helper egress auth token.
        #
        # POSIX uses an inherited pipe fd (never touches disk). Windows has no
        # ``pass_fds`` / fd inheritance in ``subprocess`` (CPython rejects
        # ``pass_fds`` outright there), so fall back to a short-lived file in the
        # helper's own private tmpdir, which the helper reads and unlinks
        # immediately. Egress is POSIX-only (its proxy binds a Unix socket), so
        # the Windows config carries no secret — only non-sensitive
        # paths/booleans — but we still keep the file private and ephemeral.
        r_fd: int | None = None
        if IS_WINDOWS:
            assert self._tmpdir is not None
            config_file = self._tmpdir / "helper-config.json"
            config_file.write_bytes(config_bytes)
            config_arg = ["--config-file", str(config_file)]
        else:
            r_fd, w_fd = os.pipe()
            # We write the entire config synchronously before spawning.
            # The pipe buffer is typically 64 KiB on macOS/Linux; the
            # config is well under that (paths + booleans). If a future
            # change inflates the config past one pipe buffer we'd
            # deadlock here — replace with a writer thread at that point.
            try:
                os.write(w_fd, config_bytes)
            finally:
                os.close(w_fd)
            config_arg = ["--config-fd", str(r_fd)]
        helper_argv = [
            sys.executable,
            "-m",
            "omnigent.inner.os_env",
            "helper",
            *config_arg,
        ]
        # Spawn-time backends (e.g. linux_bwrap) wrap helper_argv with
        # their launcher; the no-op ``none`` backend leaves it
        # unchanged. The wrap runs only when the policy is
        # active because non-active policies don't have a registered
        # backend to look up (the "none" sandbox is short-circuited
        # in resolve_sandbox without going through the registry).
        if sandbox.active:
            backend = get_backend(sandbox.backend_type)
            spawn_argv = backend.wrap_launcher_argv(
                helper_argv,
                sandbox,
                self.cwd,
                chdir=helper_cwd if helper_cwd != self.cwd else None,
            )
        else:
            spawn_argv = helper_argv
        # ``pass_fds`` (POSIX only) unsets ``FD_CLOEXEC`` on ``r_fd`` so the
        # child (and any launcher wrapping it, e.g. bwrap or sandbox-exec)
        # inherits it across the exec chain. The numeric fd value is preserved
        # in the child, which is why we can pass it as a plain ``--config-fd``
        # argv arg. On Windows the config came via ``--config-file`` instead,
        # so there is no fd to inherit.
        popen_kwargs: dict[str, Any] = {}
        if r_fd is not None:
            popen_kwargs["pass_fds"] = (r_fd,)
        try:
            self._proc = subprocess.Popen(
                spawn_argv,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                cwd=str(self.cwd),
                env=env,
                **popen_kwargs,
            )
        except Exception:
            cleanup_private_tmpdir(self._tmpdir)
            self._tmpdir = None
            raise
        finally:
            # Close the parent's copy of the read end either way —
            # the child has its own copy (via pass_fds) and the data
            # has already been written. Leaving the parent's copy
            # open would prevent the child from seeing EOF on the
            # pipe after reading the config.
            if r_fd is not None:
                with contextlib.suppress(OSError):
                    os.close(r_fd)

        # Post-spawn containment (parent side). A no-op for the POSIX
        # launcher backends (they isolate via wrap_launcher_argv before
        # exec); on Windows this assigns the helper to a kill-on-close
        # Job Object so the whole tree is torn down in ``_stop_locked``.
        if sandbox.active and self._proc.pid is not None:
            self._sandbox_handle = get_backend(sandbox.backend_type).post_spawn(
                sandbox, self._proc.pid
            )

    def _helper_exit_detail_locked(self) -> str:
        if self._proc is None:
            return "OS environment helper exited unexpectedly"
        stderr = ""
        if self._proc.stderr is not None:
            try:
                stderr = self._proc.stderr.read().strip()
            except Exception:  # noqa: BLE001 — stderr read is best-effort for error detail
                stderr = ""
        returncode = self._proc.poll()
        if stderr:
            return f"OS environment helper exited with code {returncode}: {stderr}"
        return f"OS environment helper exited with code {returncode}"

    def _stop_locked(self) -> None:
        proc = self._proc
        self._proc = None
        try:
            if proc is None:
                return
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup of helper subprocess pipes
                pass
            try:
                if proc.stdout is not None:
                    proc.stdout.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup of helper subprocess pipes
                pass
            try:
                if proc.stderr is not None:
                    proc.stderr.close()
            except Exception:  # noqa: BLE001 — best-effort cleanup of helper subprocess pipes
                pass
            if proc.poll() is None:
                try:
                    proc.terminate()
                    proc.wait(timeout=1)
                except Exception:  # noqa: BLE001 — terminate may fail on already-exited process; escalate to kill
                    with contextlib.suppress(Exception):
                        proc.kill()
        finally:
            # Release the containment handle (Windows Job Object): with
            # kill-on-close this also reaps any helper descendants that
            # outlived ``proc.terminate()`` above.
            if self._sandbox_handle is not None:
                with contextlib.suppress(Exception):
                    self._sandbox_handle.close()
                self._sandbox_handle = None
            self._stop_egress_proxy_locked()
            cleanup_private_tmpdir(self._tmpdir)
            self._tmpdir = None

    # ------------------------------------------------------------------
    # Egress proxy lifecycle
    # ------------------------------------------------------------------

    def _start_egress_proxy_locked(
        self,
        sandbox: SandboxPolicy,
        env: dict[str, str],
        *,
        credential_rewrites: list[CredentialRewriteRule] | None = None,
    ) -> SandboxPolicy:
        """Start the egress MITM proxy and inject env vars.

        Security:

        - **Random ephemeral port per helper** (vs the legacy
          hardcoded ``18080``). A port-squat attacker has to race
          every helper start instead of pre-binding a single
          well-known number.
        - **Fail-loud relay bind**: :func:`start_relay` aborts the
          helper if the port is already taken (race lost), so the
          helper's HTTP traffic never silently flows to whatever
          else might be bound on the port.
        - **Block private destinations by default**: when
          :attr:`_egress_allow_private_destinations` is ``False``
          (the default), the proxy refuses to open upstream
          connections to RFC1918 / loopback / link-local / multicast
          / reserved addresses. This is the load-bearing defense
          against DNS-rebinding attacks where the agent uses a
          permissive wildcard rule with a domain the attacker
          controls that resolves to ``127.0.0.1`` (parent localhost
          services) or ``10.x`` (VPC internals).
        - **Per-helper Proxy-Authorization token (S4)**: each helper
          gets a 256-bit random token. The proxy refuses any inbound
          request whose ``Proxy-Authorization`` header doesn't match
          (``407 Proxy Authentication Required``). Closes the
          cross-helper hole on macOS where the relay's loopback TCP
          port is reachable by any same-UID process. The token is
          shipped to the helper through the same inherited pipe FD
          we already use for the rest of the sandbox config, then
          injected into ``HTTP_PROXY`` / ``HTTPS_PROXY`` IN-PROCESS
          (after exec) so it never appears in the execve-time
          ``KERN_PROCARGS2`` / ``/proc/<pid>/environ`` snapshot
          that any same-UID process can read.

        Important env-leak rationale: ``env`` here becomes the
        ``Popen(env=...)`` for the helper. Whatever we put on it is
        visible to every same-UID process via ``ps -E`` / ``sysctl
        KERN_PROCARGS2`` on macOS. So ``HTTP_PROXY`` / ``HTTPS_PROXY``
        deliberately go through TOKEN-LESS here (just
        ``http://127.0.0.1:{port}``), and the helper does the
        token injection in :func:`_run_helper` after reading the
        config FD. A prior revision that ran the token through
        argv (and an even earlier draft that put it on this env
        dict) leaked exactly that way.

        :param sandbox: The current sandbox policy (may be mutated via
            replacement for egress fields).
        :param env: The helper environment dict — proxy/CA env vars
            are added in-place.
        :param credential_rewrites: Optional synthetic-to-real credential
            rewrites the proxy applies (secretless ``credential_proxy``).
        :returns: Updated :class:`SandboxPolicy` with egress relay
            port and socket path set. The egress auth token is NOT
            stored on the policy (which serialises to JSON and
            would land in any debugging dump) — it's plumbed out
            of band via :attr:`_egress_auth_token` for the helper
            config FD to pick up.
        """

        from .egress import apply_egress_env, start_egress_proxy

        assert self._tmpdir is not None

        # Delegate the proxy lifecycle / socket bridge / auth token
        # bootstrap to the shared controller. The helper path uses
        # ``require_auth=True`` because we have an inherited config
        # FD to deliver the token out of band — see the in-process
        # token injection in :func:`_run_helper`.
        handle = start_egress_proxy(
            rules=self._egress_rules or [],
            tmpdir=self._tmpdir,
            allow_private_destinations=self._egress_allow_private_destinations,
            require_auth=True,
            credential_rewrites=credential_rewrites,
        )

        # S4 (security): hold the controller refs on the client so
        # ``_stop_egress_proxy_locked`` can drive the lifecycle and
        # the auth token can be picked up by the config-FD writer
        # in :meth:`_start_locked`. The token is NEVER put on
        # ``env`` here — `apply_egress_env` below is called with
        # ``auth_token=None`` so the helper picks it up via the
        # FD and injects it into HTTP_PROXY in-process after exec.
        self._egress_proxy = handle._proxy
        self._egress_loop = handle._loop
        self._egress_thread = handle._thread
        self._egress_handle = handle
        self._egress_auth_token = handle.auth_token
        self._egress_relay_port = handle.relay_port

        # Set HTTP_PROXY / CA env vars on the spawn env with NO
        # token. The helper reads the token off the inherited FD
        # in :func:`_run_helper` and overwrites HTTP_PROXY in
        # ``os.environ`` post-exec — the no-token version is what
        # appears in the execve ``/proc/<pid>/environ`` snapshot
        # any same-UID process can read.
        apply_egress_env(
            env,
            relay_port=handle.relay_port,
            ca_bundle_path=handle.ca_bundle_path,
            auth_token=None,
        )

        return replace(
            sandbox,
            egress_relay_port=handle.relay_port,
            egress_socket_path=str(handle.socket_path),
        )

    def _stop_egress_proxy_locked(self) -> None:
        """Stop the egress proxy and its event loop."""
        handle = self._egress_handle
        if handle is None:
            return

        self._egress_proxy = None
        self._egress_loop = None
        self._egress_thread = None
        self._egress_handle = None
        # S4 (security): drop the in-memory token reference once the
        # proxy is gone so a heap dump of a long-lived parent
        # doesn't carry a parade of expired tokens.
        self._egress_auth_token = None
        self._egress_relay_port = None

        handle.stop()

    def __del__(self) -> None:
        self.close()


@dataclass
class CallerProcessOSEnvironment(OSEnvironment):
    """OS environment backed by a sandboxed helper subprocess."""

    sandbox: SandboxPolicy
    shell_path: str
    _fork_dir: Path | None = None
    _start_in_scratch: bool = False
    _egress_rules: list[str] | None = None
    _egress_allow_private_destinations: bool = False

    def __post_init__(self) -> None:
        self._helper = _HelperProcessClient(
            cwd=self.cwd,
            shell_path=self.shell_path,
            sandbox=self.sandbox,
            start_in_scratch=self._start_in_scratch,
            egress_rules=self._egress_rules,
            egress_allow_private_destinations=self._egress_allow_private_destinations,
        )

    async def read(
        self,
        path: str,
        offset: int = 1,
        limit: int | None = None,
        max_binary_bytes: int | None = None,
    ) -> OpResult:
        if offset < 1:
            return {"error": "offset must be >= 1"}
        if limit is not None and limit < 1:
            return {"error": "limit must be >= 1"}
        result = await run_sync_on_thread(
            self._helper.request,
            {
                "op": "read",
                "path": path,
                "offset": offset,
                "limit": limit,
                "max_binary_bytes": max_binary_bytes,
            },
        )
        return cast(OpResult, result)

    async def write(self, path: str, content: str) -> OpResult:
        result = await run_sync_on_thread(
            self._helper.request,
            {
                "op": "write",
                "path": path,
                "content": content,
            },
        )
        return cast(OpResult, result)

    async def edit(
        self,
        path: str,
        *,
        old_text: str | None = None,
        new_text: str | None = None,
        edits: Sequence[EditEntry] | None = None,
    ) -> OpResult:
        result = await run_sync_on_thread(
            self._helper.request,
            {
                "op": "edit",
                "path": path,
                "oldText": old_text,
                "newText": new_text,
                "edits": list(edits) if edits is not None else None,
            },
        )
        return cast(OpResult, result)

    async def shell(
        self,
        command: str,
        timeout: int | None = None,
        max_output: int | None = None,
    ) -> OpResult:
        if not isinstance(command, str) or not command.strip():
            return {"error": "command must be a non-empty string"}
        resolved_timeout = 120 if timeout is None else timeout
        if resolved_timeout < 1:
            return {"error": "timeout must be >= 1"}
        request: dict[str, object] = {
            "op": "shell",
            "command": command,
            "timeout": resolved_timeout,
        }
        if max_output is not None:
            request["max_output"] = max_output
        result = await run_sync_on_thread(self._helper.request, request)
        return cast(OpResult, result)

    def close(self) -> None:
        self._helper.close()
        if self._fork_dir is not None:
            shutil.rmtree(self._fork_dir, ignore_errors=True)
            self._fork_dir = None

    def __del__(self) -> None:
        self.close()


def create_os_environment(spec: OSEnvSpec | None) -> OSEnvironment | None:
    """Instantiate the configured OS environment."""
    if spec is None:
        return None
    if spec.type != "caller_process":
        raise NotImplementedError(f"os_env type '{spec.type}' is not implemented")

    cwd = Path(spec.cwd or os.getcwd()).resolve(strict=False)
    fork_dir: Path | None = None
    if spec.fork:
        fork_dir = Path(tempfile.mkdtemp(prefix="omnigent-fork-"))
        effective_cwd = fork_dir / "root"
        _copy_tree(cwd, effective_cwd)
        cwd = effective_cwd
    sandbox = resolve_sandbox(spec, cwd)
    if spec.start_in_scratch and not sandbox.active:
        raise ValueError(
            "os_env.start_in_scratch requires an active sandbox; "
            f"resolved sandbox type {sandbox.backend_type!r} is inactive"
        )
    shell_path = shutil.which("bash") or shutil.which("sh")
    if shell_path is None:
        # No POSIX shell on PATH. On Windows fall back to cmd.exe; elsewhere
        # keep the historical /bin/sh default.
        shell_path = os.environ.get("COMSPEC", "cmd.exe") if IS_WINDOWS else "/bin/sh"
    egress_rules = spec.sandbox.egress_rules if spec.sandbox else None
    egress_allow_private = (
        spec.sandbox.egress_allow_private_destinations if spec.sandbox else False
    )
    return CallerProcessOSEnvironment(
        spec=spec,
        cwd=cwd,
        sandbox=sandbox,
        shell_path=shell_path,
        _fork_dir=fork_dir,
        _start_in_scratch=spec.start_in_scratch,
        _egress_rules=egress_rules,
        _egress_allow_private_destinations=egress_allow_private,
    )


def default_os_env_spec_for_type(env_type: str) -> OSEnvSpec:
    """Build a default OSEnvSpec for string shorthand config."""
    if env_type != "caller_process":
        raise NotImplementedError(f"os_env type '{env_type}' is not implemented")
    return OSEnvSpec(type="caller_process")


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a directory tree preserving symlinks."""

    shutil.copytree(str(src), str(dst), symlinks=True)


def _handle_helper_request(
    *,
    request: OpRequest,
    cwd: Path,
    shell_path: str,
    sandbox: SandboxPolicy,
) -> OpResult:
    op = request.get("op")
    if op == "read":
        raw_path = request.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"error": "path must be a non-empty string"}
        path = _resolve_path(cwd, raw_path)
        try:
            _assert_within_cwd(cwd, path)
            _assert_read_allowed(sandbox, path)
        except PermissionError as exc:
            return {"error": str(exc)}
        offset_raw = request.get("offset", 1)
        offset = offset_raw if isinstance(offset_raw, int) else 1
        max_binary_raw = request.get("max_binary_bytes")
        max_binary_bytes = max_binary_raw if isinstance(max_binary_raw, int) else None
        return _read_impl(
            path,
            offset,
            request.get("limit"),
            max_binary_bytes=max_binary_bytes,
        )

    if op == "write":
        raw_path = request.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"error": "path must be a non-empty string"}
        path = _resolve_path(cwd, raw_path)
        try:
            _assert_within_cwd(cwd, path)
            _assert_write_allowed(sandbox, path)
        except PermissionError as exc:
            return {"error": str(exc)}
        # ``content`` is optional — JSON ``null`` or missing maps to an
        # empty-file write. Non-string values are rejected.
        raw_content = request.get("content")
        if raw_content is None:
            content = ""
        elif isinstance(raw_content, str):
            content = raw_content
        else:
            return {"error": "content must be a string"}
        return _write_impl(path, content)

    if op == "edit":
        raw_path = request.get("path")
        if not isinstance(raw_path, str) or not raw_path.strip():
            return {"error": "path must be a non-empty string"}
        path = _resolve_path(cwd, raw_path)
        try:
            _assert_within_cwd(cwd, path)
            _assert_read_allowed(sandbox, path)
            _assert_write_allowed(sandbox, path)
        except PermissionError as exc:
            return {"error": str(exc)}
        return _edit_impl(
            path,
            request.get("oldText"),
            request.get("newText"),
            request.get("edits"),
        )

    if op == "shell":
        command = request.get("command")
        if not isinstance(command, str) or not command.strip():
            return {"error": "command must be a non-empty string"}
        timeout_raw = request.get("timeout", 120)
        timeout = timeout_raw if isinstance(timeout_raw, int) else 120
        if timeout < 1:
            return {"error": "timeout must be >= 1"}
        max_output_raw = request.get("max_output")
        if max_output_raw is not None:
            if not isinstance(max_output_raw, int) or max_output_raw < 1:
                return {"error": "max_output must be >= 1"}
        # Cap at 5 MB to prevent context blowouts from adversarial or accidental values.
        max_output = min(
            max_output_raw if isinstance(max_output_raw, int) else _MAX_TOOL_OUTPUT_CHARS,
            5_000_000,
        )
        return _shell_impl(
            command=command,
            timeout=timeout,
            shell_path=shell_path,
            cwd=cwd,
            max_output=max_output,
        )

    return {"error": f"Unsupported os_env helper operation: {op!r}"}


def _resolve_path(cwd: Path, path: str) -> Path:
    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = cwd / candidate
    return candidate.resolve(strict=False)


def _assert_within_cwd(cwd: Path, resolved: Path) -> None:
    """Block access to paths outside the environment root.

    Runs **unconditionally** — even when the sandbox policy is
    inactive (``type: "none"``) — so symlink escapes are caught
    regardless of sandbox configuration.

    :param cwd: The environment's root working directory (already
        resolved).
    :param resolved: The fully-resolved target path (after
        ``_resolve_path``).
    :raises PermissionError: If *resolved* is not equal to or
        inside *cwd*.
    """
    resolved_cwd = cwd.resolve()
    try:
        resolved.relative_to(resolved_cwd)
    except ValueError as exc:
        raise PermissionError(
            f"Access to '{resolved}' is blocked: path is outside "
            f"the environment root '{resolved_cwd}'"
        ) from exc


def _assert_read_allowed(policy: SandboxPolicy, path: Path) -> None:
    roots = policy.read_roots
    if not policy.active or roots is None:
        return
    if any(_is_within(path, root) for root in roots):
        return
    raise PermissionError(f"Read access to '{path}' is blocked by sandbox.")


def _assert_write_allowed(policy: SandboxPolicy, path: Path) -> None:
    if not policy.active:
        return
    if any(path == allowed for allowed in policy.write_files):
        return
    if any(_is_within(path, root) for root in policy.write_roots):
        return
    raise PermissionError(f"Write access to '{path}' is blocked by sandbox.")


# Bytes sampled to classify a file as text vs binary. A NUL byte or an invalid
# UTF-8 sequence in this prefix marks the file binary (the same prefix-sniff
# heuristic git uses), so a multi-MB binary is never read in full just to find
# out it is not text.
_BINARY_SNIFF_BYTES = 8192


def _is_binary_file(path: Path) -> bool:
    """Classify *path* as binary by inspecting only its first chunk.

    Reads at most :data:`_BINARY_SNIFF_BYTES` and reports binary when those
    bytes contain a NUL or are not valid UTF-8. The NUL check matters because
    ``\x00`` *is* valid UTF-8, so a UTF-16/NUL-laden file would otherwise be
    misread as text; checking for it explicitly matches git's heuristic. An
    incremental decoder is used with ``final=False`` so a multi-byte character
    straddling the chunk boundary is treated as *incomplete* (text), not
    invalid (binary).

    :param path: Absolute path of the file to classify.
    :returns: ``True`` if the prefix contains a NUL or is not decodable UTF-8.
    """
    with path.open("rb") as fh:
        prefix = fh.read(_BINARY_SNIFF_BYTES)
    if b"\x00" in prefix:
        return True
    try:
        codecs.getincrementaldecoder("utf-8")("strict").decode(prefix, final=False)
    except UnicodeDecodeError:
        return True
    return False


def _read_binary_impl(path: Path, max_binary_bytes: int | None) -> OpResult:
    """Read a binary file as base64, bounded by *max_binary_bytes*.

    Only ``stat`` (for the total size) and at most *max_binary_bytes* are read
    from disk, so a large file neither saturates memory nor inflates IPC.

    :param path: Absolute path of the binary file.
    :param max_binary_bytes: Byte cap. ``None`` returns a descriptor only (the
        agent ``sys_os_read`` path); a positive int inlines up to that many
        base64-encoded bytes (the filesystem-service path).
    :returns: An :class:`OpResult` with ``encoding="base64"`` (see
        :func:`_read_impl`).
    """
    total = path.stat().st_size
    if max_binary_bytes is None:
        # Agent tool path: return a descriptor only — inlining base64 the
        # model cannot use would waste (and risk saturating) the context.
        return {
            "path": str(path),
            "encoding": "base64",
            "content": "",
            "total_bytes": total,
            # Not truncated — the content was deliberately not inlined.
            "truncated": False,
            "note": (
                f"Binary file not inlined ({total} bytes). "
                "View or download it via the file viewer."
            ),
        }
    with path.open("rb") as fh:
        payload = fh.read(max_binary_bytes)
    return {
        "path": str(path),
        "content": base64.b64encode(payload).decode("ascii"),
        "encoding": "base64",
        "total_bytes": total,
        "returned_bytes": len(payload),
        "truncated": len(payload) < total,
    }


def _read_impl(
    path: Path,
    offset: int,
    limit: JsonValue,
    max_binary_bytes: int | None = None,
) -> OpResult:
    """
    Read a file as UTF-8 text, or as base64-encoded bytes when it is binary.

    The file's first chunk is sniffed for UTF-8 validity (see
    :func:`_is_binary_file`). Files that look like text are read and returned
    with the usual line-oriented ``offset``/``limit`` windowing. Files that do
    *not* (images, archives, fonts, …) cannot be line-windowed, so they are
    capped by *bytes* instead, reading at most ``max_binary_bytes`` from disk.

    For binary files the behaviour depends on ``max_binary_bytes``:

    * ``None`` (the default, used by the agent ``sys_os_read`` tool) — the
      base64 payload is **not** inlined. A model cannot decode base64, and a
      multi-MB blob would saturate the context window, so only a descriptor
      (``total_bytes`` + a ``note``) is returned.
    * a positive int (used by the filesystem service that feeds the web
      viewer / downloads) — up to that many raw bytes are base64-encoded and
      returned, with ``truncated`` set when the file was larger.

    :param path: Absolute path of the file to read.
    :param offset: 1-based line number to start reading from (text only).
    :param limit: Maximum number of lines to return, or ``None`` for no
        limit (return all lines from *offset* to end of file).  Callers
        that want the default agent-tool cap should pass
        :data:`_DEFAULT_READ_LIMIT` explicitly.  Ignored for binary files.
    :param max_binary_bytes: Byte cap for binary files (see above). ``None``
        returns a descriptor only.
    :returns: For text, an :class:`OpResult` with ``encoding="utf-8"``,
        ``content``, ``offset``, ``limit``, ``returned_lines``, and
        ``total_lines``.  For binary, ``encoding="base64"``, ``total_bytes``,
        ``truncated`` and either ``content`` (the base64 string, byte-capped
        callers) or a ``note`` (descriptor-only callers).
    """
    if offset < 1:
        return {"error": "offset must be >= 1"}
    if limit is not None:
        if not isinstance(limit, int) or limit < 1:
            return {"error": "limit must be >= 1"}
    if max_binary_bytes is not None and max_binary_bytes < 1:
        return {"error": "max_binary_bytes must be >= 1"}

    if _is_binary_file(path):
        return _read_binary_impl(path, max_binary_bytes)

    try:
        text = path.read_text(encoding="utf-8", errors="strict")
    except UnicodeDecodeError:
        # The sniffed prefix decoded cleanly but bytes further in did not (a
        # file that is text up front and binary later). Fall back to the binary
        # path so we never return garbled text.
        return _read_binary_impl(path, max_binary_bytes)

    lines = text.splitlines(keepends=True)
    start = offset - 1
    effective_limit = len(lines) if limit is None else limit
    resolved_limit = min(len(lines), start + effective_limit)
    content = "".join(lines[start:resolved_limit])
    return {
        "path": str(path),
        "content": content,
        "encoding": "utf-8",
        "offset": offset,
        "limit": effective_limit,
        "returned_lines": max(0, resolved_limit - start),
        "total_lines": len(lines),
    }


def _write_impl(path: Path, content: str) -> OpResult:
    path.parent.mkdir(parents=True, exist_ok=True)
    existed = path.exists()
    path.write_text(content, encoding="utf-8")
    return {
        "path": str(path),
        "bytes_written": len(content.encode("utf-8")),
        "created": not existed,
    }


def _edit_impl(
    path: Path,
    old_text: JsonValue,
    new_text: JsonValue,
    edits: JsonValue,
) -> OpResult:
    original = path.read_text(encoding="utf-8", errors="replace")
    try:
        replacements = _normalize_edits(
            old_text if isinstance(old_text, str) else None,
            new_text if isinstance(new_text, str) else None,
            edits,
        )
    except ValueError as exc:
        return {"error": str(exc)}

    updated = original
    applied = 0
    for edit in replacements:
        before = edit["oldText"]
        after = edit["newText"]
        count = updated.count(before)
        if count == 0:
            return {"error": f"Could not find oldText in '{path}': {before[:80]!r}"}
        if count > 1:
            return {
                "error": (
                    f"oldText matched {count} locations in '{path}'; provide a more specific edit."
                )
            }
        updated = updated.replace(before, after, 1)
        applied += 1

    path.write_text(updated, encoding="utf-8")
    return {
        "path": str(path),
        "replacements": applied,
        "bytes_written": len(updated.encode("utf-8")),
    }


def _truncate_output(text: str, label: str, limit: int = _MAX_TOOL_OUTPUT_CHARS) -> str:
    """
    Truncate a tool output field to ``limit`` characters.

    Appends a notice so the model knows the content was cut rather than
    silently presenting a partial view as the full output.

    :param text: The raw output string to potentially truncate.
    :param label: Human-readable field name for the truncation notice,
        e.g. ``"stdout"``.
    :param limit: Maximum number of characters to return. Defaults to
        :data:`_MAX_TOOL_OUTPUT_CHARS`.
    :returns: The original string if within the limit, otherwise the
        first ``limit`` characters followed by a ``"[truncated: ...]"``
        sentinel.
    """
    if len(text) <= limit:
        return text
    truncated = text[:limit]
    dropped = len(text) - limit
    return (
        truncated + f"\n[{label} truncated: {dropped:,} characters omitted."
        " Use a more targeted command to see the full output.]"
    )


def _shell_impl(
    *,
    command: str,
    timeout: int,
    shell_path: str,
    cwd: Path,
    max_output: int = _MAX_TOOL_OUTPUT_CHARS,
) -> OpResult:
    """
    Execute a shell command and return its output.

    :param command: The shell command to run.
    :param timeout: Maximum seconds to wait before killing the command.
    :param shell_path: Absolute path to the shell binary, e.g. ``"/bin/bash"``.
    :param cwd: Working directory for the command.
    :param max_output: Maximum characters to return per output field
        (``stdout`` and ``stderr`` each). Defaults to
        :data:`_MAX_TOOL_OUTPUT_CHARS`.
    :returns: An :class:`OpResult` dict with ``stdout``, ``stderr``,
        ``exit_code``, ``timed_out``, ``shell``, and ``cwd`` fields.
        ``stdout`` and ``stderr`` are each truncated to ``max_output``
        characters.
    """
    argv = _shell_argv(shell_path, command)
    try:
        completed = subprocess.run(
            argv,
            cwd=str(cwd),
            text=True,
            capture_output=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        # ``subprocess.TimeoutExpired.stdout``/``stderr`` are ``str | bytes
        # | None`` from the stdlib; widening the op result at this boundary
        # would leak ``None`` into a JSON-serialized field that downstream
        # consumers treat as a plain string.
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return {
            "stdout": _truncate_output(stdout, "stdout", max_output),
            "stderr": _truncate_output(stderr, "stderr", max_output),
            "timed_out": True,
            "error": f"Command timed out after {timeout} seconds",
            "shell": shell_path,
            "cwd": str(cwd),
        }
    except OSError as exc:
        return {"error": f"Failed to run shell command: {exc}"}

    stdout = _truncate_output(completed.stdout, "stdout", max_output)
    stderr = _truncate_output(completed.stderr, "stderr", max_output)
    result: OpResult = {
        "stdout": stdout,
        "stderr": stderr,
        "exit_code": completed.returncode,
        "timed_out": False,
        "shell": shell_path,
        "cwd": str(cwd),
    }
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        if detail:
            result["error"] = f"Command exited with status {completed.returncode}: {detail}"
        else:
            result["error"] = f"Command exited with status {completed.returncode}"
    return result


def _normalize_edits(
    old_text: str | None,
    new_text: str | None,
    edits: JsonValue,
) -> list[EditEntry]:
    if edits is not None and (old_text is not None or new_text is not None):
        raise ValueError("Provide either oldText/newText or edits, not both")
    if edits is not None:
        if not isinstance(edits, list):
            raise ValueError("edits must be an array of {oldText, newText} objects")
        normalized: list[EditEntry] = []
        for edit in edits:
            if not isinstance(edit, dict):
                raise ValueError("Each edit must be an object")
            old_value = edit.get("oldText")
            new_value = edit.get("newText")
            if not isinstance(old_value, str) or not isinstance(new_value, str):
                raise ValueError("Each edit must contain string oldText and newText")
            normalized.append({"oldText": old_value, "newText": new_value})
        return normalized
    if old_text is None or new_text is None:
        raise ValueError("edit requires oldText/newText or edits")
    return [{"oldText": old_text, "newText": new_text}]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _shell_argv(shell_path: str, command: str) -> list[str]:
    shell_name = Path(shell_path).name.lower()
    if shell_name in ("cmd.exe", "cmd"):
        return [shell_path, "/c", command]
    if shell_name in ("powershell.exe", "powershell", "pwsh.exe", "pwsh"):
        return [shell_path, "-NoProfile", "-Command", command]
    if shell_name in ("bash", "bash.exe"):
        return [shell_path, "--noprofile", "--norc", "-c", command]
    return [shell_path, "-c", command]


def _project_root() -> Path:
    # File lives at omnigent/inner/os_env.py; climb two levels to the
    # repo root that hosts `omnigent/` as a package.
    return Path(__file__).resolve().parents[2]


def _read_config_from_fd(fd: int) -> JsonValue:
    """Read and JSON-decode the helper config from an inherited fd.

    Wraps the legacy "config-on-argv" path; see the matching
    parent-side code in
    :meth:`_HelperProcessClient._start_locked` for the rationale.
    The fd is fully drained (read until EOF) so the caller can close
    it immediately after — there is exactly one config payload per
    helper invocation.

    :param fd: The inherited read end of the parent-created pipe.
    :returns: Decoded JSON value (always a dict in practice).
    :raises ValueError: When the payload is not valid JSON or the
        pipe was closed before any data arrived.
    """
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 65536)
        if not chunk:
            break
        chunks.append(chunk)
    with contextlib.suppress(OSError):
        os.close(fd)
    raw = b"".join(chunks)
    if not raw:
        raise ValueError(
            f"os_env helper got empty config on fd {fd}; expected a "
            "JSON object written by the parent before exec."
        )
    return cast(JsonValue, json.loads(raw.decode("utf-8")))


def _read_config_from_file(path: str) -> JsonValue:
    """Read and unlink the helper config file (Windows config-delivery path).

    The parent writes the config to a file in the helper's private tmpdir when
    inherited-fd delivery is unavailable (Windows has no ``pass_fds``). Unlink
    it immediately after reading so a secret-bearing config (none on Windows
    today — egress is POSIX-only) lives on disk only momentarily.

    :param path: Absolute path to the JSON config file.
    :returns: Decoded JSON value (always a dict in practice).
    :raises ValueError: When the file is empty or not valid JSON.
    """
    config_path = Path(path)
    try:
        raw = config_path.read_bytes()
    finally:
        with contextlib.suppress(OSError):
            config_path.unlink()
    if not raw:
        raise ValueError(
            f"os_env helper got empty config file {path!r}; expected a "
            "JSON object written by the parent before spawn."
        )
    return cast(JsonValue, json.loads(raw.decode("utf-8")))


def _run_helper(config: JsonValue) -> int:
    if not isinstance(config, dict):
        raise ValueError("Invalid os_env helper config")

    cwd_value = config.get("cwd")
    shell_path_value = config.get("shell_path")
    sandbox_value = config.get("sandbox")
    if not isinstance(cwd_value, str) or not isinstance(shell_path_value, str):
        raise ValueError("Invalid os_env helper config payload")
    if not isinstance(sandbox_value, dict):
        raise ValueError("Invalid os_env helper sandbox payload")

    # S4 (security): if the parent shipped a Proxy-Authorization
    # token via the config FD, splice it into HTTP_PROXY / HTTPS_PROXY
    # NOW — before any HTTP client in the helper (or any subprocess
    # we later spawn) reads those env vars. The token MUST NOT be
    # passed to us via env (the parent puts the token-less URL there
    # specifically so a same-UID ``ps -E`` reader gets nothing). The
    # mutation lives only in libc's in-process ``environ`` array;
    # the kernel's execve-time snapshot read by ``ps`` / ``sysctl
    # KERN_PROCARGS2`` is untouched, so the token never appears in
    # any external observer's view.
    token_value = config.get("egress_auth_token")
    if isinstance(token_value, str) and token_value:
        # ``urllib.parse`` correctly handles ``http://127.0.0.1:5678``
        # and leaves us a hostname+port we can re-anchor with the
        # auth token spliced into the userinfo.
        for env_key in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
            current = os.environ.get(env_key)
            if not current:
                continue
            parts = urlparse(current)
            if not parts.hostname:
                continue
            netloc = f"omnigent:{token_value}@{parts.hostname}"
            if parts.port is not None:
                netloc += f":{parts.port}"
            os.environ[env_key] = urlunparse(parts._replace(netloc=netloc))

    cwd = Path(cwd_value)
    os.chdir(cwd)

    sandbox = SandboxPolicy.from_jsonable(sandbox_value)
    activate_sandbox(sandbox)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
            if not isinstance(request, dict):
                raise ValueError("helper requests must be JSON objects")
            response = _handle_helper_request(
                request=request,
                cwd=cwd,
                shell_path=shell_path_value,
                sandbox=sandbox,
            )
        except Exception as exc:  # noqa: BLE001 — helper loop surfaces any error through the JSON response envelope
            response = {"error": f"os_env helper exception: {exc}"}
        sys.stdout.write(json.dumps(response) + "\n")
        sys.stdout.flush()
    return 0


def main(argv: list[str] | None = None) -> int:
    args = argv or sys.argv[1:]
    if not args:
        raise SystemExit("usage: python -m omnigent.inner.os_env <helper|launch> ...")

    command = args[0]
    if command == "helper":
        # Config is delivered off-band so the policy/token never lands on argv:
        # POSIX via an inherited pipe (``--config-fd N``); Windows via a private
        # file (``--config-file PATH``), since Windows has no ``pass_fds``. The
        # legacy positional ``<base64-json>`` form was removed (it leaked the
        # policy onto the command line). Reject anything else loudly so a stale
        # launcher script doesn't silently boot a helper with no config.
        if len(args) != 3 or args[1] not in ("--config-fd", "--config-file"):
            raise SystemExit(
                "usage: python -m omnigent.inner.os_env helper "
                "(--config-fd <fd> | --config-file <path>)"
            )
        if args[1] == "--config-fd":
            try:
                fd = int(args[2])
            except ValueError as exc:
                raise SystemExit(f"os_env helper: invalid --config-fd value {args[2]!r}") from exc
            config = _read_config_from_fd(fd)
        else:
            config = _read_config_from_file(args[2])
        return _run_helper(config)

    if command == "launch":
        if len(args) < 3:
            raise SystemExit(
                "usage: python -m omnigent.inner.os_env launch <sandbox> <target> [args...]"
            )
        return _run_launcher(args[1], args[2], args[3:])

    raise SystemExit(f"unknown os_env subcommand: {command}")


if __name__ == "__main__":
    raise SystemExit(main())
