"""Point a managed-connect sandbox's jcode at the owner's Databricks model-serving gateway.

On a managed-connect sandbox (host-only ``[omnigent]`` ``~/.databrickscfg`` profile +
broker sidecar, both written only when ``IS_SANDBOX=1``), route jcode's inference
through the owner's workspace gateway. jcode is a generic ACP harness that owns its
own config, so — unlike the command-based harnesses (claude/codex/pi) — we hand it a
**session-private** ``JCODE_HOME`` containing a ``config.toml`` that pins its
openai-compatible ``dbx`` provider at the workspace gateway, plus a freshly-minted
broker bearer in ``JCODE_DBX_TOKEN``.

Writing a session-private config (rather than a shared, same-user-writable
``~/.jcode/config.toml`` that jcode re-reads at daemon start) keeps the bearer's
destination under Omnigent's control — a co-resident process can't repoint a
well-known config to exfiltrate the token. Mirrors opencode-native's session-private
config. A complete no-op off the managed-connect path (no broker sidecar), so laptops
and non-connected sandboxes are untouched.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from pathlib import Path

from omnigent.host.databricks_credential import (
    _read_sidecar,
    _sidecar_path,
    fetch_broker_bearer,
)

_logger = logging.getLogger(__name__)

# jcode's provider id for the Databricks gateway.
_JCODE_PROVIDER_ID = "dbx"

# Env vars the jcode subprocess reads (forwarded via HARNESS_ACP_ENV_PASSTHROUGH).
_JCODE_BEARER_ENV = "JCODE_DBX_TOKEN"
_JCODE_HOME_ENV = "JCODE_HOME"
_JCODE_RUNTIME_DIR_ENV = "JCODE_RUNTIME_DIR"

# Deployment model override (mirrors claude-native / opencode).
_JCODE_DATABRICKS_GATEWAY_MODEL_ENV = "OMNIGENT_DATABRICKS_GATEWAY_MODEL"

# Base dir under which each session gets its own private JCODE_HOME. Keyed by a hash
# of the session id so a session reuses one home across spawns (its own daemon) rather
# than leaking a new dir per message.
_JCODE_RUN_DIR_BASE = "omnigent-jcode-run"


def _jcode_default_model() -> str:
    """The served model jcode's gateway provider defaults to.

    The deployment override ``OMNIGENT_DATABRICKS_GATEWAY_MODEL`` if set, else the
    bundled Databricks Claude catalog default — resolved from the catalog rather than
    hardcoded, mirroring claude-native's ``_connect_broker_default_model``. Both
    ``databricks-*`` and ``system.ai.*`` ids route on the gateway's openai path.
    """
    pinned = os.environ.get(_JCODE_DATABRICKS_GATEWAY_MODEL_ENV, "").strip()
    if pinned:
        return pinned
    from omnigent.models import model_catalog

    return model_catalog.resolve_catalog_model("databricks", family="claude").model_id


def _session_jcode_home(session_id: str | None) -> Path:
    """Create and return this session's private ``JCODE_HOME`` (0700, idempotent).

    Rooted under ``OMNIGENT_HARNESS_TMP_PARENT`` when set (the harness tmp parent),
    else the OS temp dir. The leaf is a SHA-256 hash of *session_id* — a fixed hex
    string carrying no path separators, so the (untrusted) session id can't escape the
    run-dir root and a session reuses one home across spawns. A missing id falls back
    to a per-process key.
    """
    base = os.environ.get("OMNIGENT_HARNESS_TMP_PARENT") or tempfile.gettempdir()
    root = os.path.join(base, _JCODE_RUN_DIR_BASE)
    raw = session_id or f"proc-{os.getpid()}"
    name = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
    home = os.path.join(root, name)
    # Defense-in-depth: the resolved home must stay within the run-dir root.
    root_real = os.path.realpath(root)
    if os.path.commonpath([root_real, os.path.realpath(home)]) != root_real:
        raise OSError(f"jcode home escaped its root: {home!r}")
    os.makedirs(home, mode=0o700, exist_ok=True)
    os.chmod(home, 0o700)  # enforce 0700 even if the dir pre-existed or umask trimmed it
    return Path(home)


def _write_session_config(jcode_home: Path, *, host: str, model: str) -> None:
    """Write the session-private ``config.toml`` pinning jcode's ``dbx`` provider.

    Omnigent owns this file (0600, under the private ``JCODE_HOME``), so the bearer's
    destination — ``base_url`` — is set here by construction (the pinned workspace's
    openai gateway) rather than read from a shared, same-user-writable file at daemon
    start. The bearer itself is never written; only the env-var *name*
    ``JCODE_DBX_TOKEN`` jcode reads it from. Rewritten on every spawn, so a tampered
    file is reverted.
    """
    base_url = f"{host.rstrip('/')}/ai-gateway/openai/v1"
    content = (
        "[provider]\n"
        f'default_provider = "{_JCODE_PROVIDER_ID}"\n'
        f'default_model = "{model}"\n\n'
        f"[providers.{_JCODE_PROVIDER_ID}]\n"
        'type = "openai-compatible"\n'
        f'base_url = "{base_url}"\n'
        'auth = "bearer"\n'
        f'api_key_env = "{_JCODE_BEARER_ENV}"\n'
        f'default_model = "{model}"\n'
        "requires_api_key = true\n\n"
        f"[[providers.{_JCODE_PROVIDER_ID}.models]]\n"
        f'id = "{model}"\n'
    )
    config_path = jcode_home / "config.toml"
    config_path.write_text(content, encoding="utf-8")
    os.chmod(config_path, 0o600)


def connect_jcode_gateway_env(*, session_id: str | None = None) -> dict[str, str] | None:
    """Build the managed-connect jcode spawn env, or ``None`` off the connect path.

    On a managed-connect sandbox, returns the three vars the jcode subprocess needs
    (forwarded via ``HARNESS_ACP_ENV_PASSTHROUGH``): a session-private ``JCODE_HOME``
    (holding a ``config.toml`` that pins the ``dbx`` provider at the workspace gateway),
    its ``JCODE_RUNTIME_DIR`` (per-session daemon socket), and a freshly-minted
    ``JCODE_DBX_TOKEN`` bearer.

    **Origin safety:** Omnigent writes the session-private config, so ``base_url`` is
    the pinned workspace by construction — no shared mutable file decides where the
    bearer goes. **Reconnect guard:** withholds when the broker's workspace no longer
    matches the sidecar pin.

    **Token lifetime (no mid-session refresh):** the bearer is captured into the outer
    ACP-harness process env at first spawn; the process manager ignores env on cache
    hits and inner-daemon restarts reuse that env, so the token is fixed for the harness
    process's life and refreshes only when that process recycles. A session outliving
    the ~1h broker token can start failing inference — accepted for a first cut (a
    jcode-side per-request token command is the follow-up). Best-effort: any failure
    returns ``None`` (jcode falls back to its own login); errors are logged, not raised.

    :param session_id: Session/conversation id, used to scope the private home.
    """
    coords = _read_sidecar(_sidecar_path())
    if coords is None:
        return None

    try:
        resolved = fetch_broker_bearer(coords["server"], coords["host_id"], coords["host_token"])
    except Exception as exc:  # noqa: BLE001 - best-effort; any broker failure is a no-op.
        _logger.debug("jcode: broker fetch failed: %r", exc)
        return None
    if resolved is None:
        return None

    workspace_host, bearer = resolved
    # Reconnect guard: if the broker now vends a different workspace than the sidecar
    # pins (the owner reconnected elsewhere), don't forward this bearer.
    if workspace_host.rstrip("/") != coords["workspace_host"].rstrip("/"):
        return None

    try:
        model = _jcode_default_model()
    except Exception as exc:  # noqa: BLE001 - catalog unavailable ⇒ skip (no config written).
        _logger.info("jcode: could not resolve a default model: %r", exc)
        return None

    try:
        jcode_home = _session_jcode_home(session_id)
        runtime_dir = jcode_home / "run"
        os.makedirs(runtime_dir, mode=0o700, exist_ok=True)
        _write_session_config(jcode_home, host=coords["workspace_host"], model=model)
    except OSError as exc:
        _logger.warning("jcode: could not set up the session config: %r", exc)
        return None

    return {
        _JCODE_BEARER_ENV: bearer,
        _JCODE_HOME_ENV: str(jcode_home),
        _JCODE_RUNTIME_DIR_ENV: str(runtime_dir),
    }
