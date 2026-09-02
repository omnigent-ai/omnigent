"""Host-side checks for whether a harness family's inference is AI-Gateway-backed.

Smart Routing's apply layer can only rewrite a launch's model when the launch
resolves through the Databricks AI Gateway — that is where the routable model
catalog lives. These checks answer that question per harness family from config
resolution alone: no process launch, no network round-trip, so the host can
report the answer alongside harness readiness on every registration.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from typing import Final

_logger = logging.getLogger(__name__)

# Every spelling the Claude family travels under on the wire.
CLAUDE_GATEWAY_HARNESSES: Final[tuple[str, ...]] = ("claude-native", "native-claude")

# Every spelling the Codex family travels under on the wire.
CODEX_GATEWAY_HARNESSES: Final[tuple[str, ...]] = ("codex", "codex-native", "native-codex")

# The AI Gateway serves Codex/OpenAI-Responses under this path suffix; both
# gateway URL shapes (dedicated subdomain and workspace-hosted) end with it.
_CODEX_GATEWAY_PATH_SUFFIX = "/codex/v1"


def claude_gateway_inference_backed() -> bool:
    """Whether a claude-native launch on this host resolves gateway-backed inference.

    A gateway-backed launch pins a Databricks AI Gateway ``ANTHROPIC_BASE_URL``
    and delivers its bearer token through Claude Code's ``apiKeyHelper``. The
    base URL must be a genuine Databricks AI Gateway (validated with
    :func:`is_databricks_ai_gateway_url`, parity with the Codex check), since
    the external router's picks are Databricks catalog ids only that endpoint
    serves. The Bedrock path sets ``ANTHROPIC_BEDROCK_BASE_URL`` with no
    helper — not routable.

    A subscription / CLI login resolves no omnigent config, yet Claude Code
    still routes all inference through an AI Gateway when an enterprise managed
    settings file pins it. Managed settings win at the actual launch, so that
    signal counts too: it flips the answer to ``True`` even when resolution
    yields nothing.

    :returns: ``True`` iff a claude-native launch resolves AI-Gateway-backed
        inference, from omnigent config or managed settings.
    """
    from omnigent.claude_native import (
        managed_claude_gateway_signal,
        resolve_native_claude_config,
    )
    from omnigent.databricks_ai_gateway import is_databricks_ai_gateway_url

    config = resolve_native_claude_config(spec=None, refresh_models=False)
    if config is not None:
        base_url = config.env.get("ANTHROPIC_BASE_URL")
        if base_url and config.api_key_helper and is_databricks_ai_gateway_url(base_url):
            return True

    managed_base_url, managed_has_credential = managed_claude_gateway_signal()
    if (
        managed_base_url is not None
        and managed_has_credential
        and is_databricks_ai_gateway_url(managed_base_url)
    ):
        return True

    return False


def codex_gateway_inference_backed() -> bool:
    """Whether a codex-native launch on this host resolves gateway-backed inference.

    :returns: ``True`` iff the resolved launch routes through an AI Gateway
        Codex base URL.
    """
    from omnigent.codex_native_app_server import (
        native_codex_launch_base_url,
        resolve_native_codex_launch,
    )
    from omnigent.databricks_ai_gateway import is_databricks_ai_gateway_url

    base_url = native_codex_launch_base_url(resolve_native_codex_launch(model=None))
    if not base_url:
        return False
    if not is_databricks_ai_gateway_url(base_url):
        return False
    return base_url.rstrip("/").endswith(_CODEX_GATEWAY_PATH_SUFFIX)


def gateway_inference_map() -> dict[str, bool]:
    """Per-harness map of whether this host's inference for that family is gateway-backed.

    Each family is evaluated once and the result fanned out over every spelling
    that family travels under. A family whose check raises is omitted rather
    than reported as ``False``, so the server can tell "not gateway-backed"
    apart from "could not tell".

    :returns: Harness spelling → gateway-backed flag, omitting unevaluable
        families.
    """
    result: dict[str, bool] = {}
    for family, spellings, check in (
        ("claude", CLAUDE_GATEWAY_HARNESSES, claude_gateway_inference_backed),
        ("codex", CODEX_GATEWAY_HARNESSES, codex_gateway_inference_backed),
    ):
        try:
            backed = check()
        except Exception:  # noqa: BLE001 — an unevaluable family is omitted, not False
            _logger.warning(
                "gateway-inference check for the %s family failed; omitting it",
                family,
                exc_info=True,
            )
            continue
        for spelling in spellings:
            result[spelling] = backed
    return result


def gateway_inference_state(
    gateway: Mapping[str, object] | None,
    harness: str,
) -> bool | None:
    """Read *harness*'s gateway-backed flag out of a reported map.

    :param gateway: A host's ``gateway_inference`` map, or ``None``.
    :param harness: Harness id in any spelling, e.g. ``"native-codex"``.
    :returns: The reported flag, or ``None`` when the map says nothing about
        this harness — an older host, a family whose check could not run, or a
        host that has not registered yet. Unknown is not "unavailable".
    """
    if not gateway:
        return None
    for key in _family_spellings(harness):
        value = gateway.get(key)
        if isinstance(value, bool):
            return value
    return None


def _family_spellings(harness: str) -> tuple[str, ...]:
    """Every key a host may have reported *harness*'s family under.

    :func:`gateway_inference_map` fans one family verdict out over all of its
    spellings, but a caller holds only one — and the reversed aliases
    (``native-codex``) never canonicalize back. Look the family up instead, so
    any spelling finds the entry.

    :param harness: Harness id in any spelling, e.g. ``"native-codex"``.
    :returns: The family's spellings, or just *harness* when it is in neither.
    """
    from omnigent.harness_aliases import canonicalize_harness

    canonical = canonicalize_harness(harness) or harness
    for spellings in (CLAUDE_GATEWAY_HARNESSES, CODEX_GATEWAY_HARNESSES):
        if canonical in spellings or harness in spellings:
            return spellings
    return (canonical, harness)


def not_gateway_backed(
    gateway: Mapping[str, object] | None,
    harnesses: Iterable[str],
) -> list[str]:
    """Which of *harnesses* the map explicitly reports as not gateway-backed.

    Smart Routing's apply layer rewrites the launch model through the AI
    Gateway, so these are the harnesses a routed pick could not reach. Only an
    explicit ``False`` counts: unknown keeps every option.

    :param gateway: A host's ``gateway_inference`` map, or ``None``.
    :param harnesses: Harness ids to check, e.g.
        ``("claude-native", "codex-native")``.
    :returns: The not-backed ids, in the order given.
    """
    return [harness for harness in harnesses if gateway_inference_state(gateway, harness) is False]
