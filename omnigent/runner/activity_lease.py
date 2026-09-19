"""Provider-neutral activity leases for suspendable managed runners."""

from __future__ import annotations

import asyncio
import importlib
import logging
import os
from collections.abc import Callable
from typing import Protocol, TypeAlias

ACTIVITY_LEASE_PROVIDER_ENV_VAR = "OMNIGENT_ACTIVITY_LEASE_PROVIDER"
_DEFAULT_POLL_INTERVAL_S = 1.0
_DEFAULT_REFRESH_INTERVAL_S = 60.0
_DEFAULT_RELEASE_GRACE_S = 30.0
_DEFAULT_RETRY_INTERVAL_S = 5.0

logger = logging.getLogger(__name__)


class ActivityLeaseError(Exception):
    """A provider activity lease operation failed."""


class ActivityLease(Protocol):
    """Provider adapter that can refresh and release an activity hold."""

    provider: str

    async def refresh(self) -> None:
        """Create or extend the provider's activity hold."""

    async def release(self) -> None:
        """Release the provider's activity hold if it exists."""

    async def aclose(self) -> None:
        """Close resources owned by the adapter."""


ActivityLeaseFactory: TypeAlias = Callable[[str], ActivityLease]

_ACTIVITY_LEASE_FACTORIES: dict[str, ActivityLeaseFactory] = {}
_BUILTIN_LEASE_MODULES: dict[str, str] = {}


def register_activity_lease_provider(provider: str, factory: ActivityLeaseFactory) -> None:
    """Register a provider adapter factory with the runner lease framework.

    Provider integrations own their adapter and registration. The shared
    runner lifecycle resolves only the provider name and its lazily loaded
    module; it never imports a concrete adapter eagerly.

    :param provider: Environment-facing provider name, e.g. ``"acme"``.
    :param factory: Callable that builds a lease for a stable runner id.
    :raises ValueError: If *provider* is empty or already registered.
    """
    normalized = provider.strip().lower()
    if not normalized:
        raise ValueError("activity lease provider name must not be empty")
    existing = _ACTIVITY_LEASE_FACTORIES.get(normalized)
    if existing is not None and existing is not factory:
        raise ValueError(f"activity lease provider {normalized!r} is already registered")
    _ACTIVITY_LEASE_FACTORIES[normalized] = factory


def activity_lease_from_env(runner_id: str) -> ActivityLease | None:
    """Build the explicitly configured activity-lease backend, if any.

    The runner never guesses from filesystem artifacts. A sandbox launcher
    opts in by setting ``OMNIGENT_ACTIVITY_LEASE_PROVIDER`` in the host
    service environment.

    :param runner_id: Stable runner id used to namespace provider leases.
    :returns: Configured lease adapter, or ``None`` when leasing is disabled.
    :raises RuntimeError: If the configured backend is unsupported.
    """
    provider = os.environ.get(ACTIVITY_LEASE_PROVIDER_ENV_VAR)
    if provider is None:
        return None
    provider = provider.strip().lower()
    if not provider:
        raise RuntimeError(f"{ACTIVITY_LEASE_PROVIDER_ENV_VAR} must not be empty")
    factory = _ACTIVITY_LEASE_FACTORIES.get(provider)
    module_name = _BUILTIN_LEASE_MODULES.get(provider)
    if factory is None and module_name is not None:
        module = importlib.import_module(module_name)
        register = getattr(module, "register_activity_lease_provider", None)
        if not callable(register):
            raise RuntimeError(
                f"activity lease module {module_name!r} does not expose "
                "register_activity_lease_provider()"
            )
        register()
        factory = _ACTIVITY_LEASE_FACTORIES.get(provider)
    if factory is None:
        raise RuntimeError(
            f"unsupported activity lease provider {provider!r} in "
            f"{ACTIVITY_LEASE_PROVIDER_ENV_VAR}"
        )
    return factory(runner_id)


async def run_activity_lease(
    *,
    lease: ActivityLease,
    has_active_work: Callable[[], bool],
    poll_interval_s: float = _DEFAULT_POLL_INTERVAL_S,
    refresh_interval_s: float = _DEFAULT_REFRESH_INTERVAL_S,
    release_grace_s: float = _DEFAULT_RELEASE_GRACE_S,
    retry_interval_s: float = _DEFAULT_RETRY_INTERVAL_S,
) -> None:
    """Hold a suspendable runner active only while work is outstanding.

    Activity accounting is supplied by the runner and is independent of both
    the selected harness and the sandbox provider. The adapter owns the
    provider-specific acquire/release transport.

    :param lease: Provider activity-lease adapter.
    :param has_active_work: Callback reporting delivery-critical runner work.
    :param poll_interval_s: Active-work polling interval.
    :param refresh_interval_s: Successful lease refresh cadence.
    :param release_grace_s: Idle grace before releasing a held lease.
    :param retry_interval_s: Retry delay after a provider operation fails.
    :returns: Never under normal operation; cancellation releases the lease.
    """
    held = False
    next_refresh_at = 0.0
    next_release_at = 0.0
    loop = asyncio.get_running_loop()

    try:
        while True:
            active = has_active_work()
            now = loop.time()
            if active:
                next_release_at = 0.0
                if now >= next_refresh_at:
                    try:
                        await lease.refresh()
                    except ActivityLeaseError:
                        logger.warning(
                            "failed to refresh %s activity lease; active work may suspend",
                            lease.provider,
                            exc_info=True,
                        )
                        next_refresh_at = now + retry_interval_s
                    else:
                        held = True
                        next_refresh_at = now + refresh_interval_s
            elif held:
                if next_release_at == 0.0:
                    next_release_at = now + release_grace_s
                if now >= next_release_at:
                    try:
                        await lease.release()
                    except ActivityLeaseError:
                        logger.warning(
                            "failed to release %s activity lease",
                            lease.provider,
                            exc_info=True,
                        )
                        next_release_at = now + retry_interval_s
                    else:
                        held = False
            await asyncio.sleep(poll_interval_s)
    finally:
        if held:
            try:
                await lease.release()
            except ActivityLeaseError:
                logger.debug(
                    "failed to release %s activity lease during shutdown",
                    lease.provider,
                    exc_info=True,
                )
        await lease.aclose()
