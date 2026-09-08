"""Trusted runner-to-signer lifecycle contract."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlsplit

from . import _proc
from ._subprocess_lifecycle import close_subprocess_transport
from .model_auth import (
    PROVIDER_AUTH_REQUIRED,
    ProviderAuthRequired,
    _provider_auth_message,
    _validated_authority,
)
from .model_egress import FrozenModelRoute

_READINESS_TIMEOUT_SECONDS = 15.0
_SHUTDOWN_TIMEOUT_SECONDS = 6.0
_READINESS_KEYS = frozenset(
    {
        "status",
        "relay_port",
        "socket_path",
        "ca_bundle_path",
        "placeholder",
    }
)
_AUTH_ERROR_KEYS = frozenset({"status", "code"})


class SignerStartError(RuntimeError):
    """Signer failed before returning validated readiness."""


@dataclass(frozen=True)
class SignerLaunchConfig:
    """Non-secret policy sent from the runner to the signer."""

    binding_id: str
    endpoint: str
    routes: tuple[FrozenModelRoute, ...]
    auth_profile: str | None = None

    def __post_init__(self) -> None:
        if self.binding_id == "databricks-ucode-v1":
            endpoint = urlsplit(self.endpoint)
            if endpoint.hostname is None or self.auth_profile is None:
                raise ValueError("ucode signer requires trusted host and profile")
            _validated_authority(f"https://{endpoint.hostname}", self.auth_profile)
        elif self.auth_profile is not None:
            raise ValueError("test signer must not carry an authentication profile")

    def to_jsonable(self) -> dict[str, object]:
        """Return the strict non-secret child configuration."""
        payload: dict[str, object] = {
            "binding_id": self.binding_id,
            "endpoint": self.endpoint,
            "routes": [
                {"method": route.method, "host": route.host, "path": route.path}
                for route in self.routes
            ],
        }
        if self.auth_profile is not None:
            payload["auth_profile"] = self.auth_profile
        return payload


@dataclass(frozen=True)
class SignerReadiness:
    """Non-secret capabilities returned after signer preflight."""

    relay_port: int
    socket_path: Path
    ca_bundle_path: Path
    placeholder: str

    def __post_init__(self) -> None:
        if not 1 <= self.relay_port <= 65535:
            raise ValueError("signer relay port is invalid")
        if not self.socket_path.is_absolute() or not self.ca_bundle_path.is_absolute():
            raise ValueError("signer readiness paths must be absolute")
        if not self.placeholder.startswith("oa_cred_"):
            raise ValueError("signer placeholder is malformed")


class ModelSignerSession(Protocol):
    """Signer process interface held by one Codex session."""

    async def start(self) -> SignerReadiness:
        """Preflight credentials and return only non-secret readiness."""

    async def wait(self) -> int:
        """Wait for signer process exit."""

    async def close(self) -> None:
        """Invalidate forwarding and terminate signer helpers."""


def _signer_child_argv() -> list[str]:
    return [sys.executable, "-m", "omnigent.inner.model_signer_service"]


class SubprocessModelSigner:
    """Strict runner-side supervisor for the trusted signer process."""

    def __init__(self, config: SignerLaunchConfig) -> None:
        self._config = config
        self._proc: asyncio.subprocess.Process | None = None
        self._readiness: SignerReadiness | None = None

    async def start(self) -> SignerReadiness:
        if self._proc is not None:
            raise SignerStartError("model signer has already been started")
        config_bytes = json.dumps(
            self._config.to_jsonable(),
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        read_fd, write_fd = os.pipe()
        try:
            os.write(write_fd, config_bytes)
        finally:
            os.close(write_fd)

        env = {
            name: value
            for name in ("HOME", "PATH", "LANG", "LC_ALL", "TZ")
            if (value := os.environ.get(name)) is not None
        }
        try:
            self._proc = await asyncio.create_subprocess_exec(
                *_signer_child_argv(),
                "--config-fd",
                str(read_fd),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.DEVNULL,
                env=env,
                pass_fds=(read_fd,),
                **_proc.spawn_kwargs(),
            )
        finally:
            with contextlib.suppress(OSError):
                os.close(read_fd)

        try:
            assert self._proc.stdout is not None
            line = await asyncio.wait_for(
                self._proc.stdout.readline(),
                timeout=_READINESS_TIMEOUT_SECONDS,
            )
            if not line:
                raise SignerStartError("model signer exited before readiness")
            if len(line) > 16_384:
                raise SignerStartError("model signer returned oversized readiness")
            readiness = _parse_readiness(line, self._config)
        except Exception:
            await self._abort()
            raise
        self._readiness = readiness
        return readiness

    async def wait(self) -> int:
        proc = self._proc
        if proc is None:
            raise SignerStartError("model signer has not been started")
        return await proc.wait()

    async def close(self) -> None:
        proc = self._proc
        if proc is None:
            return
        if proc.returncode is None:
            if proc.stdin is not None:
                try:
                    proc.stdin.write(b"shutdown\n")
                    await proc.stdin.drain()
                except (BrokenPipeError, ConnectionError):
                    pass
            try:
                await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
            except asyncio.TimeoutError:
                _proc.terminate_tree(proc)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=2)
                except asyncio.TimeoutError:
                    _proc.kill_tree(proc)
                    await proc.wait()
        close_subprocess_transport(proc)
        self._readiness = None

    async def _abort(self) -> None:
        proc = self._proc
        if proc is None:
            return
        _proc.terminate_tree(proc)
        try:
            await asyncio.wait_for(proc.wait(), timeout=2)
        except asyncio.TimeoutError:
            _proc.kill_tree(proc)
            await proc.wait()
        close_subprocess_transport(proc)


def _parse_readiness(line: bytes, config: SignerLaunchConfig) -> SignerReadiness:
    try:
        payload = json.loads(line)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise SignerStartError("model signer returned invalid readiness") from exc
    if (
        isinstance(payload, dict)
        and set(payload) == _AUTH_ERROR_KEYS
        and payload.get("status") == "error"
        and payload.get("code") == PROVIDER_AUTH_REQUIRED
        and config.auth_profile is not None
    ):
        endpoint = urlsplit(config.endpoint)
        assert endpoint.hostname is not None
        raise ProviderAuthRequired(
            _provider_auth_message(f"https://{endpoint.hostname}", config.auth_profile)
        )
    if not isinstance(payload, dict) or set(payload) != _READINESS_KEYS:
        raise SignerStartError("model signer returned invalid readiness fields")
    if payload.get("status") != "ready":
        raise SignerStartError("model signer did not become ready")
    try:
        return SignerReadiness(
            relay_port=int(payload["relay_port"]),
            socket_path=Path(str(payload["socket_path"])),
            ca_bundle_path=Path(str(payload["ca_bundle_path"])),
            placeholder=str(payload["placeholder"]),
        )
    except (TypeError, ValueError) as exc:
        raise SignerStartError("model signer returned invalid readiness values") from exc
