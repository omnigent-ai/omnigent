"""Point a managed sandbox's Databricks auth at the owner's per-user broker.

Called by ``omnigent host`` at startup (executor-agnostic, like
:func:`omnigent.git_credential_github.configure_host_git`). When the owner has
linked a Databricks workspace via the OAuth U2M connect flow, the host writes a
host-only ``[omnigent]`` ``~/.databrickscfg`` profile (workspace ``host`` only,
**no token**), exports ``DATABRICKS_CONFIG_PROFILE``, and drops a private
sidecar recording the broker coordinates (server URL, host id, launch token).

The Databricks **bearer is never persisted in the sandbox**. Instead, the
command-based harnesses (codex / claude / pi) mint it on demand: their gateway
auth command falls back to :func:`main` here, which fetches the owner's
server-refreshed token from the generic credential broker
(:mod:`omnigent.server.routes.host_credentials`, ``provider=databricks``) each
time it runs — the same per-op broker fetch the GitHub credential helper uses.
The harness re-runs that command as tokens near expiry, so a long session
refreshes without relaunch.

The launch token in the sidecar is a lesser, expiring credential already present
in this disposable sandbox (git's credential helper bakes the same token into
``~/.gitconfig``); the owner's Databricks token is not. Best-effort throughout:
a no-op when the host token is absent, the broker is unreachable, the feature is
off, or the owner hasn't connected Databricks (the sandbox then keeps whatever
ambient ``~/.databrickscfg`` it already had).

Scope: this wires the *command-based* gateway harnesses. The opencode-native
gateway and the Databricks MCP resolver read a static profile token via the SDK
and are intentionally not wired here; a host-only profile makes them fall back
to their ambient config until a broker-backed resolver lands for them.
"""

from __future__ import annotations

import argparse
import configparser
import contextlib
import json
import logging
import os
import shlex
import sys
from pathlib import Path

import httpx

from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER

_logger = logging.getLogger(__name__)

_TIMEOUT_S = 15.0

# The profile our host section is written under. Distinct from any ambient
# profile so merging never clobbers an operator's existing sections.
HOST_DATABRICKS_PROFILE = "omnigent"

# Broker coordinates live next to the config file, not in the profile (the
# Databricks CLI would reject unknown keys) and not in the env (the launch token
# is a bearer secret kept off the runner env allowlist, like git's).
_SIDECAR_NAME = ".omnigent-databricks-broker.json"


def _credential_url(server: str, host_id: str) -> str:
    # Generic, provider-keyed host-credential broker (host_credentials.py); the
    # dedicated per-provider route was removed in favor of this one.
    return f"{server.rstrip('/')}/v1/hosts/{host_id}/credentials/databricks"


def _fetch(server: str, host_id: str, host_token: str) -> dict | None:
    """Fetch the broker JSON, or ``None`` on any failure."""
    try:
        resp = httpx.get(
            _credential_url(server, host_id),
            headers={MANAGED_HOST_TOKEN_HEADER: host_token},
            timeout=_TIMEOUT_S,
        )
    except httpx.HTTPError:
        return None
    if resp.status_code != 200:
        return None
    try:
        return resp.json()
    except ValueError:
        return None


def fetch_broker_bearer(server: str, host_id: str, host_token: str) -> tuple[str, str] | None:
    """Return ``(workspace_host, bearer)`` from the broker, or ``None``.

    The broker refreshes the OAuth token server-side before vending it, so the
    bearer is always current. ``None`` when the owner hasn't connected, the
    broker is unreachable, or the response is missing either field.
    """
    data = _fetch(server, host_id, host_token)
    if not data or not data.get("connected"):
        return None
    workspace_host = str(data.get("workspace_host") or "").rstrip("/")
    bearer = str(data.get("token") or "")
    if not workspace_host or not bearer:
        return None
    return workspace_host, bearer


def _default_cfg_path() -> Path:
    return Path(os.environ.get("DATABRICKS_CONFIG_FILE") or (Path.home() / ".databrickscfg"))


def _sidecar_path(cfg_path: Path | None = None) -> Path:
    """Sidecar path derived from the databricks config location.

    The runner and the host both resolve ``DATABRICKS_CONFIG_FILE`` (allowlisted)
    or ``~/.databrickscfg``, so both compute the same path on the shared sandbox
    filesystem.
    """
    return (cfg_path or _default_cfg_path()).parent / _SIDECAR_NAME


def _write_profile(cfg_path: Path, host: str) -> bool:
    """Merge a host-only ``[omnigent]`` section into *cfg_path*.

    Writes ``host`` only — no ``token``. The profile is a workspace selector for
    the gateway base URL and ``databricks auth token --profile``; the bearer is
    fetched from the broker, never written here. Preserves other profiles.
    Returns ``True`` on success, ``False`` if the file can't be read or written.
    """
    parser = configparser.ConfigParser()
    try:
        if cfg_path.exists():
            parser.read(cfg_path)
    except (OSError, configparser.Error):
        return False
    # Replace any prior section wholesale so a stale ``token`` from an older
    # build never lingers on disk.
    parser[HOST_DATABRICKS_PROFILE] = {"host": host}
    try:
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        with open(cfg_path, "w", encoding="utf-8") as handle:
            parser.write(handle)
        with contextlib.suppress(OSError):
            os.chmod(cfg_path, 0o600)
    except OSError:
        return False
    return True


_SIDECAR_KEYS = ("server", "host_id", "host_token", "workspace_host")


def _write_sidecar(
    cfg_path: Path, server: str, host_id: str, host_token: str, workspace_host: str
) -> bool:
    """Persist the broker coordinates (0600) so :func:`main` can refetch."""
    path = _sidecar_path(cfg_path)
    payload = {
        "server": server,
        "host_id": host_id,
        "host_token": host_token,
        "workspace_host": workspace_host,
    }
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle)
        with contextlib.suppress(OSError):
            os.chmod(path, 0o600)
    except OSError:
        return False
    return True


def _read_sidecar(path: Path) -> dict[str, str] | None:
    """Read the broker-coordinate sidecar, or ``None`` when absent/malformed."""
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    coords = {key: data.get(key) for key in _SIDECAR_KEYS}
    if not all(isinstance(value, str) and value for value in coords.values()):
        return None
    return coords  # type: ignore[return-value]


def broker_token_command(host: str, cfg_path: Path | None = None) -> str | None:
    """Gateway auth-command fallback that mints *host*'s bearer from the broker.

    Returns the command **only** when a broker sidecar exists and its workspace
    matches *host* — so a gateway pinned to the owner's connected workspace falls
    back to the broker, while one pinned to a different workspace (a spec's own
    ``profile``) is left to its own credentials. When
    ``databricks auth token`` yields nothing (the host-only ``[omnigent]`` profile
    has no OAuth cache), this fetches a fresh token from the broker instead.

    The sidecar path is baked as an absolute literal so the command works in the
    harness process regardless of its inherited environment (like git's
    credential helper baking its coordinates).
    """
    path = _sidecar_path(cfg_path)
    coords = _read_sidecar(path)
    if coords is None or coords["workspace_host"].rstrip("/") != host.rstrip("/"):
        return None
    module = "omnigent.host.databricks_credential"
    return f"python3 -m {module} token --coords {shlex.quote(str(path))}"


def configure_host_databricks(server_url: str, host_id: str) -> bool:
    """Point the sandbox's Databricks auth at the owner's per-user broker.

    When the owner has Databricks connected, writes a host-only ``[omnigent]``
    ``~/.databrickscfg`` profile, records the broker coordinates in a private
    sidecar, and exports ``DATABRICKS_CONFIG_PROFILE`` so the runner (which
    inherits it via the env allowlist) resolves the gateway workspace as them.
    The bearer itself is fetched on demand from the broker, never persisted.

    Sandbox-only: this is an auto-applied *sandbox integration*, so it no-ops
    unless ``IS_SANDBOX=1``. ``omnigent host`` also runs on a developer's laptop,
    where writing a brokered ``[omnigent]`` profile into their real
    ``~/.databrickscfg`` is an unwanted local-machine footprint. ``IS_SANDBOX``
    is baked into the managed-sandbox host image (and set on the k8s Pod) and is
    never present on a laptop, so it scopes the write to managed sandboxes on top
    of the owner-connected gate.

    :returns: ``True`` when the per-user profile was written; ``False`` otherwise
        (no-op — ambient config, if any, is left untouched).
    """
    if os.environ.get("IS_SANDBOX") != "1":
        return False
    token = (os.environ.get(HOST_TOKEN_ENV_VAR) or "").strip()
    if not token:
        return False
    resolved = fetch_broker_bearer(server_url, host_id, token)
    if resolved is None:
        return False
    # The bearer confirms the owner is connected and is discarded — only the
    # workspace host is persisted; the bearer is refetched per use.
    workspace_host, _bearer = resolved
    cfg_path = _default_cfg_path()
    if not _write_profile(cfg_path, workspace_host):
        _logger.info("Databricks credential: could not write %s", cfg_path)
        return False
    if not _write_sidecar(cfg_path, server_url, host_id, token, workspace_host):
        _logger.info("Databricks credential: could not write broker sidecar")
        return False
    os.environ["DATABRICKS_CONFIG_PROFILE"] = HOST_DATABRICKS_PROFILE
    _logger.info("Databricks credential: profile %r → %s", HOST_DATABRICKS_PROFILE, workspace_host)
    return True


def main(argv: list[str] | None = None) -> int:
    """Print the owner's current Databricks bearer, fetched from the broker.

    Invoked as ``python3 -m omnigent.host.databricks_credential token`` by the
    gateway auth command's fallback. Prints the bearer on stdout, or nothing (so
    the caller falls through) when the sidecar is absent or the broker declines.
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--coords")
    parser.add_argument("operation", nargs="?", default="token")
    args, _ = parser.parse_known_args(argv)
    if args.operation != "token":
        return 0
    coords = _read_sidecar(Path(args.coords) if args.coords else _sidecar_path())
    if coords is None:
        return 0
    resolved = fetch_broker_bearer(coords["server"], coords["host_id"], coords["host_token"])
    if resolved is None:
        return 0
    _workspace_host, bearer = resolved
    sys.stdout.write(bearer + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
