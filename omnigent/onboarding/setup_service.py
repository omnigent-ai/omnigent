"""Host-owned standard setup with a passive inventory and explicit probes."""

from __future__ import annotations

import hashlib
import os
import re
import shlex
import stat
from dataclasses import asdict
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlsplit, urlunsplit

from omnigent.onboarding import configure_models as builders
from omnigent.onboarding import setup_operations as operations
from omnigent.onboarding.acp_auth import (
    acp_agents,
    acp_agents_settings,
    shadowed_builtin_acp_rows,
    slugify,
)
from omnigent.onboarding.provider_config import (
    get_default_provider,
    load_providers,
    provider_families,
    set_default_provider,
)
from omnigent.onboarding.setup_operations import (  # noqa: F401
    record_databricks_provider,
    record_subscription,
)
from omnigent.onboarding.setup_schema import (  # noqa: F401
    SETUP_ACTION_ADAPTER,
    AcpAgent,
    AddAcp,
    AddBedrock,
    AddGateway,
    AddKey,
    AdoptDetected,
    BuiltinAcpSetup,
    CredentialInput,
    DetectedConnection,
    DismissDetection,
    HarnessSettings,
    HarnessStatus,
    ImportAcp,
    ImportPreview,
    KeyProvider,
    RemoveAcp,
    RemoveHarnessKey,
    RemoveProvider,
    SetCopilotHost,
    SetDefault,
    SetHarnessKey,
    SetOpencodeModel,
    SetupAction,
    SetupActionResult,
    SetupDetection,
    SetupDetectRequest,
    SetupInventory,
    SetupProvider,
    Subscription,
)

if TYPE_CHECKING:
    from omnigent.onboarding.openclaw_config import OpenClawDiscovery


class SetupPersistenceError(ValueError):
    """A failed settings save also left a fresh stored secret to clean up."""

    def __init__(self) -> None:
        super().__init__("Setup was not saved; stored secret cleanup did not complete")


def _public_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        host = parsed.hostname or ""
        if ":" in host:
            host = f"[{host}]"
        if parsed.port:
            host += f":{parsed.port}"
        return urlunsplit((parsed.scheme, host, parsed.path, "", ""))
    except ValueError:
        return "[invalid endpoint]"


def _validate_url(value: str) -> str:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use an HTTP(S) endpoint without credentials, query parameters or fragments"
        )
    return value.rstrip("/")


def _safe_command(command: str) -> str:
    """Show an executable summary; arbitrary arguments may contain secrets."""
    try:
        parts = shlex.split(command)
    except ValueError:
        return "[invalid command]"
    if not parts or not re.fullmatch(r"[A-Za-z0-9_./+-]+", parts[0]):
        return "[command hidden]"
    executable = parts[0].rsplit("/", 1)[-1]
    return executable + (f" ({len(parts) - 1} arguments hidden)" if len(parts) > 1 else "")


def get_setup_inventory() -> SetupInventory:
    """Read configured metadata without resolving secrets or probing vendors."""
    config = operations.load_setup_config()
    parsed = load_providers(config)
    providers = []
    for name, entry in parsed.items():
        families = sorted(provider_families(entry))
        providers.append(
            SetupProvider(
                name=name,
                kind=entry.kind,
                families=families,
                defaults=sorted(entry.default_families),
                default_scopes=families,
                credential_sources={
                    family: "command"
                    if block.auth_command
                    else "environment"
                    if block.api_key_ref and block.api_key_ref.startswith("env:")
                    else "stored"
                    if block.api_key_ref
                    else "inline"
                    for family, block in entry.families.items()
                },
                models={
                    family: block.models["default"]
                    for family, block in entry.families.items()
                    if block.models.get("default")
                },
                base_urls={
                    family: _public_url(block.base_url)
                    for family, block in entry.families.items()
                    if block.base_url
                },
                wire_api=entry.families["openai"].wire_api if "openai" in entry.families else None,
                remove_warning=(
                    "Also signs out of the standalone CLI"
                    if entry.kind == "subscription" and entry.cli != "pi"
                    else "Also removes ucode-managed harness wiring"
                    if entry.kind == "databricks"
                    else None
                ),
            )
        )
    catalog = []
    for provider in builders._CATALOG_PROVIDER_FAMILY:
        family = builders.family_for_key_provider(provider)
        endpoint = builders.key_provider_endpoint(provider)
        catalog.append(
            KeyProvider(
                id=provider,
                label=builders.provider_display_name(provider),
                family=family,
                base_url=endpoint.base_url
                if endpoint
                else builders.default_base_url_for_family(family),
                wire_api=endpoint.wire_api if endpoint else None,
            )
        )

    def block(name: str) -> dict[str, object]:
        value = config.get(name)
        return value if isinstance(value, dict) else {}

    from omnigent.onboarding.detected import dismissed_detection_names

    defaults = {
        scope: (entry.name if (entry := get_default_provider(config, scope)) else None)
        for scope in ("anthropic", "openai", "gemini", "pi")
    }
    pi_requires_detection = False
    if defaults["pi"] is None:
        for scope in ("anthropic", "openai"):
            candidate = get_default_provider(config, scope)
            if candidate is None or candidate.kind in ("subscription", "bedrock"):
                continue
            if candidate.kind == "cli-config":
                pi_requires_detection = True
            else:
                defaults["pi"] = candidate.name
            break
    agents = [
        AcpAgent(**{**asdict(agent), "command": _safe_command(agent.command)})
        for agent in acp_agents(config)
    ]
    from omnigent.acp_cli_harnesses import ACP_CLI_HARNESSES

    shadowed = shadowed_builtin_acp_rows(acp_agents(config))
    return SetupInventory(
        providers=providers,
        key_providers=catalog,
        acp_agents=agents,
        builtin_acp=[
            BuiltinAcpSetup(
                id=name,
                label=row.label,
                install_command=row.install.install_hint or row.binary,
                auth_instructions=row.install.auth_hint or "Use the CLI's own authentication.",
            )
            for name, row in sorted(ACP_CLI_HARNESSES.items())
            if name not in shadowed
        ],
        dismissed_detections=sorted(dismissed_detection_names(config)),
        effective_defaults=defaults,
        pi_default_requires_detection=pi_requires_detection,
        harness_settings=HarnessSettings(
            cursor_key_configured=bool(
                block("cursor").get("api_key_ref") or block("cursor").get("api_key")
            ),
            antigravity_key_configured=bool(
                block("antigravity").get("api_key_ref") or block("antigravity").get("api_key")
            ),
            copilot_key_configured=bool(
                block("copilot").get("github_token_ref") or block("copilot").get("github_token")
            ),
            copilot_host=str(block("copilot")["github_host"])
            if block("copilot").get("github_host")
            else None,
            opencode_model=str(config["opencode_model"]) if config.get("opencode_model") else None,
        ),
    )


def detect_setup_connections(request: SetupDetectRequest | None = None) -> SetupDetection:
    """Explicitly inspect host vendor configuration and model catalogs."""
    request = request or SetupDetectRequest()
    if request.pi_default:
        from omnigent.onboarding.provider_config import default_provider_for_harness

        try:
            entry = default_provider_for_harness(operations.load_setup_config(), "pi")
            return SetupDetection(
                pi_default_provider=entry.name if entry else None,
                pi_default_checked=True,
            )
        except Exception:
            return SetupDetection(
                warnings=["The Pi default could not be checked on this computer"]
            )
    if request.harness is not None:
        from omnigent.onboarding.harness_readiness import (
            _canonical_harness,
            _harness_availability,
        )

        try:
            availability = _harness_availability(_canonical_harness(request.harness))
            return SetupDetection(
                harness_status=HarnessStatus(
                    harness=request.harness,
                    availability=availability,
                )
            )
        except Exception:
            return SetupDetection(
                warnings=["The requested harness status could not be checked on this computer"]
            )

    from omnigent.onboarding.ambient import detect_providers
    from omnigent.onboarding.providers import default_chat_model, get_chat_models

    result = SetupDetection(
        warnings=[
            "Claude logins stored only in the OS Keychain are checked through "
            "guided sign-in, not detection."
        ]
    )
    try:
        result.providers = [
            DetectedConnection(
                name=p.name,
                kind=p.kind,
                family=p.family or "",
                source=p.source,
                display_name=p.display_name,
            )
            for p in detect_providers(allow_keychain=False)
        ]
    except Exception:
        result.warnings.append("Some host connections could not be inspected")
    discovery = _discover_imports(request.import_path, request.import_source)
    result.imports = [
        ImportPreview(
            source=p.source,
            name=p.name,
            slug=slugify(p.name),
            command=_safe_command(p.command_line),
            fingerprint=_import_fingerprint(p.name, p.command_line),
        )
        for p in discovery.agents
    ]
    if discovery.errors:
        result.warnings.append("Some OpenClaw/acpx configuration could not be read")
    for provider in builders._CATALOG_PROVIDER_FAMILY:
        try:
            result.models[provider] = [model.name for model in get_chat_models(provider)]
            result.default_models[provider] = default_chat_model(provider)
        except Exception:
            continue
    return result


def _credential_ref(action: CredentialInput, name: str) -> tuple[str, str | None]:
    if (action.secret is None) == (action.env_var is None):
        raise ValueError("Provide one credential: a secret or an environment variable")
    if action.env_var is not None:
        if not os.environ.get(action.env_var, "").strip():
            raise ValueError("The selected environment credential is not available on this host")
        return f"env:{action.env_var}", None
    secret = action.secret.get_secret_value().strip() if action.secret else ""
    if not secret:
        raise ValueError("A nonempty credential is required")
    return f"keychain:{name}", secret


def _save_credential_settings(
    settings: dict[str, object],
    name: str,
    ref: str,
    secret: str | None,
    previous_refs: list[object],
) -> bool:
    if secret is not None:
        operations.store_staged_secret(ref, secret)
    try:
        operations.save_setup_settings(settings)
    except Exception:
        if secret is not None:
            try:
                operations.cleanup_unreferenced_secret(ref, name)
            except Exception:
                raise SetupPersistenceError() from None
        raise
    cleaned = True
    for previous in previous_refs:
        try:
            operations.cleanup_unreferenced_secret(previous, name)
        except Exception:
            cleaned = False
    return cleaned


def _save_provider_action(action: AddKey | AddGateway | AddBedrock) -> tuple[str, bool]:
    config = operations.load_setup_config()
    name = action.name or action.provider if isinstance(action, AddKey) else action.name
    ref, secret = _credential_ref(action, name)
    if isinstance(action, AddKey):
        if action.provider not in builders._CATALOG_PROVIDER_FAMILY:
            raise ValueError("Unsupported API key vendor")
        model = action.model
        if model is None:
            from omnigent.onboarding.providers import default_chat_model

            model = default_chat_model(action.provider)
        if not model:
            raise ValueError("No default model is available. Choose a model in More options.")
        family = builders.family_for_key_provider(action.provider)
        name = operations.resolve_key_provider_name(config, family, name, ref)
        if secret is not None:
            ref = operations.staged_secret_ref(name)
        endpoint = builders.key_provider_endpoint(action.provider)
        entry = builders.build_key_provider_entry(
            family,
            endpoint.base_url if endpoint else builders.default_base_url_for_family(family),
            ref,
            model,
            wire_api=endpoint.wire_api if endpoint else None,
        )
    elif isinstance(action, AddGateway):
        if secret is not None:
            ref = operations.staged_secret_ref(name)
        if set(action.models) != set(action.families):
            raise ValueError("Set a default model for every selected gateway family")
        entry = builders.build_gateway_provider_entry(
            _validate_url(action.base_url),
            ref,
            families=list(dict.fromkeys(action.families)),
            wire_api=action.wire_api,
            models={str(family): model for family, model in action.models.items()},
        )
    else:
        if secret is not None:
            ref = operations.staged_secret_ref(name)
        entry = builders.build_bedrock_provider_entry(
            _validate_url(action.base_url), ref, action.model
        )
    settings, _ = operations.provider_add_settings(config, name, entry, preserve_advanced=True)
    old = load_providers(config).get(name)
    cleaned = _save_credential_settings(
        settings,
        name,
        ref,
        secret,
        [block.api_key_ref for block in old.families.values()] if old else [],
    )
    return name, cleaned


def _acp_raw(config: dict[str, object]) -> tuple[dict[str, object], list[object]]:
    raw = config.get("acp")
    block: dict[str, object] = dict(raw) if isinstance(raw, dict) else {}
    agents = block.get("agents", [])
    if not isinstance(agents, list):
        raise ValueError("Existing ACP agents must be a list")
    return block, list(agents)


def _change_acp(action: AddAcp | RemoveAcp | ImportAcp, config: dict[str, object]) -> None:
    block, raw = _acp_raw(config)
    if isinstance(action, AddAcp):
        shlex.split(action.command)
        item = action.model_dump(exclude={"action"})
        raw.append(item)
    elif isinstance(action, RemoveAcp):
        valid = acp_agents(config)
        target = next((a for a in valid if a.slug == action.slug), None)
        if target is None:
            raise ValueError("ACP agent is not configured")
        # Match the parsed list order while keeping unknown/malformed raw entries.
        seen = 0
        for i, item in enumerate(raw):
            if (
                isinstance(item, dict)
                and isinstance(item.get("name"), str)
                and item["name"].strip()
                and isinstance(item.get("command"), str)
                and item["command"].strip()
            ):
                if valid[seen].slug == target.slug:
                    raw.pop(i)
                    break
                seen += 1
    else:
        from omnigent.onboarding.openclaw_config import (
            merge_imported_acp_entries,
            openclaw_agents_to_acp_entries,
        )

        available = [
            a
            for a in _discover_imports(action.path, action.source).agents
            if a.source == action.source
        ]
        selected = [a for a in available if a.name in action.names]
        if set(action.names) != {a.name for a in selected} or any(
            action.fingerprints.get(a.name) != _import_fingerprint(a.name, a.command_line)
            for a in selected
        ):
            raise ValueError(
                "The selected imports changed or are no longer available; detect again"
            )
        _, added = merge_imported_acp_entries(
            openclaw_agents_to_acp_entries(selected), existing=acp_agents(config)
        )
        appended = acp_agents_settings(added)["acp"]
        if isinstance(appended, dict):
            raw.extend(appended["agents"])
    block["agents"] = raw
    operations.save_setup_settings({"acp": block})


def apply_setup_action(action: SetupAction) -> SetupActionResult:
    """Apply a typed action, serializing config mutations on this host."""
    with operations.SETUP_LOCK:
        config = operations.load_setup_config()
        message = "Setup saved"
        if isinstance(action, (AddKey, AddGateway, AddBedrock)):
            name, cleaned = _save_provider_action(action)
            message = f"Added {name}"
            if not cleaned:
                message += "; stored secret cleanup did not complete"
        elif isinstance(action, Subscription):
            if action.cli != "pi":
                from omnigent.onboarding.ambient import detect_providers

                if not any(
                    p.kind == "subscription" and p.name == action.cli
                    for p in detect_providers(allow_keychain=False)
                ):
                    raise ValueError("Complete the vendor login before adding this subscription")
            record_subscription(action.cli)
        elif isinstance(action, AdoptDetected):
            from omnigent.onboarding.ambient import detect_providers
            from omnigent.onboarding.detected import (
                providers_to_adopt,
                synthesize_detected_entries,
            )

            detected = [p for p in detect_providers(allow_keychain=False) if p.name == action.name]
            entry = synthesize_detected_entries(detected).get(action.name)
            if not isinstance(entry, dict):
                raise ValueError("This connection is no longer available; detect again")
            settings = operations.detection_dismissal_settings(config, action.name, False)
            adopted = providers_to_adopt({**config, **settings}, detected)
            if action.name in adopted:
                provider_settings, _ = operations.provider_add_settings(
                    config, action.name, adopted[action.name]
                )
                settings.update(provider_settings)
            operations.save_setup_settings(settings)
        elif isinstance(action, SetDefault):
            block = config.get("providers", {})
            if not isinstance(block, dict):
                raise ValueError("No configured providers")
            operations.save_setup_settings(
                {"providers": set_default_provider(block, action.name, action.surface)}
            )
        elif isinstance(action, DismissDetection):
            operations.save_setup_settings(
                operations.detection_dismissal_settings(config, action.name, action.dismissed)
            )
        elif isinstance(action, RemoveProvider):
            message = _remove_provider(config, action.name)
        elif isinstance(action, SetHarnessKey):
            ref, secret = _credential_ref(action, action.harness)
            if secret is not None:
                ref = operations.staged_secret_ref(action.harness)
            settings = operations.harness_key_settings(config, action.harness, ref)
            old = config.get(action.harness)
            field = "github_token_ref" if action.harness == "copilot" else "api_key_ref"
            if not _save_credential_settings(
                settings,
                action.harness,
                ref,
                secret,
                [old.get(field)] if isinstance(old, dict) else [],
            ):
                message += "; stored secret cleanup did not complete"
        elif isinstance(action, RemoveHarnessKey):
            settings, unset = operations.harness_key_removal_settings(config, action.harness)
            operations.save_setup_settings(settings, unset_keys=unset)
            try:
                operations.cleanup_removed_harness_secret(config, action.harness)
            except Exception:
                message = "Credential removed from config; stored secret cleanup did not complete"
        elif isinstance(action, SetCopilotHost):
            host = action.host
            if host and not re.fullmatch(r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", host):
                raise ValueError(
                    "Enter a GitHub Enterprise hostname without URL paths or credentials"
                )
            operations.save_setup_settings(operations.copilot_host_settings(config, host))
        elif isinstance(action, SetOpencodeModel):
            operations.save_setup_settings(
                {"opencode_model": action.model} if action.model else {},
                unset_keys=() if action.model else ("opencode_model",),
            )
        elif isinstance(action, (AddAcp, RemoveAcp, ImportAcp)):
            _change_acp(action, config)
        return SetupActionResult(message=message, inventory=get_setup_inventory())


def _remove_provider(config: dict[str, object], name: str) -> str:
    from omnigent.onboarding.ambient import detect_providers

    entry = load_providers(config).get(name)
    if entry is None:
        raise ValueError("Provider is not configured")
    message = "Provider removed"
    if entry.kind == "subscription" and entry.cli in ("claude", "codex"):
        from omnigent.onboarding.harness_install import harness_logout

        family = "anthropic" if entry.cli == "claude" else "openai"
        if not harness_logout(family):
            message = "Provider removed; vendor logout did not complete"
    if entry.kind == "databricks":
        from omnigent.onboarding.ucode_cleanup import remove_ucode_wiring

        try:
            remove_ucode_wiring()
        except Exception:
            message = "Provider removed; ucode wiring cleanup did not complete"
    detections = {
        p.name for p in detect_providers(allow_keychain=False) if p.kind != "subscription"
    }
    operations.save_setup_settings(
        operations.provider_removal_settings(config, name, detected_names=detections)
    )
    return message


def _import_fingerprint(name: str, command: str) -> str:
    return hashlib.sha256((name + "\0" + command).encode()).hexdigest()


def _discover_imports(path: str | None, source: str | None) -> OpenClawDiscovery:
    from omnigent.onboarding.openclaw_config import discover_openclaw_agents, read_openclaw_config

    if path is None:
        return discover_openclaw_agents()
    if source not in ("openclaw", "acpx"):
        raise ValueError("Choose an import source for the selected file")
    target = Path(path).expanduser()
    try:
        metadata = target.stat()
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > 1024 * 1024:
            raise ValueError
    except (OSError, ValueError):
        raise ValueError("Choose a readable regular import file smaller than 1 MiB") from None
    return read_openclaw_config(target, source=source)
