"""A customer-owned JSON catalog over the standard Kubernetes launcher."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field

from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
    validate_profile_config,
)
from omnigent.onboarding.sandboxes.registry import (
    SandboxProviderContribution,
    SandboxProviderMetadata,
)
from omnigent.onboarding.sandboxes.types import SandboxConfigError

MAX_CATALOG_BYTES = 64 * 1024


class CatalogConfig(BaseModel):
    """The catalog is a customer-controlled local file, readable by the server."""

    model_config = ConfigDict(extra="forbid", strict=True)

    catalog_file: str = Field(min_length=1)
    namespace: str = Field(
        default="omnigent-demo", pattern=r"^[a-z0-9]([-a-z0-9]*[a-z0-9])?$", max_length=63
    )


class _LaunchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    image: str
    resources: dict[str, object] = Field(default_factory=dict)
    egress: list[dict[str, object]] = Field(default_factory=list)

    def overrides(self) -> dict[str, object]:
        return validate_profile_config(self.model_dump(exclude_unset=True))


class _Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    id: str
    name: str
    description: str = ""
    users: list[str]
    workspaces: list[int]
    config: dict[str, object]

    def public_profile(self) -> ProviderSandboxProfile:
        payload = json.dumps(
            self.config, sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
        return ProviderSandboxProfile(
            id=self.id,
            name=self.name,
            description=self.description,
            revision=hashlib.sha256(payload).hexdigest(),
        )

    def allows(self, context: SandboxProfileContext) -> bool:
        return context.user_id in self.users and context.workspace_id in self.workspaces


class _Catalog(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    profiles: list[_Entry] = Field(max_length=100)


class FileCatalogSandboxLauncher(KubernetesSandboxLauncher):
    """Read permissions afresh and bind the selected configuration to this instance."""

    provider: ClassVar[str] = "profile_demo"
    manages_profiles: ClassVar[bool] = True

    def __init__(self, *, config: CatalogConfig) -> None:
        super().__init__(namespace=config.namespace)
        self._catalog_file = config.catalog_file
        self._provider_namespace = config.namespace

    def _read_catalog(self) -> list[_Entry]:
        try:
            # Nonblocking open rejects FIFOs without waiting for a writer.
            descriptor = os.open(self._catalog_file, os.O_RDONLY | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CATALOG_BYTES:
                    raise SandboxConfigError("Catalog must be a local file of at most 64 KiB")
                payload = source.read(MAX_CATALOG_BYTES + 1)
            if len(payload) > MAX_CATALOG_BYTES:
                raise SandboxConfigError("Catalog must be at most 64 KiB")
            entries = _Catalog.model_validate_json(payload).profiles
            if len({entry.id for entry in entries}) != len(entries):
                raise SandboxConfigError("Catalog profile IDs must be unique")
            for entry in entries:
                entry.public_profile()
                _LaunchSettings.model_validate(entry.config).overrides()
            return entries
        except (OSError, ValueError) as exc:
            # Validation errors can include private config and access rules.
            raise SandboxConfigError("Cannot read a valid sandbox profile catalog") from exc

    def list_profiles(self, context: SandboxProfileContext) -> list[ProviderSandboxProfile]:
        return [entry.public_profile() for entry in self._read_catalog() if entry.allows(context)]

    def select_profile(
        self, context: SandboxProfileContext, profile: ProviderSandboxProfile
    ) -> None:
        entry = next((entry for entry in self._read_catalog() if entry.id == profile.id), None)
        if entry is None or not entry.allows(context):
            raise SandboxProfileAccessDenied("Sandbox profile is unavailable for this caller")
        if entry.public_profile().revision != profile.revision:
            raise SandboxProfileChanged("Sandbox profile changed; refresh the profile choices")
        settings = _LaunchSettings.model_validate(entry.config)
        overrides = settings.overrides()
        # Selection precedes client creation and replaces any earlier selection.
        super().__init__(namespace=self._provider_namespace)
        super().apply_profile(overrides)


def contribution() -> SandboxProviderContribution:
    return SandboxProviderContribution(
        name="omnigent-sandbox-profile-example",
        providers={
            "profile_demo": SandboxProviderMetadata(
                name="profile_demo",
                launcher_class=(
                    "omnigent.community.sandbox.profile_demo:FileCatalogSandboxLauncher"
                ),
                config_model=CatalogConfig,
            )
        },
    )
