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


def _validated_host_id(host_id: str, *, source: str, remedy: str) -> str:
    """Normalize *host_id* and verify it is a valid (UUID-shaped) host id.

    Host ids are stored server-side in a UUID column, so the tunnel route
    normalises the ``{host_id}`` path segment through
    :func:`omnigent.db.db_models.uuid_to_bytes` and refuses anything that
    is not a 32-char hex uuid (optionally dashed or legacy-prefixed). A
    refusal there happens *before* the WebSocket is accepted, so it reaches
    the client as an opaque ``HTTP 403`` with no body — indistinguishable
    from an auth failure. Validating the id here, where it enters the
    process from the env override or config file, turns that confusing
    remote rejection into a loud, local, actionable error.

    Reuses the server's own ``uuid_to_bytes`` (imported lazily so the host
    CLI's common auto-generate path never pulls in the DB/sqlalchemy
    module) so the client and server accept exactly the same id shapes.

    :param host_id: The raw host id from the env override or config file.
    :param source: Where it came from, named in the error (e.g. the env var).
    :param remedy: How to fix it, appended to the error message.
    :returns: The normalized (to bare hex, legacy-prefix-stripped) host id.
    :raises ValueError: If *host_id* is not a valid uuid-shaped host id.
    """
    from omnigent.db.db_models import InvalidUuidError, uuid_to_bytes

    try:
        # Validate and normalize to bare hex (16 bytes -> 32-char hex string)
        normalized = uuid_to_bytes(host_id).hex()
    except InvalidUuidError:
        raise ValueError(
            f"{source} is not a valid host id: {host_id!r}. Host ids must be "
            'UUIDs (generate one with `python -c "import uuid; '
            'print(uuid.uuid4().hex)"`). ' + remedy
        ) from None
    return normalized


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
        return HostIdentity(
            host_id=_validated_host_id(
                env_host_id,
                source=f"${HOST_ID_ENV_VAR}",
                remedy=f"Set {HOST_ID_ENV_VAR} to a UUID, or unset {HOST_ID_ENV_VAR} "
                f"and {HOST_NAME_ENV_VAR} to have one generated automatically.",
            ),
            name=env_name,
        )

    cfg: dict[str, object] = {}
    if path.exists():
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}

    host_section = cfg.get("host")
    if not isinstance(host_section, dict):
        host_section = {}

    host_id = host_section.get("host_id")
    name = host_section.get("name")

    # A host_id read from config.yaml must be a valid (UUID-shaped) id, same
    # as the env override — an invalid one would only fail later, remotely, as
    # an opaque tunnel 403.
    config_host_id_remedy = (
        f"Fix host_id in the `host:` block of {path} to a UUID, or remove it "
        "to have one generated automatically."
    )

    # Fully specified: honor the config as-is, nothing to persist.
    if host_id and name:
        return HostIdentity(
            host_id=_validated_host_id(
                host_id, source=f"host_id in {path}", remedy=config_host_id_remedy
            ),
            name=name,
        )

    # Otherwise complete the section, preserving any provided value and
    # generating only what's missing, then persist so the identity is
    # stable across calls.
    host_id = (
        _validated_host_id(host_id, source=f"host_id in {path}", remedy=config_host_id_remedy)
        if host_id
        else uuid.uuid4().hex
    )
    name = name or socket.gethostname()
    identity = HostIdentity(host_id=host_id, name=name)

    host_section["host_id"] = identity.host_id
    host_section["name"] = identity.name
    cfg["host"] = host_section
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)

    return identity


def reset_host_id(path: Path = CONFIG_PATH) -> tuple[str | None, str]:
    """Replace this machine's persisted ``host_id`` with a fresh one.

    The recovery path for a host registration owned by another identity:
    the server keys hosts by ``host_id``, so once that id is claimed by a
    different account (e.g. a service principal), re-registering under the
    signed-in user is refused with HTTP 409. Minting a fresh id lets the
    machine register as a brand-new host under the current identity.

    The host ``name`` (and every other config key) is preserved; only
    ``host_id`` changes. A missing config or host section is created, same
    as :func:`load_or_create_host_identity`.

    :param path: Path to the config YAML file. Defaults to :data:`CONFIG_PATH`.
    :returns: ``(old_host_id, new_host_id)`` — ``old_host_id`` is ``None``
        when no identity was persisted before.
    """
    cfg: dict[str, object] = {}
    if path.exists():
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}

    host_section = cfg.get("host")
    if not isinstance(host_section, dict):
        host_section = {}

    old_host_id = host_section.get("host_id")
    new_host_id = uuid.uuid4().hex
    host_section["host_id"] = new_host_id
    host_section.setdefault("name", socket.gethostname())
    cfg["host"] = host_section
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=True)

    return (old_host_id if isinstance(old_host_id, str) else None), new_host_id


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

    This read is TOLERANT: an invalid ``host_id`` (a malformed env override or a
    hand-edited ``config.yaml``) — or exactly one identity env var set — yields
    ``None`` rather than raising. This path is the passive slice-key fallback
    that every request's header builder funnels through
    (:func:`omnigent.cli_auth.databricks_request_headers`), so a bad id must
    mean only "don't emit a slice key," never crash unrelated commands (login,
    chat, session list). The loud, actionable fail-fast lives on the
    ``omnigent host`` launch path (:func:`load_or_create_host_identity`), where
    a bad id is the thing the operator is trying to use.

    :param path: Path to the config YAML file. Defaults to :data:`CONFIG_PATH`.
    :returns: The existing :class:`HostIdentity`, or ``None`` when no usable
        identity is present (absent, half-specified, or invalid).
    """
    env_host_id = os.environ.get(HOST_ID_ENV_VAR)
    env_name = os.environ.get(HOST_NAME_ENV_VAR)
    if (env_host_id is None) != (env_name is None):
        # Half-set override is a launcher bug; surface it on the launch path,
        # not here — a passive header build has no identity to key on.
        return None
    if env_host_id is not None and env_name is not None:
        try:
            host_id = _validated_host_id(
                env_host_id,
                source=f"${HOST_ID_ENV_VAR}",
                remedy="",
            )
        except ValueError:
            return None
        return HostIdentity(host_id=host_id, name=env_name)

    if not path.exists():
        return None
    with open(path) as f:
        cfg = yaml.safe_load(f) or {}
    host_section = cfg.get("host")
    if isinstance(host_section, dict) and "host_id" in host_section and "name" in host_section:
        try:
            host_id = _validated_host_id(
                host_section["host_id"], source=f"host_id in {path}", remedy=""
            )
        except ValueError:
            return None
        return HostIdentity(host_id=host_id, name=host_section["name"])
    return None
