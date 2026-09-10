"""Point a managed sandbox's jcode gateway at the owner's Databricks model-serving endpoint.

Called by ``omnigent host`` at startup when the owner has linked a Databricks workspace
via the OAuth U2M connect flow. Mirrors the existing ucode/claude/codex/pi connect-broker
pattern (see :mod:`omnigent.host.databricks_credential`): configures jcode's openai-compatible
provider to route inference through the owner's workspace gateway, using a fresh bearer
token minted on demand (never persisted).

When the managed-connect signals are absent (no ``[omnigent]`` profile + broker sidecar),
or jcode is not installed, this is a complete no-op — laptops and non-connected sandboxes
are unaffected.

The configured provider id is ``"dbx"``; jcode reads ``~/.jcode/config.toml`` and runs
its daemon per-session (a unique ``JCODE_RUNTIME_DIR`` per spawn), so the fresh bearer
and model are loaded at startup and never persisted.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import subprocess
import tempfile
import threading
from pathlib import Path

import tomllib

from omnigent.host.databricks_credential import (
    HOST_DATABRICKS_PROFILE,
    _read_sidecar,
    _sidecar_path,
    broker_token_command,
    fetch_broker_bearer,
    https_url_on_workspace_host,
)
from omnigent.inner.databricks_executor import _read_databrickscfg_host

_logger = logging.getLogger(__name__)

# Sandbox configure is best-effort and off the host's dial-back path; bound it so
# a hung jcode can't leak a thread for the life of the host.
_SANDBOX_CONFIGURE_TIMEOUT_S = 120

# jcode's provider id for the Databricks gateway.
_JCODE_PROVIDER_ID = "dbx"

# Environment variable names jcode's gateway bearer is exported under.
_JCODE_BEARER_ENV = "JCODE_DBX_TOKEN"
_JCODE_RUNTIME_DIR_ENV = "JCODE_RUNTIME_DIR"

# Environment variable for the model override (mirrors claude-native and opencode).
_JCODE_DATABRICKS_GATEWAY_MODEL_ENV = "OMNIGENT_DATABRICKS_GATEWAY_MODEL"


def _jcode_default_model() -> str:
    """The served model jcode's gateway provider defaults to.

    The deployment override ``OMNIGENT_DATABRICKS_GATEWAY_MODEL`` if set, else the
    bundled Databricks Claude catalog default — resolved from the catalog rather
    than hardcoded, mirroring claude-native's ``_connect_broker_default_model``.
    Both ``databricks-*`` and ``system.ai.*`` ids route on the gateway's openai path.
    """
    pinned = os.environ.get(_JCODE_DATABRICKS_GATEWAY_MODEL_ENV, "").strip()
    if pinned:
        return pinned
    from omnigent.models import model_catalog

    return model_catalog.resolve_catalog_model("databricks", family="claude").model_id


# Base dir under which each session gets its own jcode daemon runtime dir. A unique
# JCODE_RUNTIME_DIR per session gives that session its own jcode daemon (which reads
# the fresh bearer at spawn). Keying it by session id — rather than a fresh mkdtemp
# per call — keeps the builder idempotent across the many times it runs per session,
# so run dirs don't accumulate one-per-message on a long-lived connect pod.
_JCODE_RUN_DIR_BASE = "omnigent-jcode-run"


def _session_runtime_dir(session_id: str | None) -> str:
    """Return this session's jcode runtime dir, created 0700 and idempotently.

    Rooted under ``OMNIGENT_HARNESS_TMP_PARENT`` when set (the harness tmp parent),
    else the OS temp dir. The dir *name* is a hash of *session_id*, so repeated
    spawns within one session reuse a single dir (its own daemon) without leaking a
    new dir per turn, and — since a hex digest carries no path separators — the
    (untrusted) session id can't escape the run-dir root. A missing id falls back to
    a per-process key.
    """
    base = os.environ.get("OMNIGENT_HARNESS_TMP_PARENT") or tempfile.gettempdir()
    root = os.path.join(base, _JCODE_RUN_DIR_BASE)
    raw = session_id or f"proc-{os.getpid()}"
    name = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    path = os.path.join(root, name)
    # Defense-in-depth: the resolved dir must stay within the run-dir root.
    root_real = os.path.realpath(root)
    if os.path.commonpath([root_real, os.path.realpath(path)]) != root_real:
        raise OSError(f"jcode runtime dir escaped its root: {path!r}")
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)  # enforce 0700 even if the dir pre-existed or umask trimmed it
    return path


def build_jcode_configure_command(
    jcode_command: list[str],
    *,
    host: str,
    model: str,
) -> list[str]:
    """Build the ``jcode provider add dbx`` command Omnigent runs.

    :param jcode_command: Command prefix that invokes jcode, e.g.
        ``["/usr/bin/jcode"]`` or ``["jcode"]``.
    :param host: The Databricks workspace host, e.g.
        ``"https://example.databricks.com"``. The base URL is constructed as
        ``{host}/ai-gateway/openai/v1``.
    :param model: The served model to configure, e.g.
        ``"system.ai.claude-sonnet-4-6"``. Must be non-empty.
    :returns: Command argv using jcode's ``provider add`` with openai-compatible
        config.
    :raises ValueError: If *host* or *model* is empty.
    """
    if not host or not model:
        raise ValueError("host and model must not be empty")
    base_url = f"{host.rstrip('/')}/ai-gateway/openai/v1"
    return [
        *jcode_command,
        "provider",
        "add",
        _JCODE_PROVIDER_ID,
        "--base-url",
        base_url,
        "--model",
        model,
        "--auth",
        "bearer",
        "--api-key-env",
        _JCODE_BEARER_ENV,
        "--set-default",
        "--overwrite",
        "--quiet",
    ]


def configure_jcode_for_sandbox() -> None:
    """Populate jcode's gateway provider at managed-sandbox host boot, in the background.

    Configures jcode's openai-compatible provider to route inference through the owner's
    Databricks model-serving gateway when a managed-connect sidecar signals that the owner
    has connected (host-only ``[omnigent]`` profile + broker coordinates on disk).

    A daemon thread keeps this off ``omnigent host``'s dial-back path, where a synchronous
    multi-second configure would delay the runner connect. The jcode daemon is spawned per
    session with a fresh bearer and a unique runtime dir, so tokens are never persisted.
    Best-effort: when jcode isn't available or the managed-connect gate isn't satisfied,
    silently no-ops rather than raising.

    This mirrors :func:`omnigent.onboarding.ucode_setup.configure_ucode_for_sandbox` in
    scope (host-boot only, daemon thread, best-effort), but runs with a fresh bearer passed
    in the spawn env rather than the global ``DATABRICKS_BEARER_COMMAND``.
    """
    # Managed-connect gate: workspace host from profile + broker command present.
    workspace = _read_databrickscfg_host(HOST_DATABRICKS_PROFILE)
    if not workspace:
        return
    bearer_command = broker_token_command(workspace)
    if not bearer_command:
        return  # no broker sidecar → not a managed connect host

    # Find jcode binary (best-effort; if absent, no-op).
    jcode_bin = shutil.which("jcode")
    if jcode_bin is None:
        _logger.debug("jcode: binary not found on PATH")
        return

    try:
        model = _jcode_default_model()
    except Exception as exc:  # noqa: BLE001 - best-effort; catalog unavailable ⇒ skip configure.
        _logger.info("jcode: could not resolve a default model, skipping configure: %r", exc)
        return

    argv = build_jcode_configure_command([jcode_bin], host=workspace, model=model)

    # ``jcode provider add`` only writes config.toml (no network, no token needed) —
    # the bearer is minted at spawn time by connect_jcode_gateway_env. Pass a minimal
    # env: suppress telemetry and withhold the host launch token.
    env = {**os.environ, "JCODE_NO_TELEMETRY": "1"}
    env.pop("OMNIGENT_HOST_TOKEN", None)

    def _run() -> None:
        try:
            result = subprocess.run(
                argv, capture_output=True, timeout=_SANDBOX_CONFIGURE_TIMEOUT_S, env=env
            )
        except (OSError, subprocess.SubprocessError) as exc:
            # A failed/timed-out configure is best-effort; the spawn-time
            # connect_jcode_gateway_env() will mint a fresh bearer, so the jcode
            # daemon can still run. Log at WARNING so the field isn't blind.
            _logger.warning("jcode: sandbox configure failed: %r", exc)
            return
        if result.returncode != 0:
            # Log the returncode, not stderr — stderr could echo a secret.
            _logger.warning("jcode: sandbox configure exit=%s", result.returncode)
        else:
            _logger.info("jcode: sandbox configure ok")

    threading.Thread(target=_run, name="jcode-configure", daemon=True).start()


def _jcode_config_path() -> Path:
    """Path to jcode's config.toml (``$JCODE_HOME/config.toml``, else ``~/.jcode``).

    Matches jcode's own resolution, so it points at the same file
    :func:`configure_jcode_for_sandbox` wrote at host boot.
    """
    home = os.environ.get("JCODE_HOME") or str(Path.home() / ".jcode")
    return Path(home) / "config.toml"


def _dbx_provider_base_url_on_workspace(workspace_host: str) -> bool:
    """True when jcode's on-disk ``dbx`` provider base URL is HTTPS on *workspace_host*.

    ``config.toml`` is a same-user-writable file shared across sessions, and the
    spawn forwards a live Databricks bearer to whatever ``providers.dbx.base_url``
    it names. Re-pin at spawn (mirrors the opencode-native config-origin check): a
    rewritten base URL pointing at another origin must not receive the token. A
    missing/unreadable config or provider fails closed (``False``).
    """
    try:
        with open(_jcode_config_path(), "rb") as handle:
            cfg = tomllib.load(handle)
    except (OSError, ValueError):
        return False
    providers = cfg.get("providers")
    dbx = providers.get(_JCODE_PROVIDER_ID) if isinstance(providers, dict) else None
    base_url = dbx.get("base_url") if isinstance(dbx, dict) else None
    return isinstance(base_url, str) and https_url_on_workspace_host(base_url, workspace_host)


def connect_jcode_gateway_env(*, session_id: str | None = None) -> dict[str, str] | None:
    """Mint a fresh Databricks bearer + this session's runtime dir for a jcode spawn.

    Called while building the jcode spawn env. Reads the managed-connect sidecar;
    if present and valid, fetches a **fresh** bearer via the broker and returns the
    two env vars jcode needs (``JCODE_DBX_TOKEN``, ``JCODE_RUNTIME_DIR``).

    **Token lifetime (no mid-session refresh).** The bearer is captured into the
    *outer* generic-ACP harness process's env when the harness process manager first
    spawns it; on a cache hit it ignores the freshly-built env, and when the inner
    jcode daemon idle-exits the ACP executor restarts it from that harness process's
    original env. So the token is effectively fixed for the life of the harness
    process and only refreshes when *that* process is recycled (its idle-reap), not
    on an inner-daemon respawn. A single session outliving the ~1h broker token can
    therefore start failing inference — accepted for a first cut (a jcode-side
    per-request token command is the follow-up). Minting on every call is harmless
    (the value is consumed only when the harness process actually (re)spawns) and
    keeps that (re)spawn current; it's a small per-turn broker call.

    **Runtime dir:** keyed by *session_id* (see :func:`_session_runtime_dir`) and
    created idempotently, so the session reuses one dir (its own daemon) across turns
    rather than leaking a new dir per message. It is reclaimed when the ephemeral
    managed-connect pod is torn down.

    Returns ``None`` (a complete no-op — jcode keeps its ambient config) when the
    sidecar is absent (not a managed-connect host), the broker is unreachable or
    declines, the broker's workspace no longer matches the sidecar pin, or jcode's
    on-disk ``dbx`` base URL isn't on the pinned workspace. Best-effort: errors are
    logged, never raised.

    :param session_id: The session/conversation id, used to scope the runtime dir.
    :returns: ``{JCODE_DBX_TOKEN, JCODE_RUNTIME_DIR}``, or ``None`` off the managed
        connect path.
    """
    coords = _read_sidecar(_sidecar_path())
    if coords is None:
        return None

    try:
        resolved = fetch_broker_bearer(coords["server"], coords["host_id"], coords["host_token"])
    except Exception as exc:  # noqa: BLE001
        # Catch all exceptions: httpx errors, dict access errors, etc.
        # This is best-effort code; any broker failure is a no-op.
        _logger.debug("jcode: broker fetch failed: %r", exc)
        return None

    if resolved is None:
        return None

    workspace_host, bearer = resolved
    # Reconnect guard: if the broker now vends a different workspace than the sidecar
    # pins (the owner reconnected elsewhere), don't forward this bearer to jcode's
    # config, which is pinned to the sidecar workspace. Mirrors databricks_credential.main.
    if workspace_host.rstrip("/") != coords["workspace_host"].rstrip("/"):
        return None

    # Origin guard: the bearer is forwarded to whatever base URL jcode's on-disk `dbx`
    # provider names (a same-user-writable, cross-session config.toml). Withhold it
    # unless that base URL is still HTTPS on the pinned workspace, so a rewritten
    # config can't redirect the Databricks token to another origin.
    if not _dbx_provider_base_url_on_workspace(workspace_host):
        _logger.warning(
            "jcode: dbx provider base URL is missing or not on the connected workspace; "
            "withholding the bearer."
        )
        return None

    try:
        runtime_dir = _session_runtime_dir(session_id)
    except OSError as exc:
        _logger.warning("jcode: could not create runtime dir: %r", exc)
        return None

    return {
        _JCODE_BEARER_ENV: bearer,
        _JCODE_RUNTIME_DIR_ENV: runtime_dir,
    }
