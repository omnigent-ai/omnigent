"""Regression guards for pi-native cross-family gateway misrouting.

A pi-native session launched with a Claude-family model against a
``kind: gateway`` provider that configures only an ``openai`` family
silently renders a managed ``models.json`` with ``api: openai-completions``
and the gateway's OpenAI base URL. Every turn then fails with
``404 status code (no body)`` because the gateway serves that Claude model
only on its Anthropic surface, and no routing warning is surfaced.

These tests pin the *resolution boundary* the runner calls at launch
(:func:`resolve_pi_native_provider`), which is where all three reported
defects live (``omnigent/harnesses/pi_native/credentials.py``). They encode
the reported bug's suggested regression tests A/B/C and assert the
**expected** (correct) behaviour, so they fail on the buggy build and pass
once the fix lands:

* A - openai-only gateway + a Claude-family model must not silently resolve
  to ``openai-completions`` with no routing warning (fail loud, or refuse).
* B - an unmanaged ``provider/model`` override must never be registered
  verbatim as a literal model id carrying a ``provider/`` prefix.
* C - a gateway provider's ``anthropic`` family override must keep its
  ``databricks-`` gateway prefix verbatim (the gateway routes by that
  prefixed endpoint name).

The user-visible half of this journey (the ``404`` in the Pi TUI) is filmed
by the CLI e2e sibling
``tests/e2e/test_pi_native_gateway_claude_misroute_e2e.py``.
"""

from __future__ import annotations

from omnigent.harnesses.pi_native import credentials as creds

# A Claude-family model id the gateway serves only on its Anthropic surface.
_CLAUDE_MODEL = "claude-fable-5-1"
# The same id addressed by its Databricks-gateway endpoint name.
_GATEWAY_CLAUDE_MODEL = "databricks-claude-fable-5-1"


def _openai_only_gateway_config() -> dict[str, object]:
    """A kind:gateway provider (default for pi) with ONLY an openai family.

    ``wire_api: chat`` + the gateway's OpenAI base URL; its default model is a
    Claude-family id the gateway serves only on its Anthropic surface.
    """
    return {
        "providers": {
            "rpw-fable": {
                "kind": "gateway",
                "default": ["pi"],
                "openai": {
                    "base_url": "http://127.0.0.1:9099/openai/v1",
                    "api_key": "test-gateway-key",
                    "wire_api": "chat",
                    "models": {"default": _CLAUDE_MODEL},
                },
            }
        }
    }


def _anthropic_gateway_config() -> dict[str, object]:
    """A kind:gateway provider (default for pi) with an anthropic family.

    Its default model carries the ``databricks-`` gateway endpoint prefix; the
    gateway routes by that prefixed name.
    """
    return {
        "providers": {
            "rpw-fable": {
                "kind": "gateway",
                "default": ["pi"],
                "anthropic": {
                    "base_url": "http://127.0.0.1:9099/anthropic",
                    "api_key": "test-gateway-key",
                    "models": {"default": _GATEWAY_CLAUDE_MODEL},
                },
            }
        }
    }


def test_gateway_openai_only_claude_default_is_not_silently_misrouted() -> None:
    """Defect 1 (default-model path): openai-only gateway + Claude default.

    The provider's default model is the Claude-family id. Resolution must not
    silently yield ``api == "openai-completions"`` with no routing warning:
    a Claude id POSTed to ``/chat/completions`` 404s. The fix should fail loud
    (surface a routing warning via ``credential_warning``) or refuse the model
    (return ``None``).
    """
    provider = creds.resolve_pi_native_provider(config_loader=_openai_only_gateway_config)
    assert not (
        provider is not None
        and provider.api == "openai-completions"
        and provider.credential_warning is None
    ), (
        "openai-only gateway silently routed the Claude-family model "
        f"{_CLAUDE_MODEL!r} to openai-completions with no routing warning "
        "(base_url="
        f"{getattr(provider, 'base_url', None)!r}) - every turn 404s with no "
        "explanation to the user"
    )


def test_gateway_openai_only_claude_override_is_not_silently_misrouted() -> None:
    """Defect 1 (explicit-override path): openai-only gateway + Claude override.

    Same silent cross-family fallthrough when the Claude id arrives as an
    explicit session model override rather than the provider default.
    """
    provider = creds.resolve_pi_native_provider(
        model=_CLAUDE_MODEL,
        config_loader=_openai_only_gateway_config,
    )
    assert not (
        provider is not None
        and provider.api == "openai-completions"
        and provider.credential_warning is None
    ), (
        "openai-only gateway silently routed the Claude-family override "
        f"{_CLAUDE_MODEL!r} to openai-completions with no routing warning"
    )


def test_unmanaged_provider_prefix_override_not_registered_verbatim() -> None:
    """Defect 2: an unmanaged ``provider/model`` override is registered verbatim.

    pi-native relocates ``PI_CODING_AGENT_DIR`` so a user's ``~/.pi/agent``
    providers are never read, and the managed-picker split only recognises
    ``omnigent*`` prefixes. An override like
    ``rpw-fable/databricks-claude-fable-5-1`` is therefore registered as a
    literal model id under provider ``omnigent`` - a slash-bearing id the
    gateway can't route, so the same 404. The rendered ``models.json`` must
    never contain a model id carrying an unmanaged ``provider/`` prefix.
    """
    provider = creds.resolve_pi_native_provider(
        model="rpw-fable/databricks-claude-fable-5-1",
        config_loader=_openai_only_gateway_config,
    )
    if provider is None:
        return  # Rejecting the unmanaged prefix outright is an acceptable fix.
    rendered = provider.to_models_config()
    offending = [
        entry.get("id")
        for payload in rendered["providers"].values()
        for entry in payload.get("models", [])
        if isinstance(entry.get("id"), str) and "/" in entry["id"]
    ]
    assert not offending, (
        "rendered models.json registered an unmanaged provider/ prefixed model "
        f"id verbatim: {offending!r} - the gateway can't route a slash id, so "
        "every turn 404s"
    )


def test_gateway_anthropic_override_keeps_databricks_prefix_verbatim() -> None:
    """Defect 3: gateway anthropic override loses its ``databricks-`` prefix.

    ``_inline_family_pi_provider`` hardcodes ``KEY_KIND`` in its
    ``normalize_model_for_provider`` call, so a gateway override of
    ``databricks-claude-fable-5-1`` is stripped to ``claude-fable-5-1`` - wrong
    for a gateway fronting the Databricks AI Gateway, which expects the
    prefixed endpoint name. The family ``models.default`` path (control below)
    already keeps it verbatim; the override path must too.
    """
    override = creds.resolve_pi_native_provider(
        model=_GATEWAY_CLAUDE_MODEL,
        config_loader=_anthropic_gateway_config,
    )
    assert override is not None
    assert override.model == _GATEWAY_CLAUDE_MODEL, (
        "gateway override lost its databricks- endpoint prefix: rendered "
        f"{override.model!r} instead of {_GATEWAY_CLAUDE_MODEL!r} - the gateway "
        "serves the prefixed endpoint name, so the stripped id 404s"
    )


def test_gateway_anthropic_default_keeps_databricks_prefix_verbatim() -> None:
    """Control for defect 3: the family default path already keeps the prefix.

    Confirms the divergence is override-specific (the reported asymmetry): the
    ``models.default`` path renders ``databricks-claude-fable-5-1`` verbatim.
    """
    default = creds.resolve_pi_native_provider(config_loader=_anthropic_gateway_config)
    assert default is not None
    assert default.model == _GATEWAY_CLAUDE_MODEL
