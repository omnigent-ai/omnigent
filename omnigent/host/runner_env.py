"""Env-var allowlist for host-spawned runner processes.

A deliberately tiny leaf module: both the host daemon
(:mod:`omnigent.host.connect`, which builds a runner's environment) and the CLI
helper that builds the host daemon's own environment need these two constants,
and the CLI must not pay for the whole host-connect import tree to read them.
Depends only on the import-cheap :mod:`omnigent._platform` /
:mod:`omnigent.process_logging` leaves.
"""

from __future__ import annotations

from omnigent._platform import WINDOWS_ENV_PASSTHROUGH
from omnigent.process_logging import LOG_TTY_FD_ENV_VAR

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
        # (Literal, not omnigent.host.connect.RUNNER_ENV_PASSTHROUGH_ENV_VAR,
        # which would be a circular import back into the host daemon.)
        "OMNIGENT_RUNNER_ENV_PASSTHROUGH",
        # Keep host and spawned-runner routing decisions aligned when the
        # host-slice-key kill switch is explicitly disabled.
        "OMNIGENT_HOST_SLICE_KEY_ENABLED",
    }
    # Windows system / profile constants (SYSTEMROOT is mandatory for Winsock,
    # USERPROFILE for Path.home(), etc.); a no-op on POSIX. See _platform.
    | set(WINDOWS_ENV_PASSTHROUGH)
)
# Allowed by prefix: locale family (``LC_*``), MLflow, and OpenTelemetry config —
# both the standard ``OTEL_*`` vars and Omnigent's ``OMNIGENT_OTEL_*`` knobs
# (capture-content, FastAPI toggle) so they reach the runner/harness too.
_RUNNER_ENV_ALLOWLIST_PREFIXES: tuple[str, ...] = ("LC_", "MLFLOW_", "OTEL_", "OMNIGENT_OTEL_")
