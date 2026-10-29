"""Databricks Unity Gateway routing for native Codex launches."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.inner import databricks_executor
from omnigent.inner.codex_executor import (
    _databricks_codex_auth_command,
    _databricks_codex_base_url,
    _databricks_codex_config_overrides,
)
from omnigent.models import model_catalog
from omnigent.models.codex_model_vocabulary import codex_reachable_model_slug

if TYPE_CHECKING:
    from omnigent.harnesses.codex_native.app_server import NativeCodexLaunch
    from omnigent.spec.types import AgentSpec

_logger = logging.getLogger(__name__)


def profile_host(profile: str) -> str | None:
    """Read the explicitly selected profile's host without ambient SDK overrides."""
    return databricks_executor._read_databrickscfg_host(profile)


def profile_base_url(profile: str) -> str | None:
    """Resolve the same gateway URL used by a Databricks-profile launch."""
    host = databricks_executor._databricks_gateway_host(profile)
    return codex_gateway(host.rstrip("/"), profile)[0] if host else None


@dataclass(frozen=True)
class DatabricksLaunchMaterialization:
    """
    The Databricks-profile pieces of a Codex launch, shared by the real
    app-server build and the model-options probe so the two cannot drift.

    :param config_overrides: ``-c`` overrides routing Codex through the
        profile's Unity Gateway (provider block + auth command + model pin).
    :param model: The model the overrides pin, e.g. ``"databricks-gpt-5-4"``
        — the explicit *model* when given, else the catalog default.
    :param host: The profile's workspace origin for ``DATABRICKS_HOST``.
    """

    config_overrides: list[str]
    model: str
    host: str


class CodexDatabricksProfileMissingError(OmnigentError, OSError):
    """The provider config names a Databricks profile the runner cannot resolve.

    Coded ``HARNESS_NOT_CONFIGURED``: only the person can fix the profile, and
    the message says how, so a terminal start failure shows it verbatim.
    Subclasses :class:`OSError`, the documented error of this launch step.
    """

    def __init__(self, profile: str) -> None:
        """
        :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
        """
        super().__init__(
            f"Native Codex with Databricks profile {profile!r} (from your "
            "provider config) requires a matching ~/.databrickscfg section "
            "with a host visible to the runner process. Add it with "
            f"`databricks auth login --profile {profile}`, or point the provider "
            "at an existing profile.",
            code=ErrorCode.HARNESS_NOT_CONFIGURED,
        )


def materialize_launch(
    *, model: str | None, profile: str, codex_path: str | None = None
) -> DatabricksLaunchMaterialization:
    """
    Resolve the Databricks-profile routing pieces of a Codex launch.

    Uses the profile's own host, so a ``DATABRICKS_HOST`` override in the
    runner env must not point the launch at another workspace. The gateway
    and auth come from ucode state for that host when present (see
    :func:`codex_gateway`). Model discovery still uses the
    profile's credentials; ucode state is keyed by the same host, so both
    see that workspace's catalog.

    :param model: Optional explicit model pin; ``None`` resolves the
        catalog default.
    :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
    :param codex_path: Executable whose shared catalog may resolve the model.
    :returns: The materialized overrides, pinned model, and host.
    :raises CodexDatabricksProfileMissingError: When the profile resolves no
        workspace host.
    """
    host = databricks_executor._databricks_gateway_host(profile)
    if not host:
        raise CodexDatabricksProfileMissingError(profile)
    host = host.rstrip("/")
    resolved_model = resolve_model(host, profile, model, codex_path=codex_path)
    base_url, auth_command, auth_refresh_interval_ms, auth_config = codex_gateway(host, profile)
    return DatabricksLaunchMaterialization(
        config_overrides=_databricks_codex_config_overrides(
            model=resolved_model,
            base_url=base_url,
            auth_command=auth_command,
            auth_refresh_interval_ms=auth_refresh_interval_ms,
            auth_config=auth_config,
        ),
        model=resolved_model,
        host=host,
    )


def codex_gateway(
    host: str, profile: str
) -> tuple[str, str, int | None, dict[str, object] | None]:
    """
    Return the Codex gateway URL, shell helper, cadence and structured auth for *host*.

    ucode state is the source of truth for the gateway and auth; the
    profile-derived gateway is the fallback.

    :param host: Databricks workspace host, e.g. ``"https://example.databricks.com"``.
    :param profile: ``~/.databrickscfg`` profile name, e.g. ``"oss"``.
    :returns: ucode's ``codex`` entry for *host* when it names both a base URL and
        an auth command, else the profile-derived gateway with default auth settings.
    """
    from omnigent.onboarding.ucode_state import read_ucode_state

    try:
        workspace_state = read_ucode_state(host)
    except (OSError, ValueError):  # unreadable ucode state keeps the profile path
        # Host logging mirrors WARNINGs to the terminal; frames are debug-only.
        _logger.warning(
            "native-codex: could not read ucode state for %r",
            host,
            exc_info=_logger.isEnabledFor(logging.DEBUG),
        )
        workspace_state = None
    agent = workspace_state.agent("codex") if workspace_state is not None else None
    if agent is not None and agent.base_url and agent.auth_command:
        from omnigent.onboarding.ucode_setup import (
            refresh_ucode_auth_command,
            refresh_ucode_auth_config,
        )

        return (
            agent.base_url,
            refresh_ucode_auth_command(agent.auth_command),
            agent.auth_refresh_interval_ms,
            refresh_ucode_auth_config(agent.auth),
        )
    return (
        _databricks_codex_base_url(host),
        _databricks_codex_auth_command(host, profile),
        None,
        None,
    )


def resolve_model(
    host: str, profile: str, requested: str | None, *, codex_path: str | None = None
) -> str:
    """Resolve the launch model against the workspace's Unity Gateway catalog.

    Use the live Unity Catalog listing, then ucode's cached copy of it,
    then the bundled catalog as the last resort.

    An explicit model first reuses its fresh shared Codex catalog row's model
    for the same profile, workspace and executable. A miss keeps discovery.
    The explicit model is matched against the servable ids, so a legacy
    ``model_override`` persisted before this change still launches; one the
    workspace does not serve passes through untouched, because the gateway's
    error beats a silent substitution.

    :param host: Workspace origin, e.g. ``"https://example.com"``.
    :param profile: Databricks CLI profile backing the launch.
    :param requested: Explicit model id, or ``None`` to take the newest
        servable one.
    :param codex_path: Executable whose shared catalog may resolve the model.
    :returns: The model id to pin on the codex launch.
    """
    from omnigent.models.databricks_model_discovery import (
        discover_databricks_codex_models,
        select_servable_model,
    )

    if requested:
        configured_host = profile_host(profile)
        if configured_host and configured_host.rstrip("/") == host.rstrip("/"):
            from omnigent.harnesses.codex_native.app_server import (
                NativeCodexLaunch,
                fresh_codex_launch_catalog,
            )

            rows = fresh_codex_launch_catalog(
                launch=NativeCodexLaunch([], None, profile), codex_path=codex_path
            )
            if rows:
                slug = codex_reachable_model_slug(requested, rows)
                model = next((row.get("model") for row in rows if row.get("id") == slug), None)
                if isinstance(model, str) and model.strip():
                    return model.strip()

    servable: tuple[str, ...] = ()
    try:
        from omnigent.runtime.credentials.databricks import resolve_databricks_workspace

        creds = resolve_databricks_workspace(profile)
        # The SDK honors DATABRICKS_HOST; discover against the profile's actual
        # launch host so another workspace's catalog cannot supply the model.
        servable = discover_databricks_codex_models(host, creds.token)
    except Exception as exc:  # cached ucode state is the launch fallback
        # Recoverable fallback; frames are debug-only so a TTY-mirrored
        # host console stays concise.
        _logger.warning(
            "native-codex: live Databricks model discovery failed for profile %r; "
            "falling back to ucode state (%s)",
            profile,
            exc,
            exc_info=_logger.isEnabledFor(logging.DEBUG),
        )
        try:
            from omnigent.onboarding.ucode_state import read_ucode_state

            workspace_state = read_ucode_state(host)
            if workspace_state is not None:
                servable = tuple(workspace_state.codex_models)
        except Exception:  # the bundled catalog is the last resort
            _logger.warning(
                "native-codex: could not read ucode state for %r", profile, exc_info=True
            )

    if requested:
        return select_servable_model(requested, servable) or requested
    if servable:
        return servable[0]
    return model_catalog.resolve_catalog_model("databricks", family="openai").model_id


def managed_broker_launch(
    *, model: str | None, spec: AgentSpec | None
) -> NativeCodexLaunch | None:
    """Use the managed host's broker when no provider or API-key auth selected a route."""
    from omnigent.host.databricks_credential import (
        HOST_DATABRICKS_PROFILE,
        api_key_auth_precludes_broker,
        broker_token_command,
    )

    if api_key_auth_precludes_broker(spec):
        return None
    host = profile_host(HOST_DATABRICKS_PROFILE)
    if not host or not broker_token_command(host.rstrip("/")):
        return None
    host = host.rstrip("/")
    resolved_model = resolve_model(host, HOST_DATABRICKS_PROFILE, model)

    from omnigent.harnesses.codex_native.app_server import NativeCodexLaunch
    from omnigent.process_logging import log_info_once

    log_info_once(
        _logger,
        "native-codex routing: managed connect host — Databricks Unity Gateway "
        "via the credential broker (host-only [omnigent] profile + sidecar).",
    )
    return NativeCodexLaunch(
        config_overrides=_databricks_codex_config_overrides(
            model=resolved_model,
            base_url=_databricks_codex_base_url(host),
            auth_command=_databricks_codex_auth_command(host, HOST_DATABRICKS_PROFILE),
        ),
        model=resolved_model,
        profile=None,
        summary="Databricks Unity Gateway (managed connect host, broker-minted)",
    )
