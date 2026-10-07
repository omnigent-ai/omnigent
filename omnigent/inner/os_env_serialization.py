"""Encode and decode OS-environment configuration passed to harness subprocesses."""

from typing import Any, cast

from pydantic import TypeAdapter

from omnigent.inner.datamodel import OSEnvSandboxSpec

_SANDBOX_ADAPTER = TypeAdapter(OSEnvSandboxSpec)


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
