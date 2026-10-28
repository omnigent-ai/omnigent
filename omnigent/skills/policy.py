"""Operator policy for the skills marketplace: the ``skills_marketplace:`` config block.

Admin is a property of a user; this block is a constraint set by whoever
provisions the server and holds for admins and the single local user alike.
It is enforced on every fetch path and again at delivery, so tightening it
also stops serving skills that were installed under a looser policy.

.. code-block:: yaml

    skills_marketplace:
      sources:                 # seeded at startup, read-only in the UI
        - url: https://github.com/acme/skills
          ref: v1.4.0
      allowed_sources:         # host/owner/repo globs; empty = any
        - github.com/acme/*
        - github.acme.internal/*/*
      allow_url_install: false
      credentials:             # sent only to a source both lists allow
        - match: github.acme.internal/*/*
          token_env: SKILLS_GHE_TOKEN
"""

from __future__ import annotations

import fnmatch
import logging
import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from omnigent.skills.git_source import GitSourceError, parse_github_source

_logger = logging.getLogger(__name__)

CONFIG_KEY = "skills_marketplace"


class SkillPolicyError(GitSourceError):
    """The server's policy refuses the repository; the message is user-facing."""


@dataclass(frozen=True)
class ConfiguredSource:
    """A repository the operator lists; seeded into the browse list at startup."""

    url: str
    ref: str | None
    name: str | None


@dataclass(frozen=True)
class SourceCredential:
    """A token the server sends to repositories matching one pattern."""

    match: str
    token_env: str


def repository_key(clone_url: str) -> str | None:
    """``host/owner/repo`` (lower-cased) for an HTTPS clone URL, else ``None``."""
    parts = urlsplit(clone_url)
    if parts.scheme != "https" or not parts.hostname:
        return None
    path = parts.path.strip("/").removesuffix(".git")
    segments = path.split("/")
    if len(segments) != 2 or not all(segments):
        return None
    return "/".join([parts.hostname.lower(), *segments]).lower()


def pattern_matches(pattern: str, key: str) -> bool:
    """Match a ``host/owner/repo`` glob segment by segment; ``*`` never crosses ``/``.

    A pattern with fewer segments matches the rest as ``*``, so
    ``github.acme.internal`` covers the whole host.
    """
    wanted = pattern.lower().strip("/").split("/")
    actual = key.split("/")
    if len(wanted) > len(actual):
        return False
    return all(fnmatch.fnmatchcase(have, want) for want, have in zip(wanted, actual, strict=False))


@dataclass(frozen=True)
class SkillsMarketplacePolicy:
    """The parsed ``skills_marketplace:`` block; the default allows everything."""

    sources: tuple[ConfiguredSource, ...] = ()
    allowed_sources: tuple[str, ...] = ()
    allow_url_install: bool = True
    credentials: tuple[SourceCredential, ...] = ()

    def allows(self, clone_url: str) -> bool:
        """Whether ``allowed_sources`` admits the repository (an empty list admits any)."""
        if not self.allowed_sources:
            return True
        key = repository_key(clone_url)
        return key is not None and any(pattern_matches(p, key) for p in self.allowed_sources)

    def require_allowed(self, clone_url: str) -> None:
        """:raises SkillPolicyError: When the repository is outside ``allowed_sources``."""
        if not self.allows(clone_url):
            raise SkillPolicyError(f"{clone_url} is not an allowed skill source on this server.")

    def is_configured(self, url: str, ref: str | None) -> bool:
        """Whether the operator listed this exact repository and ref."""
        return any(s.url == url and s.ref == ref for s in self.sources)

    def token_for(self, clone_url: str) -> str | None:
        """The token for a repository both ``allowed_sources`` and a credential match.

        Read from the credential's environment variable at fetch time; an
        unset variable sends nothing and is logged.
        """
        key = repository_key(clone_url)
        if key is None or not self.allows(clone_url):
            return None
        for credential in self.credentials:
            if not pattern_matches(credential.match, key):
                continue
            token = os.environ.get(credential.token_env, "").strip()
            if not token:
                _logger.warning(
                    "skills_marketplace credential for %s names %s, which is not set",
                    credential.match,
                    credential.token_env,
                )
            return token or None
        return None

    @classmethod
    def from_config(cls, raw: Any) -> SkillsMarketplacePolicy:
        """Parse the config block; an unusable entry is logged and skipped, never fatal."""
        if raw is None:
            return cls()
        if not isinstance(raw, Mapping):
            _logger.warning("%s is not a mapping, ignoring it", CONFIG_KEY)
            return cls()
        allow_url_install = raw.get("allow_url_install", True)
        if not isinstance(allow_url_install, bool):
            _logger.warning("%s.allow_url_install must be true or false", CONFIG_KEY)
            allow_url_install = True
        return cls(
            sources=tuple(_parse_sources(raw.get("sources"))),
            allowed_sources=tuple(_parse_patterns(raw.get("allowed_sources"))),
            allow_url_install=allow_url_install,
            credentials=tuple(_parse_credentials(raw.get("credentials"))),
        )


DEFAULT_POLICY = SkillsMarketplacePolicy()


def _entries(raw: Any, key: str) -> list[Any]:
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    _logger.warning("%s.%s must be a list, ignoring it", CONFIG_KEY, key)
    return []


def _parse_sources(raw: Any) -> list[ConfiguredSource]:
    sources = []
    for entry in _entries(raw, "sources"):
        item = {"url": entry} if isinstance(entry, str) else entry
        url = item.get("url") if isinstance(item, Mapping) else None
        parsed = parse_github_source(url.strip()) if isinstance(url, str) else None
        if parsed is None:
            _logger.warning("%s.sources entry %r is not a GitHub repository", CONFIG_KEY, entry)
            continue
        ref = item.get("ref")
        name = item.get("name")
        sources.append(
            ConfiguredSource(
                url=parsed.clone_url,
                ref=(str(ref).strip() or None) if ref is not None else parsed.ref,
                name=name.strip() or None if isinstance(name, str) else None,
            )
        )
    return sources


def _parse_patterns(raw: Any) -> list[str]:
    patterns = []
    for entry in _entries(raw, "allowed_sources"):
        pattern = entry.strip().strip("/").lower() if isinstance(entry, str) else ""
        if not pattern or pattern.count("/") > 2:
            _logger.warning(
                "%s.allowed_sources entry %r is not a host/owner/repo pattern", CONFIG_KEY, entry
            )
            continue
        patterns.append(pattern)
    return patterns


def _parse_credentials(raw: Any) -> list[SourceCredential]:
    credentials = []
    for entry in _entries(raw, "credentials"):
        match = entry.get("match") if isinstance(entry, Mapping) else None
        token_env = entry.get("token_env") if isinstance(entry, Mapping) else None
        if not (isinstance(match, str) and match.strip() and isinstance(token_env, str)):
            _logger.warning("%s.credentials entry %r needs match and token_env", CONFIG_KEY, entry)
            continue
        if not token_env.strip():
            _logger.warning("%s.credentials entry %r has an empty token_env", CONFIG_KEY, entry)
            continue
        credentials.append(
            SourceCredential(match=match.strip().strip("/").lower(), token_env=token_env.strip())
        )
    return credentials
