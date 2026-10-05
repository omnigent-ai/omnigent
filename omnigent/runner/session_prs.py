"""Host-local pull request associations, independent of the current checkout."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import time
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit

from filelock import FileLock
from pydantic import BaseModel, Field

from omnigent.process_logging import data_dir


def _valid_hostname(host: str) -> bool:
    """Validate bounded ASCII DNS labels without hostname regex backtracking."""
    if len(host) > 253 or not host.isascii():
        return False
    labels = host.split(".")
    return (
        len(labels) > 1
        and all(
            1 <= len(label) <= 63
            and label[0].isalnum()
            and label[-1].isalnum()
            and label.replace("-", "").isalnum()
            for label in labels
        )
        and labels[-1][0].isalpha()
    )


class PullRequestRef(BaseModel):
    """A GitHub pull request or GitLab merge request in its base project."""

    provider: Literal["github", "gitlab"] = "github"
    host: str
    repository: str
    number: int
    url: str

    @classmethod
    def from_url(cls, value: str) -> PullRequestRef:
        """Normalize an HTTPS GitHub PR or GitLab MR URL without credentials."""
        parsed = urlsplit(value.strip())
        host = (parsed.hostname or "").lower()
        github_match = re.fullmatch(
            r"/([\w.-]+/[\w.-]+)/pull/([1-9][0-9]*)(?:/(?:files|commits|checks))?/?",
            parsed.path,
            flags=re.ASCII,
        )
        gitlab_match = re.fullmatch(
            r"/([\w.-]+(?:/[\w.-]+)+)/-/merge_requests/([1-9][0-9]*)/?",
            parsed.path,
            flags=re.ASCII,
        )
        if (
            parsed.scheme != "https"
            or not _valid_hostname(host)
            or parsed.netloc.lower() != host
            or (github_match is None and gitlab_match is None)
        ):
            raise ValueError("Expected an HTTPS GitHub pull request or GitLab merge request URL")
        match = github_match or gitlab_match
        assert match is not None
        provider: Literal["github", "gitlab"] = "github" if github_match else "gitlab"
        repository = match[1].lower()
        if any(part in {".", ".."} for part in repository.split("/")):
            raise ValueError("Invalid repository")
        number = int(match[2])
        if provider == "github":
            url = f"https://{host}/{repository}/pull/{number}"
        else:
            url = f"https://{host}/{repository}/-/merge_requests/{number}"
        return cls(provider=provider, host=host, repository=repository, number=number, url=url)

    @property
    def repo_argument(self) -> str:
        """Explicit repository selector, including the provider host."""
        return f"{self.host}/{self.repository}"


class SessionPullRequest(PullRequestRef):
    relationship: Literal["created", "worked_on", "attached", "inferred"]
    source: str
    first_seen_at: float
    last_seen_at: float
    title: str | None = None
    title_checked_at: float = 0
    title_lookup_timed_out: bool = False


class _Registry(BaseModel):
    schema_version: Literal[1] = 1
    prs: list[SessionPullRequest] = Field(default_factory=list)
    excluded: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


class SessionPrRegistry:
    """Atomic per-session files shared by the host and its session runners."""

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        # Conversation IDs are globally allocated; hashing also confines disk paths.
        key = hashlib.sha256(session_id.encode()).hexdigest()
        self.path = (root or data_dir() / "github" / "session-prs") / f"{key}.json"

    def _read(self) -> _Registry:
        try:
            return _Registry.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return _Registry()

    def list(self) -> list[SessionPullRequest]:
        """Read an atomic snapshot; corruption is reported without overwriting it."""
        return sorted(self._read().prs, key=lambda pr: pr.last_seen_at, reverse=True)

    def _write(self, state: _Registry) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".session-prs-", dir=self.path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.path)
        finally:
            Path(temporary).unlink(missing_ok=True)

    def record(
        self,
        references: Sequence[PullRequestRef],
        *,
        relationship: Literal["created", "worked_on", "attached", "inferred"],
        source: str,
        observation_id: str = "",
        timestamp: float | None = None,
    ) -> None:
        """Upsert associations without losing concurrent writes or replaying removals."""
        if not references:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(self.path) + ".lock", timeout=1):
            state = self._read()
            if observation_id and observation_id in state.observations:
                return
            now = time.time() if timestamp is None else timestamp
            entries = {pr.url: pr for pr in state.prs}
            for reference in references:
                reference = PullRequestRef.from_url(reference.url)
                if reference.url in state.excluded:
                    if relationship != "attached":
                        continue
                    state.excluded.remove(reference.url)
                previous = entries.get(reference.url)
                entries[reference.url] = SessionPullRequest(
                    **reference.model_dump(),
                    relationship=(
                        previous.relationship
                        if previous and previous.relationship == "created"
                        else relationship
                    ),
                    source=previous.source if previous else source,
                    first_seen_at=min(previous.first_seen_at, now) if previous else now,
                    last_seen_at=max(previous.last_seen_at, now) if previous else now,
                    title=previous.title if previous else None,
                    title_checked_at=previous.title_checked_at if previous else 0,
                    title_lookup_timed_out=previous.title_lookup_timed_out if previous else False,
                )
            state.prs = list(entries.values())
            if observation_id:
                state.observations = [*state.observations[-511:], observation_id]
            self._write(state)

    def update_titles(
        self,
        titles: Mapping[str, str | None],
        *,
        timestamp: float | None = None,
        timed_out_urls: Collection[str] = (),
    ) -> None:
        """Cache title lookups without reordering or recreating removed associations."""
        if not titles:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(self.path) + ".lock", timeout=1):
            state = self._read()
            now = time.time() if timestamp is None else timestamp
            changed = False
            for entry in state.prs:
                if entry.url not in titles or entry.title_checked_at > now:
                    continue
                title = titles[entry.url]
                if title is not None:
                    entry.title = title
                entry.title_checked_at = now
                entry.title_lookup_timed_out = title is None and entry.url in timed_out_urls
                changed = True
            if changed:
                self._write(state)

    def remove(self, url: str) -> None:
        """Remember removal so subsequent hook replay cannot attach the PR again."""
        reference = PullRequestRef.from_url(url)
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with FileLock(str(self.path) + ".lock", timeout=1):
            state = self._read()
            state.prs = [pr for pr in state.prs if pr.url != reference.url]
            if reference.url not in state.excluded:
                state.excluded.append(reference.url)
            self._write(state)


def observation_key(source: str, call_id: str, payload: object) -> str:
    """Prefer stable provider call IDs; hash payloads when a transport omits one."""
    value = call_id or json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{source}:{value}".encode()).hexdigest()
