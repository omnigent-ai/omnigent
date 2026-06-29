"""Tests for omnigent.onboarding.detected — ambient → provider bridge.

Covers synthesizing config-shape provider entries from ambient detections,
the read-time merge (explicit wins; detected auto-default per family), and
the adopt set ``configure harnesses`` persists. Detections are constructed as
real :class:`~omnigent.onboarding.ambient.DetectedProvider` objects (not
mocks) so a regression in field handling surfaces here.
"""

from __future__ import annotations

import pytest

from omnigent.onboarding.ambient import DetectedProvider
from omnigent.onboarding.detected import (
    effective_config_with_detected,
    providers_to_adopt,
    synthesize_detected_entries,
)
from omnigent.onboarding.provider_config import (
    ANTHROPIC_FAMILY,
    GEMINI_FAMILY,
    OPENAI_FAMILY,
    default_provider_for_harness,
    get_default_provider,
    load_providers,
)


def _anthropic_key() -> DetectedProvider:
    """An ambient ANTHROPIC_API_KEY detection."""
    return DetectedProvider(
        name="anthropic", kind="key", family=ANTHROPIC_FAMILY, source="$ANTHROPIC_API_KEY"
    )


def _gemini_key() -> DetectedProvider:
    """An ambient GEMINI_API_KEY detection (the antigravity / GEMINI_API_KEY credential)."""
    return DetectedProvider(
        name="gemini", kind="key", family=GEMINI_FAMILY, source="$GEMINI_API_KEY"
    )


def _codex_login() -> DetectedProvider:
    """An ambient codex CLI login detection."""
    return DetectedProvider(
        name="codex", kind="subscription", family=OPENAI_FAMILY, source="codex CLI login"
    )


def test_synthesize_env_key_anthropic() -> None:
    """An anthropic env key becomes a ``key`` entry with an ``env:`` ref.

    Failure means the detected key wouldn't route (wrong kind/family) or
    would leak as an inline secret instead of an env reference.
    """
    entries = synthesize_detected_entries([_anthropic_key()])
    assert set(entries) == {"anthropic"}
    parsed = load_providers({"providers": entries})["anthropic"]
    assert parsed.kind == "key"
    assert set(parsed.families) == {"anthropic"}
    # The env var is referenced (env:), never the resolved secret value.
    assert entries["anthropic"]["anthropic"]["api_key_ref"] == "env:ANTHROPIC_API_KEY"
    assert entries["anthropic"]["anthropic"]["base_url"] == "https://api.anthropic.com"


def test_synthesize_env_key_openrouter_uses_vendor_endpoint_and_chat_wire() -> None:
    """A detected OpenRouter key gets OpenRouter's base_url + chat wire.

    OpenRouter is OpenAI-compatible but is NOT api.openai.com and only
    speaks Chat Completions. Failure (defaulting to the openai endpoint or
    omitting the chat wire) is the classic "OpenRouter didn't work" bug.
    """
    det = DetectedProvider(
        name="openrouter", kind="key", family=OPENAI_FAMILY, source="$OPENROUTER_API_KEY"
    )
    entries = synthesize_detected_entries([det])
    openai_block = entries["openrouter"]["openai"]
    assert openai_block["base_url"] == "https://openrouter.ai/api/v1"
    # Chat wire is required — Responses would 404 against OpenRouter.
    assert openai_block["wire_api"] == "chat"
    assert openai_block["api_key_ref"] == "env:OPENROUTER_API_KEY"


def test_synthesize_env_key_openai_honors_openai_base_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detected ``OPENAI_API_KEY`` adopts a companion ``OPENAI_BASE_URL``.

    The OpenAI SDK reads ``OPENAI_BASE_URL`` to target an OpenAI-compatible
    gateway (e.g. the Databricks AI gateway). Ambient detection must honor
    it; otherwise the env key is synthesized against ``api.openai.com`` and
    every request 401s — the credential is a gateway token, not an OpenAI
    key. This is the regression guard for that intermittent multi-turn 401.
    """
    gateway = "https://example.cloud.databricks.com/ai-gateway/openai/v1"
    monkeypatch.setenv("OPENAI_BASE_URL", gateway)
    det = DetectedProvider(
        name="openai", kind="key", family=OPENAI_FAMILY, source="$OPENAI_API_KEY"
    )
    entries = synthesize_detected_entries([det])
    openai_block = entries["openai"]["openai"]
    assert openai_block["base_url"] == gateway
    assert openai_block["api_key_ref"] == "env:OPENAI_API_KEY"


def test_synthesize_env_key_openai_without_base_url_uses_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Absent ``OPENAI_BASE_URL``, a detected OpenAI key keeps the vendor default."""
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    det = DetectedProvider(
        name="openai", kind="key", family=OPENAI_FAMILY, source="$OPENAI_API_KEY"
    )
    entries = synthesize_detected_entries([det])
    assert entries["openai"]["openai"]["base_url"] == "https://api.openai.com/v1"


def test_synthesize_subscription_cli() -> None:
    """A codex CLI login becomes a ``subscription`` entry keyed by its CLI."""
    entries = synthesize_detected_entries([_codex_login()])
    assert entries["codex"] == {"kind": "subscription", "cli": "codex"}


def test_synthesize_skips_familyless_detection() -> None:
    """A detection with no harness family is omitted.

    We may detect a key but have no harness surface for it, so synthesizing
    an entry would create an unroutable provider. Failure means a
    familyless key would show up as a (broken) configured provider.
    """
    familyless = DetectedProvider(
        name="mystery", kind="key", family=None, source="$MYSTERY_API_KEY"
    )
    assert synthesize_detected_entries([familyless]) == {}


def test_synthesize_env_key_gemini() -> None:
    """A detected GEMINI_API_KEY becomes a ``gemini``-family ``key`` entry.

    The antigravity harness drives the Gemini SDK directly with a
    GEMINI_API_KEY, so a detected key must synthesize a routable gemini
    provider (it used to be dropped as familyless). Failure means the key
    would be silently ignored, leaving the antigravity harness with no
    auto-adopted credential.
    """
    entries = synthesize_detected_entries([_gemini_key()])
    assert set(entries) == {"gemini"}
    parsed = load_providers({"providers": entries})["gemini"]
    assert parsed.kind == "key"
    assert set(parsed.families) == {"gemini"}
    gemini_block = entries["gemini"]["gemini"]
    # The env var is referenced (env:), never the resolved secret value.
    assert gemini_block["api_key_ref"] == "env:GEMINI_API_KEY"
    # Gemini's OpenAI-compatible endpoint (not api.openai.com / anthropic).
    assert gemini_block["base_url"] == "https://generativelanguage.googleapis.com/v1beta/openai"


def test_gemini_key_adopted_and_auto_defaults() -> None:
    """A detected GEMINI_API_KEY is adopted and auto-defaults its family.

    The end-to-end onboarding path: with nothing configured, a detected
    Gemini key must (1) be returned by ``providers_to_adopt`` so
    ``configure harnesses`` persists it, and (2) auto-become the gemini
    family default in the read-time merge so the gemini-surface harness
    resolves it. Failure means a detected Gemini key would not reach the
    harness even though it was found on the machine.
    """
    adopt = providers_to_adopt({}, [_gemini_key()])
    assert set(adopt) == {"gemini"}
    merged = effective_config_with_detected({}, [_gemini_key()])
    assert get_default_provider(merged, GEMINI_FAMILY).name == "gemini"
    # And resolves through the harness path the runtime uses.
    assert default_provider_for_harness(merged, "antigravity-native").name == "gemini"


def test_synthesize_local_ollama() -> None:
    """A reachable Ollama becomes a ``local`` openai-family entry with /v1."""
    det = DetectedProvider(
        name="ollama", kind="local", family=OPENAI_FAMILY, source="http://localhost:11434"
    )
    entries = synthesize_detected_entries([det])
    assert entries["ollama"]["kind"] == "local"
    # The OpenAI-compatible path is appended to the detected host.
    assert entries["ollama"]["openai"]["base_url"] == "http://localhost:11434/v1"


def test_effective_merges_and_auto_defaults_per_family() -> None:
    """Empty config + ambient creds → both merged and each its family default.

    The first-run case: nothing configured, only ambient anthropic key +
    codex login. The merged view must contain both AND make each the
    default for its surface so routing resolves (and /model can name them).
    Failure means a fresh machine would have no resolvable default.
    """
    merged = effective_config_with_detected({}, [_anthropic_key(), _codex_login()])
    providers = load_providers(merged)
    assert set(providers) == {"anthropic", "codex"}
    # Each detected provider is the default for its own family.
    assert get_default_provider(merged, ANTHROPIC_FAMILY).name == "anthropic"
    assert get_default_provider(merged, OPENAI_FAMILY).name == "codex"
    # And resolves through the harness path the runtime uses.
    assert default_provider_for_harness(merged, "claude-sdk").name == "anthropic"
    assert default_provider_for_harness(merged, "codex").name == "codex"


def test_effective_explicit_wins_on_name() -> None:
    """An explicit provider overrides a detected one of the same name.

    The user's config is authoritative: a hand-written ``anthropic`` entry
    must not be replaced by the synthesized ambient one. Failure means the
    detected entry would clobber the user's pinned model/base_url.
    """
    explicit = {
        "providers": {
            "anthropic": {
                "kind": "key",
                "anthropic": {
                    "base_url": "https://proxy.example.com",
                    "api_key": "$MY_KEY",
                    "models": {"default": "claude-opus-4-7"},
                },
            }
        }
    }
    merged = effective_config_with_detected(explicit, [_anthropic_key()])
    entry = load_providers(merged)["anthropic"]
    # The explicit model survives — the detected entry did not win.
    # (Use family_default_model, which does not resolve the secret.)
    assert entry.family_default_model("anthropic") == "claude-opus-4-7"
    # The raw merged entry keeps the explicit proxy base_url, not the
    # synthesized api.anthropic.com.
    merged_providers = merged["providers"]
    assert merged_providers["anthropic"]["anthropic"]["base_url"] == "https://proxy.example.com"


def test_effective_explicit_default_not_overridden_by_detected() -> None:
    """An explicit family default is never displaced by a detected provider.

    With an explicit anthropic default and a detected OpenRouter (openai)
    key, the anthropic default stays the explicit one while openai's default
    becomes the detected OpenRouter. Failure means auto-default trampled the
    user's chosen Claude default.
    """
    explicit = {
        "providers": {
            "my-claude": {
                "kind": "key",
                "default": True,
                "anthropic": {"base_url": "https://api.anthropic.com", "api_key": "$K"},
            }
        }
    }
    detected = [
        DetectedProvider(
            name="openrouter", kind="key", family=OPENAI_FAMILY, source="$OPENROUTER_API_KEY"
        )
    ]
    merged = effective_config_with_detected(explicit, detected)
    # Explicit anthropic default untouched; openai default = detected openrouter.
    assert get_default_provider(merged, ANTHROPIC_FAMILY).name == "my-claude"
    assert get_default_provider(merged, OPENAI_FAMILY).name == "openrouter"


def test_providers_to_adopt_skips_already_configured() -> None:
    """``providers_to_adopt`` returns only detections not already configured.

    Opening ``configure harnesses`` should persist new detections but never
    duplicate an already-configured name. Failure means re-opening the
    manager would rewrite/duplicate an existing entry.
    """
    explicit = {"providers": {"anthropic": {"kind": "subscription", "cli": "claude"}}}
    adopt = providers_to_adopt(explicit, [_anthropic_key(), _codex_login()])
    # anthropic already configured (by name) → skipped; only codex is new.
    assert set(adopt) == {"codex"}


def _claude_login() -> DetectedProvider:
    """An ambient claude CLI login detection (named by its CLI, ``claude``)."""
    return DetectedProvider(
        name="claude", kind="subscription", family=ANTHROPIC_FAMILY, source="claude CLI login"
    )


def test_providers_to_adopt_skips_subscription_for_configured_cli() -> None:
    """A detected login isn't adopted when its CLI already has a subscription.

    The ambient detector names a Claude login ``"claude"``, but the user may
    have added that same login explicitly under a different name
    (``"claude-subscription"``). Adopting the detection by name would write a
    *second* subscription for the one CLI — the ``claude`` +
    ``claude-subscription`` duplicate. Adoption must skip a detected
    subscription whose CLI is already configured (under any name).
    """
    explicit = {"providers": {"claude-subscription": {"kind": "subscription", "cli": "claude"}}}
    adopt = providers_to_adopt(explicit, [_claude_login(), _codex_login()])
    # claude's CLI is already covered by claude-subscription → not re-adopted;
    # codex is genuinely new.
    assert set(adopt) == {"codex"}


def test_effective_config_skips_duplicate_subscription_for_configured_cli() -> None:
    """The read-time merge never surfaces a detected login twice.

    With an explicit ``claude-subscription`` (the default, as the add flow
    always writes it) and a detected ``claude`` login for the same CLI, the
    merged view keeps only the explicit entry — no synthesized ``claude``
    duplicate alongside it.
    """
    explicit = {
        "providers": {
            "claude-subscription": {"kind": "subscription", "default": True, "cli": "claude"}
        }
    }
    merged = effective_config_with_detected(explicit, [_claude_login()])
    providers = merged["providers"]
    assert "claude" not in providers  # the detected duplicate is dropped
    assert "claude-subscription" in providers
    # The single remaining subscription is still the anthropic default.
    assert get_default_provider(merged, ANTHROPIC_FAMILY).name == "claude-subscription"


# ── cli-config (codex config.toml custom provider) bridging ────────────────


def _codex_config_det() -> DetectedProvider:
    """An ambient codex config.toml custom-provider detection."""
    return DetectedProvider(
        name="codex-databricks",
        kind="cli-config",
        family=OPENAI_FAMILY,
        source="~/.codex/config.toml provider 'Databricks'",
        model_provider="Databricks",
        display_name="Databricks AI Gateway",
    )


def test_synthesize_cli_config_entry() -> None:
    """A cli-config detection synthesizes a pin-by-name provider entry.

    Asserted by full equality: a missing/extra field would either fail
    config parsing on adoption or drop the display name from every label.
    """
    entries = synthesize_detected_entries([_codex_config_det()])
    assert entries == {
        "codex-databricks": {
            "kind": "cli-config",
            "cli": "codex",
            "model_provider": "Databricks",
            "display_name": "Databricks AI Gateway",
        }
    }


def test_cli_config_detection_wins_codex_default_over_login() -> None:
    """With both codex detections, the config.toml provider auto-defaults.

    Detection priority (config provider first) mirrors codex's own
    resolution — config.toml's default provider beats auth.json. Failure
    means an isaac-configured machine with a stray auth.json would default
    omnigents to the ChatGPT login while plain ``codex`` uses the gateway.
    """
    merged = effective_config_with_detected({}, [_codex_config_det(), _codex_login()])
    default = default_provider_for_harness(merged, "codex")
    assert default is not None
    # The config.toml provider wins the openai default; the login is still
    # present as a non-default entry the user can switch to.
    assert default.name == "codex-databricks"
    providers = load_providers(merged)
    assert providers["codex"].kind == "subscription"
    assert providers["codex-databricks"].model_provider == "Databricks"


def test_explicit_entry_overrides_cli_config_detection() -> None:
    """An explicit entry with the detection's name is authoritative.

    Failure means a re-detection would clobber a user-edited entry on the
    read-time merge.
    """
    explicit = {
        "providers": {
            "codex-databricks": {
                "kind": "cli-config",
                "cli": "codex",
                "model_provider": "OtherProvider",
            }
        }
    }
    merged = effective_config_with_detected(explicit, [_codex_config_det()])
    # The explicit pin (OtherProvider) survives; the detected Databricks
    # value must not overwrite it.
    assert load_providers(merged)["codex-databricks"].model_provider == "OtherProvider"


def test_providers_to_adopt_skips_configured_cli_config() -> None:
    """An already-adopted cli-config entry is not re-proposed for adoption.

    Failure means every configure-harnesses open would re-write (and
    potentially clobber) the persisted entry.
    """
    explicit = {
        "providers": {
            "codex-databricks": {
                "kind": "cli-config",
                "cli": "codex",
                "model_provider": "Databricks",
            }
        }
    }
    assert providers_to_adopt(explicit, [_codex_config_det()]) == {}


def test_dismissed_detection_skipped_by_both_merge_surfaces() -> None:
    """A dismissed detection is excluded from adoption AND the read-time merge.

    Skipping only adoption would make Remove look done while the runtime
    kept routing through the dismissed credential via the merged view;
    skipping only the merge would re-adopt it on the next configure open.
    Both must honor the dismissal.
    """
    cfg = {"dismissed_detections": ["codex-databricks"]}
    detected = [_codex_config_det(), _codex_login()]

    assert providers_to_adopt(cfg, detected) == {"codex": {"kind": "subscription", "cli": "codex"}}
    merged = effective_config_with_detected(cfg, detected)
    assert "codex-databricks" not in merged["providers"]
    # With the config provider dismissed, the next detection in priority
    # order (the codex login) takes the openai default instead.
    default = default_provider_for_harness(merged, "codex")
    assert default is not None and default.name == "codex"


def test_malformed_dismissed_detections_treated_as_empty() -> None:
    """A malformed ``dismissed_detections`` value dismisses nothing.

    A hand-edited scalar (or junk entries) must not crash setup or
    accidentally dismiss everything; the next dismissal write self-heals
    the key into a proper list.
    """
    from omnigent.onboarding.detected import dismissed_detection_names

    assert dismissed_detection_names({"dismissed_detections": "oops"}) == frozenset()
    # Non-string members are ignored; string members still count.
    assert dismissed_detection_names({"dismissed_detections": [3, "codex-databricks"]}) == (
        frozenset({"codex-databricks"})
    )
