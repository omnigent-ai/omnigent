"""Lines added and removed by a landed commit, for the Summary's Changes landed.

A landed ticket's size is ``git diff --numstat <sha>^1 <sha>``: the change its landed
commit made over its first parent. Generated files (``openapi.json``, lockfiles,
generated clients, and anything ``.gitattributes`` marks ``linguist-generated``) are
left out of the headline numbers; the raw totals are kept for the drill-down. Binary
files count no lines.

A SHA's stats never change, so they're cached in memory. When git or the commit isn't
reachable, the stats are ``None`` and callers show the counts without them.
"""

from __future__ import annotations

import fnmatch
import logging
import re
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)

#: Paths always treated as generated, matched against the path and its file name.
GENERATED_PATTERNS = (
    "openapi.json",
    "*.lock",
    "package-lock.json",
    "pnpm-lock.yaml",
    "yarn.lock",
    "uv.lock",
    "poetry.lock",
    "Cargo.lock",
    "*.generated.*",
    "*_pb2.py",
    "*_pb2.pyi",
    "*/generated/*",
)
_SHA = re.compile(r"[0-9a-f]{40}")
_GIT_TIMEOUT_SECONDS = 20
_CACHE_MAX = 4096


@dataclass(frozen=True)
class LineStats:
    """One commit's size; ``added``/``removed`` leave generated files out."""

    added: int
    removed: int
    raw_added: int
    raw_removed: int
    files: int


#: Most commits one record or missing-list call handles.
LINE_STATS_BATCH_MAX = 50


class CommitLineStats(BaseModel):
    """One landed commit's line counts, as computed by git on a host."""

    model_config = ConfigDict(extra="forbid")
    sha: str = Field(pattern=r"^[0-9a-f]{40}$", description="The landed commit's full SHA.")
    added: int = Field(ge=0, description="Lines added, generated files left out.")
    removed: int = Field(ge=0, description="Lines removed, generated files left out.")
    raw_added: int = Field(ge=0)
    raw_removed: int = Field(ge=0)
    files: int = Field(ge=0)


class RecordLineStats(BaseModel):
    model_config = ConfigDict(extra="forbid")
    stats: list[CommitLineStats] = Field(min_length=1, max_length=LINE_STATS_BATCH_MAX)


class RecordedLineStats(BaseModel):
    recorded: int = Field(description="New rows; commits that already had counts are kept.")


class MissingLineStats(BaseModel):
    shas: list[str] = Field(description="Landed commits with no line counts yet.")
    remaining: int = Field(description="How many more are missing beyond this page.")


_cache: dict[tuple[str, str], LineStats | None] = {}
_cache_lock = threading.Lock()


def is_generated(path: str, marked: set[str] | None = None) -> bool:
    """Whether *path* is generated: a built-in pattern, or marked in *marked*."""
    if marked and path in marked:
        return True
    name = path.rsplit("/", 1)[-1]
    return any(
        fnmatch.fnmatch(path, pattern) or fnmatch.fnmatch(name, pattern)
        for pattern in GENERATED_PATTERNS
    )


def _git(checkout: Path, *args: str, stdin: str | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(checkout), *args],
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=_GIT_TIMEOUT_SECONDS,
    ).stdout


def _marked_generated(checkout: Path, sha: str, paths: list[str]) -> set[str]:
    """The *paths* that ``.gitattributes`` at *sha* marks ``linguist-generated``."""
    if not paths:
        return set()
    output = _git(
        checkout,
        "check-attr",
        f"--source={sha}",
        "linguist-generated",
        "--stdin",
        stdin="\n".join(paths) + "\n",
    )
    marked = set()
    for line in output.splitlines():
        path, _, value = line.rpartition(": linguist-generated: ")
        if path and value.strip() in ("set", "true"):
            marked.add(path)
    return marked


def parse_numstat(output: str) -> list[tuple[str, int, int]]:
    """``(path, added, removed)`` per file; binary files (``-``) count no lines."""
    rows = []
    for line in output.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        added, removed, path = parts
        rows.append(
            (
                path,
                int(added) if added.isdigit() else 0,
                int(removed) if removed.isdigit() else 0,
            )
        )
    return rows


def _compute(checkout: Path, sha: str) -> LineStats:
    rows = parse_numstat(_git(checkout, "diff", "--numstat", "--no-renames", f"{sha}^1", sha))
    marked = _marked_generated(checkout, sha, [path for path, _, _ in rows])
    kept = [(added, removed) for path, added, removed in rows if not is_generated(path, marked)]
    return LineStats(
        added=sum(added for added, _ in kept),
        removed=sum(removed for _, removed in kept),
        raw_added=sum(added for _, added, _ in rows),
        raw_removed=sum(removed for _, _, removed in rows),
        files=len(rows),
    )


def line_stats(checkout: Path | None, sha: str) -> LineStats | None:
    """The landed commit *sha*'s size in *checkout*, or ``None`` when git can't tell."""
    sha = sha.lower()
    if checkout is None or not _SHA.fullmatch(sha):
        return None
    key = (str(checkout), sha)
    with _cache_lock:
        if key in _cache:
            return _cache[key]
    try:
        stats: LineStats | None = _compute(checkout, sha)
    except (OSError, subprocess.SubprocessError, ValueError):
        logger.debug("No line stats for %s in %s", sha, checkout, exc_info=True)
        # Unknown commits may arrive later (a fetch), so only successes are cached.
        return None
    with _cache_lock:
        if len(_cache) >= _CACHE_MAX:
            _cache.clear()
        _cache[key] = stats
    return stats


def stats_checkout(config: dict) -> Path | None:
    """Where landed commits are read: the landing checkout, else the server's repository."""
    from omnigent.errors import OmnigentError
    from omnigent.server import project_landing

    try:
        return project_landing.resolve_target(config).checkout
    except (OmnigentError, OSError, ValueError):
        # An invalid landing config still leaves the server's own repository.
        pass
    try:
        return project_landing._default_checkout()
    except (OSError, ValueError):
        return None
