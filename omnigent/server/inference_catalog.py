"""Owner-scoped model discovery for immutable managed-sandbox inference profiles."""

from __future__ import annotations

import asyncio
import copy
import shlex
from typing import Any
from urllib.parse import urlsplit

import httpx

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.harness_aliases import canonicalize_harness
from omnigent.inference_config import (
    binding_for_harness,
    inference_revision,
    resolve_bound_provider,
)
from omnigent.models import model_catalog
from omnigent.models.model_catalog import ModelEntry, ResolvedModelProvider
from omnigent.models.model_metadata import ModelCapability, ModelWireAPI
from omnigent.onboarding.provider_config import ProviderEntry, load_providers, resolve_secret
from omnigent.server.auth import RESERVED_USER_LOCAL
from omnigent.server.databricks_identity import resolve_databricks_token


def _invalid(message: str) -> OmnigentError:
    return OmnigentError(message, code=ErrorCode.INVALID_INPUT)


def _endpoint(value: object) -> str:
    if not isinstance(value, str):
        raise _invalid("Inference endpoints must be literal HTTP or HTTPS URLs.")
    parsed = urlsplit(value)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or "$" in value
    ):
        raise _invalid("Inference endpoints must be literal URLs without credentials or queries.")
    return value.rstrip("/")


def _family(provider: ProviderEntry, harness: str, model: str | None = None) -> str:
    canonical = canonicalize_harness(harness)
    if canonical in {"claude-native", "claude-sdk"}:
        family = "anthropic"
    elif canonical in {
        "codex",
        "codex-native",
        "openai-agents",
        "qwen",
        "jcode",
        "opencode-native",
    }:
        family = "openai"
    elif canonical in {"pi", "pi-native", "acp"}:
        preferred = (
            ("openai", "anthropic")
            if canonical in {"pi", "pi-native"} and model and "claude" not in model.lower()
            else ("anthropic", "openai")
        )
        family = next((name for name in preferred if name in provider.families), "")
    else:
        raise _invalid(f"Harness {harness!r} does not support managed inference profiles.")
    if family not in provider.families:
        raise _invalid(f"Provider {provider.name!r} has no {family} endpoint for {harness!r}.")
    return family


def _wire(provider: ProviderEntry, harness: str, model: str | None = None) -> ModelWireAPI:
    family = _family(provider, harness, model)
    if family == "anthropic":
        return ModelWireAPI.ANTHROPIC_MESSAGES
    protocol = provider.families[family].wire_api
    canonical = canonicalize_harness(harness)
    if canonical in {"codex", "codex-native"} and protocol == "chat":
        raise _invalid("Codex requires a Responses endpoint; configure wire_api: responses.")
    if canonical in {"opencode-native", "jcode", "qwen"}:
        if protocol == "responses":
            raise _invalid(
                f"{harness} requires a Chat Completions gateway with wire_api: chat. "
                "Configure a separate named gateway provider for this harness."
            )
        return ModelWireAPI.OPENAI_CHAT
    return ModelWireAPI.OPENAI_CHAT if protocol == "chat" else ModelWireAPI.OPENAI_RESPONSES


def _configured_wires(provider: ProviderEntry) -> set[ModelWireAPI]:
    wires = set()
    if "anthropic" in provider.families:
        wires.add(ModelWireAPI.ANTHROPIC_MESSAGES)
    if "openai" in provider.families:
        wires.add(
            ModelWireAPI.OPENAI_CHAT
            if provider.families["openai"].wire_api == "chat"
            else ModelWireAPI.OPENAI_RESPONSES
        )
    return wires


def _alias(model: str, tiers: dict[str, str]) -> str:
    seen: set[str] = set()
    while model in tiers and tiers[model] != model:
        if model in seen:
            raise _invalid("The configured provider's model aliases contain a cycle.")
        seen.add(model)
        model = tiers[model]
    return model


def _binding_key(config: dict[str, Any], harness: str) -> str:
    canonical = harness if harness.startswith("acp:") else canonicalize_harness(harness)
    for key in config["inference"]["harnesses"]:
        normalized = key if key.startswith("acp:") else canonicalize_harness(key)
        if normalized == canonical:
            return key
    return "acp"


class SandboxInferenceService:
    """Load a static target once, then discover through its saved configuration."""

    def __init__(self, app_state: Any, *, transport: httpx.BaseTransport | None = None) -> None:
        self._state = app_state
        self._transport = transport

    async def _connection(self, owner: str) -> tuple[str, str] | None:
        store = getattr(self._state, "databricks_store", None)
        client = getattr(self._state, "databricks_client", None)
        if store is None or client is None:
            return None
        return await resolve_databricks_token(owner, store=store, client=client)

    async def prepare(
        self,
        provider: str | None,
        harness: str,
        user_id: str | None,
        agent_auth: object = None,
    ) -> dict[str, Any] | None:
        """Capture nonsecret launch inputs and verify the selected provider's catalog."""
        deployment = getattr(self._state, "sandbox_config", None)
        if deployment is None:
            return None
        target = deployment.for_provider(provider)
        if target is None:
            raise _invalid("The selected sandbox provider is not configured.")
        raw: dict[str, Any] = copy.deepcopy(target.host_config or {})
        bound = resolve_bound_provider(raw, harness, agent_auth, allow_empty=True)
        if bound is None and not raw.get("inference"):
            return None
        owner = user_id or RESERVED_USER_LOCAL
        runtime: dict[str, Any] = {
            "providers": copy.deepcopy(raw.get("providers", {})),
            "inference": copy.deepcopy(raw.get("inference", {})),
        }
        discovery = copy.deepcopy(getattr(target, "model_discovery", None) or {})
        connections: dict[str, str] = {}
        connected_names = [
            name
            for name, entry in load_providers(raw).items()
            if entry.kind == "databricks" and entry.connection == "databricks"
        ]
        connection = None
        if connected_names:
            try:
                connection = await self._connection(owner)
            except Exception:  # noqa: BLE001 - an unrelated connection must not block the chosen gateway
                if bound is not None and bound.name in connected_names:
                    raise _invalid(
                        "Databricks connection is unavailable. Reconnect your account."
                    ) from None
        if bound is not None and bound.kind == "databricks" and bound.connection != "databricks":
            raise _invalid("Sandbox Databricks inference requires connection: databricks.")
        if bound is not None and bound.name in connected_names and connection is None:
            raise _invalid(
                "Connect Databricks before using this harness's Unity Gateway provider."
            )
        if connection is not None:
            _, workspace = connection
            workspace = _endpoint(workspace)
            command = (
                "python3 -m omnigent.host.databricks_credential token --workspace "
                + shlex.quote(workspace)
            )
            for name in connected_names:
                connections[name] = workspace
                runtime["providers"][name] = {
                    "kind": "gateway",
                    "default": raw["providers"][name].get("default", False),
                    "display_name": raw["providers"][name].get("display_name"),
                    "anthropic": {
                        **{
                            key: value
                            for key, value in raw["providers"][name].get("anthropic", {}).items()
                            if key in {"models", "pricing", "context_window", "max_output_tokens"}
                        },
                        "base_url": f"{workspace}/ai-gateway/anthropic",
                        "auth_command": command,
                    },
                    "openai": {
                        **{
                            key: value
                            for key, value in raw["providers"][name].get("openai", {}).items()
                            if key in {"models", "pricing", "context_window", "max_output_tokens"}
                        },
                        "base_url": f"{workspace}/ai-gateway/codex/v1",
                        "auth_command": command,
                        "wire_api": "responses",
                    },
                }
        providers = load_providers(runtime)
        if bound is not None and providers[bound.name].kind not in {"gateway", "key", "local"}:
            raise _invalid(
                "Managed inference requires a gateway, API key, or connected Unity provider."
            )
        referenced = {value["provider"] for value in runtime["inference"]["harnesses"].values()}
        for name in referenced:
            for family_name in ("anthropic", "openai", "gemini"):
                original_family = raw["providers"][name].get(family_name, {})
                if original_family.get("api_key") is not None:
                    raise _invalid("Keep inference credentials in api_key_ref or auth_command.")
                if original_family.get("base_url") is not None:
                    _endpoint(original_family["base_url"])
            for family in providers[name].families.values():
                _endpoint(family.base_url)
        for saved_harness, saved in runtime["inference"]["harnesses"].items():
            entry = providers[saved["provider"]]
            # An unconnected Unity reference remains unusable, never late-bound.
            if not entry.families:
                continue
            family_name = _family(entry, saved_harness)
            tiers = entry.families[family_name].models
            if saved.get("model_allowlist") is not None:
                saved["model_allowlist"] = list(
                    dict.fromkeys(_alias(model, tiers) for model in saved["model_allowlist"])
                )
            if saved.get("default_model"):
                saved["default_model"] = _alias(saved["default_model"], tiers)
        target_id = f"sandbox:{target.provider or 'default'}"
        snapshot: dict[str, Any] = {
            "version": 1,
            "target_id": target_id,
            "configuration_revision": inference_revision(
                raw,
                {
                    "model_discovery": discovery,
                    "connections": connections,
                    "target_id": target_id,
                    "harness": harness,
                },
            ),
            "harness": harness,
            "owner_id": owner,
            "runtime_config": runtime,
            "model_discovery": discovery,
            "connections": connections,
        }
        preview = await self.catalog(snapshot)
        snapshot["catalog"] = preview
        if preview["status"] == "ready":
            saved_binding = runtime["inference"]["harnesses"][_binding_key(runtime, harness)]
            saved_binding["default_model"] = preview["default_model"]
        return copy.deepcopy(snapshot)

    def _gateway_models(
        self, snapshot: dict[str, Any], provider: ProviderEntry
    ) -> tuple[ModelEntry, ...]:
        discovery = snapshot.get("model_discovery", {}).get(provider.name)
        if not isinstance(discovery, dict):
            raise _invalid(
                f"Configure sandbox.model_discovery.{provider.name} "
                "for server-side model discovery."
            )
        base_url = _endpoint(discovery.get("base_url"))
        secret_ref = discovery.get("api_key_ref")
        family = discovery.get("family", "openai")
        descriptor = ResolvedModelProvider(
            kind="key" if family == "anthropic" else "gateway",
            family=family,
            base_url=base_url,
            api_key=resolve_secret(secret_ref) if secret_ref else None,
            auth_command=discovery.get("auth_command"),
            detail=f"sandbox:{snapshot['configuration_revision']}:{snapshot['owner_id']}:{provider.name}",
        )
        listing = model_catalog._listing_for_provider(descriptor, transport=self._transport)
        if not listing.verified:
            raise _invalid(
                "Model discovery is unavailable. Check the gateway and discovery credential."
            )
        return listing.models

    async def catalog(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        """Recheck saved owner/endpoint/model policy without reading the latest target."""
        result: dict[str, Any] = {
            "models": [],
            "configured": True,
            "configuration_revision": snapshot["configuration_revision"],
            "provider_label": None,
            "default_model": None,
            "status": "unavailable",
        }
        try:
            config = snapshot["runtime_config"]
            harness = snapshot["harness"]
            binding = binding_for_harness(config, harness)
            if binding is None:
                result.update(configured=False, status="unconfigured")
                return result
            provider = load_providers(config)[binding.provider]
            workspace = snapshot.get("connections", {}).get(provider.name)
            result["provider_label"] = config["providers"][provider.name].get("display_name") or (
                "Unity Gateway" if workspace else provider.display_name or provider.name
            )
            result["default_model"] = binding.default_model
            _wire(provider, harness)
            if workspace:
                connection = await self._connection(snapshot["owner_id"])
                if connection is None:
                    raise _invalid("The session owner's Databricks connection is unavailable.")
                token, current_workspace = connection
                if _endpoint(current_workspace) != workspace:
                    raise _invalid(
                        "The Databricks workspace changed. "
                        "Restore the session's original connection."
                    )
                entries = await asyncio.to_thread(
                    model_catalog.fetch_databricks_model_service_entries,
                    workspace,
                    token,
                    transport=self._transport,
                )
            else:
                entries = await asyncio.to_thread(self._gateway_models, snapshot, provider)
            available = {
                entry.id: entry
                for entry in entries
                if (
                    not entry.metadata.wire_apis
                    or (
                        bool(_configured_wires(provider) & entry.metadata.wire_apis)
                        if canonicalize_harness(harness) == "acp"
                        else _wire(provider, harness, entry.id) in entry.metadata.wire_apis
                    )
                )
                and entry.metadata.supports(ModelCapability.TOOL_USE) is not False
            }
            ids = (
                list(available)
                if binding.model_allowlist is None
                else [model for model in binding.model_allowlist if model in available]
            )
            if not ids:
                result.update(
                    status="empty",
                    error="No usable models match this harness's configured model list.",
                )
                return result
            default = binding.default_model
            if default is not None and default not in ids:
                raise _invalid("The configured default model is unavailable for this harness.")
            if default is None:
                family = provider.families[_family(provider, harness)]
                candidate = (
                    _alias(family.default_model, family.models) if family.default_model else None
                )
                default = candidate if candidate in ids else ids[0]
            source = {
                "kind": "databricks" if workspace else provider.kind,
                "label": result["provider_label"],
                "name": provider.name,
            }
            rows = []
            effort_order = {
                name: i
                for i, name in enumerate(
                    ("none", "minimal", "low", "medium", "high", "xhigh", "max")
                )
            }
            for model in ids:
                row: dict[str, Any] = {
                    "id": model,
                    "model": model,
                    "displayName": model,
                    "isDefault": model == default,
                    "source": source,
                }
                reasoning = available[model].metadata.reasoning
                if reasoning and reasoning.efforts:
                    row["supportedReasoningEfforts"] = [
                        {"reasoningEffort": effort}
                        for effort in sorted(
                            reasoning.efforts, key=lambda e: (effort_order.get(e, 99), e)
                        )
                    ]
                rows.append(row)
            result.update(status="ready", models=rows, default_model=default)
        except OmnigentError as exc:
            result["error"] = exc.message
        except Exception:  # noqa: BLE001 - discovery failures expose no credential or upstream body
            result["error"] = (
                "Model discovery is unavailable. Check the provider connection and catalog access."
            )
        return result
