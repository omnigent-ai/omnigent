"""Host identity management for ``omnigent host``.

Reads or creates the ``host`` section in ``~/.omnigent/config.yaml``.
The host identity is auto-generated on first ``omnigent host``
if the section does not exist.
"""

from __future__ import annotations

import os
import socket
import uuid
from dataclasses import dataclass
from pathlib import Path

import yaml

CONFIG_PATH = Path.home() / ".omnigent" / "config.yaml"

# Env vars a server-managed sandbox host is launched with. The server
# provisions the sandbox, generates the identity + launch token, and
# injects all three so the host registers under the server-chosen
# identity without persisting anything to the sandbox's config.yaml
# (managed sandboxes are disposable). HOST_TOKEN is the tunnel
# credential (see MANAGED_HOST_TOKEN_HEADER); HOST_ID / HOST_NAME
# override the identity file and must be set together.
HOST_TOKEN_ENV_VAR = "OMNIGENT_HOST_TOKEN"
HOST_ID_ENV_VAR = "OMNIGENT_HOST_ID"
HOST_NAME_ENV_VAR = "OMNIGENT_HOST_NAME"

# WebSocket upgrade header carrying a managed host's launch token.
# Mirrors the runner tunnel's X-Omnigent-Runner-Tunnel-Token pattern:
# a dedicated header (not Authorization) so the credential can't be
# confused with a user Bearer token by intermediate proxies or the
# auth provider.
MANAGED_HOST_TOKEN_HEADER = "X-Omnigent-Host-Token"


@dataclass
class HostIdentity:
    """Identity of a host machine.

    :param host_id: Stable identifier, e.g.
        ``"a1b2c3d4e5f67890abcdef1234567890"``.
        Format: bare 32-char uuid4 hex.
    :param name: Human-readable name displayed in the Web UI
        host picker, e.g. ``"corey-laptop"``.
    """

    host_id: str
    name: str


# Legacy host-id prefix; older installs persist ``host_<hex>`` in config.yaml.
_LEGACY_HOST_ID_PREFIX = "host_"


def _normalize_host_id(host_id: str) -> str:
    """Strip the legacy ``host_`` prefix from *host_id* if present.

    Older installs persisted ``host_<hex>`` in config.yaml (or the launch env
    var); return the prefix-less form so a re-presented legacy id matches the
    migrated, now prefix-less server-side host row.

    :param host_id: A host id, possibly carrying the legacy prefix.
    :returns: The bare 32-char hex host id.
    """
    if host_id.startswith(_LEGACY_HOST_ID_PREFIX):
        return host_id[len(_LEGACY_HOST_ID_PREFIX) :]
    return host_id


def load_or_create_host_identity(
    path: Path = CONFIG_PATH,
) -> HostIdentity:
    """Load host identity from config.yaml, or create it if absent.

    Reads the ``host:`` section from the config file. If the
    section does not exist, generates a fresh ``host_id``, sets
    ``name`` to the machine's hostname, writes the section back,
    and returns the identity.

    A *partial* host section is completed rather than discarded: a
    config.yaml that names the host (``host: {name: my-box}``) but
    omits ``host_id`` keeps that name and only the missing
    ``host_id`` is generated — the user-provided name is never
    clobbered by the machine hostname.

    Environment override: when :data:`HOST_ID_ENV_VAR` and
    :data:`HOST_NAME_ENV_VAR` are both set (a server-managed sandbox
    host), that identity is returned directly without reading or
    writing the config file — managed sandboxes are disposable and the
    server owns their identity. Setting only one of the two is a
    launcher bug and fails loud.

    :param path: Path to the config YAML file, e.g.
        ``Path("~/.omnigent/config.yaml")``. Defaults to
        :data:`CONFIG_PATH`.
    :returns: The loaded or newly created :class:`HostIdentity`.
    :raises ValueError: If exactly one of the identity env vars is set.
    """
    env_host_id = os.environ.get(HOST_ID_ENV_VAR)
    env_name = os.environ.get(HOST_NAME_ENV_VAR)
    if (env_host_id is None) != (env_name is None):
        raise ValueError(
            f"{HOST_ID_ENV_VAR} and {HOST_NAME_ENV_VAR} must be set together "
            "(managed-host launch sets both)"
        )
    if env_host_id is not None and env_name is not None:
        return HostIdentity(host_id=_normalize_host_id(env_host_id), name=env_name)

    cfg: dict[str, object] = {}
    if path.exists():
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}

    host_section = cfg.get("host")
    if not isinstance(host_section, dict):
        host_section = {}

    host_id = host_section.get("host_id")
    name = host_section.get("name")

    # Fully specified: honor the config as-is, nothing to persist.
    if host_id and name:
        return HostIdentity(host_id=_normalize_host_id(host_id), name=name)

    # Otherwise complete the section, preserving any provided value and
    # generating only what's missing, then persist so the identity is
    # stable across calls.
    host_id = _normalize_host_id(host_id) if host_id else uuid.uuid4().hex
    name = name or socket.gethostname()
    identity = HostIdentity(host_id=host_id, name=name)

    host_section["host_id"] = identity.host_id
    host_section["name"] = identity.name
    cfg["host"] = host_section
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)

    return identity


def load_host_identity_if_present(
    path: Path = CONFIG_PATH,
) -> HostIdentity | None:
    """Return the host identity if one already exists, else ``None`` — no create.

    The read-only sibling of :func:`load_or_create_host_identity`: same
    env-override and config-file lookup, but it NEVER generates or persists an
    identity. Use this from code paths that only want to *read* whether this
    machine is a host (e.g. keying a request by the CLI's own host_id as a
    slice-key fallback), so a bare read can't mint a host identity on a machine
    that is not one, and has no filesystem side effect.

    :param path: Path to the config YAML file. Defaults to :data:`CONFIG_PATH`.
    :returns: The existing :class:`HostIdentity`, or ``None`` when neither the
        env override nor a persisted ``host:`` section is present.
    :raises ValueError: If exactly one of the identity env vars is set.
    """
    env_host_id = os.environ.get(HOST_ID_ENV_VAR)
    env_name = os.environ.get(HOST_NAME_ENV_VAR)
    if (env_host_id is None) != (env_name is None):
        raise ValueError(
            f"{HOST_ID_ENV_VAR} and {HOST_NAME_ENV_VAR} must be set together "
            "(managed-host launch sets both)"
        )
    if env_host_id is not None and env_name is not None:
        return HostIdentity(host_id=_normalize_host_id(env_host_id), name=env_name)

    if not path.exists():
        return None
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    host_section = cfg.get("host")
    if isinstance(host_section, dict) and "host_id" in host_section and "name" in host_section:
        return HostIdentity(
            host_id=_normalize_host_id(host_section["host_id"]),
            name=host_section["name"],
        )
    return None
