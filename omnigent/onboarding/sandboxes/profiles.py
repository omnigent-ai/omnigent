"""Provider-owned sandbox profile contracts."""

from __future__ import annotations

from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field

from omnigent.onboarding.sandboxes.types import SandboxError


class ProviderSandboxProfile(BaseModel):
    """Public metadata for an authorized provider profile at an exact revision.

    Configuration and access rules stay with the provider. Revisions are opaque
    identifiers; providers must change them whenever effective settings change.
    """

    model_config = ConfigDict(
        extra="forbid", strict=True, frozen=True, revalidate_instances="always"
    )

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    revision: str = Field(min_length=1, max_length=64)


class SandboxProfileSummary(ProviderSandboxProfile):
    """Public session/host reference; execution identity stays on the server."""

    provider: str = Field(min_length=1, max_length=32)


@dataclass(frozen=True)
class SandboxProfileContext:
    """Trusted server identity for profile discovery and selection, never client input."""

    user_id: str
    workspace_id: int
    sandbox_id: str | None = None


class SandboxProfileAccessDenied(SandboxError):
    """The caller cannot currently use the requested provider profile."""


class SandboxProfileChanged(SandboxError):
    """The requested provider profile revision is no longer available."""
