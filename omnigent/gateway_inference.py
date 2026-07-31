"""Host-side checks for whether a harness family's inference is AI-Gateway-backed.

Smart Routing's apply layer can only rewrite a launch's model when the launch
resolves through the Databricks AI Gateway — that is where the routable model
catalog lives. These checks answer that question per harness family from config
resolution alone: no process launch, no network round-trip, so the host can
report the answer alongside harness readiness on every registration.
"""

from __future__ import annotations

import logging
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

    A gateway-backed launch pins ``ANTHROPIC_BASE_URL`` and delivers its bearer
    token through Claude Code's ``apiKeyHelper``. The Bedrock path sets
    ``ANTHROPIC_BEDROCK_BASE_URL`` with no helper, and a subscription / CLI
    login resolves no config at all — neither is routable.

    :returns: ``True`` iff the resolved config is AI-Gateway-backed.
    """
    from omnigent.claude_native import resolve_native_claude_config

    config = resolve_native_claude_config(spec=None, refresh_models=False)
    if config is None:
        return False
    return bool(config.env.get("ANTHROPIC_BASE_URL")) and bool(config.api_key_helper)


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
