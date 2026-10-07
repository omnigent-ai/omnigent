"""Host-local pull request associations, independent of the current checkout."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from collections.abc import Collection, Mapping, Sequence
from pathlib import Path
from typing import Literal

from filelock import FileLock
from filelock import Timeout as FileLockTimeout
from pydantic import BaseModel, Field

from omnigent.process_logging import data_dir


class PullRequestRef(BaseModel):
    """A PR belongs to its base repository, including for fork PRs."""

    # Registry files written before git providers existed hold only GitHub PRs.
    provider: str = "github"
    host: str
    repository: str
    number: int
    url: str

    @classmethod
    def from_url(cls, value: str) -> PullRequestRef:
        """Normalize a PR URL with the first registered git provider that recognizes it."""
        from omnigent.git_providers import EnvInstances, resolve_pr_url

        parsed = resolve_pr_url(value.strip(), EnvInstances())
        if parsed is None:
            raise ValueError("No registered git provider recognizes this pull request URL")
        return cls(
            provider=parsed.provider,
            host=parsed.host,
            repository=parsed.repository,
            number=parsed.number,
            url=parsed.url,
        )


class SessionPullRequest(PullRequestRef):
    relationship: Literal["created", "worked_on", "attached", "inferred"]
    source: str
    first_seen_at: float
    last_seen_at: float
    title: str | None = None
    title_checked_at: float = 0
    title_lookup_timed_out: bool = False


class _Associations(BaseModel):
    schema_version: Literal[1] = 1
    prs: list[SessionPullRequest] = Field(default_factory=list)


class _Registry(_Associations):
    excluded: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)


class _ProviderRegistry(_Associations):
    order: list[str] = Field(default_factory=list)


class SessionPrRegistry:
    """Atomic per-session files shared by the host and its session runners."""

    def __init__(self, session_id: str, *, root: Path | None = None) -> None:
        # Conversation IDs are globally allocated; hashing also confines disk paths.
        key = hashlib.sha256(session_id.encode()).hexdigest()
        self.path = (root or data_dir() / "github" / "session-prs") / f"{key}.json"
        self._providers_path = self.path.with_suffix(".providers.json")

    def _read(self) -> _Registry:
        """Read both files under the legacy lock, migrating pre-split registries."""
        try:
            state = _Registry.model_validate_json(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            state = _Registry()
        try:
            providers = _ProviderRegistry.model_validate_json(
                self._providers_path.read_text(encoding="utf-8")
            )
        except FileNotFoundError:
            providers = _ProviderRegistry()
        needs_migration = any(pr.provider != "github" for pr in state.prs)
        # The companion is written first, so it wins after an interrupted migration.
        excluded = set(state.excluded)
        entries = {pr.url: pr for pr in [*state.prs, *providers.prs] if pr.url not in excluded}
        # Equal timestamps retain insertion order across files and process restarts.
        order = {url: index for index, url in enumerate(providers.order)}
        state.prs = sorted(entries.values(), key=lambda pr: order.get(pr.url, len(order)))
        if needs_migration:
            self._write(state)
        return state

    def list(self) -> list[SessionPullRequest]:
        """Read a locked snapshot, reporting corruption or contention as ValueError."""
        if not self.path.parent.exists():
            return []
        try:
            with FileLock(str(self.path) + ".lock", timeout=1):
                return sorted(self._read().prs, key=lambda pr: pr.last_seen_at, reverse=True)
        except FileLockTimeout as exc:
            raise ValueError("PR tracking is busy; try again.") from exc

    def _write(self, state: _Registry, *, legacy_first: bool = False) -> None:
        # Old hosts may rewrite this file; keep foreign associations out of it.
        legacy = state.model_copy(
            update={"prs": [pr for pr in state.prs if pr.provider == "github"]}
        )
        if legacy_first:
            self._write_file(self.path, legacy)
        foreign = [pr for pr in state.prs if pr.provider != "github"]
        if foreign or self._providers_path.exists():
            self._write_file(
                self._providers_path,
                _ProviderRegistry(prs=foreign, order=[pr.url for pr in state.prs]),
            )
        if not legacy_first:
            self._write_file(self.path, legacy)

    @staticmethod
    def _write_file(path: Path, state: _Associations) -> None:
        fd, temporary = tempfile.mkstemp(prefix=".session-prs-", dir=path.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                stream.write(state.model_dump_json() + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
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
            # Commit the exclusion before deleting its companion association.
            self._write(state, legacy_first=reference.provider != "github")


def observation_key(source: str, call_id: str, payload: object) -> str:
    """Prefer stable provider call IDs; hash payloads when a transport omits one."""
    value = call_id or json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(f"{source}:{value}".encode()).hexdigest()
