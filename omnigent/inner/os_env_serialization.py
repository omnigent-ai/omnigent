"""Encode and decode OS-environment configuration passed to harness subprocesses."""

import json
import os
from dataclasses import fields, is_dataclass
from typing import Any, cast

from pydantic import TypeAdapter

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

_SANDBOX_ADAPTER = TypeAdapter(OSEnvSandboxSpec)
_OS_ENV_ADAPTER = TypeAdapter(OSEnvSpec)


def encode_os_env_spec(value: OSEnvSpec) -> dict[str, Any]:
    """Encode an OS environment, including nested bindings, for JSON transport."""
    return cast(dict[str, Any], _OS_ENV_ADAPTER.dump_python(value, mode="json"))


def encode_sandbox_spec(value: OSEnvSandboxSpec) -> dict[str, Any]:
    """Encode nested sandbox bindings as JSON-safe values."""
    return cast(dict[str, Any], _SANDBOX_ADAPTER.dump_python(value, mode="json"))


def decode_sandbox_spec(value: object) -> OSEnvSandboxSpec:
    """Restore a sandbox from its JSON-safe representation.

    This is the normalized runtime representation, not the agent YAML schema.
    Decode nested credential entries, sources, and Databricks profiles so
    sandbox startup can resolve credentials using their typed attributes.
    Fields are validated during decoding, before sandbox initialization.
    """
    return _SANDBOX_ADAPTER.validate_python(value)


def resolve_harness_os_env(env_var: str) -> OSEnvSpec:
    """Restore the complete policy; only an absent variable uses the legacy default."""
    raw = os.environ.get(env_var)
    if raw is None:
        return OSEnvSpec(sandbox=OSEnvSandboxSpec(type="none"))
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{env_var} must contain a valid OS environment policy") from exc
    if not isinstance(payload, dict):
        raise ValueError(f"{env_var} must contain an OS environment object")
    spec = _OS_ENV_ADAPTER.validate_json(raw, strict=True)
    _reject_unknown_policy_fields(payload, spec, env_var)
    if spec.sandbox is not None:
        validate_sandbox_scan_options(spec.sandbox)
    return spec


def validate_sandbox_scan_options(sandbox: OSEnvSandboxSpec) -> None:
    """Reject scan settings that would silently fall back to partial masking."""
    from omnigent.inner.sandbox import HarnessSandboxValidationError

    limit = sandbox.cwd_hidden_scan_max_entries
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise HarnessSandboxValidationError(
            "os_env.sandbox.cwd_hidden_scan_max_entries must be a positive integer",
            reason="scan_limit",
        )
    if sandbox.cwd_hidden_scan_overflow not in ("error", "warn", "unlimited"):
        raise HarnessSandboxValidationError(
            "os_env.sandbox.cwd_hidden_scan_overflow must be error, warn or unlimited",
            reason="scan_overflow",
        )


def _reject_unknown_policy_fields(payload: object, value: object, location: str) -> None:
    if isinstance(payload, dict) and is_dataclass(value) and not isinstance(value, type):
        names = {field.name for field in fields(value)}
        unknown = payload.keys() - names
        if unknown:
            raise ValueError(f"{location} contains unknown fields: {', '.join(sorted(unknown))}")
        for name, nested in payload.items():
            _reject_unknown_policy_fields(nested, getattr(value, name), f"{location}.{name}")
    elif isinstance(payload, list) and isinstance(value, list):
        for index, (nested, item) in enumerate(zip(payload, value, strict=True)):
            _reject_unknown_policy_fields(nested, item, f"{location}[{index}]")
