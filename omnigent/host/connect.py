"""Host process main loop for ``omnigent host``.

Connects to the server via WebSocket, registers as a host, and
listens for ``host.launch_runner`` / ``host.stop_runner`` frames.
Spawns runner subprocesses on demand and reports results back to
the server.
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import websockets.asyncio.client
from websockets.exceptions import InvalidStatus, InvalidURI

from omnigent._platform import WINDOWS_ENV_PASSTHROUGH
from omnigent.host.frames import (
    HARNESS_NOT_CONFIGURED_ERROR_CODE,
    HostCreateDirFrame,
    HostCreateDirResultFrame,
    HostCreateWorktreeFrame,
    HostCreateWorktreeResultFrame,
    HostHelloFrame,
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostListDirEntry,
    HostListDirFrame,
    HostListDirResultFrame,
    HostRemoveWorktreeFrame,
    HostRemoveWorktreeResultFrame,
    HostRunnerExitedFrame,
    HostStatFrame,
    HostStatResultFrame,
    HostStopRunnerFrame,
    HostStopRunnerResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.host.git_worktree import (
    WorktreeError,
    create_worktree,
    remove_worktree,
)
from omnigent.host.identity import HostIdentity, load_or_create_host_identity
from omnigent.onboarding.harness_install import harness_setup_hint
from omnigent.onboarding.harness_readiness import (
    configured_harness_map,
    harness_is_configured,
)
from omnigent.runner.identity import (
    RUNNER_ID_ENV_VAR,
    RUNNER_PARENT_PID_ENV_VAR,
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

_logger = logging.getLogger(__name__)


def _runner_log_dir() -> Path:
    """Return the directory holding per-session runner logs for this host.

    Each ``host.launch_runner`` writes its runner subprocess's captured
    stdout/stderr to a ``runner-*.log`` file here. Computed at call time
    (not a module constant) so tests that repoint ``Path.home`` see the
    override.

    :returns: The host-runner log directory, e.g.
        ``Path.home() / ".omnigent" / "logs" / "host-runner"``.
    """
    return Path.home() / ".omnigent" / "logs" / "host-runner"


def _display_log_path(path: Path) -> str:
    """Format a log path for display, collapsing the home prefix to ``~``.

    :param path: Absolute path, typically under the user's state dir, e.g.
        ``Path("/Users/alice/.omnigent/logs/host-runner/runner-ab12.log")``.
    :returns: ``"~/.omnigent/..."`` when *path* is under ``$HOME``,
        otherwise ``str(path)``.
    """
    try:
        return f"~/{path.relative_to(Path.home())}"
    except ValueError:
        # Not under $HOME (e.g. an OMNIGENT_DATA_DIR outside home).
        return str(path)


# Max bytes read from the end of a dead runner's log when composing an
# exit report. 4 KiB is roughly the last 40-60 lines — enough to carry
# a Python traceback or the tunnel rejection message.
_LOG_TAIL_MAX_BYTES = 4096

# Max log-tail lines included in a runner exit report. The report ends
# up verbatim in a CLI error message, so it must stay short enough that
# the error summary above it remains visible.
_LOG_TAIL_MAX_LINES = 15

# Poll cadence for the per-runner exit watcher. 0.5s matches the
# client's online-poll cadence (daemon_launch.DAEMON_POLL_INTERVAL_S),
# so a crashed runner is reported within about one client poll.
_RUNNER_WATCH_INTERVAL_S = 0.5


def _read_log_tail(path: Path, max_bytes: int = _LOG_TAIL_MAX_BYTES) -> str:
    """Read the last portion of a runner log file for diagnostics.

    :param path: The runner's captured stdout/stderr log file, e.g.
        ``Path("~/.omnigent/logs/host-runner/runner-ab12.log")``.
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
    message += f" (log on host: {_display_log_path(log_path)})"
    tail = _read_log_tail(log_path)
    if tail.strip():
        lines = tail.strip().splitlines()[-_LOG_TAIL_MAX_LINES:]
        message += "\n--- runner log tail ---\n" + "\n".join(lines)
    return message


def _url_is_loopback(url: str) -> bool:
    """Whether ``url``'s host is loopback (``127.0.0.1`` / ``localhost`` / ``::1``).

    Used to distinguish a daemon-spawned local server (no proxy in
    front) from a remote deploy behind the Databricks Apps ingress, so
    the reconnect heuristic only treats an abrupt ``no close frame`` as
    a benign ingress recycle when there actually IS an ingress.

    :param url: A server or ws:// URL, e.g. ``"ws://127.0.0.1:49175"``.
    :returns: ``True`` for a loopback host, ``False`` otherwise (incl.
        unparseable URLs — fail toward "remote", the safer default for
        the recycle heuristic).
    """
    from urllib.parse import urlparse

    try:
        return urlparse(url).hostname in ("127.0.0.1", "localhost", "::1")
    except ValueError:
        return False


_RECONNECT_BASE_S = 0.5
_RECONNECT_CAP_S = 10.0
_RECONNECT_JITTER = 0.5

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
        # Not a secret. OMNIGENT_ACCOUNTS_ENABLED is the deprecated pre-rename
        # alias, still propagated so existing setups keep working.
        "OMNIGENT_AUTH_ENABLED",
        "OMNIGENT_ACCOUNTS_ENABLED",
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
        # Kubernetes config path. A filesystem path (typically
        # ``~/.kube/config``), not a bearer secret — the file *contains*
        # cluster certs/tokens but the env var is just a path string,
        # analogous to ``HOME``. Without it, ``kubectl`` / helm / k9s
        # inside the agent's shell fall back to the default path which may
        # not match what the host owner configured (e.g. a non-standard
        # kubeconfig location or a colon-separated multi-file list).
        "KUBECONFIG",
    }
    # Windows system / profile constants (SYSTEMROOT is mandatory for Winsock,
    # USERPROFILE for Path.home(), etc.); a no-op on POSIX. See _platform.
    | set(WINDOWS_ENV_PASSTHROUGH)
)
# Locale family (``LC_ALL``, ``LC_CTYPE``, …) — allowed by prefix.
_RUNNER_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_", "MLFLOW_", "OTEL_")

# Harness credential / endpoint env vars forwarded host→runner when
# present. These are the names the harnesses themselves resolve —
# ANTHROPIC_* for claude-sdk / pi (claude-code also honors
# ANTHROPIC_AUTH_TOKEN + ANTHROPIC_BASE_URL for gateways,
# AWS_BEARER_TOKEN_BEDROCK + ANTHROPIC_BEDROCK_BASE_URL for Bedrock mode,
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
HARNESS_CREDENTIAL_ENV_VARS: frozenset[str] = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
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

    Raised when the WebSocket upgrade fails in a way that reconnecting
    can never fix — the Databricks Apps proxy bounced the connection to
    a login page (wrong/absent workspace credentials), or the server
    returned a permanent ``4xx`` (unauthenticated, unauthorized, or a
    build that predates the host API). The reconnect loop re-raises this
    instead of backing off, so ``omnigent host`` exits with an
    actionable message rather than looping silently forever.

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
    :returns: The runner subprocess environment.
    """
    extra_names = {
        name.strip()
        for name in base_env.get(RUNNER_ENV_PASSTHROUGH_ENV_VAR, "").split(",")
        if name.strip()
    }
    forwarded = HARNESS_CREDENTIAL_ENV_VARS | extra_names
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
    env[RUNNER_WORKSPACE_ENV_VAR] = workspace
    env[RUNNER_PARENT_PID_ENV_VAR] = str(parent_pid)
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


@dataclass
class _RunnerHandle:
    """A spawned runner subprocess and where its output lands.

    :param proc: The runner subprocess handle.
    :param log_path: File capturing the runner's stdout/stderr, e.g.
        ``Path("~/.omnigent/logs/host-runner/runner-ab12.log")``.
        Read back for diagnostics when the runner dies before
        connecting its tunnel.
    """

    proc: subprocess.Popen[bytes]
    log_path: Path


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
    ) -> None:
        """Initialize the host process.

        :param identity: Host identity from ``config.yaml``.
        :param server_url: Server URL to connect to.
        """
        self._identity = identity
        self._server_url = server_url.rstrip("/")
        self._runners: dict[str, _RunnerHandle] = {}
        # Set on the first accepted WS upgrade. Distinguishes a host that
        # never authenticated (login redirects turn fatal after
        # _LOGIN_REDIRECT_FATAL_ATTEMPTS) from a live host hit by a server
        # restart (login redirects retry forever).
        self._ever_connected = False
        # Consecutive login-page redirects; reset by a successful upgrade.
        self._login_redirect_streak = 0
        # Live tunnel connection, set by _serve_frames for the watcher
        # tasks (which outlive any single connection) to report on.
        self._ws: websockets.asyncio.client.ClientConnection | None = None
        # runner_id → composed error for exits that could not be sent
        # (tunnel down at the time). Flushed after the next hello.
        self._unreported_exits: dict[str, str] = {}
        # Strong refs to per-runner watcher tasks; asyncio only keeps
        # weak refs, so an unreferenced task can be GC'd mid-flight.
        self._watcher_tasks: set[asyncio.Task[None]] = set()

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

    def _credentials_fix_hint(self) -> str:
        """Build the remedy for a credential failure.

        Shared by the login-redirect and HTTP 401 messages.

        :returns: An actionable remedy sentence naming the exact
            command, e.g. ``"Run `omnigent login <url>` ..."``.
        """
        return (
            f"Run `omnigent login {self._server_url}` to authenticate (it "
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
            f"`omnigent login {self._server_url}` to authenticate."
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
        if status == 401:
            return HostConnectError(
                "Authentication failed (HTTP 401): the server rejected the "
                "supplied credentials. "
                + self._credentials_fix_hint()
                + " "
                + self._login_fix_hint()
            )
        if status == 403:
            return HostConnectError(
                "Connection refused (HTTP 403): the credentials authenticated, "
                "but the server did not accept the host tunnel. Either your "
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
            )

        runner_id = token_bound_runner_id(frame.binding_token)
        env = _build_runner_env(
            os.environ,
            server_url=self._server_url,
            runner_id=runner_id,
            binding_token=frame.binding_token,
            workspace=str(workspace),
            parent_pid=os.getpid(),
        )

        try:
            log_dir = _runner_log_dir()
            log_dir.mkdir(parents=True, exist_ok=True)
            import tempfile

            _log_fd, _log_name = tempfile.mkstemp(
                prefix="runner-",
                suffix=".log",
                dir=log_dir,
            )
            _log_fh = os.fdopen(_log_fd, "wb")
            proc = subprocess.Popen(
                [sys.executable, "-m", "omnigent.runner._entry"],
                env=env,
                # Runners are WS-tunnel clients with no interactive input.
                # Give them a clean /dev/null stdin instead of inheriting the
                # daemon's: a long-lived daemon (e.g. backgrounded / nohup'd)
                # can end up with a closed or recycled stdin fd, and an
                # inherited bad fd makes the runner die at interpreter startup
                # with "init_sys_streams: Bad file descriptor" — it never
                # connects, so the session fails with "runner did not connect".
                stdin=subprocess.DEVNULL,
                stdout=_log_fh,
                stderr=_log_fh,
            )
            _log_fh.close()
        except OSError as exc:
            return HostLaunchRunnerResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=f"failed to spawn runner: {exc}",
            )

        log_path = Path(_log_name)
        if proc.poll() is not None:
            # The runner died before Popen returned — its actual error
            # is in the captured log, so ship the tail with the result
            # instead of making the user go find the file on the host.
            return HostLaunchRunnerResultFrame(
                request_id=frame.request_id,
                status="failed",
                error=_runner_exit_error(proc.returncode, log_path),
            )

        self._runners[runner_id] = _RunnerHandle(proc=proc, log_path=log_path)
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
        print(
            f"  ↑ Runner started: {runner_id} (pid={proc.pid})\n"
            f"    log: {_display_log_path(log_path)}",
            flush=True,
        )
        return HostLaunchRunnerResultFrame(
            request_id=frame.request_id,
            status="launched",
            runner_id=runner_id,
        )

    def _handle_stop(
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
        if handle.proc.poll() is None:
            handle.proc.terminate()
            try:
                handle.proc.wait(timeout=5.0)
            except subprocess.TimeoutExpired:
                handle.proc.kill()
                handle.proc.wait()
        _logger.info("Stopped runner %s", frame.runner_id)
        print(
            f"  ↓ Runner stopped: {frame.runner_id}",
            flush=True,
        )
        return HostStopRunnerResultFrame(
            request_id=frame.request_id,
            status="stopped",
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
        :returns: None. Returns silently for intentional stops.
        """
        handle = self._runners.get(runner_id)
        if handle is None:  # pragma: no cover — spawned just before us
            return
        while handle.proc.poll() is None:
            await asyncio.sleep(_RUNNER_WATCH_INTERVAL_S)
        if self._runners.get(runner_id) is not handle:
            # _handle_stop (or _cleanup_runners) removed it first —
            # an intentional termination, not a crash to report.
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

    async def run(self) -> None:
        """Run the host process with reconnection.

        Connects to the server, sends hello, and enters the
        receive loop. Reconnects with exponential backoff on
        disconnect. Ctrl-C / SIGTERM exit cleanly.

        :returns: None. Runs until the process is terminated.
        """
        backoff = _RECONNECT_BASE_S
        try:
            while True:
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
                except Exception as exc:  # noqa: BLE001 — reconnect loop
                    if not isinstance(exc, InvalidURI):
                        # Any non-redirect failure (5xx bounce, network
                        # blip, mid-serve drop) breaks a login-redirect
                        # streak — _login_redirect_streak counts
                        # CONSECUTIVE redirects only, so a fresh host
                        # riding out a messy restart isn't killed by
                        # redirects accumulated across unrelated errors.
                        self._login_redirect_streak = 0
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
                    recycle = explicit_recycle or (
                        ingress_recycle and not _url_is_loopback(self._server_url)
                    )
                    wait_s = _RECONNECT_BASE_S if recycle else backoff
                    _logger.warning(
                        "Host tunnel disconnected: %s. Reconnecting in %.1fs%s",
                        exc,
                        wait_s,
                        " (recycle — prompt reconnect)" if recycle else "",
                    )
                    await asyncio.sleep(wait_s)
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
            self._cleanup_runners()

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
        url = self._tunnel_url()
        headers = self._build_connect_headers()

        _logger.info("Connecting to %s", url)
        try:
            ws_cm = websockets.asyncio.client.connect(
                url,
                additional_headers=headers,
                max_size=100 * 1024 * 1024,
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

        managed_token = os.environ.get(HOST_TOKEN_ENV_VAR)
        if managed_token:
            headers[MANAGED_HOST_TOKEN_HEADER] = managed_token
            return headers
        try:
            from omnigent.runner._entry import _make_auth_token_factory

            # Pass server_url explicitly. The factory's OIDC-token path
            # would otherwise look up ``RUNNER_SERVER_URL`` from env,
            # which only the runner subprocess sets — without it the
            # stored ``omnigent login`` token is silently skipped and
            # the factory falls through to the Databricks path.
            factory = _make_auth_token_factory(server_url=self._server_url)
            token = factory() if factory else None
            if token:
                headers["Authorization"] = f"Bearer {token}"
        except Exception:  # noqa: BLE001
            _logger.debug("Could not obtain auth token", exc_info=True)
        return headers

    async def _serve_frames(self, ws: websockets.asyncio.client.ClientConnection) -> None:
        """Announce readiness, then service host frames until disconnect.

        Sends the ``host.hello`` frame, prints the success banner, then
        loops dispatching launch/stop/stat/list_dir/worktree requests and
        answering runner pings until the connection closes.

        :param ws: The open tunnel connection returned by the websockets
            client.
        :returns: None. Returns when the receive loop is broken.
        :raises Exception: On WebSocket disconnect or error — propagated
            to the reconnect loop in :meth:`run`.
        """
        hello = HostHelloFrame(
            version="0.1.0",
            frame_protocol_version=1,
            name=self._identity.name,
            runners=self._alive_runner_ids(),
            # Off the event loop: probes PATH (shutil.which) and reads
            # ~/.omnigent/config.yaml. Recomputed on every (re)connect, so
            # the server's view refreshes whenever the tunnel does; the
            # launch-time check above stays the authoritative gate.
            configured_harnesses=await asyncio.to_thread(configured_harness_map),
        )
        await ws.send(encode_host_frame(hello))
        self._ws = ws
        # Flush exit reports that raced a disconnect: a runner that died
        # while the tunnel was down would otherwise never be reported and
        # the waiting client would poll to its timeout.
        for runner_id, error in list(self._unreported_exits.items()):
            del self._unreported_exits[runner_id]
            await self._report_runner_exit(runner_id, error)
        # ``print`` (not ``_logger.warning``) so the user always sees the
        # success line after the noisy ``databricks.sdk`` warnings —
        # otherwise the terminal goes silent after auth and there's no
        # signal the WS handshake actually completed.
        print(
            f"✓ Connected as {self._identity.name!r} "
            f"({self._identity.host_id}), {len(hello.runners)} live runner(s). "
            "Listening for sessions — Ctrl-C to disconnect.",
            flush=True,
        )

        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60.0)
            except asyncio.TimeoutError:
                continue
            if isinstance(raw, str):
                await self._handle_raw_message(ws, raw)

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
        if isinstance(frame, HostLaunchRunnerFrame):
            await ws.send(encode_host_frame(await self._handle_launch(frame)))
        elif isinstance(frame, HostStopRunnerFrame):
            await ws.send(encode_host_frame(self._handle_stop(frame)))
        elif isinstance(frame, HostStatFrame):
            await ws.send(encode_host_frame(self._handle_stat(frame)))
        elif isinstance(frame, HostListDirFrame):
            await ws.send(encode_host_frame(self._handle_list_dir(frame)))
        elif isinstance(frame, HostCreateDirFrame):
            await ws.send(encode_host_frame(self._handle_create_dir(frame)))
        elif isinstance(frame, HostCreateWorktreeFrame):
            await ws.send(encode_host_frame(await self._handle_create_worktree(frame)))
        elif isinstance(frame, HostRemoveWorktreeFrame):
            await ws.send(encode_host_frame(await self._handle_remove_worktree(frame)))


def run_host_process(
    server_url: str,
    config_path: Path | None = None,
) -> None:
    """Entry point for ``omnigent host``.

    Loads (or creates) the host identity from the ``host`` section
    of ``~/.omnigent/config.yaml``, then runs the host process.

    :param server_url: Server URL to connect to, e.g.
        ``"https://omnigent-app.databricksapps.com"``.
    :param config_path: Optional path to ``config.yaml``.
        Defaults to ``~/.omnigent/config.yaml``.
    :raises SystemExit: With code 1 when the tunnel fails permanently
        (auth / authorization / outdated server). The
        actionable cause is printed to stderr first.
    """
    from omnigent.host.identity import CONFIG_PATH

    path = config_path or CONFIG_PATH
    identity = load_or_create_host_identity(path)
    if not path.exists():
        print(f"Auto-generated {path} ({identity.host_id}, name: {identity.name})")
    print(f"Connecting to {server_url} as {identity.name!r} ({identity.host_id})")
    # Tell the user where logs land up front — `omnigent host` used to run
    # silently, so a stuck/quiet host gave no hint where to look. Session
    # work goes to per-runner files under the host-runner dir (the exact
    # file is printed when each runner launches). The foreground process's
    # own diagnostics (warnings, tracebacks) go to the always-on cli-*.log;
    # that path is None in the background daemon (no setup_cli_logging) —
    # its stdout is already captured to the daemon log, so skip the line.
    print(f"Session logs: {_display_log_path(_runner_log_dir())}/")
    from omnigent.cli_diagnostics import current_cli_log_path

    _cli_log = current_cli_log_path()
    if _cli_log is not None:
        print(f"This host's log: {_display_log_path(_cli_log)}")

    host = HostProcess(identity, server_url)
    try:
        asyncio.run(host.run())
    except HostConnectError as exc:
        # Fail loud: a permanent connection failure must not look like the
        # process is still working. Print the cause + fix, then exit non-zero
        # instead of the old behavior of reconnecting silently forever.
        print(f"\n✗ Could not connect to {server_url}.\n{exc}", file=sys.stderr, flush=True)
        raise SystemExit(1) from exc
