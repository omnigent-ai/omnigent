"""Host process main loop for ``omnigent host``.

Connects to the server via WebSocket, registers as a host, and
listens for ``host.launch_runner`` / ``host.stop_runner`` frames.
Spawns runner subprocesses on demand and reports results back to
the server.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import functools
import json
import logging
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Protocol, SupportsIndex, SupportsInt, cast

import websockets.asyncio.client
from websockets.exceptions import ConnectionClosed, InvalidStatus, InvalidURI

from omnigent._platform import IS_POSIX, WINDOWS_ENV_PASSTHROUGH
from omnigent.cli_invocation import cli_invocation
from omnigent.debug_logging import (
    ORIGIN_WORKSPACE_ID_ENV_VAR,
    PRIMARY_SESSION_ID_ENV_VAR,
    USER_ID_ENV_VAR,
)
from omnigent.env_credentials import env_names_with_omnigent_prefix
from omnigent.gateway_inference import gateway_inference_map
from omnigent.harness_aliases import canonicalize_harness, is_claude_sdk_harness_name
from omnigent.harness_availability import HARNESS_BINARY_MISSING, HarnessAvailability
from omnigent.host import HOST_FATAL_EXIT_CODE
from omnigent.host.daemon_lifecycle import DaemonLifecycleLock
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE,
    WORKSPACE_MISSING_ERROR_CODE,
    HostConnectionErrorFrame,
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostCreateWorktreeFrame,
    HostCreateWorktreeResultFrame,
    HostDetectCredentialsFrame,
    HostDetectCredentialsResultFrame,
    HostFsRequestFrame,
    HostFsResultFrame,
    HostHarnessReadinessFrame,
    HostHelloFrame,
    HostImportedLocalSession,
    HostImportLocalDoneFrame,
    HostImportLocalFrame,
    HostImportLocalSessionFrame,
    HostInstallHarnessFrame,
    HostInstallHarnessResultFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostListDirEntry,
    HostListDirFrame,
    HostListDirResultFrame,
    HostListWorktreesFrame,
    HostListWorktreesResultFrame,
    HostModelOptionsFrame,
    HostModelOptionsResultFrame,
    HostRemoveWorktreeFrame,
    HostRemoveWorktreeResultFrame,
    HostRunnerExitedFrame,
    HostRunnerStatusFrame,
    HostRunnerStatusResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    HostStopRunnerResultFrame,
    HostStoreSecretFrame,
    HostStoreSecretResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.git_worktree import (
    WorktreeError,
    create_worktree,
    list_worktrees,
    remove_worktree,
)
from omnigent.host.identity import HostIdentity, load_or_create_host_identity
from omnigent.host.runner_zygote import ZygoteManager, ZygoteRunnerProc, ZygoteUnavailable
from omnigent.inner import _proc
from omnigent.onboarding.harness_auth import (
    adopt_env_credential,
    detect_adoptable_credentials,
    store_harness_credential,
)
from omnigent.onboarding.harness_install import (
    harness_cli_installed,
    harness_setup_hint,
    try_install_harness_cli,
    ui_install_key,
)
from omnigent.onboarding.harness_readiness import (
    configured_harness_map,
    harness_is_configured,
)
from omnigent.onboarding.provider_config import ANTHROPIC_FAMILY, OPENAI_FAMILY
from omnigent.process_logging import (
    LOG_TTY_FD_ENV_VAR,
    PROCESS_LOG_FILE_ENV_VAR,
    child_logging_popen_kwargs,
    configure_process_logging,
    display_log_path,
    env_truthy,
    open_process_log_file,
    process_log_dir,
)
from omnigent.runner._zygote import ZYGOTE_ENABLED_ENV_VAR
from omnigent.runner.identity import (
    RUNNER_DELEGATED_AUTH_ENV_VAR,
    RUNNER_ID_ENV_VAR,
    RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR,
    RUNNER_LAUNCH_HARNESS_ENV_VAR,
    RUNNER_PARENT_PID_ENV_VAR,
    RUNNER_SLICE_KEY_ENV_VAR,
    RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR,
    RUNNER_WORKSPACE_ENV_VAR,
    token_bound_runner_id,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from omnigent.runner.transports.ws_tunnel.limits import (
    TUNNEL_KEEPALIVE_PING_INTERVAL_S,
    TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
)
from omnigent.suspend_watch import watch_for_resume
from omnigent.tls import client_ssl_context
from omnigent.version import VERSION

_logger = logging.getLogger(__name__)


class _WaitidInfo(Protocol):
    si_pid: int


def _coerce_int(value: object) -> int:
    """Convert a validated JSON scalar with the standard ``int`` semantics."""
    return int(cast(str | bytes | bytearray | SupportsInt | SupportsIndex, value))


# Binary appearance is cheap to probe, so new CLI installs surface quickly.
HARNESS_READINESS_REFRESH_INTERVAL_S = 5.0
# Auth changes and removals need the full, potentially expensive readiness map.
HARNESS_READINESS_FULL_REFRESH_INTERVAL_S = 60.0


def _unavailable_harness_became_ready(
    previous: Mapping[str, HarnessAvailability],
) -> bool:
    """Detect newly available binaries; auth changes wait for the full refresh."""
    return any(
        (availability is False or availability == HARNESS_BINARY_MISSING)
        and harness_is_configured(harness)
        for harness, availability in previous.items()
    )


def _runner_log_dir() -> Path:
    """Return the directory holding per-session runner logs for this host.

    Each ``host.launch_runner`` writes its runner subprocess's captured
    stdout/stderr to a ``runner-*.log`` file here. Computed at call time
    (not a module constant) so tests that repoint ``Path.home`` see the
    override.

    :returns: The runner log directory, e.g.
        ``<data-dir>/logs/runner``.
    """
    return process_log_dir("runner")


# Max bytes read from the end of a dead runner's log when composing an
# exit report. 4 KiB is roughly the last 40-60 lines — enough to carry
# a Python traceback or the tunnel rejection message.
_LOG_TAIL_MAX_BYTES = 4096

# Max log-tail lines included in a runner exit report. The report ends
# up verbatim in a CLI error message, so it must stay short enough that
# the error summary above it remains visible.
_LOG_TAIL_MAX_LINES = 15

# Poll cadence for the per-runner exit watcher. 0.5s matches the client's
# steady-state online-poll cadence (daemon_launch.DAEMON_POLL_INTERVAL_S),
# so a crashed runner is reported within about one client poll.
_RUNNER_WATCH_INTERVAL_S = 0.5

# Cadence of the orphan-reaper sweep. The host installs itself as a child
# subreaper (Linux — see :func:`_install_child_subreaper`), so a harness's
# detached tool subprocess (node/npm/chromium/tmux/python) whose runner
# parent died reparents to the host. With no reaper such an orphan lingers
# as a ``<defunct>`` zombie; over an overnight blocked run they reached
# ~900 zombies and OOM'd the box (#1782). A ``WNOHANG`` sweep is a cheap
# syscall, so 2s keeps zombie lifetime short at negligible cost.
_ORPHAN_REAP_INTERVAL_S = 2.0


def _install_child_subreaper() -> bool:
    """Make this process reap orphaned descendants (Linux only).

    ``prctl(PR_SET_CHILD_SUBREAPER, 1)`` asks the kernel to reparent any
    orphaned descendant — e.g. a harness's detached tool subprocess whose
    runner parent exited — to THIS process instead of PID 1, so the host's
    orphan reaper can ``wait()`` on it even when the host is not itself
    PID 1 (e.g. ``omni host --server`` launched under a shell). When the
    host already IS PID 1 (container entrypoint) orphans reparent here
    regardless and this call is a harmless no-op.

    Complements — does not replace — the per-runner ``_watch_runner``
    reaping of the host's own direct children.

    :returns: ``True`` if the subreaper bit was set; ``False`` on non-Linux
        or if ``prctl`` is unavailable. Both are non-fatal: direct-child
        reaping and the PID-1 case still work; only the non-PID-1 orphan
        case degrades.
    """
    if sys.platform != "linux":
        return False
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        _PR_SET_CHILD_SUBREAPER = 36
        return libc.prctl(_PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, AttributeError):
        return False


def _read_log_tail(path: Path, max_bytes: int = _LOG_TAIL_MAX_BYTES) -> str:
    """Read the last portion of a runner log file for diagnostics.

    :param path: The runner's captured stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/runner/runner-ab12.log")``.
    :param max_bytes: Max bytes to read from the end of the file,
        e.g. ``4096``.
    :returns: The decoded tail (lossy UTF-8 — runner output may
        contain arbitrary bytes), or ``""`` when the file is empty,
        missing, or unreadable. Diagnostics are best-effort: an
        unreadable log must not turn a useful "runner died with code
        1" answer into a failure.
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, os.SEEK_END)
            size = fh.tell()
            fh.seek(max(0, size - max_bytes))
            return fh.read().decode("utf-8", errors="replace")
    except OSError:
        return ""


def _runner_exit_error(exit_code: int | None, log_path: Path) -> str:
    """Compose the human-readable error for a runner that died.

    Carries the actual cause to the user: exit code, the host-side log
    path (for the full log), and the trailing log lines — the part that
    usually holds the traceback or tunnel-rejection message. Without
    this, the cause stays in a file on the host and every consumer just
    sees a connect timeout.

    :param exit_code: The runner process's exit code, e.g. ``1``.
        ``None`` when unknown.
    :param log_path: The runner's captured stdout/stderr log file.
    :returns: A multi-line error message ready to surface verbatim in
        a CLI error or API ``error`` field.
    """
    message = "runner process exited"
    if exit_code is not None:
        message += f" with code {exit_code}"
    message += f" (log on host: {display_log_path(log_path)})"
    tail = _read_log_tail(log_path)
    if tail.strip():
        lines = tail.strip().splitlines()[-_LOG_TAIL_MAX_LINES:]
        message += "\n--- runner log tail ---\n" + "\n".join(lines)
    return message


def _url_is_loopback(url: str) -> bool:
    """Whether ``url``'s host is loopback (``127.0.0.1`` / ``localhost`` / ``::1``).

    Used to distinguish a daemon-spawned local server (no proxy in
    front) from a remote deploy behind the Databricks Apps ingress: the
    reconnect loop treats an abrupt ``no close frame`` as a benign
    ingress recycle only when there IS an ingress, and bounds
    connection-refused retries only when the server is local.

    :param url: A server or ws:// URL, e.g. ``"ws://127.0.0.1:49175"``.
    :returns: ``True`` for a loopback host, ``False`` otherwise (incl.
        unparseable URLs — fail toward "remote", the safer default for
        both reconnect heuristics).
    """
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


def _connection_refused(exc: BaseException) -> bool:
    """Whether *exc* means the target port actively refused the connection.

    Dual-stack connects surface wrapped: asyncio combines per-address
    failures into ``OSError("Multiple exceptions: ...")`` whose errno is
    lost (only the ``[Errno N]`` texts survive), or an exception group.
    A wrapped form counts only when every sub-error is itself refused.

    :param exc: The exception a connect attempt raised.
    :returns: ``True`` for a connection-refused failure, ``False``
        otherwise (fail toward "not refused" — the loop keeps retrying).
    """
    # BaseExceptionGroup is a 3.11+ builtin (we require 3.12); ruff's pinned
    # py310 target misflags it as undefined.
    if isinstance(exc, BaseExceptionGroup):  # noqa: F821
        return all(_connection_refused(sub) for sub in exc.exceptions)
    if not isinstance(exc, OSError):
        return False
    if exc.errno is not None:
        return exc.errno == errno.ECONNREFUSED
    errnos = re.findall(r"\[Errno (\d+)\]", str(exc))
    return bool(errnos) and all(int(n) == errno.ECONNREFUSED for n in errnos)


_RECONNECT_BASE_S = 0.5
_RECONNECT_CAP_S = 3.0
_RECONNECT_JITTER = 0.5
# How often the lifecycle monitor re-checks that this daemon still owns its
# registry record. Cheap local-file read; a stale daemon retiring within a
# minute is prompt enough, and 60s keeps polling churn negligible.
# ``OMNIGENT_HOST_LIFECYCLE_POLL_S`` overrides it (e2e tests set it low so the
# self-terminate path is observable without a minute-long wait).
try:
    _LIFECYCLE_POLL_INTERVAL_S = float(os.environ.get("OMNIGENT_HOST_LIFECYCLE_POLL_S", "60"))
except ValueError:
    _LIFECYCLE_POLL_INTERVAL_S = 60.0
# Keep first startup tolerant of a cold server, but do not spend the library's
# full default timeout on each reconnect after an established tunnel drops.
_INITIAL_CONNECT_OPEN_TIMEOUT_S = 10.0
_RECONNECT_OPEN_TIMEOUT_S = 3.0
# Fresh hosts get a short auth-retry window for Databricks OAuth refreshes.
# Established hosts retry auth failures indefinitely to preserve sessions.
_MAX_CONSECUTIVE_AUTH_ERRORS = 3
# Consecutive connection-refused failures against a loopback server before the
# host exits (~5 minutes at the backoff cap). Refused on loopback means no
# process listens on the port — the local server is gone, not unreachable.
_LOOPBACK_REFUSED_FATAL_ATTEMPTS = 100

# Consecutive post-connect 401/403 rejections (~5 min at the backoff cap)
# before the retry loop escalates from "check your VPN" to a re-auth prompt.
# Operator-facing only — the host keeps retrying and never exits.
_AUTH_REJECT_ESCALATE_ATTEMPTS = 30

# Consecutive accepted-then-silent connections (upgrade completed, then the
# socket died without one inbound frame) before the reconnect loop treats the
# endpoint as unhealthy: it stops using the prompt "recycle" cadence and
# escalates once, loudly. A healthy tunnel sends a frame within seconds; an
# endpoint that accepts and never speaks is functionally down.
_SILENT_CONNECT_ESCALATE_ATTEMPTS = 10

# Capability discovery is advisory and must not delay the host channel forever.
_HOST_CAPABILITY_INIT_TIMEOUT_S = 15.0

# Host-environment variables a spawned runner is allowed to inherit.
# Deliberately an allowlist (not ``{**os.environ}``): the host runs as the
# user, so its environment holds the user's personal secrets (API keys,
# tokens). A runner has no need for those — agent credentials and config
# come from the agent spec, not the host owner's shell (spec
# self-containment). Anything an agent
# legitimately needs must flow through its spec's env config. Limited to
# process essentials (PATH/HOME/shell/locale/temp) and TLS trust stores so
# the runner's outbound HTTPS still works.
_RUNNER_ENV_ALLOWLIST: frozenset[str] = frozenset(
    {
        "PATH",
        "PYTHONPATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "TMPDIR",
        "TZ",
        "TERM",
        "TERMINFO",
        "TERMINFO_DIRS",
        "LANG",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        # Force UTF-8 I/O on Windows. Without this, Python on Windows defaults
        # to the system ANSI code page (e.g. cp1252), causing UnicodeEncodeError
        # when the host daemon / runner prints Unicode characters such as "✓" or
        # "↑" in connection status messages — which kills the tunnel in an
        # infinite reconnect loop. Safe to propagate: a non-secret interpreter
        # flag. No-op on POSIX where UTF-8 is the default.
        "PYTHONUTF8",
        # Environment descriptor baked into the sandbox host image
        # (deploy/docker/Dockerfile `host` target), never set on
        # laptops. Claude Code refuses --dangerously-skip-permissions
        # under root unless this devcontainer-convention flag is set,
        # and sandbox containers run as root — without it the
        # claude-sdk harness cannot start inside managed sandboxes.
        "IS_SANDBOX",
        # Databricks config selectors are not bearer secrets. They must
        # reach host-spawned runners so native harnesses resolve the same
        # profile/config file the host resolved (e.g. a spec-declared
        # executor.profile propagated into the daemon's env).
        "DATABRICKS_CONFIG_PROFILE",
        "DATABRICKS_CONFIG_FILE",
        # DATABRICKS_AUTH_STORAGE selects the token-storage backend ("secure"
        # OS keychain vs "plaintext" JSON cache) — also a non-secret selector.
        # Without it a runner falls back to the ~/.databrickscfg [__settings__]
        # auth_storage default and can resolve a DIFFERENT token store than the
        # host/daemon (which inherits it via the daemon env's DATABRICKS_ prefix
        # in cli.py). That mismatch makes the runner read an empty/stale store
        # and fail to mint a token — the runner tunnel is rejected with HTTP 401
        # even though the host authenticated fine.
        "DATABRICKS_AUTH_STORAGE",
        # Runtime config/data-dir selection. These are filesystem PATHS, not
        # secrets, so they're safe to propagate to the host owner's own
        # daemon/runner subprocesses. They MUST propagate so the whole local
        # chain (CLI → daemon → local server → runner) agrees:
        #   - OMNIGENT_CONFIG_HOME: where config.yaml / provider config live,
        #     so the runner resolves the same providers the CLI configured.
        #   - OMNIGENT_DATA_DIR: where the sqlite db + pidfile live, so the
        #     CLI doesn't read the local-server pidfile from one dir while the
        #     daemon writes it to another (that mismatch timed out discovery).
        # OMNIGENT_DATABASE_URI is intentionally NOT here — it may embed a
        # DB password, so it's propagated to the local daemon only (see
        # cli._ensure_host_daemon), never to a (possibly hosted) runner.
        "OMNIGENT_CONFIG_HOME",
        "OMNIGENT_DATA_DIR",
        # Auth provider selection. The env-unset default was flipped
        # to "accounts", so the whole CLI → daemon → local-server chain has
        # to agree on the mode. Without this, the daemon strips
        # OMNIGENT_AUTH_PROVIDER and the daemon-spawned local server
        # silently boots in accounts mode while the CLI thinks it's talking
        # to a header-mode server — every CLI request 401s (e.g. the
        # test_run_omnigent_resumption suite). Not a secret; safe to propagate to
        # any subprocess.
        "OMNIGENT_AUTH_PROVIDER",
        # Multi-user opt-in switch (create_auth_provider): OMNIGENT_AUTH_ENABLED
        # turns the env-unset header/local default into accounts (or oidc, when
        # OMNIGENT_OIDC_* is set); =0 opts back out. Must propagate down the
        # CLI → daemon → local-server chain or `omnigent run`/`connect` would
        # spawn the wrong auth mode while the operator set the switch on the CLI.
        # Not a secret.
        "OMNIGENT_AUTH_ENABLED",
        # Process logging controls. These are diagnostics knobs, not secrets.
        "OMNIGENT_LOG_LEVEL",
        "OMNIGENT_LOG_TO_STDERR",
        LOG_TTY_FD_ENV_VAR,
        # Debug-log sink config + creds (OMNI-4198). The runner uploads its OWN
        # process logs to the debug-logs table, so it needs these — including the
        # service-principal secret. That is the one deliberate exception to the
        # "no secrets" rule: it is the app's SP creds for log upload (not a user
        # secret), and the runner is a trusted child. Without them the runner's
        # sink never arms and runner logs never reach the table.
        "OMNIGENT_DEBUG_LOG_CLIENT_ID",
        "OMNIGENT_DEBUG_LOG_CLIENT_SECRET",
        "OMNIGENT_DEBUG_LOG_WORKSPACE_URL",
        "OMNIGENT_DEBUG_LOG_ENDPOINT",
        # Secret-store backend selector. The CLI's `configure harnesses` stores
        # pasted API keys via the file backend when this is set (headless /
        # locked-keyring hosts), writing `keychain:<name>` refs. The runner
        # RESOLVES those refs, so it must pick the SAME backend — otherwise it
        # falls back to the OS keyring and fails with "no stored secret named
        # …" for a key the CLI just saved to the file. Not a secret (a boolean
        # flag); safe to propagate.
        "OMNIGENT_DISABLE_KEYRING",
        # claude-sdk sandbox bypass flag. A diagnostic knob (not a
        # secret — a plain boolean) read inside the harness to decide
        # whether to wrap the brain CLI in sandbox-exec. Without it in
        # the allowlist the daemon→runner env strip drops it, so a bare
        # ``OMNIGENT_CLAUDE_SDK_NO_SANDBOX=1 omnigent run …`` had no
        # effect (the operator also had to set
        # ``OMNIGENT_RUNNER_ENV_PASSTHROUGH=OMNIGENT_CLAUDE_SDK_NO_SANDBOX``).
        # Safe to propagate: not a secret.
        "OMNIGENT_CLAUDE_SDK_NO_SANDBOX",
        # Native-Claude launcher plugin selector: the entry-point NAME of a
        # launcher registered in the ``omnigent.claude_launcher`` group (e.g.
        # ``isaac``). Read by omnigent.claude_launcher.resolve_claude_launch in
        # the managed-host runner (``_auto_create_claude_terminal``) to wrap the
        # Claude launch through a downstream binary (e.g. Databricks' isaac).
        # The daemon→runner env strip would otherwise drop it, leaving the
        # runner on the default launch. Safe to propagate: not a secret, just a
        # plugin name.
        "OMNIGENT_CLAUDE_LAUNCHER",
        # Testing knob: override the context window size for compaction
        # trigger threshold. Not a secret — a plain integer.
        "AP_CONTEXT_WINDOW_OVERRIDE",
        # Claude Code's Bedrock-mode switch: a non-secret boolean flag that
        # turns on AWS Bedrock / Bedrock-compatible gateway mode. The matching
        # credential (AWS_BEARER_TOKEN_BEDROCK) and endpoint
        # (ANTHROPIC_BEDROCK_BASE_URL) are NOT here: they are credentials and
        # live in HARNESS_CREDENTIAL_ENV_VARS, mirroring ANTHROPIC_API_KEY /
        # ANTHROPIC_BASE_URL. Safe to propagate: not a secret.
        "CLAUDE_CODE_USE_BEDROCK",
        # Claude Code's Bedrock-auth-skip switch: a non-secret boolean flag
        # that disables AWS SigV4 auth so Claude Code can talk to a LiteLLM
        # proxy fronting Bedrock. Without it the runner attempts native AWS
        # auth, which fails for non-AWS proxies. Same rationale as
        # CLAUDE_CODE_USE_BEDROCK above. Safe to propagate: not a secret.
        "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
        # Non-secret Claude Code flags the native-claude provider path reads from
        # os.environ. If stripped, the runner re-adds CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1,
        # which turns off MCP tool search and loads every tool schema eagerly.
        "CLAUDE_CODE_USE_GATEWAY",
        "ENABLE_TOOL_SEARCH",
        # Kubernetes config path. A filesystem path (typically
        # ``~/.kube/config``), not a bearer secret — the file *contains*
        # cluster certs/tokens but the env var is just a path string,
        # analogous to ``HOME``. Without it, ``kubectl`` / helm / k9s
        # inside the agent's shell fall back to the default path which may
        # not match what the host owner configured (e.g. a non-standard
        # kubeconfig location or a colon-separated multi-file list).
        "KUBECONFIG",
        # ssh-agent socket path. Same class as KUBECONFIG above: a path to a
        # unix socket, not a bearer secret. Without it every runner-spawned
        # context (sys_os_shell, terminal panes, coding sub-agents) loses
        # ssh-agent auth, so git-over-SSH and SSH-cert-authenticated tooling
        # fail with "dial unix: missing address".
        "SSH_AUTH_SOCK",
        # Telemetry master opt-in. MUST propagate, or the daemon-spawned runner
        # (and the harness it spawns) never see OMNIGENT_TELEMETRY_ENABLED, so
        # telemetry.init() no-ops there and omni-runner / omni-harness export
        # nothing — inheriting OTEL_* alone is no longer enough now that
        # telemetry is opt-in. Not a secret (a boolean). The OMNIGENT_OTEL_*
        # knobs (capture-content, FastAPI toggle) ride the prefix allowlist below.
        "OMNIGENT_TELEMETRY_ENABLED",
        # Opaque request-routing headers (dev/test): a JSON header map folded by
        # cli_auth.databricks_request_headers into every client→server connection
        # so a request pins to a specific server instance/replica. Must reach the
        # spawned runner so its tunnel + server callbacks route to the SAME
        # instance the host registered on — otherwise the host lands on the
        # selected instance while its runners fall back to the default one.
        # Routing config, not a secret; unset in prod. Allowlisting it forwards it
        # host→runner intrinsically, so the setter need not also list it in
        # OMNIGENT_RUNNER_ENV_PASSTHROUGH.
        "OMNIGENT_DATABRICKS_EXTRA_HEADERS",
        # The operator's env-forwarding control var itself. Without it here, the
        # var is stripped before it reaches the daemon in --server mode (the
        # remote daemon prefixes are DATABRICKS_ + LC_/MLFLOW_/OTEL_/OMNIGENT_OTEL_,
        # not plain OMNIGENT_), so _build_runner_env never sees the names it lists
        # and the whole passthrough is a no-op remotely. It carries only env var
        # NAMES, not secrets, so allowlisting it leaks nothing on its own.
        # (Literal, not RUNNER_ENV_PASSTHROUGH_ENV_VAR, which is defined below.)
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH",
    }
    # Windows system / profile constants (SYSTEMROOT is mandatory for Winsock,
    # USERPROFILE for Path.home(), etc.); a no-op on POSIX. See _platform.
    | set(WINDOWS_ENV_PASSTHROUGH)
)
# Allowed by prefix: locale family (``LC_*``), MLflow, and OpenTelemetry config —
# both the standard ``OTEL_*`` vars and Omnigent's ``OMNIGENT_OTEL_*`` knobs
# (capture-content, FastAPI toggle) so they reach the runner/harness too.
_RUNNER_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_", "MLFLOW_", "OTEL_", "OMNIGENT_OTEL_")

# Harness credential / endpoint env vars forwarded host→runner when
# present. These are the names the harnesses themselves resolve —
# ANTHROPIC_* for claude-sdk / pi (claude-code also honors
# ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL for gateways, and
# ANTHROPIC_MODEL to pin a gateway-served model (must travel with the
# key/endpoint, else native Claude launches with a default the gateway
# rejects), AWS_BEARER_TOKEN_BEDROCK + ANTHROPIC_BEDROCK_BASE_URL for Bedrock mode,
# and CLAUDE_CODE_OAUTH_TOKEN for `claude setup-token` subscription auth),
# OPENAI_* for codex / openai-agents (CODEX_ACCESS_TOKEN is the codex
# CLI's headless ChatGPT-workspace credential, minted in the ChatGPT
# admin console — Business/Enterprise plans), GEMINI_API_KEY for the
# gemini family. GIT_TOKEN / GIT_USERNAME feed the sandbox host
# image's git credential helper (deploy/docker/Dockerfile `host`
# target) so the agent's own fetch/push against a private repository
# authenticates, not just the launch-time clone. Unlike the rest of
# the host's environment, these are credentials the host owner sets
# PRECISELY so their runners can use them (on a laptop: exported keys;
# on a server-managed sandbox: the deployment's injected provider
# secrets) — forwarding them is the intent, not a leak. Vars absent
# from the host env are simply not set.
_BASE_HARNESS_CREDENTIAL_ENV_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "ANTHROPIC_MODEL",
        "ANTHROPIC_BEDROCK_BASE_URL",
        "AWS_BEARER_TOKEN_BEDROCK",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CODEX_ACCESS_TOKEN",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "GEMINI_API_KEY",
        "GIT_TOKEN",
        "GIT_USERNAME",
    }
)
HARNESS_CREDENTIAL_ENV_VARS: frozenset[str] = frozenset(
    name
    for canonical in _BASE_HARNESS_CREDENTIAL_ENV_VARS
    for name in env_names_with_omnigent_prefix(canonical)
)

# Comma-separated EXTRA env var names to forward host→runner, beyond
# HARNESS_CREDENTIAL_ENV_VARS — for provider wiring the defaults don't
# cover (custom gateway vars, `providers:`-config `env:` refs, exotic
# SDK knobs). Operator-controlled: the host owner names exactly what
# their runners need; everything unnamed stays behind the allowlist.
RUNNER_ENV_PASSTHROUGH_ENV_VAR: str = "OMNIGENT_RUNNER_ENV_PASSTHROUGH"

# HTTP statuses on the WebSocket upgrade that are worth retrying. Everything
# else in the 4xx range is a permanent client error (auth, authorization,
# wrong/old server) where reconnecting can never succeed — those fail loud.
# 408 (Request Timeout) and 429 (Too Many Requests) are transient by HTTP
# semantics, so they stay in the reconnect path.
_RETRYABLE_UPGRADE_STATUSES: frozenset[int] = frozenset({408, 429})

# Consecutive login-page redirects tolerated on a host that has NEVER
# completed a WS upgrade in this process. A single redirect can be a server
# mid-restart (the Apps OAuth proxy answers before the app is ready),
# so a couple of retries rule out a blip; past that, a host with
# no prior successful upgrade is almost certainly unauthenticated and must
# fail loud instead of looping silently forever. A host that
# HAS connected keeps retrying indefinitely, so a deploy restart never
# kills a live host with running sessions.
_LOGIN_REDIRECT_FATAL_ATTEMPTS = 3


class HostConnectError(Exception):
    """A non-retryable failure while opening the host tunnel.

    Raised when connection setup or the server reports a failure that
    reconnecting cannot fix — the Databricks Apps proxy bounced the connection to
    a login page (wrong/absent workspace credentials), the server
    returned a permanent ``4xx`` (unauthenticated, unauthorized, or a
    build that predates the host API), or a loopback server refused a
    sustained streak of connects (nothing listens — the local server is
    gone). The reconnect loop re-raises this instead of backing off, so
    ``omnigent host`` exits with an actionable message rather than
    looping silently forever.

    The message is the full, user-facing explanation including the
    suggested fix; it is printed verbatim by :func:`run_host_process`.
    """


def _build_runner_env(
    base_env: Mapping[str, str],
    *,
    server_url: str,
    runner_id: str,
    binding_token: str,
    workspace: str,
    parent_pid: int,
    initial_auth_token: str | None = None,
    host_id: str | None = None,
    harness: str | None = None,
) -> dict[str, str]:
    """
    Build the environment for a spawned runner subprocess.

    Inherits only the allowlisted subset of *base_env* (see
    :data:`_RUNNER_ENV_ALLOWLIST`) so the host owner's secrets don't leak
    into runners, then layers on the runner wiring vars.

    Harness credentials are the deliberate exception to the allowlist:
    the names in :data:`HARNESS_CREDENTIAL_ENV_VARS` (plus any extras
    the host owner lists in :data:`RUNNER_ENV_PASSTHROUGH_ENV_VAR`)
    forward when present, so runners can authenticate to LLM providers
    with the credentials the host owner provisioned for them.

    :param base_env: Host process environment to filter, e.g.
        ``os.environ``.
    :param server_url: Omnigent server URL the runner connects back to, e.g.
        ``"https://example.databricks.com"``.
    :param runner_id: Token-bound runner id, e.g. ``"runner_abc123"``.
    :param binding_token: One-time tunnel binding token.
    :param workspace: Absolute runner cwd on the host, e.g.
        ``"/Users/alice/proj"``.
    :param parent_pid: Host process pid, for orphan detection.
    :param initial_auth_token: Current host bearer for the runner's initial
        server connection. The runner consumes and removes it before spawning
        any children. ``None`` leaves the legacy auth path unchanged.
    :param harness: Canonical harness of the launching session, e.g.
        ``"claude-native"``; lets the runner start harness-specific prewarms
        at boot. ``None`` (unknown / older server) omits the stamp.
    :returns: The runner subprocess environment.
    """
    extra_names = {
        name.strip()
        for name in base_env.get(RUNNER_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
        if name.strip()
    }
    # Forward env vars that the providers config references via
    # ``api_key_ref: env:VAR`` or ``api_key: $VAR``. Without this, a user
    # who configures a gateway provider with a custom env var (e.g.
    # ``api_key_ref: env:MY_TOKEN``) would need to manually add it to
    # OMNIGENT_RUNNER_ENV_PASSTHROUGH — their credential resolves fine in
    # the CLI/daemon but silently drops before reaching the runner subprocess.
    from omnigent.errors import OmnigentError as _OmnigentError

    try:
        from omnigent.onboarding.provider_config import (
            load_config,
            provider_credential_env_vars,
        )

        config_env_vars = provider_credential_env_vars(load_config())
    except (OSError, _OmnigentError):
        config_env_vars = frozenset()
    forwarded = HARNESS_CREDENTIAL_ENV_VARS | extra_names | config_env_vars
    env = {
        key: value
        for key, value in base_env.items()
        if key in _RUNNER_ENV_ALLOWLIST
        or key.startswith(_RUNNER_ENV_ALLOWLIST_PREFIXES)
        or key in forwarded
    }
    env["RUNNER_SERVER_URL"] = server_url
    env[RUNNER_ID_ENV_VAR] = runner_id
    env[RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR] = binding_token
    env[RUNNER_DELEGATED_AUTH_ENV_VAR] = "1"
    if initial_auth_token:
        env[RUNNER_INITIAL_AUTH_TOKEN_ENV_VAR] = initial_auth_token
    env[RUNNER_WORKSPACE_ENV_VAR] = workspace
    env[RUNNER_PARENT_PID_ENV_VAR] = str(parent_pid)
    # Bound glibc allocator RSS in the runner (no-op off Linux). Injected
    # explicitly because the allowlist above would otherwise drop an inherited
    # MALLOC_ARENA_MAX. setdefault so an operator override still wins.
    for key, value in _proc.malloc_tuning_env().items():
        env.setdefault(key, value)
    if host_id:
        # Tell the runner its host so its tunnel co-locates with the host's on
        # one replica (turn dispatch / terminal-attach for its sessions reach it
        # there). The runner forwards this to databricks_request_headers, which
        # emits the routing header only on a host-sharded deployment.
        env[RUNNER_SLICE_KEY_ENV_VAR] = host_id
    if harness:
        env[RUNNER_LAUNCH_HARNESS_ENV_VAR] = harness
    return env


def _paginate_list_dir(
    *,
    entries: list[HostListDirEntry],
    request_id: str,
    limit: int,
    after: str | None,
    before: str | None,
) -> HostListDirResultFrame:
    """
    Slice a sorted directory listing into a page.

    Cursors (``after`` / ``before``) reference an entry's ``path``.
    Forward pagination (``after``) returns up to ``limit`` entries
    strictly after the cursor; backward pagination (``before``)
    returns up to ``limit`` entries strictly before. Empty cursors
    return the first page. ``has_more`` is set when more entries
    remain in the pagination direction: forward of the page for
    ``after``, before the page for ``before``.

    :param entries: Full sorted list of directory entries.
    :param request_id: Request id to echo back on the result frame.
    :param limit: Max entries per page, e.g. ``20``. Capped at
        1000 by the route layer.
    :param after: Cursor for forward pagination. ``None`` → start
        at the first entry.
    :param before: Cursor for backward pagination. ``None`` → no
        upper bound.
    :returns: A list_dir result frame with the requested page.
    """
    # Identify the cut points by entry path so cursors survive
    # concurrent directory mutations between calls.
    start = 0
    end = len(entries)
    if after is not None:
        for idx, entry in enumerate(entries):
            if entry.path == after:
                start = idx + 1
                break
    if before is not None:
        for idx, entry in enumerate(entries):
            if entry.path == before:
                end = idx
                break
    if before is not None:
        page_start = max(start, end - limit)
        page = entries[page_start:end]
        has_more = page_start > start
    else:
        page = entries[start:end][:limit]
        has_more = end - start > limit
    return HostListDirResultFrame(
        request_id=request_id,
        status="ok",
        entries=page,
        has_more=has_more,
    )


@dataclass(frozen=True)
class ModelOptionsResult:
    """One resolved model listing: picker rows + the settable-but-unlisted ids.

    :param models: Verbatim catalog rows (id/model/displayName/isDefault…).
    :param routable_models: Ids a launch can pin that the picker does not
        list (older generations the endpoint still serves).
    """

    models: list[dict[str, object]]
    routable_models: list[str]


@dataclass
class _RunnerHandle:
    """A spawned runner subprocess and where its output lands.

    :param proc: The runner subprocess handle.
    :param log_path: File capturing the runner's stdout/stderr, e.g.
        ``Path("~/.omnigent/logs/runner/runner-ab12.log")``.
        Read back for diagnostics when the runner dies before
        connecting its tunnel.
    :param session_id: The session this runner was launched for, e.g.
        ``"conv_abc123"``. Lets a relaunch tear down the session's
        previous runner (the server rotates the binding token per
        attempt, so the runner id alone can't identify a predecessor).
        ``None`` for frames from servers that predate ``session_id``.
    """

    proc: subprocess.Popen[bytes] | ZygoteRunnerProc
    log_path: Path
    session_id: str | None = None


class HostRetryableConnectionError(Exception):
    """Server-reported channel failure that should use reconnect backoff."""


class HostProcess:
    """Manages the host daemon lifecycle.

    Connects to the server, handles launch/stop commands, and
    tracks spawned runner subprocesses.

    :param identity: Host identity (id + name) from ``config.yaml``.
    :param server_url: Omnigent server URL, e.g.
        ``"https://omnigent-app.databricksapps.com"``.
    """

    def __init__(
        self,
        identity: HostIdentity,
        server_url: str,
        lifecycle_lock: DaemonLifecycleLock | None = None,
    ) -> None:
        """Initialize the host process.

        :param identity: Host identity from ``config.yaml``.
        :param server_url: Server URL to connect to.
        :param lifecycle_lock: Optional guard binding this daemon's lifetime
            to its registry record. When present, the daemon holds the lock
            and self-terminates once the record is deleted or reassigned.
        """
        self._identity = identity
        self._server_url = server_url.rstrip("/")
        self._runners: dict[str, _RunnerHandle] = {}
        # Retain the host's refreshable auth context after the first tunnel
        # handshake so runner launches can reuse its warm bearer. Failed or
        # unavailable resolution is not latched, allowing a later reconnect
        # to retry credential discovery.
        self._auth_token_factory: Callable[[], str | None] | None = None
        self._auth_token_factory_resolved = False
        # This host's owning user, resolved once after the first accepted tunnel
        # upgrade (GET /v1/me). Injected into every runner it spawns and published
        # to OMNIGENT_USER_ID so host/runner debug-log rows carry it. None until
        # resolved, or on a single-user server / managed host where it is absent.
        self._owner_user_id: str | None = None
        # The fronting Databricks workspace id for this server, resolved once from
        # the server URL (its ?o= selector or the stored login record) — the same
        # resolution the display URL uses, no extra network. Published to
        # DATABRICKS_WORKSPACE_ID for the host's own debug-log rows and injected
        # into every runner this host spawns; None on a non-Databricks server.
        self._origin_workspace_id: str | None = None
        try:
            from omnigent.server_url import ServerUrl

            self._origin_workspace_id = ServerUrl.from_api_base(self._server_url).org_id
        except Exception:  # noqa: BLE001 — attribution is best-effort
            self._origin_workspace_id = None
        if self._origin_workspace_id:
            os.environ[ORIGIN_WORKSPACE_ID_ENV_VAR] = self._origin_workspace_id
        # Set on the first accepted WS upgrade. Distinguishes a host that
        # never authenticated (login redirects / 401 / 403 turn fatal) from a
        # live host hit by a transient failure — a server restart or a dropped
        # VPN whose proxy answers 401/403 before the request reaches the
        # server — where the same failures retry forever instead of killing a
        # host with running sessions.
        self._ever_connected = False
        # Capability discovery belongs to daemon initialization, not the
        # connection handshake. Reconnects reuse this snapshot while the live
        # refresh task keeps it current.
        self._configured_harnesses: dict[str, HarnessAvailability] | None = None
        self._gateway_inference: dict[str, bool] | None = None
        self._capabilities_initialized = False
        # Consecutive login-page redirects; reset by a successful upgrade.
        self._login_redirect_streak = 0
        # Reset by a successful upgrade or non-auth error; bounds fresh-host refresh retries.
        self._auth_retry_streak = 0
        # Consecutive connection-refused connect failures; reset by an accepted
        # upgrade or any non-refused error. Fatal past a bounded streak only
        # when the server URL is loopback (the local server is gone).
        self._refused_streak = 0
        # Consecutive connections that were accepted but died without a single
        # inbound frame; reset by any received frame or a rejected upgrade.
        # Past a bound the reconnect loop escalates instead of fast-recycling.
        self._silent_connect_streak = 0
        # Per-connection markers feeding the silent-connect streak.
        self._conn_upgrade_accepted = False
        self._conn_frame_received = False
        # Live tunnel connection, set by _serve_frames for the watcher
        # tasks (which outlive any single connection) to report on.
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        # runner_id → composed error for exits that could not be sent
        # (tunnel down at the time). Flushed after the next hello.
        self._unreported_exits: dict[str, str] = {}
        # Strong refs to per-runner watcher tasks; asyncio only keeps
        # weak refs, so an unreferenced task can be GC'd mid-flight.
        self._watcher_tasks: set[asyncio.Task[None]] = set()
        # Strong refs to detached superseded-runner stops (see
        # _spawn_superseded_stop), for the same GC reason.
        self._supersede_stop_tasks: set[asyncio.Task[None]] = set()
        # Strong ref to the orphan-reaper task (see :meth:`_orphan_reaper_loop`).
        self._reaper_task: asyncio.Task[None] | None = None
        # Number of host-owned ``subprocess`` operations (e.g. the git worktree
        # commands in :mod:`omnigent.host.git_worktree`) currently in flight.
        # The orphan reaper skips its sweep while this is >0 so it never
        # ``wait()``s a child that ``subprocess.run`` is about to reap itself —
        # stealing it would corrupt that command's returncode to 0 (#1782).
        # Mutated only via :meth:`_host_subprocess_op`; safe as a plain int
        # because both the mutation and the reaper run on the event loop.
        self._owned_subprocess_ops = 0
        # Copy-on-write runner forkserver, on by default; set
        # OMNIGENT_RUNNER_ZYGOTE=0 (or false/no/off) to opt out onto the direct
        # Popen path. POSIX-only (needs os.fork + AF_UNIX fd-passing); the host
        # daemon runs on the user's own machine, most often macOS, so gating on
        # IS_POSIX rather than IS_LINUX lets those users share the ~120MB import
        # floor too. Instantiated cheaply here (no process yet — start() spawns
        # it, idempotent under its own lock). On any failure ``_zygote_disabled``
        # latches so future launches take the direct Popen path, while
        # ``_zygote`` is retained so a still-running zygote is reaped on daemon
        # shutdown. The zygote is an optimization, never required.
        _zygote_optout = os.environ.get(ZYGOTE_ENABLED_ENV_VAR)
        self._zygote_enabled = IS_POSIX and not (
            _zygote_optout is not None and not env_truthy(_zygote_optout)
        )
        self._zygote: ZygoteManager | None = ZygoteManager() if self._zygote_enabled else None
        self._zygote_disabled = False
        # Warms the zygote at daemon start so the first launch doesn't pay
        # its one-time import; see run().
        self._zygote_prestart_task: asyncio.Task[ZygoteManager | None] | None = None
        # Inbound frames are handled on their own tasks (see
        # _start_frame_task) so one slow handler — a model-options CLI exec,
        # an npm install — can't head-of-line block a launch or a stat behind
        # it. Launch/stop keep their arrival order relative to each other via
        # this lock: a session DELETE racing a slow create must not have its
        # stop overtake the launch it targets.
        self._runner_lifecycle_lock = asyncio.Lock()
        # Strong refs to in-flight frame tasks (create_task results are
        # otherwise GC-able); each discards itself on completion.
        self._frame_tasks: set[asyncio.Task[None]] = set()
        # Background watcher that force-drops a stale tunnel on wake from system
        # suspend (laptop sleep) so the reconnect loop reattaches at once
        # instead of waiting out the ~90s keepalive timeout. See run() /
        # _on_resume_from_suspend.
        self._suspend_task: asyncio.Task[None] | None = None
        # Set by _on_resume_from_suspend when it aborts a live tunnel after a
        # detected resume; read+cleared in run()'s reconnect handler to force a
        # prompt reconnect (skip the backoff).
        self._woke_from_suspend = False
        # Lifecycle guard: hold the target's flock and watch its registry
        # record. When the record is deleted/reassigned the monitor sets
        # _lifecycle_lost and aborts the live tunnel so run() breaks out of its
        # serve and tears down cleanly instead of lingering as a stale daemon.
        self._lifecycle_lock = lifecycle_lock
        self._lifecycle_task: asyncio.Task[None] | None = None
        self._lifecycle_lost = asyncio.Event()

    def _tracked_runner_pids(self) -> set[int]:
        """PIDs of runners this host spawned and still tracks directly.

        The orphan reaper must NOT ``wait()`` these: their exit status is
        owned by their :class:`subprocess.Popen` (read via ``poll()`` /
        ``.returncode`` for the ``host.runner_exited`` report). Reaping one
        out from under ``Popen`` makes ``poll()`` either spin forever
        (``while poll() is None`` in ``_watch_runner``) or report a bogus
        exit 0 for a crash — so the reaper skips these and lets
        ``_watch_runner`` / ``_handle_stop`` own them.

        The runner zygote is included for the same reason: it is a direct
        ``Popen`` child of the daemon whose status ``ZygoteManager._proc``
        owns. Reaping it as an "orphan" on an unexpected crash would consume
        its status out from under the manager, confusing ``is_running()`` /
        ``stop()``.

        :returns: Set of live tracked pids (runners + the zygote).
        """
        pids = {h.proc.pid for h in self._runners.values()}
        zygote_pid = self._zygote.pid if self._zygote is not None else None
        if zygote_pid is not None:
            pids.add(zygote_pid)
        return pids

    async def _orphan_reaper_loop(self) -> None:
        """Reap orphaned descendant processes reparented to this host.

        A harness spawns its tool subprocesses detached
        (``start_new_session=True`` — ``omnigent.inner._proc.spawn_kwargs``),
        so when the runner that owns them dies, those grandchildren
        (``node`` / ``npm`` / ``chromium`` / ``tmux`` / ``python``) are
        orphaned and reparented to this host (it is PID 1 in a container, or
        a child subreaper otherwise — see :func:`_install_child_subreaper`).
        Nothing ``wait()``s them, so each becomes a permanent ``<defunct>``
        zombie; a blocked overnight run accumulated ~900 and OOM'd the box
        (#1782).

        This loop periodically reaps any ready-to-reap child that is NOT a
        Popen-tracked runner (:meth:`_tracked_runner_pids`), draining zombies
        without disturbing runner exit reporting. Non-Linux (no reparenting)
        and the "no orphans yet" case both make this a cheap no-op sweep.

        :returns: None. Runs until cancelled on shutdown.
        """
        while True:
            try:
                await asyncio.sleep(_ORPHAN_REAP_INTERVAL_S)
                self._reap_orphans_once()
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 — a reaper must never die on a stray error
                _logger.debug("orphan reaper sweep failed", exc_info=True)

    def _reap_orphans_once(self) -> int:
        """Reap ready orphaned children without corrupting runner exits.

        The hazard: the host reads each runner's exit status through its
        :class:`subprocess.Popen` (``poll()`` / ``.returncode``) for the
        ``host.runner_exited`` report. A blind ``waitpid(-1)`` reaper that
        consumes a just-crashed tracked runner makes ``Popen.poll()`` report
        a bogus exit 0 (verified) — the crash cause is lost. So the reaper
        must drain orphans while leaving tracked runners' status intact.

        Two implementations, same guarantee:

        * **Linux/POSIX with** ``os.waitid`` — *peek* at the next reapable
          child with ``WNOWAIT`` (does not consume). Reap it only if it is
          not a tracked runner; if it is, stop the sweep and let the runner's
          own Popen reaper (``_watch_runner``) consume it. Cleanest: a tracked
          runner's status is never touched.
        * **Platforms without** ``os.waitid`` **(e.g. macOS)** — ``waitpid``
          has no peek, so reap with ``WNOHANG`` and, if the reaped pid is a
          tracked runner, re-inject its exit status onto the ``Popen`` so
          ``_watch_runner`` still reports the true code. Safe because
          ``_reap_orphans_once`` runs to completion on the event loop without
          awaiting, so it cannot interleave with ``_watch_runner`` /
          ``_handle_stop``.

        This runs only when the host is PID 1 (container) or a child
        subreaper (:func:`_install_child_subreaper`); otherwise no orphan
        ever reparents here and every sweep is a no-op.

        :returns: Count of orphan (non-runner) processes reaped this sweep.
        """
        if self._owned_subprocess_ops > 0:
            # A host-owned subprocess (e.g. a git worktree command) is running
            # in a worker thread. Its child is a DIRECT child of this process
            # but NOT a tracked runner, so it is indistinguishable from an
            # orphan to the reaper — reaping it would steal it from
            # ``subprocess.run``'s own ``wait()`` and corrupt that command's
            # returncode to 0 (CPython swallows the ECHILD and reports 0).
            # Skip this sweep; a later one drains any real orphans once the op
            # finishes. A worktree op can hold this off for up to
            # ``_GIT_TIMEOUT_S`` (120s) per git command, so real orphans can
            # linger that long in the rare case a runner dies mid-worktree-op —
            # acceptable, since the leak this guards against accrues over hours,
            # not a two-minute worst case.
            return 0
        if not hasattr(os, "WNOHANG"):
            # Windows: no child reparenting to a subreaper and no ``WNOHANG`` /
            # ``waitpid(-1, ...)`` — nothing to reap and the calls would raise.
            return 0
        if hasattr(os, "waitid") and hasattr(os, "P_ALL"):
            return self._reap_orphans_waitid()
        return self._reap_orphans_waitpid()

    @contextlib.contextmanager
    def _host_subprocess_op(self) -> Iterator[None]:
        """Mark a host-owned ``subprocess`` operation as in flight.

        Wrap any host-owned :mod:`subprocess` call (or the ``to_thread`` that
        runs it) in this so the orphan reaper pauses and cannot ``wait()`` the
        child out from under ``subprocess``'s own reaping — see
        :meth:`_reap_orphans_once` for why that would corrupt the command's
        exit code (#1782).

        Increment/decrement run on the event loop (the reaper does too), so a
        plain counter needs no lock. Re-entrant and exception-safe: the
        decrement is in a ``finally``.

        :returns: A context manager; the body runs with the reaper paused.
        """
        self._owned_subprocess_ops += 1
        try:
            yield
        finally:
            self._owned_subprocess_ops -= 1

    def _reap_orphans_waitid(self) -> int:
        """Peek-and-reap using ``os.waitid(WNOWAIT)`` (Linux/POSIX).

        :returns: Count of orphan processes reaped.
        """
        reaped = 0
        tracked = self._tracked_runner_pids()
        waitid = cast(
            "Callable[[object, int, int], _WaitidInfo | None]",
            vars(os)["waitid"],
        )
        p_all = vars(os)["P_ALL"]
        while True:
            try:
                info = waitid(p_all, 0, os.WEXITED | os.WNOHANG | os.WNOWAIT)
            except (ChildProcessError, OSError):
                break
            if info is None:
                break  # children exist but none ready to reap
            pid = info.si_pid
            if pid in tracked:
                # Leave it for _watch_runner's Popen to reap+report. Break, not
                # continue: WNOWAIT keeps returning the same head pid, so
                # continuing would spin. The runner is reaped within ~0.5s and
                # the next sweep proceeds past it.
                break
            try:
                os.waitpid(pid, 0)  # consume the orphan
                reaped += 1
            except ChildProcessError:
                break
        if reaped:
            _logger.debug("orphan reaper reaped %d process(es)", reaped)
        return reaped

    def _reap_orphans_waitpid(self) -> int:
        """Reap with ``waitpid(WNOHANG)``, re-injecting tracked-runner status.

        Fallback for platforms without ``os.waitid`` (no peek). See
        :meth:`_reap_orphans_once` for why re-injection is race-free.

        :returns: Count of orphan (non-runner) processes reaped.
        """
        reaped = 0
        while True:
            try:
                pid, status = os.waitpid(-1, os.WNOHANG)
            except (ChildProcessError, OSError):
                break
            if pid == 0:
                break  # children exist but none ready
            handle = self._runner_handle_for_pid(pid)
            if handle is not None:
                # A tracked runner — do NOT count it as an orphan. Re-inject
                # the status so its Popen (and thus _watch_runner) reports the
                # true exit code instead of ECHILD → bogus 0.
                if handle.proc.returncode is None:
                    handle.proc.returncode = os.waitstatus_to_exitcode(status)
                continue
            reaped += 1
        if reaped:
            _logger.debug("orphan reaper reaped %d process(es)", reaped)
        return reaped

    def _runner_handle_for_pid(self, pid: int) -> _RunnerHandle | None:
        """Return the tracked runner handle owning *pid*, or ``None``.

        :param pid: An OS process id observed by the reaper.
        :returns: The matching :class:`_RunnerHandle`, or ``None`` if *pid*
            is not a tracked runner (i.e. an orphan to reap).
        """
        for handle in self._runners.values():
            if handle.proc.pid == pid:
                return handle
        return None

    def _alive_runner_ids(self) -> list[str]:
        """Return IDs of runners that are still alive.

        Cleans up dead entries as a side effect.

        :returns: List of alive runner ID strings.
        """
        dead = [rid for rid, handle in self._runners.items() if handle.proc.poll() is not None]
        for rid in dead:
            self._runners.pop(rid)
        return list(self._runners.keys())

    def _tunnel_url(self) -> str:
        """Build the WebSocket tunnel URL.

        :returns: Full WS URL, e.g.
            ``"wss://server/v1/hosts/host_abc/tunnel"``.
        """
        base = self._server_url
        scheme = "wss" if base.startswith("https") else "ws"
        host_part = base.split("://", 1)[1] if "://" in base else base
        return f"{scheme}://{host_part}/v1/hosts/{self._identity.host_id}/tunnel"

    def _login_hint_url(self) -> str:
        """The server URL to show in ``omnigent login`` remedy hints.

        The display form (the workspace ``/omnigent`` URL with ``?o=``
        when known) rather than the internal API mount — it reads right
        and round-trips through ``omnigent login`` to the same server.

        :returns: The display URL, e.g.
            ``"https://ws.databricks.com/omnigent?o=123"``.
        """
        from omnigent.server_url import display_server_url

        return display_server_url(self._server_url)

    def _credentials_fix_hint(self) -> str:
        """Build the remedy for a credential failure.

        Shared by the login-redirect and HTTP 401 messages.

        :returns: An actionable remedy sentence naming the exact
            command, e.g. ``"Run `omnigent login <url>` ..."``.
        """
        return (
            f"Run `{cli_invocation()} login {self._login_hint_url()}` to authenticate (it "
            "detects Databricks-fronted servers and logs in to the right "
            "workspace), or check your ambient Databricks credentials."
        )

    def _login_fix_hint(self) -> str:
        """Suggest ``omnigent login`` as a remedy for an auth rejection.

        The host tunnel's bearer is resolved from a stored ``omnigent
        login`` record first, then ambient Databricks credentials (see
        :func:`omnigent.runner._entry._make_auth_token_factory`). When
        the server runs Omnigent accounts or OIDC auth, a Databricks
        workspace token can authenticate at the proxy yet still be rejected
        by the server itself — so the actionable fix is to log in to the
        server directly, which stores the session token the tunnel needs.

        :returns: A one-sentence remedy naming the exact command, e.g.
            ``"If this server uses Omnigent accounts or OIDC login, run
            `omnigent login http://localhost:6767` to authenticate."``.
        """
        return (
            "If this server uses Omnigent accounts or OIDC login, run "
            f"`{cli_invocation()} login {self._login_hint_url()}` to authenticate."
        )

    def _fatal_upgrade_error(self, exc: InvalidURI | InvalidStatus) -> HostConnectError | None:
        """Classify a WebSocket-upgrade failure as fatal, or return ``None``.

        Distinguishes permanent failures (auth / authorization / wrong or
        outdated server) from transient ones (server bounce, network blip)
        so the reconnect loop only backs off on the latter.

        Login-page redirects (``InvalidURI``) are ambiguous: they mean
        missing/wrong credentials, but also occur transiently while the
        server restarts behind the Apps OAuth proxy. They become fatal
        only on a host that has never completed an upgrade in this
        process, after :data:`_LOGIN_REDIRECT_FATAL_ATTEMPTS` consecutive
        occurrences; an already-connected host retries them forever.

        :param exc: The upgrade-time exception raised while opening the
            tunnel — either an :class:`~websockets.exceptions.InvalidURI`
            (redirect to a non-ws scheme) or an
            :class:`~websockets.exceptions.InvalidStatus` carrying e.g. a
            ``403`` upgrade response.
        :returns: A :class:`HostConnectError` with a user-facing message
            when *exc* is non-retryable, or ``None`` when the caller
            should treat *exc* as transient and reconnect.
        """
        if isinstance(exc, InvalidURI):
            # websockets followed a redirect whose Location wasn't ws/wss —
            # the Apps OAuth proxy bounced the upgrade to a login page. This
            # also happens transiently during server restarts (the proxy
            # redirects before the app is ready), so a host that has already
            # connected retries forever, while a host that never
            # authenticated gets a few retries to rule out a blip and then
            # fails loud instead of looping silently while the
            # only diagnostics land in the log file.
            self._login_redirect_streak += 1
            cause = (
                "Authentication failed: the server redirected the host "
                "tunnel to a login page instead of accepting it, so no "
                "session was established."
            )
            if (
                not self._ever_connected
                and self._login_redirect_streak >= _LOGIN_REDIRECT_FATAL_ATTEMPTS
            ):
                return HostConnectError(
                    f"{cause} The redirect persisted across "
                    f"{self._login_redirect_streak} attempts. "
                    + self._credentials_fix_hint()
                    + " (If the server is mid-restart, wait a minute and retry.)"
                )
            _logger.warning("%s %s", cause, self._credentials_fix_hint())
            if self._login_redirect_streak == 1:
                # The warning above lands in the CLI log file, not the
                # terminal — print once per redirect streak so a foreground
                # `omnigent host` shows the auth problem and its fix instead
                # of sitting silent while it retries.
                print(
                    f"⚠ {cause} Retrying — this also happens briefly while "
                    f"the server restarts. {self._credentials_fix_hint()}",
                    file=sys.stderr,
                    flush=True,
                )
            return None
        return self._classify_http_status(exc.response.status_code)

    def _classify_http_status(self, status: int) -> HostConnectError | None:
        """Map a rejected-upgrade HTTP status to a fatal error, or ``None``.

        :param status: HTTP status on the failed WS upgrade response, e.g.
            ``403``.
        :returns: A :class:`HostConnectError` for a permanent 4xx, or
            ``None`` for a transient status (retryable 4xx in
            :data:`_RETRYABLE_UPGRADE_STATUSES`, or any non-4xx such as a
            5xx server bounce) that the reconnect loop should retry.
        """
        if status in _RETRYABLE_UPGRADE_STATUSES or not (400 <= status < 500):
            return None
        if status in (401, 403):
            # Fresh hosts can race OAuth refresh; connected hosts preserve active sessions.
            self._auth_retry_streak += 1
            cause = f"Connection refused (HTTP {status}): the host tunnel was rejected."
            should_retry = self._ever_connected or (
                self._auth_retry_streak < _MAX_CONSECUTIVE_AUTH_ERRORS
            )
            if should_retry:
                # A sustained streak (vs. a brief VPN blip) means the credential
                # is very likely permanently rejected: escalate the operator
                # signal and name re-auth, but keep retrying so a real outage
                # self-heals.
                if (
                    self._auth_retry_streak >= _AUTH_REJECT_ESCALATE_ATTEMPTS
                    and self._auth_retry_streak % _AUTH_REJECT_ESCALATE_ATTEMPTS == 0
                ):
                    escalated = (
                        f"{cause} The server has rejected it "
                        f"{self._auth_retry_streak} times in a row — this is no "
                        "longer a transient network blip. If it persists, the "
                        "stored credential is likely no longer valid: run "
                        f"`{cli_invocation()} login {self._login_hint_url()}` and restart the "
                        "host. Still retrying."
                    )
                    _logger.warning("%s", escalated)
                    print(f"⚠ {escalated}", file=sys.stderr, flush=True)
                else:
                    _logger.warning("%s Retrying — check your VPN/network.", cause)
                    if self._auth_retry_streak == 1:
                        # The warning above lands only in the CLI log file;
                        # print once per outage so a foreground `omnigent host`
                        # isn't silent.
                        print(
                            f"⚠ {cause} Retrying — this usually means the VPN or "
                            "network dropped. It will reconnect automatically "
                            "once connectivity returns.",
                            file=sys.stderr,
                            flush=True,
                        )
                return None
        if status == 401:
            return HostConnectError(
                "Authentication failed (HTTP 401): the server rejected the supplied "
                f"credentials across {_MAX_CONSECUTIVE_AUTH_ERRORS} consecutive attempts. "
                + self._credentials_fix_hint()
                + " "
                + self._login_fix_hint()
            )
        if status == 403:
            # An expired stored login is the common way to land here: the
            # token loader yields nothing, the dial goes out
            # unauthenticated, and the server's refusal looks like an
            # authorization or version-skew problem. Name the real cause.
            from omnigent.cli_auth import stored_token_status

            if stored_token_status(self._server_url) == "expired":
                login_url = self._login_hint_url()
                return HostConnectError(
                    "Connection refused (HTTP 403): your stored login "
                    f"session for {login_url} has EXPIRED, so the "
                    "tunnel was dialed without credentials. Run `omnigent "
                    f"login {login_url}` to re-authenticate, then "
                    "restart the host."
                )
            return HostConnectError(
                "Connection refused (HTTP 403): the server repeatedly rejected the host "
                "tunnel. Either your "
                "identity is not authorized to register a host on this server, "
                "or the server is running a build that predates the host API "
                "(the /v1/hosts tunnel route). Confirm you have access and that "
                "the server is up to date, then retry. " + self._login_fix_hint()
            )
        if status == 409:
            return HostConnectError(
                "Connection refused (HTTP 409): this machine is already "
                "registered to a different account on this server, so the "
                "account you authenticated as cannot claim it. This usually "
                "means the host was first registered under another identity "
                "(e.g. the single-user 'local' owner before the server "
                "switched to accounts auth). Ask an administrator to remove "
                "the existing host registration, or reset this machine's host "
                "id, then retry. " + self._login_fix_hint()
            )
        return HostConnectError(
            f"Connection refused (HTTP {status}): the server rejected the host "
            "tunnel request. This is a permanent error; retrying will not help. "
            "Check the server URL and your access."
        )

    async def _handle_launch(
        self,
        frame: HostLaunchRunnerFrame,
    ) -> HostLaunchRunnerResultFrame:
        """Handle a launch_runner request from the server.

        Spawns a runner subprocess with the binding token and
        workspace from the frame, after verifying the session's
        harness (when the frame carries one) is configured on this
        machine.

        :param frame: The launch request frame.
        :returns: Result frame with status and runner_id, or a
            ``"failed"`` result with ``error_code`` set to
            ``"harness_not_configured"`` when the harness check
            refuses the launch.
        """
        # Refuse to spawn for a harness this machine can't actually run —
        # otherwise the runner starts, the session looks alive, and the
        # first turn dies confusingly inside the executor. ``None`` (an
        # older server, or a session with no resolvable harness) skips the
        # check so version skew fails open.
        if frame.harness is not None and not harness_is_configured(frame.harness):
            return HostLaunchRunnerResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=(
                    f"harness {frame.harness!r} is not configured on host "
                    f"{self._identity.name!r} — {harness_setup_hint(frame.harness)}"
                ),
                error_code=HARNESS_NOT_CONFIGURED_ERROR_CODE,
            )

        workspace = Path(frame.workspace).expanduser()
        if not workspace.is_dir():
            return HostLaunchRunnerResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"workspace path does not exist: {workspace}",
                error_code=WORKSPACE_MISSING_ERROR_CODE,
            )

        runner_id = token_bound_runner_id(frame.binding_token)
        initial_auth_token = await asyncio.to_thread(
            self._current_auth_token,
            initialize=False,
        )
        env = _build_runner_env(
            os.environ,
            server_url=self._server_url,
            runner_id=runner_id,
            binding_token=frame.binding_token,
            workspace=str(workspace),
            parent_pid=os.getpid(),
            initial_auth_token=initial_auth_token,
            host_id=self._identity.host_id,
            harness=frame.harness,
        )
        # The runner serves one primary session (plus any co-located subagents);
        # pass it so runner-level log records can be attributed to that session.
        if frame.session_id:
            env[PRIMARY_SESSION_ID_ENV_VAR] = frame.session_id
        # The runner is 1:1 with this host's owner (cross-owner co-location is
        # rejected server-side), so hand it our resolved owner for user_id
        # attribution of runner-level log records.
        if self._owner_user_id:
            env[USER_ID_ENV_VAR] = self._owner_user_id
        # The workspace id is resolved from the same server URL the runner dials,
        # so hand it the already-resolved value rather than making it re-derive.
        if self._origin_workspace_id:
            env[ORIGIN_WORKSPACE_ID_ENV_VAR] = self._origin_workspace_id

        # Embed the session id so operators can find all logs for a session
        # with `omnigent debug logs --session <id>`. Cap at 32 chars to keep
        # filenames manageable; strip anything non-word to guard against
        # unexpected id shapes from older servers.
        import re

        _session_slug = (
            re.sub(r"[^\w-]", "", frame.session_id)[:32] + "-" if frame.session_id else ""
        )

        # Spawning blocks (log-file open, plus the zygote's one-time import on
        # first launch), so run it off the event loop. Shielded so a
        # cancellation mid-spawn cannot abandon a live runner: the handle is
        # only registered in ``self._runners`` after this returns, so an
        # abandoned fork would never be watched, stopped, or reaped, and the
        # zygote would retain its exit status forever. On cancellation we let
        # the spawn land and then tear that runner down.
        spawn = asyncio.ensure_future(
            asyncio.to_thread(self._spawn_runner_proc, env, _session_slug, workspace)
        )
        try:
            proc, log_path = await asyncio.shield(spawn)
        except asyncio.CancelledError:
            spawn.add_done_callback(self._discard_abandoned_spawn)
            raise
        except OSError as exc:
            return HostLaunchRunnerResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"failed to spawn runner: {exc}",
            )

        if proc.poll() is not None:
            # The runner died before Popen returned — its actual error
            # is in the captured log, so ship the tail with the result
            # instead of making the user go find the file on the host.
            return HostLaunchRunnerResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=_runner_exit_error(proc.returncode, log_path),
            )

        # One live runner per session: the session's previous runner —
        # whose binding the server has already rotated away — is
        # superseded, but only now that its replacement is alive (a failed
        # spawn must not trade a working runner for nothing). Left alive
        # it would idle forever: tunnel still authenticating, transcript
        # forwarder still posting — one leaked generation per relaunch.
        # The pops run under the _runner_lifecycle_lock the frame
        # dispatcher holds (mirroring _handle_stop, so the watcher reads
        # the exit as intentional); the SIGTERM/SIGKILL round itself is
        # detached so a stop that waits out its 5s grace cannot
        # head-of-line block other sessions' launches on this host.
        if frame.session_id:
            superseded = [
                (rid, handle)
                for rid, handle in self._runners.items()
                if handle.session_id == frame.session_id
            ]
            for rid, handle in superseded:
                self._runners.pop(rid, None)
                self._spawn_superseded_stop(rid, handle, frame.session_id)
        self._runners[runner_id] = _RunnerHandle(
            proc=proc, log_path=log_path, session_id=frame.session_id or None
        )
        watcher = asyncio.create_task(self._watch_runner(runner_id))
        self._watcher_tasks.add(watcher)
        watcher.add_done_callback(self._watcher_tasks.discard)
        _logger.info(
            "Launched runner %s for workspace %s (pid=%d)",
            runner_id,
            workspace,
            proc.pid,
        )
        # Print the exact runner log file (not just the dir): a foreground
        # host's own terminal shows lifecycle lines, but the runner's real
        # output — the agent turn, tracebacks — lands only in this file.
        session_line = f"\n    session: {frame.session_id}" if frame.session_id else ""
        print(
            f"  ↑ Runner started: {runner_id} (pid={proc.pid})\n"
            f"    log: {display_log_path(log_path)}"
            f"{session_line}",
            flush=True,
        )
        return HostLaunchRunnerResultFrame(
            request_id=frame.request_id,
            status="launched",
            runner_id=runner_id,
        )

    def _ensure_zygote_started(self) -> ZygoteManager | None:
        """Start (or reuse) the runner zygote, latching the fallback on failure.

        Blocking — the first call pays the zygote's one-time import of the
        runner graph — so call it from a worker thread.
        ``ZygoteManager.start`` is idempotent under its own lock, so the
        boot-time prewarm (see :meth:`run`) and a concurrent launch can both
        call this safely.

        :returns: The started zygote, or ``None`` when the zygote is disabled
            (opt-out, non-POSIX, or a prior failure) or failed to start now.
        """
        zygote = self._zygote
        if zygote is None or self._zygote_disabled:
            return None
        try:
            zygote.start()
        except ZygoteUnavailable as exc:
            # Spawning the zygote itself is broken; retrying on every
            # launch would only add a doomed spawn to each, so disable
            # it for the daemon's life and fall back to direct Popen.
            _logger.warning(
                "Runner zygote failed to start (%s); disabling it and "
                "falling back to direct spawn",
                exc,
            )
            self._zygote_disabled = True
            return None
        return zygote

    def _spawn_runner_proc(
        self,
        env: dict[str, str],
        session_slug: str,
        workspace: Path,
    ) -> tuple[subprocess.Popen[bytes] | ZygoteRunnerProc, Path]:
        """Open the session log and spawn the runner, via zygote or direct Popen.

        Runs on a worker thread (blocking log open + first-launch zygote import).
        When the zygote is enabled it forks the runner there — sharing the import
        graph copy-on-write — and rewrites ``RUNNER_PARENT_PID`` to the zygote's
        pid so the runner's orphan watchdog (which compares ``os.getppid()``)
        stays correct across the extra process hop. A zygote start failure, or a
        channel failure while the zygote is still alive, disables it for the
        daemon's life; a zygote that died is reaped so the next launch respawns
        a fresh one. Either way this launch falls back to a direct Popen.

        :param env: Runner environment from :func:`_build_runner_env` (its
            ``RUNNER_PARENT_PID`` is the daemon pid; overridden on the zygote path).
        :param session_slug: Sanitized session id fragment for the log filename.
        :param workspace: Existing session workspace to use as the runner's cwd.
        :returns: ``(process_handle, log_path)`` — the handle quacks like Popen.
        :raises OSError: If the log file or a direct Popen spawn fails.
        """
        log_path, log_fh = open_process_log_file("runner", prefix=f"runner-{session_slug}")
        try:
            env[PROCESS_LOG_FILE_ENV_VAR] = str(log_path)

            zygote = self._ensure_zygote_started()
            if zygote is not None:
                try:
                    # The runner's OS parent will be the zygote, so its
                    # getppid()-based orphan check must watch the zygote pid.
                    zygote_env = dict(env)
                    zygote_env[RUNNER_PARENT_PID_ENV_VAR] = str(zygote.pid)
                    proc = zygote.fork_runner(zygote_env, str(log_path), str(workspace))
                    _logger.info(
                        "Forked runner via zygote (zygote pid=%s, runner pid=%s)",
                        zygote.pid,
                        proc.pid,
                    )
                    return proc, log_path
                except ZygoteUnavailable as exc:
                    if zygote.is_running():
                        # Alive but its control channel failed. Do NOT stop
                        # it: healthy runners already forked from it would
                        # see their parent die and self-terminate via the
                        # orphan watchdog. Disable for future launches; the
                        # still-running zygote is reaped on daemon shutdown
                        # (see run()'s finally).
                        _logger.warning(
                            "Runner zygote unavailable (%s); disabling it "
                            "and falling back to direct spawn",
                            exc,
                        )
                        self._zygote_disabled = True
                    else:
                        # The zygote process died — its forked runners are
                        # already self-terminating via their own orphan
                        # watchdogs, so nothing depends on this instance.
                        # Reap it and let the next launch's start() respawn
                        # a fresh one instead of losing copy-on-write
                        # forking for the rest of the daemon's life.
                        _logger.warning(
                            "Runner zygote died (%s); falling back to "
                            "direct spawn and respawning the zygote on "
                            "the next launch",
                            exc,
                        )
                        with contextlib.suppress(Exception):
                            zygote.stop()

            with child_logging_popen_kwargs(env) as logging_kwargs:
                proc = subprocess.Popen(
                    # -P keeps cwd off sys.path: a workspace that is itself an
                    # omnigent checkout would otherwise shadow the installed
                    # package. _entry re-adds it for spec-declared local tools.
                    [sys.executable, "-P", "-m", "omnigent.runner._entry"],
                    env=env,
                    # A daemon may outlive the checkout it started from.
                    cwd=str(workspace),
                    # Runners are WS-tunnel clients with no interactive input.
                    # Give them a clean /dev/null stdin instead of inheriting the
                    # daemon's: a long-lived daemon (e.g. backgrounded / nohup'd)
                    # can end up with a closed or recycled stdin fd, and an
                    # inherited bad fd makes the runner die at interpreter startup
                    # with "init_sys_streams: Bad file descriptor" — it never
                    # connects, so the session fails with "runner did not connect".
                    stdin=subprocess.DEVNULL,
                    stdout=log_fh,
                    stderr=log_fh,
                    **logging_kwargs,
                )
            return proc, log_path
        finally:
            log_fh.close()

    def _discard_abandoned_spawn(self, spawn: asyncio.Future[Any]) -> None:
        """Tear down a runner whose launch was cancelled before registration.

        ``_handle_launch`` shields the spawn, so a cancellation still lets the
        fork land — but nothing registered it in ``self._runners``, so it would
        never be watched, stopped, or reaped (and the zygote would hold its exit
        status forever). Kill it off the loop, since for a zygote-forked runner
        the terminate/wait round-trips are blocking control-socket exchanges.

        :param spawn: The completed spawn future.
        """
        if spawn.cancelled():
            return
        if spawn.exception() is not None:
            return  # spawn failed; nothing was created
        proc, _log_path = spawn.result()
        _logger.warning(
            "Launch cancelled after runner spawn (pid=%s); terminating the orphan",
            proc.pid,
        )
        with contextlib.suppress(RuntimeError):
            # No running loop during interpreter shutdown — best effort.
            asyncio.get_running_loop().run_in_executor(
                None, functools.partial(self._stop_runner_proc, proc)
            )

    async def _handle_stop(
        self,
        frame: HostStopRunnerFrame,
    ) -> HostStopRunnerResultFrame:
        """Handle a stop_runner request from the server.

        Terminates the runner subprocess if it exists.

        :param frame: The stop request frame.
        :returns: Result frame with status.
        """
        handle = self._runners.pop(frame.runner_id, None)
        if handle is None:
            return HostStopRunnerResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"unknown runner: {frame.runner_id}",
            )
        # The poll/terminate/wait round-trips are lock-free waitpid calls for a
        # direct-Popen runner, but blocking control-socket exchanges for a
        # zygote-forked one — run them off the loop so a wedged zygote can't
        # freeze the daemon's control handler.
        await asyncio.to_thread(self._stop_runner_proc, handle.proc)
        _logger.info("Stopped runner %s", frame.runner_id)
        print(
            f"  ↓ Runner stopped: {frame.runner_id}",
            flush=True,
        )
        return HostStopRunnerResultFrame(
            request_id=frame.request_id,
            status="stopped",
        )

    def _spawn_superseded_stop(
        self, runner_id: str, handle: _RunnerHandle, session_id: str
    ) -> None:
        """
        Terminate a superseded runner as a retained background task.

        The handle is already popped from ``self._runners`` (so its watcher
        reads the exit as intentional); only the SIGTERM/SIGKILL round runs
        detached, keeping the frame dispatcher's lifecycle lock free while
        a stubborn runner waits out its termination grace. A failure here
        is logged loudly — the process would otherwise linger untracked
        until the orphan reaper collects it post-exit.

        :param runner_id: The superseded runner's id (for logging).
        :param handle: Its popped :class:`_RunnerHandle`.
        :param session_id: The session being relaunched (for logging).
        """

        async def _stop_and_log() -> None:
            try:
                await asyncio.to_thread(self._stop_runner_proc, handle.proc)
                _logger.info(
                    "Stopped superseded runner %s for session %s",
                    runner_id,
                    session_id,
                )
            except Exception:  # noqa: BLE001 — must never die unobserved
                _logger.warning(
                    "Failed to stop superseded runner %s for session %s; "
                    "the process may linger until it exits on its own",
                    runner_id,
                    session_id,
                    exc_info=True,
                )

        task = asyncio.create_task(_stop_and_log())
        self._supersede_stop_tasks.add(task)
        task.add_done_callback(self._supersede_stop_tasks.discard)

    @staticmethod
    def _stop_runner_proc(proc: subprocess.Popen[bytes] | ZygoteRunnerProc) -> None:
        """Terminate a runner: SIGTERM, brief wait, then SIGKILL. Blocking.

        Runs on a worker thread (see :meth:`_handle_stop`) because for a
        zygote-forked runner these are blocking control-socket round-trips, not
        lock-free waitpid calls.

        :param proc: The runner process handle (Popen or zygote shim).
        """
        if proc.poll() is not None:
            return
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            # Bounded: a bare wait() would hang if the handle can't observe the
            # exit (e.g. a zygote-forked runner whose zygote died and whose pid
            # probe is the only signal). The kill has been sent; give it a short
            # window, then move on.
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5.0)

    async def _handle_runner_status(
        self,
        frame: HostRunnerStatusFrame,
    ) -> HostRunnerStatusResultFrame:
        """Answer whether a runner's process is alive, dead, or unknown.

        The host is the authoritative owner of runner liveness: it holds
        the runner's :class:`subprocess.Popen`. A runner tracked with a
        still-running process is ``alive`` (covers a runner that is still
        booting — it is inserted at ``Popen`` time, before its tunnel
        connects — so the server waits for it). A tracked-but-exited
        process is ``dead``. A runner this host has no record of is
        ``unknown`` — it was stopped (``_handle_stop`` popped it) or a
        fresh post-restart host never spawned it; either way it will never
        connect, so the server relaunches without waiting.

        :param frame: The status query frame.
        :returns: Result frame with ``alive`` / ``dead`` / ``unknown``.
        """
        handle = self._runners.get(frame.runner_id)
        if handle is None:
            status = "unknown"
        else:
            # poll() is a lock-free waitpid for a direct-Popen runner, but a
            # blocking control-socket round-trip (bounded only by the 30s
            # control timeout, and contended against a booting zygote) for a
            # zygote-forked one — so run it off the loop, matching
            # _watch_runner / _handle_stop, lest a slow zygote stall the daemon.
            status = "alive" if await asyncio.to_thread(handle.proc.poll) is None else "dead"
        return HostRunnerStatusResultFrame(
            request_id=frame.request_id,
            status=status,
        )

    async def _watch_runner(self, runner_id: str) -> None:
        """Watch a spawned runner and report an unexpected exit.

        Polls the runner subprocess until it exits. An exit while the
        runner is still tracked in ``self._runners`` is unexpected (a
        ``host.stop_runner`` pops the entry *before* terminating), so
        the watcher composes the exit error — code plus log tail — and
        reports it to the server via ``host.runner_exited``. Without
        this, a runner that crashes before connecting its tunnel
        (auth rejection, bad env, import error) leaves the client
        polling to a timeout with the cause stranded in a log file on
        this host.

        :param runner_id: The runner to watch, e.g.
            ``"runner_abc123..."``.
        :returns: None. Returns silently for intentional stops and clean
            (exit-code-0) shutdowns.
        """
        handle = self._runners.get(runner_id)
        if handle is None:  # pragma: no cover — spawned just before us
            return
        # poll() is a lock-free waitpid for a direct-Popen runner, but a
        # blocking control-socket round-trip (with lock contention against a
        # booting zygote) for a zygote-forked one — so run it off the event
        # loop to avoid freezing the daemon on the enabled path.
        while await asyncio.to_thread(handle.proc.poll) is None:
            await asyncio.sleep(_RUNNER_WATCH_INTERVAL_S)
        if self._runners.get(runner_id) is not handle:
            # _handle_stop (or _cleanup_runners) removed it first —
            # an intentional termination, not a crash to report.
            return
        if handle.proc.returncode == 0:
            # A clean exit (code 0) is a graceful shutdown, not a crash — the
            # idle reaper shutting an inactive runner down, or any orderly
            # self-exit. Reporting it as host.runner_exited would attach a
            # scary "runner process exited" error to a session the user only
            # has to message to reactivate, so stay silent. A non-zero exit
            # below is a genuine crash and still reports its cause.
            _logger.info("Runner %s exited cleanly (code 0); no crash report", runner_id)
            return
        error = _runner_exit_error(handle.proc.returncode, handle.log_path)
        _logger.warning("Runner %s died unexpectedly: %s", runner_id, error)
        await self._report_runner_exit(runner_id, error)

    async def _report_runner_exit(self, runner_id: str, error: str) -> None:
        """Send a ``host.runner_exited`` report, queueing on failure.

        :param runner_id: The dead runner, e.g. ``"runner_abc123..."``.
        :param error: Composed exit error from
            :func:`_runner_exit_error`.
        :returns: None. A report that cannot be sent (tunnel down or
            mid-reconnect) is parked in ``self._unreported_exits`` and
            flushed by :meth:`_serve_frames` after the next hello.
        """
        frame = encode_host_frame(HostRunnerExitedFrame(runner_id=runner_id, error=error))
        ws = self._ws
        if ws is not None:
            try:
                await ws.send(frame)
                return
            except Exception:  # noqa: BLE001 — any send failure parks the report
                _logger.debug(
                    "Could not send runner_exited for %s; queueing for reconnect",
                    runner_id,
                    exc_info=True,
                )
        self._unreported_exits[runner_id] = error

    def _handle_stat(self, frame: HostStatFrame) -> HostStatResultFrame:
        """Handle a ``host.stat`` request from the server.

        Expands ``~`` against the host process owner's home (the
        host is the source of truth for ``~`` — the server never
        does this), follows symlinks via ``os.stat``, computes the
        canonical realpath, and collapses ENOENT + EACCES into
        ``exists: false``. Unexpected I/O errors return ``status:
        "failed"``. See designs/SESSION_WORKSPACE_SELECTION.md.

        :param frame: The stat request frame. ``frame.path`` may
            be a fully absolute path or a tilde-prefixed path.
        :returns: Stat result frame with ``exists``, ``type``, and
            ``canonical_path`` populated when the path is reachable.
        """
        try:
            expanded = os.path.expanduser(frame.path)
        except (TypeError, ValueError) as exc:
            # Defensive: expanduser shouldn't raise on str inputs,
            # but a malformed path could in principle. Fail loud
            # with a useful message rather than letting a generic
            # error bubble up to the server.
            return HostStatResultFrame(
                request_id=frame.request_id,
                status="failed",
                exists=False,
                error=f"path expansion failed: {exc}",
            )
        try:
            # ``os.stat`` follows symlinks by default — exactly
            # what the design wants ("type reflects the target").
            st = os.stat(expanded)
        except (FileNotFoundError, PermissionError):
            # ENOENT and EACCES collapse to "exists: false" so the
            # server validation has a single contract for "not
            # reachable."
            return HostStatResultFrame(
                request_id=frame.request_id,
                status="ok",
                exists=False,
            )
        except OSError as exc:
            return HostStatResultFrame(
                request_id=frame.request_id,
                status="failed",
                exists=False,
                error=f"stat failed: {exc.strerror or str(exc)}",
            )
        try:
            canonical = os.path.realpath(expanded)
        except OSError as exc:
            return HostStatResultFrame(
                request_id=frame.request_id,
                status="failed",
                exists=False,
                error=f"realpath failed: {exc.strerror or str(exc)}",
            )
        from stat import S_ISDIR, S_ISREG

        if S_ISDIR(st.st_mode):
            entry_type = "directory"
        elif S_ISREG(st.st_mode):
            entry_type = "file"
        else:
            entry_type = "other"
        return HostStatResultFrame(
            request_id=frame.request_id,
            status="ok",
            exists=True,
            type=entry_type,
            canonical_path=canonical,
        )

    async def _handle_import_local(
        self, ws: websockets.asyncio.client.ClientConnection, frame: HostImportLocalFrame
    ) -> None:
        """Stream the host's recent local transcripts, one frame per session.

        The host owns the session files (``~/.claude`` etc.). It enumerates the
        targets ("all" merges every harness into one global recency order, top
        ``limit`` total), then reads + normalizes each and sends it immediately
        (``host.import_local_session``) so a large batch never rides in one frame
        and the server persists as each arrives. A terminal ``host.import_local_done``
        closes the stream. Sessions that fail to load are skipped; a single-harness
        enumeration failure fails the request.
        """

        def _targets() -> tuple[list[tuple[str, str]], str | None]:
            from omnigent.session_import.local import (
                list_recent_local_session_ids,
                list_recent_sessions_across_harnesses,
            )
            from omnigent.session_import.models import ImportSource, SessionImportNotFoundError

            if frame.source == "all":
                return list(list_recent_sessions_across_harnesses(limit=frame.limit)), None
            source = cast(ImportSource, frame.source)
            try:
                ids = list_recent_local_session_ids(source, limit=frame.limit)
            except SessionImportNotFoundError:
                return [], None
            except (OSError, ValueError, TypeError) as exc:
                return [], str(exc)
            return [(source, sid) for sid in ids], None

        def _load(source: str, session_id: str) -> HostImportedLocalSession | None:
            from omnigent.session_import.local import load_local_session
            from omnigent.session_import.models import ImportSource, SessionImportNotFoundError

            try:
                local = load_local_session(cast(ImportSource, source), session_id)
            except (SessionImportNotFoundError, OSError, ValueError, TypeError):
                return None
            return HostImportedLocalSession(
                external_session_id=local.external_session_id,
                workspace=local.workspace,
                items=[
                    {
                        "type": item.type,
                        "response_id": item.response_id,
                        "data": item.data.model_dump(mode="json", exclude_none=True),
                    }
                    for item in local.items
                ],
                title=local.title,
                source=local.source,
            )

        try:
            targets, enum_error = await asyncio.to_thread(_targets)
            if enum_error is not None:
                await ws.send(
                    encode_host_frame(
                        HostImportLocalDoneFrame(
                            request_id=frame.request_id, status="failed", error=enum_error
                        )
                    )
                )
                return

            # Oldest first so the server imports newest last → newest sits atop the sidebar.
            ordered = list(reversed(targets))
            total = len(ordered)
            load_failed = 0
            for source, session_id in ordered:
                session = await asyncio.to_thread(_load, source, session_id)
                if session is None:
                    # Unreadable/corrupt transcript: no frame to send, but report
                    # it on the done frame so the server's counts stay honest.
                    load_failed += 1
                    continue
                await ws.send(
                    encode_host_frame(
                        HostImportLocalSessionFrame(
                            request_id=frame.request_id, total=total, session=session
                        )
                    )
                )
            await ws.send(
                encode_host_frame(
                    HostImportLocalDoneFrame(
                        request_id=frame.request_id, status="ok", failed=load_failed
                    )
                )
            )
        except ConnectionClosed:
            raise  # tunnel died mid-stream; _run_frame_handler owns recovery
        except Exception as exc:
            # Send a terminal failure so the server fails fast instead of waiting
            # out its per-frame timeout on an unanswered request.
            _logger.exception("import_local handler failed for source=%r", frame.source)
            with contextlib.suppress(Exception):
                await ws.send(
                    encode_host_frame(
                        HostImportLocalDoneFrame(
                            request_id=frame.request_id, status="failed", error=str(exc)
                        )
                    )
                )

    def _handle_list_dir(self, frame: HostListDirFrame) -> HostListDirResultFrame:
        """Handle a ``host.list_dir`` request from the server.

        Walks the requested directory with ``os.scandir``, follows
        symlinks for type detection (matching ``host.stat``), and
        returns a paginated result. ``~`` in the input path expands
        against the host process owner's home, same rules as
        ``host.stat``. Per-entry I/O errors (broken symlinks,
        ephemeral files) are silently skipped so a single bad
        entry doesn't fail the whole listing — same posture as
        the runner's ``list_dir``.

        :param frame: The list_dir request frame. ``frame.path``
            may be absolute or tilde-prefixed; ``limit`` /
            ``after`` / ``before`` drive pagination.
        :returns: List_dir result frame with entries sorted by
            name plus a ``has_more`` flag for the page.
        """
        try:
            expanded = os.path.expanduser(frame.path)
        except (TypeError, ValueError) as exc:
            return HostListDirResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"path expansion failed: {exc}",
            )
        try:
            scanned = list(os.scandir(expanded))
        except FileNotFoundError:
            return HostListDirResultFrame(
                request_id=frame.request_id,
                status="ok",
                error="path does not exist",
            )
        except NotADirectoryError:
            return HostListDirResultFrame(
                request_id=frame.request_id,
                status="ok",
                error="path is not a directory",
            )
        except PermissionError:
            return HostListDirResultFrame(
                request_id=frame.request_id,
                status="ok",
                error="permission denied",
            )
        except OSError as exc:
            return HostListDirResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"scandir failed: {exc.strerror or str(exc)}",
            )

        from stat import S_ISDIR, S_ISREG

        # Walk every entry, classifying by target type. Per-entry
        # OSError → skip (e.g. dangling symlink) so the listing
        # surfaces real entries instead of failing wholesale.
        entries: list[HostListDirEntry] = []
        for de in scanned:
            try:
                # follow_symlinks=True so type reflects the target.
                st = de.stat(follow_symlinks=True)
            except OSError:
                continue
            if S_ISDIR(st.st_mode):
                entry_type = "directory"
                size: int | None = None
            elif S_ISREG(st.st_mode):
                entry_type = "file"
                size = st.st_size
            else:
                entry_type = "other"
                size = None
            entries.append(
                HostListDirEntry(
                    name=de.name,
                    path=de.path,
                    type=entry_type,
                    bytes=size,
                    modified_at=int(st.st_mtime),
                )
            )

        # Sort by name for stable pagination cursors. Cursors are
        # entry paths so they survive concurrent directory writes
        # better than an in-memory index.
        entries.sort(key=lambda e: e.name)

        return _paginate_list_dir(
            entries=entries,
            request_id=frame.request_id,
            limit=frame.limit,
            after=frame.after,
            before=frame.before,
        )

    def _handle_create_dir(self, frame: HostCreateDirFrame) -> HostCreateDirResultFrame:
        """Handle a ``host.create_dir`` request from the server.

        Creates the directory (and any missing parents) with
        ``os.makedirs``. ``~`` expands against the host process
        owner's home, same rules as ``host.list_dir``. Expected
        filesystem errors (the directory already exists, permission
        denied, a parent component is a file) return ``status: "ok"``
        with a descriptive ``error`` so the route layer can map them
        to a 409 rather than a 500 — mirroring how ``_handle_list_dir``
        reports a missing path. Only unexpected I/O errors surface as
        ``status: "failed"``.

        :param frame: The create-dir request frame. ``frame.path`` may
            be absolute or tilde-prefixed.
        :returns: Result frame carrying the created absolute path on
            success, or an ``error`` describing why it was not created.
        """
        try:
            expanded = os.path.expanduser(frame.path)
        except (TypeError, ValueError) as exc:
            return HostCreateDirResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"path expansion failed: {exc}",
            )
        try:
            # exist_ok=False so creating an existing folder is a clear
            # "already exists" rather than a silent no-op — the picker
            # should tell the user the name is taken.
            os.makedirs(expanded, exist_ok=False)
        except FileExistsError:
            # makedirs raises FileExistsError whether the leaf is an
            # existing directory or a regular file. Distinguish the two
            # so "name is taken by a file" isn't mislabelled as an
            # existing directory.
            error = (
                "directory already exists"
                if os.path.isdir(expanded)
                else "a file already exists at that path"
            )
            return HostCreateDirResultFrame(
                request_id=frame.request_id,
                status="ok",
                error=error,
            )
        except NotADirectoryError:
            return HostCreateDirResultFrame(
                request_id=frame.request_id,
                status="ok",
                error="a parent path component is not a directory",
            )
        except PermissionError:
            return HostCreateDirResultFrame(
                request_id=frame.request_id,
                status="ok",
                error="permission denied",
            )
        except OSError as exc:
            return HostCreateDirResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"mkdir failed: {exc.strerror or str(exc)}",
            )
        created = os.path.abspath(expanded)
        _logger.info("Created directory %s", created)
        return HostCreateDirResultFrame(
            request_id=frame.request_id,
            status="ok",
            path=created,
        )

    def _handle_install_harness(
        self, frame: HostInstallHarnessFrame
    ) -> HostInstallHarnessResultFrame:
        """Handle a ``host.install_harness`` request from the server.

        Runs the same installer :func:`try_install_harness_cli` (hence
        ``omnigent setup``) uses, then recomputes readiness so the result frame
        carries a fresh ``configured_harnesses`` map. The ``ui_install_key``
        guard re-checks the allowlist as defence in depth against a spoofed
        frame. Idempotent: an already-installed CLI skips the install. Runs off
        the event loop (it shells out / probes ``PATH``).

        :param frame: The install request frame. ``frame.harness`` is a UI
            harness identifier, e.g. ``"claude"``.
        :returns: Result frame with ``status`` ``"ok"``/``"failed"``, the
            refreshed readiness map on success, and a reason on failure.
        """
        key = ui_install_key(frame.harness)
        if key is None:
            return HostInstallHarnessResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"harness {frame.harness!r} is not installable from the UI",
            )
        if harness_cli_installed(key):
            # Already installed — skip the slow npm re-resolve and just report
            # current readiness (which may still be "needs-auth", e.g. codex).
            _logger.info("Harness %s already installed; skipping install", frame.harness)
            return HostInstallHarnessResultFrame(
                request_id=frame.request_id,
                status="ok",
                configured_harnesses=configured_harness_map(),
                gateway_inference=gateway_inference_map(),
            )
        installed, reason = try_install_harness_cli(key)
        if not installed:
            return HostInstallHarnessResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=reason or "install failed",
            )
        _logger.info("Installed harness %s via UI request", frame.harness)
        return HostInstallHarnessResultFrame(
            request_id=frame.request_id,
            status="ok",
            configured_harnesses=configured_harness_map(),
            gateway_inference=gateway_inference_map(),
        )

    def _handle_store_secret(self, frame: HostStoreSecretFrame) -> HostStoreSecretResultFrame:
        """Handle a ``host.store_secret`` request from the server.

        Writes a harness provider credential on THIS host via the same
        non-interactive core (:func:`store_harness_credential` /
        :func:`adopt_env_credential`) the ``omnigent setup`` wizard's
        "add a key / gateway" path uses — the secret goes to the OS keychain
        (else ``~/.omnigent/secrets.json``) and ``config.yaml`` gets a
        ``providers:`` entry referencing it, never the raw secret. Then it
        recomputes readiness so the result frame flips the badge (yellow →
        green) without a reconnect.

        The ``ui_install_key`` guard re-checks the allowlist as defence in depth
        against a spoofed frame — only the UI-auth families (Claude/Codex/Pi)
        can drive the writer. The secret is never logged. Runs off the event
        loop (keychain / file I/O).

        :param frame: The store-secret request. ``frame.harness`` is a UI
            harness id; ``frame.kind`` is ``"key"`` / ``"gateway"`` / ``"adopt"``.
        :returns: Result with ``status`` ``"ok"``/``"failed"``, refreshed
            readiness on success, and a non-secret reason on failure.
        """
        # Resolve the harness to a provider family, re-checking the allowlist.
        # claude→anthropic, codex→openai; pi consumes both and prefers anthropic
        # (its first fallback family), so a typed pi key lands on anthropic.
        install_key = ui_install_key(frame.harness)
        family = {
            ANTHROPIC_FAMILY: ANTHROPIC_FAMILY,
            OPENAI_FAMILY: OPENAI_FAMILY,
            "pi": ANTHROPIC_FAMILY,
        }.get(install_key or "")
        if family is None:
            return HostStoreSecretResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"harness {frame.harness!r} is not configurable from the UI",
            )

        if frame.kind == "adopt":
            if not frame.env_var:
                return HostStoreSecretResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error="adopt requires an env_var",
                )
            # Adopt ONLY a credential the host actually detected, and under its
            # OWN family. Requiring a match in detect_adoptable_credentials()
            # enforces the adopt boundary server-side (the raw API is owner-authz'd
            # but otherwise unvalidated): without it a caller could name any set
            # env var — a DB password, an unrelated secret — and have it persisted
            # as a provider credential and sent to the vendor endpoint as auth.
            # Using the detected family (not the harness default) also keeps a
            # cross-family pick correct: pi consumes both anthropic + openai, so a
            # detected $OPENAI_API_KEY adopted for pi lands on openai, not
            # anthropic's endpoint.
            detected_family = next(
                (d.family for d in detect_adoptable_credentials() if d.env_var == frame.env_var),
                None,
            )
            if detected_family is None:
                return HostStoreSecretResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error=f"{frame.env_var} is not an adoptable credential on this host",
                )
            result = adopt_env_credential(family=detected_family, env_var=frame.env_var)
        elif frame.kind in ("key", "gateway"):
            result = store_harness_credential(
                family=family,
                kind=cast(Literal["key", "gateway"], frame.kind),
                secret=frame.secret_value or "",
                base_url=frame.base_url,
                default_model=frame.default_model,
                wire_api=frame.wire_api,
            )
        else:
            return HostStoreSecretResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"unknown credential kind {frame.kind!r}",
            )

        if not result.stored:
            return HostStoreSecretResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=result.reason or "could not write the credential",
            )
        # Deliberately log only the non-secret entry name, never the secret.
        _logger.info(
            "Wrote %s credential for %s (family %s) via UI request",
            frame.kind,
            frame.harness,
            family,
        )
        return HostStoreSecretResultFrame(
            request_id=frame.request_id,
            status="ok",
            configured_harnesses=configured_harness_map(),
            gateway_inference=gateway_inference_map(),
        )

    def _handle_detect_credentials(
        self, frame: HostDetectCredentialsFrame
    ) -> HostDetectCredentialsResultFrame:
        """Handle a ``host.detect_credentials`` request from the server.

        Returns the adoptable credentials already on this host as NON-secret
        descriptors (family + source label + env var name) so the UI can offer
        a one-click "adopt". Never reads or returns a secret value; never raises
        (a detection failure yields an empty list).

        :param frame: The detect request (carries only a request id).
        :returns: Result frame with the non-secret credential descriptors.
        """
        detected = detect_adoptable_credentials()
        return HostDetectCredentialsResultFrame(
            request_id=frame.request_id,
            credentials=[
                {"family": d.family, "source": d.source, "env_var": d.env_var} for d in detected
            ],
        )

    def _handle_fs_request(self, frame: HostFsRequestFrame) -> HostFsResultFrame:
        """Serve a read-only workspace filesystem request from the host.

        Runs :class:`omnigent.workspace_fs.WorkspaceReader` against the
        session's workspace so the web UI's file panel keeps working when
        the runner is offline but the host still holds the workspace on
        disk. Read-only and confined to the workspace root; never writes
        or runs a shell. Called inside a worker thread by the dispatcher
        because git / directory-walk work can block.

        :param frame: The fs request frame (op + workspace + params).
        :returns: A result frame with the runner-shaped payload, or an
            error frame mirroring the status the runner would return.
        """
        from pathlib import Path

        from omnigent.workspace_fs import WorkspaceReader, WorkspaceReaderError

        try:
            expanded = os.path.expanduser(frame.workspace)
        except (TypeError, ValueError) as exc:
            return HostFsResultFrame(
                request_id=frame.request_id,
                status="error",
                error_status=400,
                error_code="invalid_workspace",
                error=f"workspace path expansion failed: {exc}",
            )
        if not os.path.isdir(expanded):
            return HostFsResultFrame(
                request_id=frame.request_id,
                status="error",
                error_status=404,
                error_code="not_found",
                error="workspace directory does not exist on host",
            )

        reader = WorkspaceReader(Path(expanded))
        params = frame.params or {}
        try:
            payload = self._dispatch_fs_op(reader, frame.op, frame.session_id, params)
        except WorkspaceReaderError as exc:
            return HostFsResultFrame(
                request_id=frame.request_id,
                status="error",
                error_status=exc.status,
                error_code=exc.code,
                error=exc.message,
            )
        except ValueError as exc:
            return HostFsResultFrame(
                request_id=frame.request_id,
                status="error",
                error_status=400,
                error_code="invalid_request",
                error=str(exc),
            )
        except Exception as exc:
            _logger.exception("host fs_request op %r failed", frame.op)
            return HostFsResultFrame(
                request_id=frame.request_id,
                status="error",
                error_status=500,
                error_code="fs_read_failed",
                error=str(exc),
            )
        return HostFsResultFrame(
            request_id=frame.request_id,
            status="ok",
            payload=payload,
        )

    async def _prewarm_model_options(self) -> None:
        """
        Fill the on-disk model catalogs for the probing harnesses at boot.

        Runs both harness probes CONCURRENTLY, as detached background work —
        nothing (the tunnel, registration, readiness reporting, launches)
        ever waits on this. A picker request racing the boot probe joins the
        same single-flight probe through the shared store instead of
        starting a second one.

        :returns: None. Probe failures are absorbed by the probe wrappers.
        """
        await asyncio.gather(
            self._probed_codex_model_options(),
            self._probed_claude_model_options(),
            return_exceptions=True,
        )

    async def _probed_codex_model_options(self) -> ModelOptionsResult | None:
        """
        Store-backed harness-truth Codex listing, or ``None`` on failure.

        Every launch shape is answered from the shared on-disk catalog
        (probed from the configured Codex binary on a miss). There is no
        curated fallback: no catalog means an honest empty answer.

        :returns: The catalog listing, or ``None`` when unavailable.
        """
        from omnigent.codex_native_app_server import codex_launch_catalog

        try:
            rows = await codex_launch_catalog()
        except Exception:  # noqa: BLE001 — no catalog, never a crash
            _logger.warning("Codex model catalog unavailable", exc_info=True)
            return None
        if rows is None:
            return None
        routable = [row["id"] for row in rows if isinstance(row.get("id"), str) and row["id"]]
        return ModelOptionsResult(models=rows, routable_models=routable)

    async def _probed_claude_model_options(self) -> ModelOptionsResult | None:
        """
        Store-backed harness-truth Claude listing, or ``None`` on failure.

        The shared catalog is keyed by the resolved launch config's
        fingerprint — the same file the runner reads at launch and serves in
        the session gear, so the pre-launch picker and the session cannot
        drift.

        :returns: The catalog listing, or ``None`` when unavailable.
        """
        from omnigent.claude_native import claude_launch_catalog, resolve_native_claude_config

        try:
            config = await asyncio.to_thread(resolve_native_claude_config, spec=None)
            rows = await claude_launch_catalog(config)
        except Exception:  # noqa: BLE001 — no catalog, never a crash
            _logger.warning("Claude model catalog unavailable", exc_info=True)
            return None
        if rows is None:
            return None
        routable = list(config.routable_models) if config is not None else []
        return ModelOptionsResult(models=rows, routable_models=routable)

    async def _handle_model_options(
        self,
        frame: HostModelOptionsFrame,
    ) -> HostModelOptionsResultFrame:
        """Resolve the launch picker catalog on the machine that will run it.

        This is a pre-launch PREVIEW of the host's ambient default
        configuration (``spec=None`` — no session exists yet, so there is no
        agent spec to pin). A session whose spec pins a different provider
        resolves its own catalog at launch, and the in-session picker
        re-reads that authoritative snapshot after bind.
        """
        harness = canonicalize_harness(frame.harness) or frame.harness
        if harness == "codex-native":
            # Harness-truth lane: every launch shape is answered from the
            # shared catalog, probed from the configured Codex binary itself.
            # No curated fallback and no serving-endpoints listing — a probe
            # that cannot run yields an honest empty answer with the reason.
            probed = await self._probed_codex_model_options()
            if probed is not None:
                return HostModelOptionsResultFrame(
                    request_id=frame.request_id,
                    status="ok",
                    models=probed.models,
                    routable_models=probed.routable_models,
                )
            return HostModelOptionsResultFrame(
                request_id=frame.request_id,
                status="ok",
                models=[],
                error="the codex model probe failed — see the host log",
            )

        if harness == "pi-native":
            try:
                from omnigent.pi_native_credentials import pi_native_model_options

                pi_models = await asyncio.to_thread(pi_native_model_options)
            except Exception:
                _logger.exception("Failed to resolve pre-launch Pi model options")
                return HostModelOptionsResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error="failed to resolve Pi model options",
                )
            return HostModelOptionsResultFrame(
                request_id=frame.request_id,
                status="ok",
                models=pi_models,
            )

        if is_claude_sdk_harness_name(harness):
            # SDK-mode Claude is a pass-through client with no model catalog
            # of its own, so the endpoint listing IS the harness truth — the
            # ids are already in the exact spelling the SDK sends.
            try:
                from omnigent.model_catalog import list_models_for_worker
                from omnigent.spec.types import AgentSpec, ExecutorSpec

                sdk_spec = AgentSpec(
                    spec_version=1,
                    name="claude-sdk-prelaunch",
                    executor=ExecutorSpec(
                        type="omnigent",
                        config={"harness": "claude-sdk"},
                    ),
                )
                listing = await asyncio.to_thread(list_models_for_worker, sdk_spec, "claude-sdk")
            except Exception:
                _logger.exception("Failed to resolve pre-launch Claude SDK model options")
                return HostModelOptionsResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error="failed to resolve Claude SDK model options",
                )
            if not listing.models:
                # Subscription / CLI-login providers list nothing endpoint-side.
                # The SDK drives the claude CLI, so the CLI's own probed rows
                # (its aliases resolve inside the harness) are the truth here.
                probed = await self._probed_claude_model_options()
                if probed is not None:
                    return HostModelOptionsResultFrame(
                        request_id=frame.request_id,
                        status="ok",
                        models=probed.models,
                        routable_models=probed.routable_models,
                    )
            return HostModelOptionsResultFrame(
                request_id=frame.request_id,
                status="ok",
                models=[{"id": model.id, "displayName": model.id} for model in listing.models],
                routable_models=[model.id for model in listing.models],
            )
        if harness != "claude-native":
            return HostModelOptionsResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"model options are unsupported for harness {frame.harness!r}",
            )
        probed = await self._probed_claude_model_options()
        if probed is not None:
            return HostModelOptionsResultFrame(
                request_id=frame.request_id,
                status="ok",
                models=probed.models,
                routable_models=probed.routable_models,
            )
        return HostModelOptionsResultFrame(
            request_id=frame.request_id,
            status="ok",
            models=[],
            error="the claude model probe failed — see the host log",
        )

    @staticmethod
    def _dispatch_fs_op(
        reader: object,
        op: str,
        session_id: str,
        params: dict[str, object],
    ) -> dict[str, object]:
        """Route an fs op to the matching :class:`WorkspaceReader` method.

        :param reader: The workspace reader bound to the workspace root.
        :param op: Operation name from the request frame.
        :param session_id: Session id forwarded to change-registry ops.
        :param params: Operation-specific arguments.
        :returns: The runner-shaped result dict.
        :raises ValueError: On an unknown op.
        """
        from typing import cast

        from omnigent.workspace_fs import WorkspaceReader

        r = cast("WorkspaceReader", reader)
        if op == "list_or_read":
            return r.list_or_read(
                str(params.get("path", "")),
                limit=_coerce_int(params.get("limit", 20)),
                after=cast("str | None", params.get("after")),
                before=cast("str | None", params.get("before")),
                order=str(params.get("order", "desc")),
            )
        if op == "changes":
            return r.changes(session_id)
        if op == "diff":
            return r.diff(session_id, str(params.get("path", "")))
        if op == "search":
            return r.search(
                str(params.get("q", "")),
                include=cast("str | None", params.get("include")),
                exclude=cast("str | None", params.get("exclude")),
                limit=_coerce_int(params.get("limit", 500)),
            )
        raise ValueError(f"unknown fs op: {op!r}")

    async def _handle_create_worktree(
        self,
        frame: HostCreateWorktreeFrame,
    ) -> HostCreateWorktreeResultFrame:
        """Handle a ``host.create_worktree`` request from the server.

        Runs the blocking git work in a worker thread so the tunnel
        loop keeps servicing pings. See designs/SESSION_GIT_WORKTREE.md.

        :param frame: The create-worktree request frame.
        :returns: Result frame with the worktree path and branch on
            success, or ``status: "failed"`` with an error message.
        """
        try:
            # Pause the orphan reaper: create_worktree runs git via
            # subprocess.run, whose children are direct children of this host
            # but not tracked runners — the reaper must not wait() them out
            # from under subprocess (#1782).
            with self._host_subprocess_op():
                created = await asyncio.to_thread(
                    create_worktree,
                    repo_path=frame.repo_path,
                    branch_name=frame.branch_name,
                    base_branch=frame.base_branch,
                )
        except WorktreeError as exc:
            return HostCreateWorktreeResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=exc.message,
            )
        _logger.info(
            "Created worktree %s (branch %s) from %s",
            created.worktree_path,
            created.branch,
            frame.repo_path,
        )
        return HostCreateWorktreeResultFrame(
            request_id=frame.request_id,
            status="ok",
            worktree_path=created.worktree_path,
            branch=created.branch,
        )

    async def _handle_remove_worktree(
        self,
        frame: HostRemoveWorktreeFrame,
    ) -> HostRemoveWorktreeResultFrame:
        """Handle a ``host.remove_worktree`` request from the server.

        Runs the blocking git work in a worker thread.

        :param frame: The remove-worktree request frame.
        :returns: Result frame with ``status: "ok"`` on success, or
            ``status: "failed"`` with an error message.
        """
        try:
            # Pause the orphan reaper while remove_worktree runs git — see
            # _handle_create_worktree above and _reap_orphans_once (#1782).
            with self._host_subprocess_op():
                await asyncio.to_thread(
                    remove_worktree,
                    worktree_path=frame.worktree_path,
                    branch=frame.branch,
                    delete_branch=frame.delete_branch,
                )
        except WorktreeError as exc:
            return HostRemoveWorktreeResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=exc.message,
            )
        _logger.info(
            "Removed worktree %s (delete_branch=%s, branch=%s)",
            frame.worktree_path,
            frame.delete_branch,
            frame.branch,
        )
        return HostRemoveWorktreeResultFrame(
            request_id=frame.request_id,
            status="ok",
        )

    async def _handle_list_worktrees(
        self,
        frame: HostListWorktreesFrame,
    ) -> HostListWorktreesResultFrame:
        """Handle a ``host.list_worktrees`` request from the server.

        Runs the blocking git work in a worker thread so the tunnel
        loop keeps servicing pings.

        :param frame: The list-worktrees request frame.
        :returns: Result frame with the worktrees on success, or
            ``status: "failed"`` with an error message.
        """
        try:
            # Pause the orphan reaper while git runs — see
            # _handle_create_worktree above and _reap_orphans_once.
            with self._host_subprocess_op():
                worktrees = await asyncio.to_thread(
                    list_worktrees,
                    repo_path=frame.repo_path,
                )
        except WorktreeError as exc:
            return HostListWorktreesResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=exc.message,
            )
        return HostListWorktreesResultFrame(
            request_id=frame.request_id,
            status="ok",
            worktrees=[
                {
                    "path": wt.path,
                    "branch": wt.branch,
                    "is_main": wt.is_main,
                    "detached": wt.detached,
                }
                for wt in worktrees
            ],
        )

    async def _probe_configured_harnesses(
        self,
        *,
        startup: bool,
    ) -> dict[str, HarnessAvailability] | None:
        """Collect harness readiness without letting a probe break the channel."""
        try:
            return await asyncio.to_thread(configured_harness_map)
        except Exception as exc:
            _logger.exception("Host harness readiness probe failed")
            if startup:
                print(
                    "⚠ Could not inspect installed harnesses; the host will "
                    f"connect with harness readiness unknown: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            return None

    async def _probe_gateway_inference(self, *, startup: bool) -> dict[str, bool] | None:
        """Collect gateway metadata without letting a probe break the channel."""
        try:
            return await asyncio.to_thread(gateway_inference_map)
        except Exception as exc:
            _logger.exception("Host gateway-inference probe failed")
            if startup:
                print(
                    "⚠ Could not inspect gateway inference; the host will "
                    f"connect with gateway backing unknown: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
            return None

    async def _initialize_capabilities(self) -> None:
        """Build the initial capability snapshot once, before any handshake."""
        if self._capabilities_initialized:
            return
        try:
            configured, gateway = await asyncio.wait_for(
                asyncio.gather(
                    self._probe_configured_harnesses(startup=True),
                    self._probe_gateway_inference(startup=True),
                ),
                timeout=_HOST_CAPABILITY_INIT_TIMEOUT_S,
            )
        except TimeoutError:
            configured = gateway = None
            _logger.error(
                "Host capability discovery exceeded %.0fs; continuing with unknown metadata",
                _HOST_CAPABILITY_INIT_TIMEOUT_S,
            )
            print(
                "⚠ Host capability discovery timed out; connecting with readiness unknown.",
                file=sys.stderr,
                flush=True,
            )
        self._configured_harnesses = configured
        self._gateway_inference = gateway
        self._capabilities_initialized = True

    async def _lifecycle_monitor_loop(self) -> None:
        """Self-terminate once this daemon no longer owns its registry record.

        Polls the record; a delete (``omnigent host stop``) or pid change (a
        newer daemon claimed the target) after we have owned it at least once
        means this process is stale. It then sets ``_lifecycle_lost`` and aborts
        the live tunnel (same mechanism as the suspend watcher) so an in-flight
        :meth:`_connect_and_serve` returns now; :meth:`run` sees the flag and
        breaks, and its ``finally`` reaps runners and releases the lock.

        The "owned at least once" latch tolerates the startup window where the
        launching CLI has not written the record yet, so a fresh daemon is not
        killed before it is registered.

        :returns: None.
        """
        lock = self._lifecycle_lock
        if lock is None:
            return
        confirmed = False
        while True:
            await asyncio.sleep(_LIFECYCLE_POLL_INTERVAL_S)
            if await asyncio.to_thread(lock.still_owner):
                confirmed = True
                continue
            if not confirmed:
                continue
            _logger.warning(
                "Host daemon record for %s is gone or reassigned; self-terminating.",
                lock.target,
            )
            self._lifecycle_lost.set()
            self._abort_live_tunnel()
            return

    def _abort_live_tunnel(self) -> None:
        """Abort the live tunnel transport, if any, to break the serve loop.

        Reads ``self._ws`` and aborts synchronously on the event loop, so it is
        atomic w.r.t. ``_serve_frames``. A no-op when nothing is connected — the
        top-of-loop / backoff checks in :meth:`run` handle that case.

        :returns: None.
        """
        ws = self._ws
        transport = getattr(ws, "transport", None) if ws is not None else None
        if transport is not None:
            # Best-effort: aborting an already-closing/dead transport can raise
            # a socket-level error. It's harmless here (run() still exits via
            # _lifecycle_lost), but log it rather than swallow it silently.
            try:
                transport.abort()
            except OSError:
                _logger.debug("lifecycle tunnel abort raised", exc_info=True)

    async def run(self) -> None:
        """Run the host process with reconnection.

        Connects to the server, sends hello, and enters the
        receive loop. Reconnects with exponential backoff on
        disconnect. Ctrl-C / SIGTERM exit cleanly.

        :returns: None. Runs until the process is terminated.
        :raises HostConnectError: On a permanent failure — auth /
            authorization / outdated server, or a loopback server that
            kept refusing connections (the local server is gone).
        """
        # Capability probes may shell out or inspect local config, so perform
        # them once during daemon initialization. They are advisory: a broken
        # harness is reported as unknown and must not prevent registration.
        await self._initialize_capabilities()

        # Reap orphaned harness/tool grandchildren that reparent here when a
        # runner dies (this host is PID 1 in a container, or a subreaper
        # otherwise). Without this they pile up as <defunct> zombies and can
        # OOM the box on a long-blocked run (#1782).
        if _install_child_subreaper():
            _logger.debug("installed PR_SET_CHILD_SUBREAPER; host will reap orphans")
        self._reaper_task = asyncio.create_task(
            self._orphan_reaper_loop(), name="host-orphan-reaper"
        )
        # Detect wake from system suspend (laptop sleep) and force-drop the
        # then-dead tunnel so the reconnect loop reattaches within seconds
        # instead of waiting out the ~90s keepalive ping timeout.
        self._suspend_task = asyncio.create_task(
            watch_for_resume(self._on_resume_from_suspend), name="host-suspend-watch"
        )
        # Bind this daemon's lifetime to its registry record: hold the target's
        # flock and watch the record so a stale daemon retires itself.
        if self._lifecycle_lock is not None:
            self._lifecycle_lock.acquire()
            self._lifecycle_task = asyncio.create_task(
                self._lifecycle_monitor_loop(), name="host-lifecycle-monitor"
            )
        # Warm the runner zygote now: start() blocks on its one-time import
        # of the runner graph (~1-2s), which otherwise lands inside the first
        # session launch of the daemon's life. Best-effort — a failure
        # latches the same direct-Popen fallback the launch path uses.
        if self._zygote is not None and not self._zygote_disabled:
            self._zygote_prestart_task = asyncio.create_task(
                asyncio.to_thread(self._ensure_zygote_started),
                name="host-zygote-prestart",
            )
        backoff = _RECONNECT_BASE_S
        try:
            while True:
                if self._lifecycle_lost.is_set():
                    break
                try:
                    await self._connect_and_serve()
                    backoff = _RECONNECT_BASE_S
                except (KeyboardInterrupt, asyncio.CancelledError):
                    break
                except HostConnectError:
                    # Permanent failure (auth / authorization / outdated
                    # server). Do NOT back off and retry — propagate so
                    # ``run_host_process`` can fail loud.
                    raise
                except Exception as exc:
                    if self._lifecycle_lost.is_set():
                        # The lifecycle monitor aborted the tunnel to retire this
                        # stale daemon; the resulting disconnect is expected, so
                        # exit cleanly rather than logging a spurious reconnect.
                        break
                    if not isinstance(exc, InvalidURI):
                        # Any non-redirect failure (5xx bounce, network
                        # blip, mid-serve drop) breaks a login-redirect
                        # streak — _login_redirect_streak counts
                        # CONSECUTIVE redirects only, so a fresh host
                        # riding out a messy restart isn't killed by
                        # redirects accumulated across unrelated errors.
                        self._login_redirect_streak = 0
                    if not (
                        isinstance(exc, InvalidStatus) and exc.response.status_code in (401, 403)
                    ):
                        # Keep the refresh window limited to consecutive auth rejections.
                        self._auth_retry_streak = 0
                    # Refused on loopback is decisive: nothing listens on the
                    # port and no network path can heal it, so bound the
                    # retries. Remote refusals retry forever (outages recover).
                    if _connection_refused(exc) and _url_is_loopback(self._server_url):
                        self._refused_streak += 1
                        if self._refused_streak >= _LOOPBACK_REFUSED_FATAL_ATTEMPTS:
                            _logger.error(
                                "Giving up: %s refused %d consecutive connection "
                                "attempts — no server is listening there anymore. "
                                "Exiting.",
                                self._server_url,
                                self._refused_streak,
                            )
                            raise HostConnectError(
                                f"The server at {self._server_url} refused "
                                f"{self._refused_streak} consecutive connection "
                                "attempts — nothing is listening on that local "
                                "address anymore. Start the server, then run "
                                f"`{cli_invocation()} host` again."
                            ) from exc
                    else:
                        self._refused_streak = 0
                    # An accepted upgrade that died without one inbound frame
                    # is an endpoint that answers the door but never speaks —
                    # functionally down even though every connect "succeeds",
                    # so the recycle classification below would spin at the
                    # prompt cadence forever, silently. Escalate once past a
                    # streak; any received frame resets it.
                    if self._conn_upgrade_accepted and not self._conn_frame_received:
                        self._silent_connect_streak += 1
                        if self._silent_connect_streak == _SILENT_CONNECT_ESCALATE_ATTEMPTS:
                            cause = (
                                f"The server at {self._server_url} accepted "
                                f"{self._silent_connect_streak} consecutive "
                                "connections but never responded on any of them."
                            )
                            _logger.error(
                                "%s Treating the endpoint as unhealthy; "
                                "reconnecting on slow backoff until it responds.",
                                cause,
                            )
                            print(
                                f"⚠ {cause} The server may be unhealthy. "
                                "Retrying on a slower cadence — this recovers "
                                "automatically once the server responds.",
                                file=sys.stderr,
                                flush=True,
                            )
                    else:
                        self._silent_connect_streak = 0
                    # Classify the disconnect to choose a reconnect cadence.
                    #
                    # 1012 "service restart" / 1001 "going away" are explicit
                    # close codes a server (or a graceful Apps recycle) sends —
                    # always a prompt reconnect.
                    #
                    # An abrupt "no close frame" / 502 is, on a REMOTE server,
                    # the Databricks Apps ingress cycling a long-lived WebSocket
                    # out from under a healthy app — also a prompt reconnect, so
                    # the host tunnel isn't down long enough to drop a
                    # launch_runner frame ("runner did not connect").
                    #
                    # But on a LOOPBACK server there is no Apps ingress — an
                    # abrupt drop is a real condition (the server closed our
                    # tunnel, e.g. a re-registration of the same host_id). Fast
                    # 0.5s reconnects there *fuel* a re-registration flap: the
                    # next connect overlaps the previous teardown, the server
                    # drops a duplicate, repeat. Back off normally on loopback
                    # so the overlap window closes and the tunnel settles (and a
                    # genuinely persistent failure surfaces instead of a silent
                    # tight loop).
                    reason = str(exc).lower()
                    explicit_recycle = any(
                        t in reason for t in ("1012", "service restart", "1001", "going away")
                    )
                    ingress_recycle = any(t in reason for t in ("no close frame", "502"))
                    # A silent-connect streak overrides the recycle fast path:
                    # prompt reconnects are for endpoints that answer.
                    silent_churn = self._silent_connect_streak >= _SILENT_CONNECT_ESCALATE_ATTEMPTS
                    # A resume from system suspend (laptop wake) always reconnects
                    # promptly: _on_resume_from_suspend already aborted the dead
                    # tunnel, but the abrupt "no close frame" that abort produces
                    # counts as a benign recycle only on a REMOTE server — a local
                    # server would otherwise ride the escalating backoff. OR woke in
                    # outside the silent-churn gate so wake never takes the slow path.
                    woke = self._woke_from_suspend
                    self._woke_from_suspend = False
                    recycle = woke or (
                        (
                            explicit_recycle
                            or (ingress_recycle and not _url_is_loopback(self._server_url))
                        )
                        and not silent_churn
                    )
                    wait_s = _RECONNECT_BASE_S if recycle else backoff
                    _logger.warning(
                        "Host tunnel disconnected: %s. Reconnecting in %.1fs%s",
                        exc,
                        wait_s,
                        " (resumed from suspend — prompt reconnect)"
                        if woke
                        else (" (recycle — prompt reconnect)" if recycle else ""),
                    )
                    # Interruptible backoff: wake early if the lifecycle
                    # monitor loses ownership mid-backoff so the top-of-loop
                    # check breaks instead of reconnecting one more time.
                    with contextlib.suppress(asyncio.TimeoutError):
                        await asyncio.wait_for(self._lifecycle_lost.wait(), timeout=wait_s)
                    import random

                    if recycle:
                        backoff = _RECONNECT_BASE_S
                    else:
                        backoff = min(
                            backoff * 2 * (1 + random.random() * _RECONNECT_JITTER),
                            _RECONNECT_CAP_S,
                        )
        except (KeyboardInterrupt, asyncio.CancelledError):
            pass
        finally:
            # Await the cancellations: a bare cancel() leaves the tasks
            # pending at loop close ("Task was destroyed but it is pending!").
            if self._reaper_task is not None:
                self._reaper_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._reaper_task
                self._reaper_task = None
            if self._suspend_task is not None:
                self._suspend_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._suspend_task
                self._suspend_task = None
            if self._lifecycle_task is not None:
                self._lifecycle_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._lifecycle_task
                self._lifecycle_task = None
            # Release the flock but leave the record deletion to the CLI stop /
            # takeover paths that own it (this daemon may have been superseded).
            if self._lifecycle_lock is not None:
                self._lifecycle_lock.release()
            if self._zygote_prestart_task is not None:
                self._zygote_prestart_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await self._zygote_prestart_task
                self._zygote_prestart_task = None
            for watcher in list(self._watcher_tasks):
                watcher.cancel()
            for watcher in list(self._watcher_tasks):
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watcher
            self._cleanup_runners()
            # Final drain: _cleanup_runners has just reaped the tracked
            # runners via Popen, so any of their still-orphaned tool
            # grandchildren are now reapable and no tracked pid can be stolen.
            self._reap_orphans_once()
            # Stop the runner zygote last: its forked children were just
            # terminated above, and closing its control socket lets it exit.
            if self._zygote is not None:
                with contextlib.suppress(Exception):
                    self._zygote.stop()
                self._zygote = None

    def _on_resume_from_suspend(self, gap_s: float) -> None:
        """Force-drop the tunnel after a detected wake from system suspend.

        On laptop sleep the WebSocket becomes a half-open socket the server
        already dropped; without this the reconnect loop waits out the ~90s
        keepalive ping timeout (:data:`TUNNEL_KEEPALIVE_PING_TIMEOUT_S`),
        leaving the host — and every session it owns — offline that whole
        time. Aborting the transport makes :meth:`_serve_frames`' ``recv``
        raise ``ConnectionClosed`` now, and the flag makes :meth:`run` skip the
        backoff so the reconnect is prompt.

        No-op when no connection is live (e.g. the wake landed during a
        reconnect backoff): there is nothing to abort, and the pending backoff
        sleep's deadline is already past so it reconnects immediately anyway.
        The flag is only set when a live tunnel was actually aborted, so a
        wake-during-backoff never triggers a spurious prompt reconnect.

        Runs synchronously on the event loop (invoked by the suspend watcher),
        so reading ``self._ws`` and aborting is atomic w.r.t. ``_serve_frames``
        — no lock needed.

        :param gap_s: Approximate seconds the machine was asleep (for logging).
        :returns: None.
        """
        ws = self._ws
        if ws is None:
            return
        self._woke_from_suspend = True
        _logger.info(
            "Resumed from suspend (~%.0fs); dropping stale host tunnel to reconnect",
            gap_s,
        )
        transport = getattr(ws, "transport", None)
        if transport is not None:
            with contextlib.suppress(Exception):
                transport.abort()

    def _cleanup_runners(self) -> None:
        """Terminate all live runners on shutdown.

        :returns: None.
        """
        for runner_id, handle in self._runners.items():
            if handle.proc.poll() is None:
                _logger.info("Terminating runner %s on shutdown", runner_id)
                handle.proc.terminate()
        for handle in self._runners.values():
            try:
                handle.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                handle.proc.kill()
        self._runners.clear()

    async def _connect_and_serve(self) -> None:
        """Single connection attempt: connect, hello, serve.

        :returns: None.
        :raises Exception: On WebSocket disconnect or error.
        """
        # Fresh per-connection markers for the silent-connect streak.
        self._conn_upgrade_accepted = False
        self._conn_frame_received = False
        url = self._tunnel_url()
        headers = self._build_connect_headers()

        _logger.info("Connecting to %s", url)
        # Build a verifying SSL context from a real CA bundle for wss:// — a bare
        # default context loads zero roots on uv / python-build-standalone Pythons
        # (no OpenSSL default cert path), which fails handshake verification.
        # ``ssl=None`` for ws:// is the library default (no TLS).
        ssl_ctx = client_ssl_context() if url.startswith("wss://") else None
        try:
            ws_cm = websockets.asyncio.client.connect(
                url,
                additional_headers=headers,
                max_size=100 * 1024 * 1024,
                ssl=ssl_ctx,
                open_timeout=(
                    _RECONNECT_OPEN_TIMEOUT_S
                    if self._ever_connected
                    else _INITIAL_CONNECT_OPEN_TIMEOUT_S
                ),
                # Align the host->server tunnel's protocol keepalive to the same
                # 90 s app-level budget as the runner tunnel (not the 20 s library
                # default that drops a busy-but-healthy tunnel with 1011 — #1116).
                # Symmetric with serve.py's runner-side connect().
                ping_interval=TUNNEL_KEEPALIVE_PING_INTERVAL_S,
                ping_timeout=TUNNEL_KEEPALIVE_PING_TIMEOUT_S,
            )
            ws = await ws_cm.__aenter__()
        except (InvalidURI, InvalidStatus) as exc:
            # The upgrade itself was rejected. Fail loud on permanent
            # failures (auth / authorization / outdated server); let the
            # reconnect loop retry transient ones.
            fatal = self._fatal_upgrade_error(exc)
            if fatal is not None:
                raise fatal from exc
            raise
        # An accepted upgrade proves the credentials work: login redirects
        # from here on are server restarts, not an unauthenticated host.
        self._ever_connected = True
        self._login_redirect_streak = 0
        self._auth_retry_streak = 0
        self._refused_streak = 0
        self._conn_upgrade_accepted = True
        await self._ensure_owner_user_id()
        try:
            await self._serve_frames(ws)
        finally:
            # Drop the watcher tasks' send target — exit reports raised
            # between connections park in _unreported_exits instead of
            # racing a half-closed socket.
            self._ws = None
            # Close the tunnel context whether the serve loop returned
            # normally or raised (disconnect → reconnect). Mirrors the
            # ``async with`` this replaced; the manual enter is only so the
            # upgrade-time exception can be classified above.
            await ws_cm.__aexit__(*sys.exc_info())

    async def _ensure_owner_user_id(self) -> None:
        """Resolve this host's owning user once and publish it for attribution.

        Best-effort ``GET /v1/me`` (the same call the CLI resume picker uses),
        run after the tunnel upgrade is accepted so credentials are known to
        work. On success, store it on the handler (injected into every runner
        this host spawns) and in ``OMNIGENT_USER_ID`` (so the host's own
        debug-log rows carry it). A single-user server / managed host answers no
        owner, and any failure is swallowed -- attribution must never disrupt the
        host, and those rows simply ship ``user_id = NULL``.
        """
        if self._owner_user_id is not None:
            return
        try:
            from omnigent.resume_dispatch import _resolve_current_user_id

            headers = self._build_connect_headers()
            owner = await asyncio.to_thread(
                _resolve_current_user_id, base_url=self._server_url, headers=headers
            )
        except Exception:  # noqa: BLE001 — attribution is best-effort
            return
        if owner:
            self._owner_user_id = owner
            os.environ[USER_ID_ENV_VAR] = owner

    def _build_connect_headers(self) -> dict[str, str]:
        """Build the WebSocket upgrade headers for the tunnel connection.

        Server-managed sandbox hosts authenticate with the launch token
        the server injected at spawn (:data:`HOST_TOKEN_ENV_VAR`); when
        present it is sent on its dedicated header and the user-token
        path is skipped entirely (a sandbox has no user credentials).

        Otherwise mints a fresh Databricks bearer token via the runner's
        auth factory (refreshed every reconnect so long-lived hosts
        survive token expiry). Token acquisition failures are swallowed —
        the upgrade proceeds unauthenticated and the server/proxy
        decides.

        :returns: Header mapping for the WS upgrade; carries either the
            managed-host token header or — only when a token could be
            minted — ``{"Authorization": "Bearer <token>"}``.
        """
        from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER
        from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN

        # Identify as a first-party client so the server's WebSocket origin
        # guard (CSWSH protection) allows the handshake — the host process
        # is not a browser. Seeded before either auth branch so it is sent
        # on both the managed-token and Bearer paths.
        headers: dict[str, str] = {"Origin": OMNIGENT_INTERNAL_WS_ORIGIN}
        # Workspace routing: the tunnel handshake must name the workspace or
        # it routes to the account. Empty for single-workspace and managed
        # hosts (no recorded selector), so neither is affected.
        from omnigent.cli_auth import databricks_request_headers

        # Pin this host's tunnel to its replica via the host_id; the builder
        # emits the routing header only on a host-sharded deployment.
        headers.update(
            databricks_request_headers(self._server_url, host_id=self._identity.host_id)
        )

        managed_token = os.environ.get(HOST_TOKEN_ENV_VAR)
        if managed_token:
            headers[MANAGED_HOST_TOKEN_HEADER] = managed_token
            return headers
        token = self._current_auth_token()
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    def _current_auth_token(self, *, initialize: bool = True) -> str | None:
        """Return a bearer from the host's retained refreshable auth context.

        The first call builds the same factory the host tunnel already used.
        Later calls reuse its SDK ``Config`` and in-memory token cache, so a
        runner launch normally performs no CLI or network authentication.

        :param initialize: Build the factory when it has not been used yet.
            Runner launch passes ``False`` because it must only reuse the
            already-warm host context, never add auth work to the launch path.
        :returns: Current bearer token, or ``None`` when credentials are not
            available or this is a managed host authenticated by launch token.
        """
        from omnigent.host.identity import HOST_TOKEN_ENV_VAR

        if os.environ.get(HOST_TOKEN_ENV_VAR):
            return None
        try:
            if not self._auth_token_factory_resolved:
                if not initialize:
                    return None
                from omnigent.runner._entry import _make_auth_token_factory

                factory = _make_auth_token_factory(server_url=self._server_url)
                if factory is not None:
                    self._auth_token_factory = factory
                    self._auth_token_factory_resolved = True
            if self._auth_token_factory is not None:
                return self._auth_token_factory()
        except Exception:  # noqa: BLE001
            _logger.debug("Could not obtain auth token", exc_info=True)
        return None

    async def _serve_frames(self, ws: websockets.asyncio.client.ClientConnection) -> None:
        """Send the cached host hello, then service frames until disconnect."""
        _tel_opt_out = False
        try:
            from omnigent.telemetry.client import is_disabled as _tel_disabled

            _tel_opt_out = _tel_disabled()
        except Exception:  # noqa: BLE001 — telemetry errors must not abort hello
            pass
        _tel_install_id: str | None = None
        try:
            from omnigent.telemetry.installation_id import get_installation_id as _get_install_id

            if not _tel_opt_out:
                _tel_install_id = _get_install_id()
        except Exception:  # noqa: BLE001
            pass
        hello = HostHelloFrame(
            version=VERSION,
            frame_protocol_version=1,
            name=self._identity.name,
            runners=self._alive_runner_ids(),
            configured_harnesses=self._configured_harnesses,
            gateway_inference=self._gateway_inference,
            telemetry_opt_out=_tel_opt_out,
            installation_id=_tel_install_id,
        )
        try:
            encoded_hello = encode_host_frame(hello)
        except Exception as exc:
            raise HostConnectError(f"Could not encode host.hello: {exc}") from exc
        await ws.send(encoded_hello)
        self._ws = ws
        # Reports raised while disconnected must wait until registration; the
        # server cannot route them before this connection owns the host.
        for runner_id, error in list(self._unreported_exits.items()):
            del self._unreported_exits[runner_id]
            await self._report_runner_exit(runner_id, error)
        print(
            f"✓ Connected as {self._identity.name!r} "
            f"({self._identity.host_id}), {len(hello.runners)} live runner(s). "
            "Listening for sessions — Ctrl-C to disconnect.",
            flush=True,
        )

        # Readiness refresh runs in its own task, never on this receive loop:
        # a harness probe that blocks (a hung CLI ``--version`` / ``auth
        # status``) must not delay ``ws.recv()`` or the inline keepalive pong
        # the server's watchdog counts as liveness, or it closes the tunnel
        # with ``4003 ping timeout``.
        readiness_task = asyncio.create_task(self._harness_readiness_loop(ws))
        # Warm the pre-launch model listings once a server can actually ask
        # for them, so the first picker open is served from cache instead of
        # waiting on a harness probe. Cache-fresh reconnects are a no-op.
        prewarm_task = asyncio.create_task(
            self._prewarm_model_options(), name="host-model-options-prewarm"
        )
        try:
            while True:
                raw = await ws.recv()
                self._conn_frame_received = True
                if isinstance(raw, str):
                    # Connection-control frames decide whether this receive
                    # loop exits or reconnects, so handle them inline. Ordinary
                    # request frames run concurrently below; exceptions raised
                    # on those detached tasks are intentionally contained.
                    self._raise_connection_error_from_raw(raw)
                    # Each request frame is handled on its own task so a slow
                    # handler (a model-options CLI exec, a long git walk) can't
                    # head-of-line block the frames behind it — measured
                    # 0.3-1.3s of added session-create latency when a launch
                    # or stat queued behind one. Responses correlate by
                    # request_id, so completion order doesn't matter; the one
                    # ordering that does (launch vs stop) is preserved by
                    # _runner_lifecycle_lock in _dispatch_host_frame.
                    self._start_frame_task(ws, raw)
        finally:
            prewarm_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await prewarm_task
            readiness_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await readiness_task

    async def _harness_readiness_loop(
        self,
        ws: websockets.asyncio.client.ClientConnection,
    ) -> None:
        """Refresh advisory capabilities without endangering the tunnel."""
        configured = self._configured_harnesses
        gateway = self._gateway_inference
        loop = asyncio.get_running_loop()
        next_quick = loop.time() + HARNESS_READINESS_REFRESH_INTERVAL_S
        next_full = loop.time() + HARNESS_READINESS_FULL_REFRESH_INTERVAL_S
        while True:
            await asyncio.sleep(max(0.0, min(next_quick, next_full) - loop.time()))
            now = loop.time()
            refresh_full = configured is None or now >= next_full
            if now >= next_quick:
                next_quick = now + HARNESS_READINESS_REFRESH_INTERVAL_S
                if not refresh_full and configured is not None:
                    try:
                        refresh_full = await asyncio.to_thread(
                            _unavailable_harness_became_ready, configured
                        )
                    except Exception:
                        _logger.exception("Host harness quick readiness probe failed")
            if not refresh_full:
                continue

            latest = await self._probe_configured_harnesses(startup=False)
            latest_gateway = await self._probe_gateway_inference(startup=False)
            next_full = now + HARNESS_READINESS_FULL_REFRESH_INTERVAL_S
            new_configured = latest if latest is not None else configured
            new_gateway = latest_gateway if latest_gateway is not None else gateway
            if new_configured is None:
                continue
            if new_configured != configured or new_gateway != gateway:
                await ws.send(
                    encode_host_frame(
                        HostHarnessReadinessFrame(
                            configured_harnesses=new_configured,
                            gateway_inference=new_gateway,
                        )
                    )
                )
                configured = new_configured
                gateway = new_gateway
                self._configured_harnesses = configured
                self._gateway_inference = gateway

    def _raise_connection_error(self, frame: HostConnectionErrorFrame) -> None:
        """Raise the lifecycle exception requested by a server error frame."""
        message = f"Host connection failed during {frame.stage}: {frame.error}"
        if frame.retryable:
            raise HostRetryableConnectionError(message)
        raise HostConnectError(message)

    def _raise_connection_error_from_raw(self, raw: str) -> None:
        """Handle connection-level frames before request-task dispatch."""
        try:
            frame = decode_host_frame(raw)
        except ValueError:
            return
        if isinstance(frame, HostConnectionErrorFrame):
            self._raise_connection_error(frame)

    def _start_frame_task(self, ws: websockets.asyncio.client.ClientConnection, raw: str) -> None:
        """Handle one inbound frame on its own task, off the receive loop.

        :param ws: The open tunnel connection, passed through to the handler.
        :param raw: The raw text frame received off the socket.
        :returns: None.
        """
        task = asyncio.create_task(self._run_frame_handler(ws, raw), name="host-frame")
        self._frame_tasks.add(task)
        task.add_done_callback(self._frame_tasks.discard)

    async def _run_frame_handler(
        self, ws: websockets.asyncio.client.ClientConnection, raw: str
    ) -> None:
        """Run one frame handler, containing its failures.

        A handler failure must not tear down the tunnel — every queued frame
        (and the reconnect churn) would pay for one bad request. The server
        side times out or retries its unanswered request.

        :param ws: The open tunnel connection the handler replies on.
        :param raw: The raw text frame received off the socket.
        :returns: None.
        """
        try:
            await self._handle_raw_message(ws, raw)
        except ConnectionClosed:
            # The tunnel died while this frame was in flight; the reconnect
            # loop owns recovery.
            _logger.debug("dropped frame result: tunnel closed mid-handling")
        except Exception:
            _logger.exception("host frame handler failed")

    async def _handle_raw_message(
        self, ws: websockets.asyncio.client.ClientConnection, raw: str
    ) -> None:
        """Decode one inbound text frame and route it to a handler.

        Host frames go to :meth:`_dispatch_host_frame`; a runner
        ``ping`` is answered with a ``pong`` inline; anything that
        decodes as neither is ignored (forward-compatible with frame
        types this host version doesn't know).

        :param ws: The open tunnel connection, used to send replies.
        :param raw: The raw text frame received off the socket.
        :returns: None.
        """
        try:
            frame = decode_host_frame(raw)
        except ValueError:
            # Not a host frame — it may be a runner ping (the tunnel
            # multiplexes both frame families over one socket).
            try:
                runner_frame = decode_frame(raw)
            except ValueError:
                return
            if isinstance(runner_frame, PingFrame):
                await ws.send(encode_frame(PongFrame(ts=runner_frame.ts)))
            return
        # Handle the frame inside a CONSUMER span parented on the trace
        # context the server stamped into the frame envelope, so the
        # host's work (and the result frame it sends back) nests under
        # the server request that triggered it.
        from omnigent.runtime import telemetry

        try:
            carrier = json.loads(raw)
        except ValueError:
            carrier = {}
        if not isinstance(carrier, dict):
            carrier = {}
        raw_kind = carrier.get("kind")
        kind = raw_kind if isinstance(raw_kind, str) else type(frame).__name__
        with telemetry.consume_frame_span(kind, carrier):
            await self._dispatch_host_frame(ws, frame)

    async def _dispatch_host_frame(
        self,
        ws: websockets.asyncio.client.ClientConnection,
        frame: object,
    ) -> None:
        """Handle a decoded host frame and send its result back.

        :param ws: The open tunnel connection, used to send the result.
        :param frame: A decoded host frame (one of the
            ``Host*Frame`` request types); unrecognized frame types are
            ignored.
        :returns: None.
        """
        if isinstance(frame, HostConnectionErrorFrame):
            # Defensive for direct callers; production handles this inline in
            # _serve_frames so detached request tasks cannot swallow it.
            self._raise_connection_error(frame)
        if isinstance(frame, HostLaunchRunnerFrame):
            # Frames run on concurrent tasks, but launch/stop must keep their
            # arrival order relative to each other (a stop for a session must
            # not overtake the launch it targets). The lock is this task's
            # first await, and tasks start in frame-arrival order, so waiters
            # queue FIFO in that same order — keep it first.
            async with self._runner_lifecycle_lock:
                launch_result = await self._handle_launch(frame)
            await ws.send(encode_host_frame(launch_result))
        elif isinstance(frame, HostStopRunnerFrame):
            async with self._runner_lifecycle_lock:
                stop_result = await self._handle_stop(frame)
            await ws.send(encode_host_frame(stop_result))
        elif isinstance(frame, HostRunnerStatusFrame):
            await ws.send(encode_host_frame(await self._handle_runner_status(frame)))
        elif isinstance(frame, HostStatFrame):
            await ws.send(encode_host_frame(self._handle_stat(frame)))
        elif isinstance(frame, HostListDirFrame):
            await ws.send(encode_host_frame(self._handle_list_dir(frame)))
        elif isinstance(frame, HostCreateDirFrame):
            await ws.send(encode_host_frame(self._handle_create_dir(frame)))
        elif isinstance(frame, HostInstallHarnessFrame):
            # The installer shells out (npm) and can run for minutes, so run
            # it off the event loop and reply when it completes.
            install_result = await asyncio.to_thread(self._handle_install_harness, frame)
            await ws.send(encode_host_frame(install_result))
        elif isinstance(frame, HostStoreSecretFrame):
            # The credential write touches the OS keychain / config file, so run
            # it off the event loop and reply when it completes.
            secret_result = await asyncio.to_thread(self._handle_store_secret, frame)
            await ws.send(encode_host_frame(secret_result))
        elif isinstance(frame, HostDetectCredentialsFrame):
            # Ambient detection may probe files / a localhost socket, so run it
            # off the event loop.
            credentials_result = await asyncio.to_thread(self._handle_detect_credentials, frame)
            await ws.send(encode_host_frame(credentials_result))
        elif isinstance(frame, HostCreateWorktreeFrame):
            await ws.send(encode_host_frame(await self._handle_create_worktree(frame)))
        elif isinstance(frame, HostRemoveWorktreeFrame):
            await ws.send(encode_host_frame(await self._handle_remove_worktree(frame)))
        elif isinstance(frame, HostListWorktreesFrame):
            await ws.send(encode_host_frame(await self._handle_list_worktrees(frame)))
        elif isinstance(frame, HostFsRequestFrame):
            # Git status and directory walks can block, so run the read
            # off the event loop and reply when it completes.
            fs_result = await asyncio.to_thread(self._handle_fs_request, frame)
            await ws.send(encode_host_frame(fs_result))
        elif isinstance(frame, HostModelOptionsFrame):
            # Every dispatched frame already runs on its own task (see
            # _start_frame_task), so a cold harness probe here cannot stall
            # the receive loop — answer inline, with a crash converted to an
            # honest failed frame so the server's request future settles.
            try:
                options_result = await self._handle_model_options(frame)
            except Exception:
                _logger.exception("Model options resolution crashed for %r", frame.harness)
                options_result = HostModelOptionsResultFrame(
                    request_id=frame.request_id,
                    status="failed",
                    error=f"model options resolution crashed for {frame.harness!r}",
                )
            await ws.send(encode_host_frame(options_result))
        elif isinstance(frame, HostImportLocalFrame):
            # Streams one host.import_local_session per session (reads run off the
            # event loop inside), then a terminal host.import_local_done.
            await self._handle_import_local(ws, frame)


def run_host_process(
    server_url: str,
    config_path: Path | None = None,
    *,
    daemon_target: str | None = None,
) -> None:
    """Entry point for ``omnigent host``.

    Loads (or creates) the host identity from the ``host`` section
    of ``~/.omnigent/config.yaml``, then runs the host process.

    :param server_url: Server URL to connect to, e.g.
        ``"https://omnigent-app.databricksapps.com"``.
    :param config_path: Optional path to ``config.yaml``.
        Defaults to ``~/.omnigent/config.yaml``.
    :param daemon_target: Normalized registry target this process owns, e.g.
        ``"local"`` or a server URL. When given, the daemon binds its lifetime
        to that record (flock + self-terminate on delete/reassign). ``None``
        (the historical default) runs without the lifecycle guard.
    :raises SystemExit: With :data:`HOST_FATAL_EXIT_CODE` when the tunnel
        fails permanently (auth / authorization / outdated server, or a
        loopback server that is gone). The actionable cause is printed
        to stderr first.
    """
    host_log_path = configure_process_logging("host")
    # Initialize tracing so the host daemon exports its own spans
    # (e.g. handling launch_runner / stat / list_dir frames) into the
    # same distributed trace as the server that requested them. The
    # daemon inherits OTEL_*/MLFLOW_* config from the launching CLI.
    from omnigent.runtime import telemetry

    telemetry.init("omni-host")

    from omnigent.host.identity import CONFIG_PATH

    path = config_path or CONFIG_PATH
    identity = load_or_create_host_identity(path)
    if not path.exists():
        print(f"Auto-generated {path} ({identity.host_id}, name: {identity.name})")
    # User-facing: the display form (workspace /omnigent URL with ?o= when
    # known) — the API mount is an implementation detail.
    from omnigent.server_url import display_server_url

    print(
        f"Connecting to {display_server_url(server_url)} as {identity.name!r} ({identity.host_id})"
    )
    # Tell the user where logs land up front — `omnigent host` used to run
    # silently, so a stuck/quiet host gave no hint where to look. Session
    # work goes to per-runner files under the runner dir (the exact
    # file is printed when each runner launches). The host process's
    # own diagnostics go to the host destination.
    print(f"Session logs: {display_log_path(_runner_log_dir())}/")
    print(f"This host's log: {display_log_path(host_log_path)}")
    from omnigent.cli_diagnostics import current_cli_log_path

    _cli_log = current_cli_log_path()
    if _cli_log is not None and _cli_log != host_log_path:
        print(f"CLI diagnostics: {display_log_path(_cli_log)}")

    lifecycle_lock = (
        DaemonLifecycleLock.for_target(daemon_target) if daemon_target is not None else None
    )
    host = HostProcess(identity, server_url, lifecycle_lock=lifecycle_lock)
    try:
        asyncio.run(host.run())
    except HostConnectError as exc:
        # Fail loud: a permanent connection failure must not look like the
        # process is still working. Print the cause + fix, then exit non-zero
        # instead of the old behavior of reconnecting silently forever.
        # The dedicated code (not a bare 1) tells a supervisor this can never
        # succeed, so it stops retrying instead of looping on a bad credential.
        print(
            f"\n✗ Could not connect to {display_server_url(server_url)}.\n{exc}",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(HOST_FATAL_EXIT_CODE) from exc
