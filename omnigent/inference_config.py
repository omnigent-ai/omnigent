"""Typed inference profiles shared by static configuration and session runtime."""

from __future__ import annotations

import copy
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness

if TYPE_CHECKING:
    from omnigent.onboarding.provider_config import ProviderEntry

_runtime_config: ContextVar[dict[str, object] | None] = ContextVar(
    "inference_config", default=None
)


@dataclass(frozen=True)
class HarnessInferenceBinding:
    """A harness's provider and optional ordered model restriction."""

    provider: str
    default_model: str | None = None
    model_allowlist: tuple[str, ...] | None = None


def _harness_key(harness: str) -> str:
    if harness.startswith("native-"):
        harness = harness.removeprefix("native-") + "-native"
    return harness if harness.startswith("acp:") else (canonicalize_harness(harness) or harness)


def parse_inference_config(config: dict[str, object]) -> dict[str, HarnessInferenceBinding]:
    """Validate profile shape without resolving credentials or contacting providers."""
    raw = config.get("inference")
    if raw is None:
        return {}
    if not isinstance(raw, dict) or set(raw) - {"harnesses"}:
        raise ValueError("inference must contain only a harnesses mapping")
    harnesses = raw.get("harnesses", {})
    if not isinstance(harnesses, dict):
        raise ValueError("inference.harnesses must be a mapping")
    providers = config.get("providers", {})
    if not isinstance(providers, dict):
        raise ValueError("providers must be a mapping")
    result: dict[str, HarnessInferenceBinding] = {}
    for name, value in harnesses.items():
        if not isinstance(name, str) or not name.strip() or not isinstance(value, dict):
            raise ValueError("Each inference harness must name a configuration mapping")
        if set(value) - {"provider", "default_model", "model_allowlist"}:
            raise ValueError(f"Unknown inference setting for harness {name!r}")
        provider = value.get("provider")
        if not isinstance(provider, str) or provider not in providers:
            raise ValueError(f"Harness {name!r} must select a configured provider")
        default = value.get("default_model")
        if default is not None and (not isinstance(default, str) or not default.strip()):
            raise ValueError(f"Harness {name!r} default_model must be a nonempty model ID")
        allowed = value.get("model_allowlist")
        if allowed is not None and (
            not isinstance(allowed, (list, tuple))
            or any(not isinstance(model, str) or not model.strip() for model in allowed)
        ):
            raise ValueError(f"Harness {name!r} model_allowlist must be a list of model IDs")
        if allowed is not None and default is not None and default not in allowed:
            raise ValueError(f"Harness {name!r} default_model must belong to model_allowlist")
        key = _harness_key(name)
        if key not in {
            "claude-sdk",
            "claude-native",
            "codex",
            "codex-native",
            "openai-agents",
            "qwen",
            "jcode",
            "opencode-native",
            "pi",
            "pi-native",
            "acp",
        } and not (key.startswith("acp:") and key.removeprefix("acp:").strip()):
            raise ValueError(f"Harness {name!r} does not support inference bindings")
        if key in result:
            raise ValueError(f"Duplicate inference binding for harness {key!r}")
        result[key] = HarnessInferenceBinding(
            provider=provider,
            default_model=default,
            model_allowlist=tuple(dict.fromkeys(allowed)) if allowed is not None else None,
        )
    return result


def binding_for_harness(config: dict[str, object], harness: str) -> HarnessInferenceBinding | None:
    """Resolve exact ACP identities before an explicitly configured generic ACP fallback."""
    bindings = parse_inference_config(config)
    key = _harness_key(harness)
    return bindings.get(key) or (bindings.get("acp") if key.startswith("acp:") else None)


def resolve_bound_provider(
    config: dict[str, object], harness: str, auth: object = None, *, allow_empty: bool = False
) -> ProviderEntry | None:
    """Resolve an authoritative binding before legacy family defaults or spec credentials."""
    from omnigent.onboarding.provider_config import load_providers
    from omnigent.spec.types import ProviderAuth

    binding = binding_for_harness(config, harness)
    if binding is None:
        return None
    if auth is not None and (not isinstance(auth, ProviderAuth) or auth.name != binding.provider):
        raise OmnigentError(
            f"Harness {harness!r} is configured to use provider {binding.provider!r}; "
            "the agent's authentication conflicts with that binding.",
            code=ErrorCode.INVALID_INPUT,
        )
    provider = load_providers(config).get(binding.provider)
    if provider is None:
        raise OmnigentError(
            f"Configured provider {binding.provider!r} is unavailable",
            code=ErrorCode.INVALID_INPUT,
        )
    if binding.model_allowlist == () and not allow_empty:
        raise OmnigentError(
            f"Harness {harness!r} has no allowed models", code=ErrorCode.INVALID_INPUT
        )
    families = {}
    for name, family in provider.families.items():
        models = dict(family.models)
        default = binding.default_model or (
            family.resolve_model_tier(family.default_model) if family.default_model else None
        )
        if binding.model_allowlist is not None:
            models = {f"model_{i}": model for i, model in enumerate(binding.model_allowlist)}
        if default is not None:
            models["default"] = default
        families[name] = replace(family, models=models)
    return replace(provider, families=families)


def resolve_bound_model(config: dict[str, object], harness: str, model: str | None) -> str | None:
    """Choose and validate a literal model ID within the saved harness policy."""
    binding = binding_for_harness(config, harness)
    if binding is None:
        return model
    selected = model or binding.default_model
    if selected is None:
        provider = resolve_bound_provider(config, harness)
        if provider is not None:
            key = _harness_key(harness)
            preferred = (
                "anthropic"
                if key in {"claude-native", "claude-sdk"}
                else (
                    "openai"
                    if key
                    in {
                        "codex",
                        "codex-native",
                        "openai-agents",
                        "qwen",
                        "jcode",
                        "opencode-native",
                    }
                    else None
                )
            )
            families = (
                [provider.families[preferred]]
                if preferred in provider.families
                else list(provider.families.values())
                if preferred is None
                else []
            )
            selected = next(
                (
                    family.resolve_model_tier(family.default_model)
                    for family in families
                    if family.default_model
                ),
                None,
            )
    if selected is None and binding.model_allowlist:
        selected = binding.model_allowlist[0]
    if binding.model_allowlist is not None and selected not in binding.model_allowlist:
        raise OmnigentError(
            f"Model {selected!r} is not in the configured model list for {harness!r}",
            code=ErrorCode.INVALID_INPUT,
        )
    return selected


def inference_revision(config: dict[str, object], discovery: dict[str, object]) -> str:
    """Identify all nonsecret inputs that determine a new session's provider and catalog."""
    payload = {
        "providers": config.get("providers", {}),
        "inference": config.get("inference", {}),
        "model_discovery": discovery,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@contextmanager
def inference_config_scope(config: dict[str, object] | None) -> Iterator[None]:
    """Apply a session profile to one request/task without changing process configuration."""
    token = _runtime_config.set(config)
    try:
        yield
    finally:
        _runtime_config.reset(token)


def load_runtime_inference_config(
    base_config: dict[str, object] | None = None,
) -> dict[str, object]:
    """Overlay a saved profile on ambient settings; invalid saved files fail closed."""
    from omnigent.onboarding.provider_config import _load_config

    base = dict(_load_config() if base_config is None else base_config)
    overlay = _runtime_config.get()
    path = os.environ.get("OMNIGENT_INFERENCE_CONFIG")
    if overlay is None and path:
        try:
            overlay = json.loads(Path(path).read_text())
            if not isinstance(overlay, dict):
                raise ValueError("expected a mapping")
        except (OSError, ValueError) as exc:
            raise OmnigentError(
                "Cannot load this session's saved inference configuration",
                code=ErrorCode.INVALID_INPUT,
            ) from exc
    if overlay is not None:
        for key in ("providers", "inference"):
            base[key] = copy.deepcopy(overlay.get(key, {}))
    return base


def snapshot_runtime_config(snapshot: dict[str, Any] | None) -> dict[str, object] | None:
    """Return only the runtime portion; discovery credentials remain server-side."""
    return copy.deepcopy(snapshot["runtime_config"]) if snapshot is not None else None
