"""Keep one ``omnigent host`` running on each of a user's static sandboxes.

A static sandbox is a sandbox the user owns at a provider that declares
``SandboxCapabilities.static_sandboxes`` (e.g. a persistent VM on ``remote``),
saved as an entry the provider keeps. The provider owns all of its state,
including the link to the Omnigent host: the entry carries
``omnigent_host_id``, which names an ordinary external row in Omnigent's
``hosts`` table. That row keeps the managed-only ``sandbox_provider`` /
``sandbox_id`` columns NULL, so the managed reaper, keepalive, and relaunch
paths never act on the user's VM.

A host is static only when one of its owner's entries links its id; its
name and row shape mean nothing. Lookups go through the provider:

- forward, ``(owner, provider, sandbox_id)`` → entry → ``omnigent_host_id``,
  used by session create and the New Session wake;
- reverse, ``host_id`` → the owner's entries at each static provider, cached
  briefly per (provider, owner) and used only for an OFFLINE host, so healthy
  sessions never call the provider.

Omnigent never creates, stops, or deletes the VM: a gone VM is an error.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
import uuid
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NamedTuple

import click
from fastapi import HTTPException

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.onboarding.sandboxes.base import (
    SandboxGoneError,
    SandboxHostLauncher,
    StaticSandboxProvider,
)
from omnigent.server.auth import is_reserved_user
from omnigent.server.managed_hosts import host_wake_lock, wake_host_in_sandbox
from omnigent.stores.host_store import Host, HostStore

if TYPE_CHECKING:
    from omnigent.server.host_registry import HostConnection, HostRegistry
    from omnigent.server.managed_hosts import ManagedSandboxConfig, ManagedSandboxDeployment

_logger = logging.getLogger(__name__)

# hosts.name is VARCHAR(64) and unique per user.
_HOST_NAME_MAX = 64
# Reverse-lookup cache: one list call per (provider, owner) per window.
_LOOKUP_TTL_S = 30.0
# How long a woken host gets to register on THIS replica before the caller is
# told to re-address.
_LOCAL_TUNNEL_WAIT_S = 10.0
# One diagnostics call is bounded by this; the provider's probe takes ~6s.
_DIAGNOSTICS_TIMEOUT_S = 45.0
# How much of the host log rides along in the error the client sees.
_DIAGNOSTICS_DETAIL_CHARS = 1500
# The last failed start per host, so callers queued on the host's lock fail
# with it instead of each burning another full online wait.
# custom-lint: disable-next=workspace-scoped-cache -- host ids are globally unique uuids
_recent_failures: dict[str, tuple[float, str]] = {}


@dataclass(frozen=True)
class StaticHostRef:
    """A host found to be the host of one of its owner's static sandboxes."""

    host_id: str
    owner: str
    provider: str
    sandbox_id: str
    status: str


def static_host_name(sandbox_id: str, host_id: str | None = None) -> str:
    """A display name for a new static host, within ``hosts.name``.

    Only a default: the row keeps whatever name it has once created. With
    *host_id*, the name carries the id's prefix so it can't collide with
    another of the owner's hosts.
    """
    suffix = f"-{host_id[:8]}" if host_id else ""
    if len(sandbox_id) + len(suffix) <= _HOST_NAME_MAX:
        return f"{sandbox_id}{suffix}"
    digest = hashlib.sha256(sandbox_id.encode()).hexdigest()[:8]
    return f"{sandbox_id[: _HOST_NAME_MAX - 9 - len(suffix)]}-{digest}{suffix}"


async def _host_name(host_store: HostStore, owner: str, host_id: str, sandbox_id: str) -> str:
    """The host's current name, or a free default for a host not created yet."""
    existing = await asyncio.to_thread(host_store.get_host, host_id)
    if existing is not None:
        return existing.name
    name = static_host_name(sandbox_id)
    taken = await asyncio.to_thread(host_store.get_host_by_name, owner, name)
    return name if taken is None else static_host_name(sandbox_id, host_id)


class StaticLauncher(NamedTuple):
    """A provider's launcher bound to an owner, for the static-sandbox path."""

    config: ManagedSandboxConfig
    # The static-sandbox operations.
    sandboxes: StaticSandboxProvider
    # The same launcher, for the host-start path shared with managed hosts.
    hosts: SandboxHostLauncher


def _static_candidates(
    sandbox_config: ManagedSandboxDeployment | None,
) -> list[tuple[ManagedSandboxConfig, SandboxHostLauncher]]:
    """Configured providers whose launcher implements :class:`StaticSandboxProvider`.

    Builds each launcher but asks no service, so a provider found here may
    still turn out not to offer static sandboxes.
    """
    found: list[tuple[ManagedSandboxConfig, SandboxHostLauncher]] = []
    for config in sandbox_config.offered() if sandbox_config is not None else ():
        if config.provider is None:
            continue
        try:
            launcher = config.launcher_factory()
        except Exception:  # noqa: BLE001 - a staged provider's factory refuses.
            continue
        if isinstance(launcher, StaticSandboxProvider):
            found.append((config, launcher))
    return found


def static_provider_candidates(sandbox_config: ManagedSandboxDeployment | None) -> list[str]:
    """Configured providers that could offer static sandboxes, without asking them."""
    return [str(config.provider) for config, _ in _static_candidates(sandbox_config)]


def static_sandboxes_configured(sandbox_config: ManagedSandboxDeployment | None) -> bool:
    """Whether any configured provider could offer static sandboxes, without asking it."""
    return bool(_static_candidates(sandbox_config))


# custom-lint: disable-next=workspace-scoped-cache -- deployment-wide provider capabilities
_providers_cache: dict[int, tuple[float, list[str]]] = {}
# /v1/info is hot and unauthenticated; a provider's capabilities rarely change.
_PROVIDERS_TTL_S = 60.0


def static_sandbox_providers(sandbox_config: ManagedSandboxDeployment | None) -> list[str]:
    """The configured providers declaring ``static_sandboxes``, in configured order.

    Reads each provider's declared capabilities, which may ask its service, at
    most once per :data:`_PROVIDERS_TTL_S`.
    """
    key = id(sandbox_config)
    cached = _providers_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < _PROVIDERS_TTL_S:
        return list(cached[1])
    providers = [
        str(config.provider)
        for config, launcher in _static_candidates(sandbox_config)
        if launcher.capabilities.static_sandboxes
    ]
    _providers_cache[key] = (time.monotonic(), providers)
    return list(providers)


def static_launcher(
    sandbox_config: ManagedSandboxDeployment | None, owner: str, provider: str
) -> StaticLauncher:
    """Build *provider*'s launcher acting for *owner*.

    :raises OmnigentError: ``INVALID_INPUT`` when *owner* is a reserved user
        (e.g. ``local`` on a server with auth off), since only a signed-in user
        owns sandboxes at the provider, or when *provider* is not configured or
        does not offer static sandboxes.
    """
    # SECURITY: never act at the provider as a placeholder identity.
    if not owner or is_reserved_user(owner):
        raise OmnigentError("static sandboxes need a signed-in user", code=ErrorCode.INVALID_INPUT)
    config = sandbox_config.for_provider(provider) if sandbox_config is not None else None
    launcher = config.launcher_factory() if config is not None else None
    if config is None or not isinstance(launcher, StaticSandboxProvider):
        raise OmnigentError(
            f"provider {provider!r} does not offer static sandboxes",
            code=ErrorCode.INVALID_INPUT,
        )
    launcher.bind_principal(owner)
    if not launcher.capabilities.static_sandboxes:
        raise OmnigentError(
            f"provider {provider!r} does not offer static sandboxes",
            code=ErrorCode.INVALID_INPUT,
        )
    return StaticLauncher(config=config, sandboxes=launcher, hosts=launcher)


def shim_error(exc: Exception, *, action: str) -> OmnigentError:
    """Translate a shim failure into the HTTP error the caller sees."""
    if isinstance(exc, SandboxGoneError):
        return OmnigentError(str(exc.message), code=ErrorCode.SANDBOX_GONE)
    status = getattr(exc, "status_code", None)
    if status == 404:
        return OmnigentError(
            "Not one of your static sandboxes, or the VM does not exist",
            code=ErrorCode.NOT_FOUND,
        )
    if status == 400:
        return OmnigentError(
            f"The sandbox service refused to {action}: {exc}", code=ErrorCode.INVALID_INPUT
        )
    if status == 503:
        return OmnigentError(
            "Static sandboxes are not available on the sandbox service yet",
            code=ErrorCode.RUNNER_UNAVAILABLE,
        )
    if status == 504:
        return OmnigentError(
            f"Timed out trying to {action}, try again", code=ErrorCode.RUNNER_UNAVAILABLE
        )
    return OmnigentError(f"Could not {action}: {exc}", code=ErrorCode.RUNNER_UNAVAILABLE)


def _normalize_host_id(raw: object) -> str:
    try:
        return uuid.UUID(str(raw)).hex
    except ValueError as exc:
        raise OmnigentError(
            f"the sandbox service returned an invalid host id {raw!r}",
            code=ErrorCode.INTERNAL_ERROR,
        ) from exc


async def _call(fn: Any, *args: Any, action: str) -> Any:
    """Run a blocking shim call, mapping its errors to HTTP errors."""
    try:
        return await asyncio.to_thread(fn, *args)
    except OmnigentError:
        raise
    except Exception as exc:
        raise shim_error(exc, action=action) from exc


async def static_entry(launcher: StaticSandboxProvider, sandbox_id: str) -> dict[str, object]:
    """The owner's entry for *sandbox_id*.

    :raises OmnigentError: 404 when it is not one of the owner's static sandboxes.
    """
    entry = await _call(launcher.get_static_sandbox, sandbox_id, action="read your static sandbox")
    if entry is None:
        raise OmnigentError(
            f"{sandbox_id} is not one of your static sandboxes",
            code=ErrorCode.NOT_FOUND,
        )
    return entry


async def _resolve_host_id(launcher: StaticSandboxProvider, sandbox_id: str) -> str:
    """The host id linked to the entry, linking a new one when it has none.

    The host row is only written after the link is stored, so a lost race
    across replicas orphans nothing: every caller uses the winning id.
    """
    entry = await static_entry(launcher, sandbox_id)
    if entry.get("status") == "gone":
        raise OmnigentError(f"VM {sandbox_id} no longer exists", code=ErrorCode.SANDBOX_GONE)
    linked = entry.get("omnigent_host_id")
    if linked:
        return _normalize_host_id(linked)
    # Only the entry links a host: a new entry always gets a new host.
    candidate = uuid.uuid4().hex
    result = await _call(
        launcher.link_static_host, sandbox_id, candidate, action=f"link {sandbox_id}"
    )
    return _normalize_host_id(result.get("omnigent_host_id") or candidate)


async def _host_diagnostics(
    launcher: StaticSandboxProvider, sandbox_id: str, server_url: str
) -> tuple[str, str]:
    """``(fatal_reason, report)`` from one bounded diagnostics call; never raises.

    *fatal_reason* is empty unless the host can never come online; *report* is
    the host's version and log tail, or a note when there is no report.
    """
    try:
        diag = await asyncio.wait_for(
            asyncio.to_thread(launcher.diagnose_host, sandbox_id, server_url),
            timeout=_DIAGNOSTICS_TIMEOUT_S,
        )
    except Exception as exc:  # noqa: BLE001 - diagnostics are best effort.
        if getattr(exc, "status_code", None) == 409:
            return "", f"(no diagnostics: {sandbox_id} is not running)"
        return "", f"(could not read diagnostics from the VM: {exc!r})"
    version = str(diag.get("omnigent_version") or "")
    log_tail = str(diag.get("log_tail") or "").strip()
    report = "\n".join(
        part for part in (f"omnigent {version}" if version else "", log_tail) if part
    )
    return str(diag.get("fatal_reason") or ""), report


def _diagnostics_tail(output: str) -> str:
    return output[-_DIAGNOSTICS_DETAIL_CHARS:]


async def ensure_vm_host(
    *,
    owner: str,
    provider: str,
    sandbox_id: str,
    sandbox_config: ManagedSandboxDeployment | None,
    host_store: HostStore,
    on_stage: Any = None,
) -> str:
    """Make sure *owner*'s static sandbox *sandbox_id* at *provider* is awake with its host online.

    Never creates a VM. Reads the entry (not-found when it isn't the owner's),
    links a host id if needed, wakes a paused VM, and starts the host when it
    is offline, under the per-host single-flight lock.

    :param on_stage: Optional ``(stage) -> None`` progress observer; receives
        ``"waking"`` and ``"starting"``.
    :returns: The host id.
    :raises OmnigentError: ``INVALID_INPUT`` *provider* offers no static
        sandboxes, 404 not the owner's sandbox, ``SANDBOX_GONE`` VM
        gone, 503 the service is unavailable or the VM did not wake in time,
        ``HOST_START_FAILED`` the host did not start or connect.
    """
    bound = static_launcher(sandbox_config, owner, provider)
    entry, launcher = bound.config, bound.sandboxes
    host_id = await _resolve_host_id(launcher, sandbox_id)
    queued_at = time.monotonic()
    async with host_wake_lock(host_id):
        failed = _recent_failures.get(host_id)
        if failed is not None and failed[0] >= queued_at:
            # The start we queued behind just failed; retrying now would only
            # repeat it for another full wait.
            raise OmnigentError(failed[1], code=ErrorCode.HOST_START_FAILED)
        status = (await static_entry(launcher, sandbox_id)).get("status")
        if status == "gone":
            raise OmnigentError(f"VM {sandbox_id} no longer exists", code=ErrorCode.SANDBOX_GONE)
        paused = status != "running"
        if not paused and await asyncio.to_thread(host_store.is_online, host_id):
            return host_id
        if paused:
            if on_stage is not None:
                on_stage("waking")
            # The provider waits for the VM to run before answering.
            await _call(launcher.resume_static_sandbox, sandbox_id, action=f"wake {sandbox_id}")
        name = await _host_name(host_store, owner, host_id, sandbox_id)

        async def _arm(token: str, token_expires_at: int) -> bool:
            await asyncio.to_thread(
                host_store.upsert_static_host,
                host_id=host_id,
                name=name,
                user_id=owner,
                token=token,
                token_expires_at=token_expires_at,
            )
            return True

        last_diagnostics: list[tuple[str, str]] = []

        async def _probe() -> str | None:
            found = await _host_diagnostics(launcher, sandbox_id, entry.server_url)
            last_diagnostics[:] = [found]
            return found[0] or None

        _logger.info("Starting static host %s on VM %s (paused=%s)", host_id, sandbox_id, paused)
        try:
            await wake_host_in_sandbox(
                bound.hosts,
                sandbox_id,
                host_store=host_store,
                host_id=host_id,
                host_name=name,
                server_url=entry.server_url,
                host_config=entry.host_config,
                token_ttl_s=entry.token_ttl_s,
                arm=_arm,
                resume=False,
                on_stage=on_stage,
                what=f"the host on {sandbox_id}",
                where="on the VM",
                failure_probe=_probe,
            )
        except HTTPException as exc:
            # The probe's report is fresh when it stopped the wait; a timeout
            # reads the VM once more so the error shows its final state.
            fail_fast = "stopped before coming online" in str(exc.detail)
            fatal, report = (
                last_diagnostics[0]
                if fail_fast and last_diagnostics
                else await _host_diagnostics(launcher, sandbox_id, entry.server_url)
            )
            reason = fatal if fatal and fatal not in str(exc.detail) else ""
            _logger.warning(
                "Static host %s on VM %s did not come online: %s\nVM diagnostics:\n%s",
                host_id,
                sandbox_id,
                exc.detail,
                "\n".join(part for part in (fatal, report) if part),
            )
            detail = "\n".join(
                part for part in (str(exc.detail), reason, _diagnostics_tail(report)) if part
            )
            _recent_failures[host_id] = (time.monotonic(), detail)
            raise OmnigentError(detail, code=ErrorCode.HOST_START_FAILED) from exc
        except OmnigentError:
            raise
        except ValueError as exc:
            raise OmnigentError(str(exc), code=ErrorCode.CONFLICT) from exc
        except click.ClickException as exc:
            raise OmnigentError(
                f"could not start the host on {sandbox_id}: {exc.message}",
                code=ErrorCode.HOST_START_FAILED,
            ) from exc
    _recent_failures.pop(host_id, None)
    forget_static_hosts(owner, provider)
    return host_id


# custom-lint: disable-next=workspace-scoped-cache -- keyed by owner, scoped by the provider
_lookup_cache: dict[tuple[str, str], tuple[float, dict[str, StaticHostRef]]] = {}


def forget_static_hosts(owner: str, provider: str) -> None:
    """Drop *owner*'s cached reverse lookup at *provider* after a link changed."""
    _lookup_cache.pop((provider, owner), None)


async def _linked_hosts(
    owner: str, provider: str, sandbox_config: ManagedSandboxDeployment | None
) -> dict[str, StaticHostRef] | None:
    """*owner*'s linked host ids at *provider* mapped to their entries, cached briefly.

    ``None`` when the provider can't be read; callers treat that as "not static".
    """
    if is_reserved_user(owner):
        return {}
    key = (provider, owner)
    cached = _lookup_cache.get(key)
    if cached is not None and time.monotonic() - cached[0] < _LOOKUP_TTL_S:
        return cached[1]
    try:
        launcher = static_launcher(sandbox_config, owner, provider).sandboxes
        entries = await asyncio.to_thread(launcher.list_static_sandboxes)
    except OmnigentError as exc:
        if exc.code != ErrorCode.INVALID_INPUT:
            raise
        # The provider does not offer static sandboxes, so none of its hosts is static.
        entries = []
    except Exception:  # noqa: BLE001 - the lookup is advisory.
        _logger.warning(
            "static sandbox lookup failed for %s at %s", owner, provider, exc_info=True
        )
        return None
    refs: dict[str, StaticHostRef] = {}
    for entry in entries:
        linked = entry.get("omnigent_host_id")
        if not linked:
            continue
        try:
            host_id = uuid.UUID(str(linked)).hex
        except ValueError:
            continue
        refs[host_id] = StaticHostRef(
            host_id=host_id,
            owner=owner,
            provider=provider,
            sandbox_id=str(entry["sandbox_id"]),
            status=str(entry.get("status") or "paused"),
        )
    _lookup_cache[key] = (time.monotonic(), refs)
    return refs


async def find_static_host(
    host: Host | None, sandbox_config: ManagedSandboxDeployment | None
) -> StaticHostRef | None:
    """Reverse lookup: the owner's static sandbox entry linking *host*, if any.

    Managed hosts are never static, so they skip the providers. Callers must
    call this only for an offline or unreachable host. A failed lookup reads
    as "not static" so existing behaviour applies.
    """
    if host is None or host.sandbox_provider is not None:
        return None
    for provider in static_provider_candidates(sandbox_config):
        refs = await _linked_hosts(host.user_id, provider, sandbox_config)
        ref = refs.get(host.host_id) if refs is not None else None
        if ref is not None:
            return ref
    return None


async def static_sandbox_refs(
    hosts: list[Host], sandbox_config: ManagedSandboxDeployment | None
) -> dict[str, StaticHostRef]:
    """Map each static host in *hosts* to its static sandbox entry, for host lists.

    One cached list per (static provider, owner in *hosts*), whatever the
    hosts are named.
    """
    providers = static_provider_candidates(sandbox_config)
    owners = sorted({h.user_id for h in hosts if h.sandbox_provider is None})
    found: dict[str, StaticHostRef] = {}
    lookups = [(p, o) for p in providers for o in owners]
    for refs in await asyncio.gather(*(_linked_hosts(o, p, sandbox_config) for p, o in lookups)):
        for ref in (refs or {}).values():
            found.setdefault(ref.host_id, ref)
    return {h.host_id: found[h.host_id] for h in hosts if h.host_id in found}


async def wake_static_host_for_create(
    host_id: str,
    *,
    user_id: str | None,
    host_store: HostStore,
    sandbox_config: ManagedSandboxDeployment | None,
    host_registry: HostRegistry | None,
) -> None:
    """Wake an offline static host named by a plain ``host_id`` create.

    A no-op for a host connected to this replica, another user's host, or a
    non-static host, so workspace validation then runs unchanged.
    """
    if host_registry is not None and host_registry.get(host_id) is not None:
        return
    if not static_sandboxes_configured(sandbox_config):
        return
    host = await asyncio.to_thread(host_store.get_host, host_id)
    if host is None or (user_id is not None and host.user_id != user_id):
        return
    await ensure_static_host_for(
        host_id,
        host_store=host_store,
        sandbox_config=sandbox_config,
        host_registry=host_registry,
    )


def sandbox_state_for(ref: StaticHostRef) -> str:
    """The snapshot's ``sandbox_state`` for an offline static host."""
    return ref.status if ref.status in ("running", "gone") else "paused"


async def ensure_static_host_for(
    host_id: str,
    *,
    host_store: HostStore,
    sandbox_config: ManagedSandboxDeployment | None,
    host_registry: HostRegistry | None,
    on_stage: Any = None,
) -> HostConnection | None:
    """Wake *host_id* if it is a static host, returning its local tunnel.

    ``None`` when the host is not a static host (callers keep their existing
    path) or when it came online without a local registry to check.

    :raises OmnigentError: The ensure failed (409 gone, 503 otherwise), or
        ``WRONG_REPLICA`` when the host registered on another replica.
    """
    host = await asyncio.to_thread(host_store.get_host, host_id)
    ref = await find_static_host(host, sandbox_config)
    if ref is None:
        return None
    await ensure_vm_host(
        owner=ref.owner,
        provider=ref.provider,
        sandbox_id=ref.sandbox_id,
        sandbox_config=sandbox_config,
        host_store=host_store,
        on_stage=on_stage,
    )
    if host_registry is None:
        return None
    deadline = time.monotonic() + _LOCAL_TUNNEL_WAIT_S
    while (conn := host_registry.get(host_id)) is None and time.monotonic() < deadline:
        await asyncio.sleep(0.25)
    if conn is None and await asyncio.to_thread(host_store.is_online, host_id):
        raise OmnigentError(
            f"host {host_id} is live on another replica after its wake; retry without the "
            "routing key",
            code=ErrorCode.WRONG_REPLICA,
        )
    return conn


async def stop_vm_host(
    *,
    owner: str,
    provider: str,
    sandbox_id: str,
    sandbox_config: ManagedSandboxDeployment | None,
    host_store: HostStore,
) -> bool:
    """Remove *owner*'s static-sandbox entry and stop its host; the VM is left alone.

    Kills the host process with one exec (best effort: a paused VM has no
    running host), revokes the launch token, keeps the ``hosts`` row so session
    history still resolves, then deletes the provider's entry. Never terminates.

    :returns: Whether the entry existed.
    """
    launcher = static_launcher(sandbox_config, owner, provider).sandboxes
    entry = await _call(launcher.get_static_sandbox, sandbox_id, action="read your static sandbox")
    linked = entry.get("omnigent_host_id") if entry is not None else None
    if linked:
        host_id = _normalize_host_id(linked)
        if entry is not None and entry.get("status") == "running":
            try:
                await asyncio.to_thread(launcher.stop_host, sandbox_id)
            except Exception:  # noqa: BLE001 - the revoked token stops it reconnecting anyway.
                _logger.warning("could not stop the host on %s", sandbox_id, exc_info=True)
        host = await asyncio.to_thread(host_store.get_host, host_id)
        if host is not None and host.user_id == owner and host.sandbox_provider is None:
            await asyncio.to_thread(host_store.revoke_launch_token, host_id)
    forget_static_hosts(owner, provider)
    return bool(
        await _call(launcher.delete_static_sandbox, sandbox_id, action=f"remove {sandbox_id}")
    )
