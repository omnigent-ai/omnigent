"""Read GitLab's REST API through the execution host's authenticated glab CLI."""

from __future__ import annotations

import json
import os
import subprocess
import time
from typing import Any
from urllib.parse import urlencode

from omnigent.git_providers import EnvInstances
from omnigent.git_providers.gitlab import GitLabProvider, instance_authority

REQUEST_BUDGET_SECONDS = 8.0
_PAGE_SIZE = 100
_MAX_PAGES = 5


class GitLabError(ValueError):
    """A CLI/API operation failed without exposing CLI output or credentials."""


class GitLabClient:
    """Host-scoped, read-only requests bounded by one panel request's deadline."""

    def __init__(self, root: str, host: str, *, deadline: float | None = None) -> None:
        authority = instance_authority(host)
        if authority is None or not GitLabProvider().matches_host(authority, EnvInstances()):
            raise GitLabError(
                "Configure this GitLab instance on the execution host before accessing it."
            )
        self.root, self.host = root, authority
        self.deadline = (
            deadline if deadline is not None else time.monotonic() + REQUEST_BUDGET_SECONDS
        )

    def _run(self, args: list[str]) -> Any:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("GitLab request timed out. Refresh to retry.")
        try:
            result = subprocess.run(
                ["glab", *args],
                cwd=self.root,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                timeout=remaining,
                check=False,
                env={
                    **os.environ,
                    "NO_COLOR": "1",
                    "GLAB_NO_PROMPT": "1",
                    "GITLAB_HOST": self.host,
                },
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError("GitLab request timed out. Refresh to retry.") from exc
        except OSError as exc:
            raise GitLabError(
                "Install glab on the execution host, then sign in to this GitLab instance."
            ) from exc
        if result.returncode != 0:
            raise GitLabError(
                "GitLab could not read this resource. "
                f"Check access with GITLAB_HOST={self.host} glab auth status."
            )
        try:
            return json.loads(result.stdout)
        except (ValueError, UnicodeDecodeError) as exc:
            raise GitLabError("GitLab returned an invalid API response.") from exc

    def get(self, path: str, **query: str | int) -> Any:
        """GET one relative REST endpoint, validating JSON and refusing arbitrary URLs."""
        if (
            path.startswith(("/", "-"))
            or "://" in path
            or any(p in {".", ".."} for p in path.split("/"))
        ):
            raise GitLabError("Invalid GitLab API path.")
        endpoint = path + ("?" + urlencode(query) if query else "")
        return self._run(["api", "--method", "GET", endpoint])

    def object(self, path: str, **query: str | int) -> dict[str, Any]:
        """GET a JSON object, rejecting responses with a different shape."""
        value = self.get(path, **query)
        if not isinstance(value, dict):
            raise GitLabError("GitLab returned an invalid object response.")
        return value

    def pages(self, path: str, **query: str | int) -> tuple[list[dict[str, Any]], bool]:
        """Read bounded list pages, reporting a partial list instead of dropping it."""
        values: list[dict[str, Any]] = []
        for page in range(1, _MAX_PAGES + 1):
            try:
                batch = self.get(path, **query, page=page, per_page=_PAGE_SIZE)
                if not isinstance(batch, list) or any(
                    not isinstance(value, dict) for value in batch
                ):
                    raise GitLabError("GitLab returned an invalid list response.")
            except (GitLabError, TimeoutError):
                if values:
                    return values, True
                raise
            values.extend(batch)
            if len(batch) < _PAGE_SIZE:
                return values, False
        return values, True
