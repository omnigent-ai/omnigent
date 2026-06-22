"""Stable runner identity helpers."""

from __future__ import annotations

import hashlib
import os
import signal
import uuid
from collections.abc import Mapping
from pathlib import Path

RUNNER_ID_ENV_VAR = "OMNIGENT_RUNNER_ID"
RUNNER_PARENT_PID_ENV_VAR = "OMNIGENT_RUNNER_PARENT_PID"
# Signal the CLI sends to "adopt" a runner: stop watching the parent
# pid so the runner survives an intentional CLI exit (tmux detach) and
# keeps serving the web UI. SIGUSR1 is unused elsewhere in the runner.
# Some platforms (notably native Windows) do not define SIGUSR1; keep
# imports working there and let callers skip adopt signaling.
RUNNER_ADOPT_SIGNAL: signal.Signals | None = getattr(signal, "SIGUSR1", None)
RUNNER_WORKSPACE_ENV_VAR = "OMNIGENT_RUNNER_WORKSPACE"
RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR = "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN"
RUNNER_TUNNEL_TOKEN_HEADER = "X-Omnigent-Runner-Tunnel-Token"
# Sentinel ``Origin`` header that the project's own non-browser WebSocket
# clients (runner -> server tunnel, host/daemon -> server tunnel,
# terminal-attach) set on their handshakes so the server's CSWSH origin
# guard allows them. Lives here, alongside the tunnel token header,
# because it is part of the same client/server handshake contract and the
# server imports it from this module (server -> runner, not the reverse).
# The non-HTTP scheme is deliberate: a browser computes ``Origin`` from
# the page URL and can never emit this value.
OMNIGENT_INTERNAL_WS_ORIGIN = "omnigent://internal"
# "1" enables per-session workspace isolation so each session
# gets its own subdirectory. Set by shared-host servers; single-user
# CLI flows leave it unset (agent sees the project root directly).
RUNNER_ISOLATE_SESSION_ENV_VAR = "OMNIGENT_RUNNER_ISOLATE_SESSION"

# Marker env var stamped into every agent-facing environment so any
# process launched inside an Omnigent agent session can detect it is
# running under Omnigent. This is the analog of Claude Code's
# ``CLAUDE_CODE`` / ``CLAUDECODE`` and Codex's ``CODEX``. It is set once
# on the runner process (see :mod:`omnigent.runner._entry`) and inherited
# by harness workers, terminals, and the in-process SDK harnesses. The
# deny-by-default env scrubbers (os_env sandbox, codex CLI, pi CLI) name
# it in their passthrough allowlists so this one marker survives the
# scrub.
OMNIGENT_SESSION_ENV_VAR = "OMNIGENT"
OMNIGENT_SESSION_ENV_VALUE = "1"

# Env vars carrying the runner's control-plane auth secret. The tunnel
# binding token is seeded into the runner process by the launcher and
# reused as the runner-side request auth token, but must never reach a
# spawned child: the agent payload there could use it to impersonate the
# runner. Stripped at every runner→child spawn boundary via
# :func:`strip_runner_auth_secrets`.
RUNNER_AUTH_SECRET_ENV_VARS: frozenset[str] = frozenset({RUNNER_TUNNEL_BINDING_TOKEN_ENV_VAR})


def strip_runner_auth_secrets(env: Mapping[str, str]) -> dict[str, str]:
    """Return a copy of *env* with runner-auth secrets removed.

    Applied at every boundary where the runner spawns a child it does
    not fully trust with its control-plane credentials — harness
    subprocesses and sandboxed tool targets. See
    :data:`RUNNER_AUTH_SECRET_ENV_VARS` for the rationale.

    :param env: Source environment to filter, e.g. ``os.environ`` or a
        merged spawn env dict. Read-only — not mutated.
    :returns: A new dict with every name in
        :data:`RUNNER_AUTH_SECRET_ENV_VARS` removed; all other entries
        preserved unchanged.
    """
    return {key: value for key, value in env.items() if key not in RUNNER_AUTH_SECRET_ENV_VARS}


def get_stable_runner_id() -> str:
    """Return the stable runner id for this local machine.

    Parent processes may set :data:`RUNNER_ID_ENV_VAR` when they
    need child server and runner processes to agree on one id before
    either process touches the on-disk cache. Without that override,
    the id is loaded from ``~/.omnigent/runners/runner_id`` or
    created there on first use.

    :returns: A stable runner id, e.g.
        ``"runner_0123456789abcdef"``.
    :raises RuntimeError: If :data:`RUNNER_ID_ENV_VAR` is set to an
        empty value.
    """
    env_runner_id = os.environ.get(RUNNER_ID_ENV_VAR)
    if env_runner_id is not None:
        runner_id = env_runner_id.strip()
        if not runner_id:
            raise RuntimeError(f"{RUNNER_ID_ENV_VAR} must not be empty")
        return runner_id
    return load_or_create_runner_id(_default_runner_id_path())


def token_bound_runner_id(token: str) -> str:
    """Return the runner id authorized by a tunnel binding token.

    Remote ``run --server`` sessions sit behind an auth proxy.
    Binding the tunnel runner id to a per-run random token prevents
    one authenticated caller from claiming another caller's runner id
    on the same shared server.

    :param token: Secret tunnel binding token, e.g.
        ``"uA6Zz..."``.
    :returns: Deterministic runner id for this token.
    :raises RuntimeError: If *token* is empty.
    """
    stripped = token.strip()
    if not stripped:
        raise RuntimeError("tunnel binding token must not be empty")
    digest = hashlib.sha256(f"omnigent-runner:{stripped}".encode()).hexdigest()
    return f"runner_token_{digest[:32]}"


def load_or_create_runner_id(path: Path) -> str:
    """Load a runner id from *path*, creating one if needed.

    :param path: Path to the runner id cache file, e.g.
        ``Path.home() / ".omnigent" / "runners" / "runner_id"``.
    :returns: The cached or newly-created runner id.
    :raises RuntimeError: If the cache file exists but is empty.
    """
    if path.exists():
        runner_id = path.read_text().strip()
        if not runner_id:
            raise RuntimeError(f"runner id file is empty: {path}")
        return runner_id
    path.parent.mkdir(parents=True, exist_ok=True)
    runner_id = f"runner_{uuid.uuid4().hex}"
    path.write_text(runner_id)
    return runner_id


def _default_runner_id_path() -> Path:
    """Return the default runner id cache path.

    :returns: ``~/.omnigent/runners/runner_id``.
    """
    return Path.home() / ".omnigent" / "runners" / "runner_id"
