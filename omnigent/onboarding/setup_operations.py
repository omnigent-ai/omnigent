"""Prompt-free setup persistence shared by the CLI and host service."""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Mapping
from copy import deepcopy
from typing import TYPE_CHECKING

from omnigent import config as config_store
from omnigent.onboarding.provider_config import (
    get_default_provider,
    load_providers,
    provider_families,
    set_default_provider,
    surface_default_provider,
)

if TYPE_CHECKING:
    from omnigent.onboarding.acp_auth import AcpAgentEntry

SETUP_LOCK = threading.RLock()


def load_setup_config() -> dict[str, object]:
    """Fail closed before any mutation when existing config cannot be parsed."""
    try:
        import yaml

        path = config_store.global_config_path()
        config = yaml.safe_load(path.read_text()) if path.exists() else {}
        if config is None:
            config = {}
        if not isinstance(config, dict) or any(not isinstance(k, str) for k in config):
            raise ValueError
        for field in ("providers", "cursor", "antigravity", "copilot", "acp"):
            if field in config and not isinstance(config[field], dict):
                raise ValueError
        load_providers(config)
        from omnigent.onboarding.acp_auth import acp_agents

        acp_agents(config)
        for scope in ("anthropic", "openai", "gemini", "pi"):
            get_default_provider(config, scope)
    except Exception:
        raise ValueError(
            "Setup cannot read the existing configuration; repair it before saving"
        ) from None
    return config


def save_setup_settings(
    settings: Mapping[str, object],
    *,
    deep_merge_keys: tuple[str, ...] = (),
    unset_keys: tuple[str, ...] = (),
) -> None:
    """Validate before using the application's atomic config writer."""
    with SETUP_LOCK:
        config = load_setup_config()
        for key, value in settings.items():
            if key in deep_merge_keys and isinstance(value, Mapping):
                old = config.get(key)
                config[key] = {**(old if isinstance(old, dict) else {}), **value}
            else:
                config[key] = value
        load_providers(config)
        config_store.save_global_config(
            settings, deep_merge_keys=deep_merge_keys, unset_keys=unset_keys
        )


def existing_key_name_for_ref(
    config: dict[str, object], family: str, api_key_ref: str
) -> str | None:
    for name, entry in load_providers(config).items():
        block = entry.families.get(family)
        if entry.kind == "key" and block is not None and block.api_key_ref == api_key_ref:
            return name
    return None


def unique_provider_name(config: dict[str, object], candidate: str) -> str:
    existing = config.get("providers", {})
    if not isinstance(existing, dict) or candidate not in existing:
        return candidate
    suffix = 2
    while f"{candidate}-{suffix}" in existing:
        suffix += 1
    return f"{candidate}-{suffix}"


def resolve_key_provider_name(
    config: dict[str, object], family: str, candidate: str, api_key_ref: str
) -> str:
    """Reuse an exact source match or the candidate's UI-staged key slot.

    A CLI fixed reference also replaces its same-named UI connection; otherwise
    allocate a free name so credentials from different sources can coexist.
    """
    same_source = existing_key_name_for_ref(config, family, api_key_ref)
    if same_source is not None:
        return same_source
    entry = load_providers(config).get(candidate)
    if entry is not None and entry.kind == "key" and api_key_ref == f"keychain:{candidate}":
        block = entry.families.get(family)
        if (
            block
            and block.api_key_ref
            and re.fullmatch(rf"keychain:{re.escape(candidate)}-[a-f0-9]{{32}}", block.api_key_ref)
        ):
            return candidate
    return unique_provider_name(config, candidate)


def _merge_fields(old: dict[str, object], new: dict[str, object]) -> dict[str, object]:
    merged = deepcopy(old)
    if "api_key_ref" in new:
        merged.pop("api_key", None)
        merged.pop("auth_command", None)
    if "base_url" in new and new["base_url"] != old.get("base_url") and "wire_api" not in new:
        merged.pop("wire_api", None)
    for key, value in new.items():
        previous = merged.get(key)
        merged[key] = (
            _merge_fields(previous, value)
            if isinstance(previous, dict) and isinstance(value, dict)
            else value
        )
    return merged


def provider_add_settings(
    config: dict[str, object],
    name: str,
    entry: dict[str, object],
    *,
    surface: str | None = None,
    preserve_advanced: bool = False,
) -> tuple[dict[str, object], list[str]]:
    """Build setup's replacement, optionally preserving fields absent from UI forms."""
    block = config.get("providers")
    providers = deepcopy(block) if isinstance(block, dict) else {}
    old = providers.get(name)
    if preserve_advanced and isinstance(old, dict):
        removed = {key for key in ("anthropic", "openai", "gemini") if key not in entry}
        old = {key: value for key, value in old.items() if key not in removed}
        default = old.get("default")
        if isinstance(default, list):
            old["default"] = [scope for scope in default if scope not in removed]
            if not old["default"]:
                old.pop("default")
        elif isinstance(default, str) and default in removed:
            old.pop("default")
    providers[name] = (
        _merge_fields(old, entry)
        if preserve_advanced and isinstance(old, dict) and old.get("kind") == entry.get("kind")
        else entry
    )
    updated = {**config, "providers": providers}
    parsed = load_providers(updated)[name]
    scopes = (
        [surface]
        if entry["kind"] == "databricks" and surface
        else sorted(provider_families(parsed))
    )
    became_default = []
    for scope in scopes:
        if surface_default_provider(updated, scope) is None:
            providers = set_default_provider(providers, name, scope)
            updated["providers"] = providers
            became_default.append(scope)
    return {"providers": providers}, became_default


def persist_provider(
    name: str, entry: dict[str, object], *, surface: str | None = None
) -> list[str]:
    with SETUP_LOCK:
        settings, defaults = provider_add_settings(
            load_setup_config(), name, entry, surface=surface
        )
        save_setup_settings(settings)
        return defaults


def subscription_settings(
    config: dict[str, object], cli: str
) -> tuple[dict[str, object], list[str]]:
    from omnigent.onboarding.configure_models import build_subscription_provider_entry

    if cli not in ("claude", "codex", "pi"):
        raise ValueError("Unsupported subscription")
    raw = config.get("providers")
    providers = dict(raw) if isinstance(raw, dict) else {}
    parsed = load_providers(config)
    old_names = [n for n, e in parsed.items() if e.kind == "subscription" and e.cli == cli]
    old_defaults = set().union(*(parsed[n].default_families for n in old_names))
    name = f"{cli}-subscription"
    entry = build_subscription_provider_entry(cli)
    for previous in old_names:
        old_entry = providers.pop(previous, None)
        if isinstance(old_entry, dict):
            entry = {**old_entry, **entry}
    settings, defaults = provider_add_settings({**config, "providers": providers}, name, entry)
    block = settings["providers"]
    if isinstance(block, dict):
        for scope in old_defaults:
            block = set_default_provider(block, name, scope)
        settings["providers"] = block
    return settings, defaults


def record_subscription(cli: str) -> str:
    with SETUP_LOCK:
        settings, _ = subscription_settings(load_setup_config(), cli)
        save_setup_settings(settings)
        return f"{cli}-subscription"


def record_databricks_provider(profile: str, *, surface: str | None = None) -> str:
    from omnigent.onboarding.configure_models import build_databricks_provider_entry

    persist_provider("databricks", build_databricks_provider_entry(profile), surface=surface)
    return "databricks"


def detection_dismissal_settings(
    config: dict[str, object], name: str, dismissed: bool
) -> dict[str, object]:
    from omnigent.onboarding.detected import DISMISSED_DETECTIONS_KEY, dismissed_detection_names

    names = set(dismissed_detection_names(config))
    names.add(name) if dismissed else names.discard(name)
    return {DISMISSED_DETECTIONS_KEY: sorted(names)}


def provider_removal_settings(
    config: dict[str, object], name: str, *, detected_names: set[str]
) -> dict[str, object]:
    providers = config.get("providers")
    if not isinstance(providers, dict) or name not in providers:
        raise ValueError("Provider is not configured")
    settings: dict[str, object] = {"providers": {n: e for n, e in providers.items() if n != name}}
    if name in detected_names:
        settings.update(detection_dismissal_settings(config, name, True))
    return settings


def harness_key_settings(
    config: dict[str, object], harness: str, ref: str | None
) -> dict[str, object]:
    old = config.get(harness)
    block = dict(old) if isinstance(old, dict) else {}
    field = "github_token_ref" if harness == "copilot" else "api_key_ref"
    if ref is None:
        block.pop(field, None)
        block.pop("github_token" if harness == "copilot" else "api_key", None)
    else:
        block.pop("github_token" if harness == "copilot" else "api_key", None)
        block[field] = ref
    return {harness: block}


def staged_secret_ref(name: str) -> str:
    """Fresh slots keep existing credentials active until config replacement succeeds."""
    return f"keychain:{name}-{uuid.uuid4().hex}"


def store_staged_secret(ref: str, secret: str) -> None:
    from omnigent.onboarding.secrets import store_secret

    load_setup_config()
    try:
        store_secret(ref.removeprefix("keychain:"), secret)
    except Exception:
        raise ValueError("The credential could not be stored on this host") from None


def store_setup_credential(name: str, secret: str) -> str:
    """Store a CLI credential under its established setup slot name."""
    ref = f"keychain:{name}"
    store_staged_secret(ref, secret)
    return ref


def copilot_host_settings(config: dict[str, object], host: str | None) -> dict[str, object]:
    old = config.get("copilot")
    block = dict(old) if isinstance(old, dict) else {}
    if host:
        block["github_host"] = host
    else:
        block.pop("github_host", None)
    return {"copilot": block}


def acp_entries_settings(
    config: dict[str, object], entries: list[AcpAgentEntry]
) -> dict[str, object]:
    """Keep unknown ACP fields while adding/removing the selected agent rows."""
    from omnigent.onboarding.acp_auth import acp_agents, acp_agents_settings

    old = config.get("acp")
    block = dict(old) if isinstance(old, dict) else {}
    raw = block.get("agents", [])
    if not isinstance(raw, list):
        raise ValueError("Existing ACP agents must be a list")
    current = iter(acp_agents(config))
    remaining = list(entries)
    result = []
    for row in raw:
        if (
            isinstance(row, dict)
            and isinstance(row.get("name"), str)
            and row["name"].strip()
            and isinstance(row.get("command"), str)
            and row["command"].strip()
        ):
            parsed = next(current)
            match = next(
                (i for i, entry in enumerate(remaining) if entry.slug == parsed.slug), None
            )
            if match is not None:
                result.append(row)
                remaining.pop(match)
        else:
            result.append(row)
    added = acp_agents_settings(remaining)["acp"]
    if isinstance(added, dict):
        result.extend(added["agents"])
    block["agents"] = result
    return {"acp": block}


def cleanup_removed_harness_secret(config: dict[str, object], harness: str) -> None:
    """Delete only our unreferenced slot after the config removal succeeds."""
    old = config.get(harness)
    if not isinstance(old, dict):
        return
    ref = old.get("github_token_ref" if harness == "copilot" else "api_key_ref")
    cleanup_unreferenced_secret(ref, harness)


def cleanup_unreferenced_secret(ref: object, name: str) -> None:
    from omnigent.onboarding.secrets import delete_secret

    if not isinstance(ref, str) or not re.fullmatch(
        rf"keychain:{re.escape(name)}(?:-[a-f0-9]{{32}})?", ref
    ):
        return
    remaining = load_setup_config()

    def contains(value: object) -> bool:
        if isinstance(value, dict):
            return any(contains(v) for v in value.values())
        if isinstance(value, list):
            return any(contains(v) for v in value)
        return value == ref

    if not contains(remaining):
        delete_secret(ref.removeprefix("keychain:"))


def harness_key_removal_settings(
    config: dict[str, object], harness: str
) -> tuple[dict[str, object], tuple[str, ...]]:
    settings = harness_key_settings(config, harness, None)
    return (settings, ()) if settings[harness] else ({}, (harness,))
