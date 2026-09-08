"""Trusted runner-to-signer lifecycle contract."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol


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
