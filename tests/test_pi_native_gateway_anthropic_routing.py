"""Native-Anthropic routing for a Databricks AI Gateway declared as ``kind: gateway``.

A ``kind: gateway`` provider commonly declares only its OpenAI-compatible
(Codex/Responses) family, pasting the Databricks AI Gateway ``.../codex/v1``
base URL. Claude is not served on that gateway's OpenAI ``/chat/completions``
surface — the same origin serves it at ``/anthropic``, which Pi speaks
natively. So instead of only warning that a Claude id landed on the OpenAI
wire, the resolver routes it to the derived Anthropic Messages surface *for
real*: ``api == "anthropic-messages"`` at the ``/anthropic`` base URL, bearer
auth, no warning.

This is gated on a recognized Databricks AI Gateway base URL (host allowlist +
path shape). A generic gateway's Messages URL is not derivable, so it keeps the
advisory-warning fallthrough — pinned in
``tests/test_pi_native_gateway_claude_routing.py``.

The model id is left verbatim on purpose: the user named it for this gateway,
and we only change which surface it is sent to (matching how the explicit-model
``databricks`` / ``cli-config`` paths already behave).
"""

from __future__ import annotations

from collections.abc import Callable

from omnigent.harnesses.pi_native import credentials as creds

# A dedicated-subdomain AI Gateway (shape 1: ``ai-gateway`` DNS label).
_DBX_GATEWAY_CODEX_URL = "https://myws.ai-gateway.cloud.databricks.com/codex/v1"
_DBX_GATEWAY_ANTHROPIC_URL = "https://myws.ai-gateway.cloud.databricks.com/anthropic"
# A workspace-hosted AI Gateway (shape 2: ``/ai-gateway/`` path prefix).
_DBX_WORKSPACE_CODEX_URL = "https://myws.cloud.databricks.com/ai-gateway/codex/v1"
_DBX_WORKSPACE_ANTHROPIC_URL = "https://myws.cloud.databricks.com/ai-gateway/anthropic"
# Databricks gateway HOSTS but NON-Codex paths: `is_databricks_ai_gateway_url`
# accepts them, yet `_gateway_anthropic_base_url` can't rewrite them — so they
# must fall through, never derive a bogus `.../anthropic` that 404s silently.
_DBX_GATEWAY_NONCODEX_URL = "https://myws.ai-gateway.cloud.databricks.com/openai/v1"
_DBX_WORKSPACE_MLFLOW_URL = "https://myws.cloud.databricks.com/ai-gateway/mlflow/v1"
# A distinct base URL for an explicitly configured anthropic family, so a test
# can tell "used the configured family" apart from "derived from openai".
_DBX_CONFIGURED_ANTHROPIC_URL = "https://anthropic.corp.example/v1"

_CLAUDE_MODEL = "claude-fable-5-1"
_GPT_MODEL = "gpt-5-4"


def _dbx_gateway_openai_only_config(
    *, base_url: str = _DBX_GATEWAY_CODEX_URL, default_model: str = _CLAUDE_MODEL
) -> Callable[[], dict[str, object]]:
    """A kind:gateway provider (default for pi) with ONLY an openai family whose
    base URL is a recognized Databricks AI Gateway."""

    def _loader() -> dict[str, object]:
        return {
            "providers": {
                "corp-gateway": {
                    "kind": "gateway",
                    "default": ["pi"],
                    "openai": {
                        "base_url": base_url,
                        "api_key": "test-gateway-key",
                        "wire_api": "chat",
                        "models": {"default": default_model},
                    },
                }
            }
        }

    return _loader


def _dbx_gateway_two_family_config() -> dict[str, object]:
    """A Databricks-gateway provider that ALSO declares its anthropic family.

    The anthropic family carries its own (explicitly configured) base URL, so
    the resolver must use it directly rather than deriving one from openai.
    """
    return {
        "providers": {
            "corp-gateway": {
                "kind": "gateway",
                "default": ["pi"],
                "anthropic": {
                    "base_url": _DBX_CONFIGURED_ANTHROPIC_URL,
                    "api_key": "configured-anthropic-key",
                    "models": {"default": _CLAUDE_MODEL},
                },
                "openai": {
                    "base_url": _DBX_GATEWAY_CODEX_URL,
                    "api_key": "test-gateway-key",
                    "wire_api": "chat",
                    "models": {"default": _GPT_MODEL},
                },
            }
        }
    }


# --------------------------------------------------------------------------- #
# The derivation helper in isolation                                          #
# --------------------------------------------------------------------------- #
def test_derivation_helper_only_fires_for_claude_on_databricks_openai_family() -> None:
    surface = creds._databricks_gateway_anthropic_surface
    # Claude id + Databricks openai family -> derives the /anthropic surface.
    assert surface("openai", _DBX_GATEWAY_CODEX_URL, _CLAUDE_MODEL) == _DBX_GATEWAY_ANTHROPIC_URL
    assert (
        surface("openai", _DBX_WORKSPACE_CODEX_URL, _CLAUDE_MODEL) == _DBX_WORKSPACE_ANTHROPIC_URL
    )
    # Not a Claude id -> no derivation (GPT belongs on the OpenAI wire).
    assert surface("openai", _DBX_GATEWAY_CODEX_URL, _GPT_MODEL) is None
    # Already the anthropic family -> nothing to derive.
    assert surface("anthropic", _DBX_GATEWAY_ANTHROPIC_URL, _CLAUDE_MODEL) is None
    # Generic (non-Databricks) gateway -> not derivable.
    assert surface("openai", "https://gw.example.com/openai/v1", _CLAUDE_MODEL) is None
    assert surface("openai", "http://127.0.0.1:9099/openai/v1", _CLAUDE_MODEL) is None
    # Databricks gateway HOST but a non-Codex path -> not derivable: blindly
    # appending /anthropic would 404 silently, so these must fall through.
    assert surface("openai", _DBX_GATEWAY_NONCODEX_URL, _CLAUDE_MODEL) is None
    assert surface("openai", _DBX_WORKSPACE_MLFLOW_URL, _CLAUDE_MODEL) is None


# --------------------------------------------------------------------------- #
# End-to-end resolution: openai-only Databricks gateway + a Claude model       #
# --------------------------------------------------------------------------- #
def test_databricks_gateway_openai_only_claude_default_routes_to_anthropic() -> None:
    """The family default Claude id is routed to the derived Anthropic surface."""
    provider = creds.resolve_pi_native_provider(
        config_loader=_dbx_gateway_openai_only_config(),
    )
    assert provider is not None
    assert provider.api == "anthropic-messages", (
        f"expected native Anthropic routing, got api={provider.api!r} at "
        f"{provider.base_url!r} — a Claude id was not routed to the gateway's "
        "Anthropic surface"
    )
    assert provider.base_url == _DBX_GATEWAY_ANTHROPIC_URL
    assert provider.auth_header is True  # Databricks gateway uses Authorization: Bearer
    assert provider.model == _CLAUDE_MODEL  # id left verbatim; only the surface changed
    assert provider.credential_warning is None, (
        "the model is routed correctly now, so no cross-family warning should fire; "
        f"got {provider.credential_warning!r}"
    )


def test_databricks_gateway_openai_only_claude_override_routes_to_anthropic() -> None:
    """Same routing when the Claude id arrives as a session model override."""
    provider = creds.resolve_pi_native_provider(
        model=_CLAUDE_MODEL,
        config_loader=_dbx_gateway_openai_only_config(default_model=_GPT_MODEL),
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.base_url == _DBX_GATEWAY_ANTHROPIC_URL
    assert provider.model == _CLAUDE_MODEL
    assert provider.credential_warning is None


def test_databricks_gateway_workspace_hosted_shape_derives_anthropic() -> None:
    """A workspace-hosted gateway (``/ai-gateway/`` path) also derives correctly."""
    provider = creds.resolve_pi_native_provider(
        config_loader=_dbx_gateway_openai_only_config(base_url=_DBX_WORKSPACE_CODEX_URL),
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    assert provider.base_url == _DBX_WORKSPACE_ANTHROPIC_URL


def test_databricks_gateway_openai_only_gpt_stays_on_openai() -> None:
    """A GPT id on the same Databricks gateway stays on the OpenAI surface.

    Only Claude ids are re-routed; GPT is served natively over the gateway's
    OpenAI wire, so the base URL and api must be the configured openai family's.
    """
    provider = creds.resolve_pi_native_provider(
        model=_GPT_MODEL,
        config_loader=_dbx_gateway_openai_only_config(),
    )
    assert provider is not None
    # wire_api: chat -> openai-completions on the configured openai base URL.
    assert provider.api == "openai-completions"
    assert provider.base_url == _DBX_GATEWAY_CODEX_URL
    assert provider.model == _GPT_MODEL


def test_configured_anthropic_family_used_directly_not_derived() -> None:
    """When the anthropic family IS declared, it wins — derivation only fills a gap.

    A two-family Databricks gateway must route Claude to the *configured*
    anthropic base URL (which could differ from the derived one), never the
    derived openai-family surface.
    """
    provider = creds.resolve_pi_native_provider(
        model=_CLAUDE_MODEL,
        config_loader=_dbx_gateway_two_family_config,
    )
    assert provider is not None
    assert provider.api == "anthropic-messages"
    # The *configured* anthropic base URL (deliberately distinct from the URL
    # that would be derived from openai) — proves the family won, not derivation.
    assert provider.base_url == _DBX_CONFIGURED_ANTHROPIC_URL


def test_databricks_gateway_noncodex_path_falls_through_to_warning() -> None:
    """A Databricks gateway host on a non-Codex path is NOT silently rerouted.

    `_gateway_anthropic_base_url` can only rewrite a `.../codex/v1` URL; a
    gateway host on `/openai/v1` (etc.) must fall through to the advisory
    warning, never derive a bogus `/openai/v1/anthropic` that launches with no
    warning and 404s on every turn — the precise failure this PR removes.
    """
    provider = creds.resolve_pi_native_provider(
        config_loader=_dbx_gateway_openai_only_config(base_url=_DBX_GATEWAY_NONCODEX_URL),
    )
    assert provider is not None
    assert provider.api == "openai-completions"  # stayed on the openai wire
    assert provider.base_url == _DBX_GATEWAY_NONCODEX_URL  # NOT rewritten
    assert provider.credential_warning is not None, (
        "a non-Codex Databricks gateway path must surface the advisory warning, "
        "not launch as a bogus silent anthropic reroute"
    )
