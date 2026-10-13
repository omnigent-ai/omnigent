"""Read-only team membership supplied by an operator-installed provider."""

from __future__ import annotations

import abc
import importlib.metadata
import inspect
import os

from pydantic import BaseModel, ConfigDict, Field

TEAM_PROVIDER_ENV_VAR = "OMNIGENT_TEAM_PROVIDER"
TEAM_PROVIDER_ENTRY_POINT_GROUP = "omnigent.team_providers"


class Team(BaseModel):
    """A provider-owned team ID and its display name."""

    model_config = ConfigDict(strict=True, frozen=True, extra="forbid")

    id: str = Field(min_length=1, max_length=512)
    name: str = Field(min_length=1, max_length=512)


class TeamProvider(abc.ABC):
    """An installed provider querying its directory without storing teams locally."""

    @abc.abstractmethod
    async def list_teams(self, user_id: str) -> list[Team]:
        """Return the user's complete memberships, or raise if lookup fails.

        ``user_id`` is Omnigent's authenticated user ID. The provider owns any
        translation to directory identity and the meaning of nested membership.
        An empty list means the lookup succeeded and found no memberships.
        """
        raise NotImplementedError


def load_team_provider() -> TeamProvider | None:
    """Load the selected zero-argument provider factory once at server startup.

    Unset selection disables team lookup. A configured provider must be unique
    and load successfully; failure must not masquerade as empty membership.
    """
    name = os.environ.get(TEAM_PROVIDER_ENV_VAR, "").strip()
    if not name:
        return None
    try:
        matches = [
            entry
            for entry in importlib.metadata.entry_points(group=TEAM_PROVIDER_ENTRY_POINT_GROUP)
            if entry.name == name
        ]
    except Exception:  # noqa: BLE001 — installed package metadata is an external boundary
        raise ValueError("Could not discover team providers; check installed packages") from None
    if len(matches) != 1:
        raise ValueError(
            f"{TEAM_PROVIDER_ENV_VAR}={name!r} must select exactly one installed "
            f"{TEAM_PROVIDER_ENTRY_POINT_GROUP} entry point; found {len(matches)}"
        )
    try:
        provider = matches[0].load()()
        if not isinstance(provider, TeamProvider) or not inspect.iscoroutinefunction(
            provider.list_teams
        ):
            raise TypeError("Expected an asynchronous TeamProvider")
    except Exception:  # noqa: BLE001 — provider exceptions may contain credentials
        raise ValueError(
            f"Could not load team provider {name!r}; check its package and configuration. "
            "The entry point must be a zero-argument factory returning a TeamProvider "
            "with async list_teams."
        ) from None
    return provider
