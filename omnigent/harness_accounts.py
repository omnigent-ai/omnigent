"""Wire-safe, non-secret harness account metadata."""

from __future__ import annotations

from typing import Any, TypeAlias

HarnessAccount: TypeAlias = dict[str, Any]
HarnessAccounts: TypeAlias = dict[str, HarnessAccount]

_ACCOUNT_KINDS = frozenset({"none", "chatgpt", "api_key", "subscription", "providers", "unknown"})


def normalize_harness_accounts(value: object) -> HarnessAccounts | None:
    """Return a wire-safe account map while dropping malformed entries."""
    if not isinstance(value, dict):
        return None
    normalized: HarnessAccounts = {}
    for harness, raw_account in value.items():
        if not isinstance(harness, str) or not isinstance(raw_account, dict):
            continue
        kind = raw_account.get("kind")
        if kind not in _ACCOUNT_KINDS:
            continue
        account: HarnessAccount = {"kind": kind}
        for key in ("email", "org", "plan"):
            field = raw_account.get(key)
            if isinstance(field, str) and field:
                account[key] = field
        if isinstance(raw_account.get("expired"), bool):
            account["expired"] = raw_account["expired"]
        raw_providers = raw_account.get("providers")
        if isinstance(raw_providers, list):
            providers: list[dict[str, str]] = []
            for raw_provider in raw_providers:
                if not isinstance(raw_provider, dict):
                    continue
                provider_id = raw_provider.get("id")
                label = raw_provider.get("label")
                auth_type = raw_provider.get("auth_type")
                if (
                    not isinstance(provider_id, str)
                    or not provider_id
                    or not isinstance(label, str)
                    or not label
                    or not isinstance(auth_type, str)
                    or not auth_type
                ):
                    continue
                providers.append({"id": provider_id, "label": label, "auth_type": auth_type})
            account["providers"] = providers
        normalized[harness] = account
    return normalized
