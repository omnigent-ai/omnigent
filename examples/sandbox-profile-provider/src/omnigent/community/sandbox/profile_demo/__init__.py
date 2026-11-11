"""A customer-owned JSON catalog over the standard Kubernetes launcher."""

from __future__ import annotations

import hashlib
import json
import os
import stat
from typing import Annotated, ClassVar

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from omnigent.onboarding.sandboxes.kubernetes import KubernetesSandboxLauncher
from omnigent.onboarding.sandboxes.profiles import (
    ProviderSandboxProfile,
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
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


_Quantity = Annotated[
    str,
    StringConstraints(
        pattern=r"^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+|[numkKMGTPE]|[KMGTPE]i)?$"
    ),
]


class _ResourceValues(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    cpu: _Quantity | None = None
    memory: _Quantity | None = None
    ephemeral_storage: _Quantity | None = Field(default=None, alias="ephemeral-storage")


class _Resources(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    requests: _ResourceValues | None = None
    limits: _ResourceValues | None = None


class _LaunchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    image: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]
    resources: _Resources | None = None


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
            # Nonblocking open avoids waiting for a FIFO writer; fstat rejects non-files.
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
                _LaunchSettings.model_validate(entry.config)
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
        self._close_clients()
        super().__init__(
            namespace=self._provider_namespace,
            image=settings.image,
            resources=settings.resources.model_dump(by_alias=True, exclude_none=True)
            if settings.resources is not None
            else None,
        )


def contribution() -> SandboxProviderContribution:
    from .controller import ControllerConfig

    return SandboxProviderContribution(
        name="omnigent-sandbox-profile-example",
        providers={
            "controller_demo": SandboxProviderMetadata(
                name="controller_demo",
                launcher_class="omnigent.community.sandbox.profile_demo.controller:ControllerSandboxLauncher",
                config_model=ControllerConfig,
                manages_profiles=True,
            ),
            "profile_demo": SandboxProviderMetadata(
                name="profile_demo",
                launcher_class=(
                    "omnigent.community.sandbox.profile_demo:FileCatalogSandboxLauncher"
                ),
                config_model=CatalogConfig,
                manages_profiles=True,
            ),
        },
    )
