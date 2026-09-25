"""Strict operator-owned seed manifests and exact workspace-profile selection."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from omnigent.onboarding.sandboxes.types import RepoWorkspace, clone_dir_names

_COMPONENT = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,254}\Z")
_RESOURCE_NAME = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_IMAGE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:[0-9a-f]{64}\Z")


def safe_component(value: object) -> bool:
    """Accept one ordinary filename, excluding hidden names and path traversal."""
    return isinstance(value, str) and _COMPONENT.fullmatch(value) is not None


def valid_branch(value: object) -> bool:
    """Require an explicit branch with Git's ordinary reference-name constraints."""
    return (
        isinstance(value, str)
        and bool(value)
        and value != "@"
        and not value.startswith(("/", "-"))
        and not value.endswith(("/", "."))
        and not any(part.startswith(".") or part.endswith(".lock") for part in value.split("/"))
        and not any(part in value for part in ("..", "//", "@{"))
        and re.search(r"[\x00-\x20\x7f~^:?*\[\\]", value) is None
    )


def canonical_github_url(value: str) -> str:
    """Normalize credential-free GitHub HTTPS/SSH clone URLs to canonical HTTPS."""
    if not isinstance(value, str) or any(char.isspace() for char in value):
        raise ValueError("A workspace seed requires a credential-free GitHub clone URL.")
    if value.startswith("git@github.com:"):
        value = "ssh://git@github.com/" + value.removeprefix("git@github.com:")
    try:
        parsed = urlsplit(value)
        if (
            parsed.hostname != "github.com"
            or parsed.query
            or parsed.fragment
            or parsed.port is not None
            or parsed.password is not None
            or parsed.scheme not in {"https", "ssh"}
            or (parsed.scheme == "https" and parsed.username is not None)
            or (parsed.scheme == "ssh" and parsed.username != "git")
        ):
            raise ValueError
        segments = parsed.path.strip("/").split("/")
        if len(segments) != 2:
            raise ValueError
        owner, repo = segments
        repo = repo.removesuffix(".git")
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9-]*", owner) is None or not safe_component(repo):
            raise ValueError
    except ValueError:
        raise ValueError("A workspace seed requires a credential-free GitHub clone URL.") from None
    return f"https://github.com/{owner.lower()}/{repo.lower()}.git"


def _fields(value: object, names: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != names:
        raise ValueError(f"Invalid {label} fields.")
    return value


@dataclass(frozen=True)
class SeedRepository:
    url: str
    branch: str
    directory: str
    commit: str
    bundle: str
    bundle_sha256: str

    def __post_init__(self) -> None:
        if canonical_github_url(self.url) != self.url:
            raise ValueError("Seed repository URLs must use canonical HTTPS GitHub URLs.")
        if not valid_branch(self.branch):
            raise ValueError("Seed repositories require an explicit valid branch.")
        if not safe_component(self.directory) or not safe_component(self.bundle):
            raise ValueError("Seed directories and bundles must be safe single filenames.")
        for value, size in ((self.commit, 40), (self.bundle_sha256, 64)):
            if not isinstance(value, str) or re.fullmatch(rf"[0-9a-f]{{{size}}}", value) is None:
                raise ValueError("Seed commits and bundle digests must be lowercase hex.")

    def to_dict(self) -> dict[str, str]:
        return {
            "url": self.url,
            "branch": self.branch,
            "directory": self.directory,
            "commit": self.commit,
            "bundle": self.bundle,
            "bundle_sha256": self.bundle_sha256,
        }


@dataclass(frozen=True)
class SeedManifest:
    version: int
    repos: tuple[SeedRepository, ...]

    def __post_init__(self) -> None:
        if type(self.version) is not int or self.version != 1 or not self.repos:
            raise ValueError("Workspace seed manifests require version 1 and repositories.")
        if any(not isinstance(repo, SeedRepository) for repo in self.repos):
            raise ValueError("Invalid seed repository.")
        for values in (
            [(repo.url, repo.branch) for repo in self.repos],
            [repo.directory for repo in self.repos],
            [repo.bundle for repo in self.repos],
        ):
            if len(set(values)) != len(values):
                raise ValueError(
                    "Seed repository identities, directories, and bundles must be unique."
                )

    def to_dict(self) -> dict[str, object]:
        return {"version": self.version, "repos": [repo.to_dict() for repo in self.repos]}


def parse_manifest(data: object) -> SeedManifest:
    data = _fields(data, {"version", "repos"}, "seed manifest")
    if not isinstance(data["repos"], list):
        raise ValueError("Seed manifest repositories must be a list.")
    repositories = tuple(
        SeedRepository(
            **_fields(
                repo,
                {"url", "branch", "directory", "commit", "bundle", "bundle_sha256"},
                "seed repository",
            )
        )
        for repo in data["repos"]
    )
    return SeedManifest(version=data["version"], repos=repositories)


def manifest_sha(manifest: SeedManifest) -> str:
    encoded = json.dumps(manifest.to_dict(), sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class WorkspaceProfile:
    name: str
    warm_pool: str
    image: str
    manifest: SeedManifest

    def __post_init__(self) -> None:
        if any(
            not isinstance(value, str) or _RESOURCE_NAME.fullmatch(value) is None
            for value in (self.name, self.warm_pool)
        ):
            raise ValueError("Workspace profile and pool names must be DNS labels.")
        if (
            not isinstance(self.image, str)
            or _IMAGE.fullmatch(self.image) is None
            or "://" in self.image
        ):
            raise ValueError("Workspace profile images must be pinned by a SHA-256 digest.")
        if not isinstance(self.manifest, SeedManifest):
            raise ValueError("A workspace profile requires a validated seed manifest.")

    @property
    def manifest_sha(self) -> str:
        return manifest_sha(self.manifest)

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "warm_pool": self.warm_pool,
            "image": self.image,
            "manifest": self.manifest.to_dict(),
        }


def parse_profiles(data: object) -> tuple[WorkspaceProfile, ...]:
    if not isinstance(data, list):
        raise ValueError("Workspace profiles must be a JSON list.")
    profiles = []
    for entry in data:
        entry = _fields(entry, {"name", "warm_pool", "image", "manifest"}, "workspace profile")
        profiles.append(
            WorkspaceProfile(
                name=entry["name"],
                warm_pool=entry["warm_pool"],
                image=entry["image"],
                manifest=parse_manifest(entry["manifest"]),
            )
        )
    for values in (
        [profile.name for profile in profiles],
        [profile.warm_pool for profile in profiles],
    ):
        if len(set(values)) != len(values):
            raise ValueError("Workspace profile names and pool names must be unique.")
    return tuple(profiles)


def load_profiles(path: Path | str) -> tuple[WorkspaceProfile, ...]:
    return parse_profiles(json.loads(Path(path).read_text()))


def profile_matches(profile: WorkspaceProfile, repos: Sequence[RepoWorkspace]) -> bool:
    """Match every repository, explicit branch, and actual destination directory."""
    if len(profile.manifest.repos) != len(repos):
        return False
    try:
        requested = [
            (canonical_github_url(repo.url), repo.branch, directory)
            for repo, directory in zip(repos, clone_dir_names(repos), strict=True)
            if valid_branch(repo.branch) and safe_component(directory)
        ]
    except ValueError:
        return False
    expected = {(repo.url, repo.branch, repo.directory) for repo in profile.manifest.repos}
    return (
        len(requested) == len(repos)
        and len(set(requested)) == len(repos)
        and set(requested) == expected
    )


def select_profile(
    profiles: Sequence[WorkspaceProfile], repos: Sequence[RepoWorkspace]
) -> WorkspaceProfile | None:
    matches = [profile for profile in profiles if profile_matches(profile, repos)]
    if len(matches) > 1:
        raise ValueError("Multiple workspace profiles match this exact repository set.")
    return matches[0] if matches else None
