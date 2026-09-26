"""Per-user GitLab OAuth connection entity."""

from __future__ import annotations

import dataclasses


@dataclasses.dataclass(frozen=True)
class GitlabConnection:
    """A user's connected GitLab account for one canonical instance origin."""

    user_id: str
    gitlab_login: str
    gitlab_user_id: int
    gitlab_host: str
    access_token: str | None
    refresh_token: str | None
    token_expires_at: int | None
    scopes: str
    created_at: int
    updated_at: int
