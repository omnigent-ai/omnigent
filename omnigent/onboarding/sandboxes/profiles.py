"""Provider-owned profile contracts and validated local profile configuration."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_network
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    StringConstraints,
    field_validator,
    model_validator,
)

from omnigent.onboarding.sandboxes.types import SandboxError


class ProviderSandboxProfile(BaseModel):
    """Public metadata for an authorized provider profile at an exact revision.

    Configuration and access rules stay with the provider. Revisions are opaque
    identifiers; providers must change them whenever effective settings change.
    """

    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)

    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{0,63}$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)
    revision: str = Field(min_length=1, max_length=64)


@dataclass(frozen=True)
class SandboxProfileContext:
    """Trusted server identity for profile discovery and selection, never client input."""

    user_id: str
    workspace_id: int


class SandboxProfileAccessDenied(SandboxError):
    """The caller cannot currently use the requested provider profile."""


class SandboxProfileChanged(SandboxError):
    """The requested provider profile revision is no longer available."""


_Quantity = Annotated[
    str,
    StringConstraints(
        strip_whitespace=True,
        pattern=r"^(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+|[numkKMGTPE]|[KMGTPE]i)?$",
    ),
]


class _Config(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    @field_validator("*", mode="before")
    @classmethod
    def reject_null(cls, value: object) -> object:
        if value is None:
            raise ValueError("omit optional fields instead of setting them to null")
        return value


class _ResourceValues(_Config):
    cpu: _Quantity | None = None
    memory: _Quantity | None = None
    ephemeral_storage: _Quantity | None = Field(default=None, alias="ephemeral-storage")


class _Resources(_Config):
    requests: _ResourceValues | None = None
    limits: _ResourceValues | None = None


class _SelectorRequirement(_Config):
    key: Annotated[str, StringConstraints(min_length=1)]
    operator: Literal["In", "NotIn", "Exists", "DoesNotExist"]
    values: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_values(self) -> Self:
        if bool(self.values) != (self.operator in ("In", "NotIn")):
            raise ValueError("selector values must be nonempty only for In/NotIn")
        return self


class _Selector(_Config):
    match_labels: dict[str, str] = Field(default_factory=dict, alias="matchLabels")
    match_expressions: list[_SelectorRequirement] = Field(
        default_factory=list, alias="matchExpressions"
    )


class _IPBlock(_Config):
    cidr: str
    exceptions: list[str] = Field(default_factory=list, alias="except")

    @model_validator(mode="after")
    def check_networks(self) -> Self:
        network = ip_network(self.cidr)
        for value in self.exceptions:
            excluded = ip_network(value)
            if (
                excluded.version != network.version
                or int(excluded.network_address) < int(network.network_address)
                or int(excluded.broadcast_address) > int(network.broadcast_address)
            ):
                raise ValueError("ipBlock exceptions must be subnets of cidr")
        return self


class _Peer(_Config):
    ip_block: _IPBlock | None = Field(default=None, alias="ipBlock")
    namespace_selector: _Selector | None = Field(default=None, alias="namespaceSelector")
    pod_selector: _Selector | None = Field(default=None, alias="podSelector")

    @model_validator(mode="after")
    def check_peer(self) -> Self:
        if self.ip_block is not None and (
            self.namespace_selector is not None or self.pod_selector is not None
        ):
            raise ValueError("ipBlock cannot be combined with namespaceSelector/podSelector")
        return self


class _Port(_Config):
    protocol: Literal["TCP", "UDP", "SCTP"] = "TCP"
    port: (
        Annotated[int, Field(ge=1, le=65535)]
        | Annotated[str, StringConstraints(min_length=1, max_length=15, pattern=r"^[a-z0-9-]+$")]
        | None
    ) = Field(default=None)
    end_port: Annotated[int, Field(ge=1, le=65535)] | None = Field(default=None, alias="endPort")

    @model_validator(mode="after")
    def check_range(self) -> Self:
        if self.end_port is not None and (
            not isinstance(self.port, int) or self.end_port < self.port
        ):
            raise ValueError("endPort requires an integer port and must be >= port")
        return self


class _EgressRule(_Config):
    ports: list[_Port] = Field(default_factory=list)
    to: list[_Peer] = Field(default_factory=list)


class _ProfileConfig(_Config):
    image: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)] | None = None
    resources: _Resources | None = None
    egress: list[_EgressRule] | None = None


def validate_profile_config(config: object) -> dict[str, object]:
    """Reject unknown fields and return canonical, JSON-compatible profile config.

    Omitted egress preserves the provider's network configuration; an empty list
    selects Kubernetes default-deny egress, including DNS and control traffic.
    """
    return _ProfileConfig.model_validate(config).model_dump(by_alias=True, exclude_none=True)
