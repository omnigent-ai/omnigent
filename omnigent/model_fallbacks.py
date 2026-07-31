"""Owned static model fallbacks for CLI surfaces without discovery."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class StaticModelFallback:
    """A release-curated model list with auditable ownership and provenance."""

    model_ids: tuple[str, ...]
    owner: str
    provenance: str
    discovery_gap: str


_CLAUDE_SUBSCRIPTION_MODELS = (
    "claude-fable-5",
    "claude-opus-5",
    "claude-opus-4-8",
    "claude-sonnet-5",
    "claude-sonnet-4-6",
    "claude-haiku-4-5",
)

_CODEX_MODELS = ("gpt-5.5", "gpt-5.4", "gpt-5.4-mini")

_STATIC_MODEL_FALLBACKS = {
    ("subscription", "claude"): StaticModelFallback(
        model_ids=_CLAUDE_SUBSCRIPTION_MODELS,
        owner="Claude subscription adapter",
        provenance="Omnigent's release-curated Claude Code alias catalog",
        discovery_gap="Claude subscription logins expose no model-listing API",
    ),
    ("subscription", "codex"): StaticModelFallback(
        model_ids=_CODEX_MODELS,
        owner="Codex subscription adapter",
        provenance="Omnigent's release-curated Codex alias catalog",
        discovery_gap="Codex subscription availability is not exposed before launch",
    ),
    ("cli-config", "codex"): StaticModelFallback(
        model_ids=_CODEX_MODELS,
        owner="Codex CLI-config adapter",
        provenance="Omnigent's release-curated Codex alias catalog",
        discovery_gap=(
            "Custom model_provider entries live in Codex config.toml and cannot "
            "be enumerated on this catalog path"
        ),
    ),
}


def static_model_fallback(provider_kind: str, cli: str) -> StaticModelFallback | None:
    """Return the owned fallback for a provider kind and CLI, if registered."""
    return _STATIC_MODEL_FALLBACKS.get((provider_kind, cli))
