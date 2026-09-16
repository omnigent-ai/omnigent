"""Decode OS-environment configuration passed to harness subprocesses."""

from pydantic import TypeAdapter

from omnigent.inner.datamodel import OSEnvSandboxSpec

_SANDBOX_ADAPTER = TypeAdapter(OSEnvSandboxSpec)


def decode_sandbox_spec(value: object) -> OSEnvSandboxSpec:
    """Restore a sandbox serialized with ``dataclasses.asdict``.

    This is the normalized runtime representation, not the agent YAML schema.
    Decode nested credential entries, sources, and Databricks profiles so
    sandbox startup can resolve credentials using their typed attributes.
    Invalid policies raise rather than falling back to an unsandboxed default.
    """
    return _SANDBOX_ADAPTER.validate_python(value)
