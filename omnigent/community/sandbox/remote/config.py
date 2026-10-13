"""Configuration for the remote sandbox provider (the ``sandbox.remote`` YAML block)."""

from __future__ import annotations

import os

from pydantic import BaseModel, ConfigDict, Field

from omnigent.community.sandbox.remote.client import RemoteSandboxError

# The registry publishes one static TTL per provider and the server reads it
# before any ``sandbox.remote`` block exists, so the launch-token lifetime
# comes from the environment rather than the YAML config.
TOKEN_TTL_ENV_VAR = "OMNIGENT_SANDBOX_TOKEN_TTL_S"
DEFAULT_MANAGED_TOKEN_TTL_S = 7 * 24 * 3600


class RemoteSandboxConfig(BaseModel):
    """Validated ``sandbox.remote`` block.

    Three fields, all about reaching the service. What a sandbox is made of —
    its shape, its lifetime, its network, how its repository is checked out —
    is the service's to decide and never appears here.

    :param api_url: Base URL of the sandbox-control API.
    :param request_timeout_s: Per-request HTTP timeout for the control
        routes. The routes the API documents as blocking keep this timeout
        for connect, pool, and write phases but have no response-read
        deadline; see :mod:`omnigent.community.sandbox.remote.client`.
    :param api_key_env_var: Name of the environment variable holding the API
        key. The key is a secret, so it comes from the server environment and
        never from the YAML config file.
    """

    model_config = ConfigDict(extra="forbid")

    api_url: str = "http://127.0.0.1:8081"
    request_timeout_s: float = Field(default=30.0, gt=0)
    api_key_env_var: str = "OMNIGENT_SANDBOX_API_KEY"


def managed_token_ttl_s() -> int:
    """Return the configured managed launch-token lifetime in seconds."""
    raw = os.environ.get(TOKEN_TTL_ENV_VAR, "").strip()
    if not raw:
        return DEFAULT_MANAGED_TOKEN_TTL_S
    try:
        ttl = int(raw)
    except ValueError as exc:
        raise RemoteSandboxError(f"{TOKEN_TTL_ENV_VAR} must be a whole number of seconds") from exc
    if ttl <= 0:
        raise RemoteSandboxError(f"{TOKEN_TTL_ENV_VAR} must be positive, got {ttl}")
    return ttl


def api_key_from_env(env_var: str) -> str:
    """Return the API key, or raise when the server environment lacks it.

    Raises :class:`RemoteSandboxError` rather than a bare ``RuntimeError``:
    an ``except RemoteSandboxError`` clause does not catch its parent class,
    so a bare one would escape every lifecycle handler and make
    ``is_running`` raise where callers expect a tri-state.
    """
    key = os.environ.get(env_var, "").strip()
    if not key:
        raise RemoteSandboxError(f"{env_var} must be set for the remote sandbox provider")
    return key
