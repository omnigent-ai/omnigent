"""Duplicate detection for Inbox items.

Agents re-read the same history on every run, so a suggestion would repeat
until the user acts on it. The server builds a versioned key from an item's
``kind``, canonical primary link, and ``topic``, and :func:`find_duplicate` is
the only place that decides whether a post repeats an existing item. Routes,
tools, and the UI never inspect keys themselves, so the formula can change:
add a new version to ``_KEY_FORMULAS``, backfill stored keys from the stored
inputs, then move ``CURRENT_KEY_VERSION``. During the backfill,
``LEGACY_KEY_VERSIONS`` keeps older keys matching.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from omnigent.entities import InboxItem
from omnigent.stores.inbox_item_store import InboxItemStore

_logger = logging.getLogger(__name__)

CURRENT_KEY_VERSION = "v1"
# Older versions still checked while their stored keys are being backfilled.
LEGACY_KEY_VERSIONS: tuple[str, ...] = ()

_GITHUB_PULL_PATH = re.compile(r"^/([^/]+)/([^/]+)/pull/(\d+)(?:/.*)?$")
_TOPIC_SEPARATORS = re.compile(r"[\s_]+")


@dataclass(frozen=True)
class DedupeInputs:
    """The fields a duplicate key is built from.

    :param kind: ``"followup"``, ``"info"``, or ``"reminder"``.
    :param primary_link: Canonical primary link, or ``None``.
    :param topic: Normalized topic slug.
    """

    kind: str
    primary_link: str | None
    topic: str


@dataclass(frozen=True)
class DuplicateMatch:
    """Result of :func:`find_duplicate`.

    :param existing: The item the post repeats, or ``None`` if it is new.
    :param expired: An expired item holding the post's key, which the new
        item replaces; ``None`` if there is none.
    """

    existing: InboxItem | None
    expired: InboxItem | None = None


def normalize_topic(topic: str) -> str:
    """Lowercase *topic*, trim it, and join words with hyphens."""
    return _TOPIC_SEPARATORS.sub("-", topic.strip().lower()).strip("-")


def canonical_link(link: Mapping[str, Any]) -> str | None:
    """Return one stable string for a link, so spellings of the same target match.

    Sessions become ``session:<id>``. GitHub pull request URLs keep only
    ``github.com/<owner>/<repo>/pull/<n>``, dropping subpages such as
    ``/files``. Other URLs drop the scheme, fragment, and trailing slash and
    lowercase the host.

    :param link: A link with ``type`` and ``ref``.
    :returns: The canonical form, or ``None`` when ``ref`` is empty.
    """
    ref = str(link.get("ref") or "").strip()
    if not ref:
        return None
    if link.get("type") == "session":
        return f"session:{ref}"
    parts = urlsplit(ref if "://" in ref else f"https://{ref}")
    host = (parts.hostname or "").lower()
    if host == "www.github.com":
        host = "github.com"
    path = parts.path.rstrip("/")
    if host == "github.com":
        match = _GITHUB_PULL_PATH.match(path)
        if match is not None:
            owner, repo, number = match.groups()
            return f"github.com/{owner.lower()}/{repo.lower()}/pull/{number}"
    query = f"?{parts.query}" if parts.query else ""
    return f"{host}{path}{query}"


def _key_v1(inputs: DedupeInputs) -> str:
    material = "|".join((inputs.kind, inputs.primary_link or "", inputs.topic))
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


_KEY_FORMULAS: dict[str, Callable[[DedupeInputs], str]] = {"v1": _key_v1}


def dedupe_key(inputs: DedupeInputs, version: str = CURRENT_KEY_VERSION) -> str:
    """Return the versioned key for *inputs*, e.g. ``"v1:3f2a..."``."""
    return f"{version}:{_KEY_FORMULAS[version](inputs)}"


def find_duplicate(
    store: InboxItemStore,
    user_id: str,
    inputs: DedupeInputs,
    *,
    now: int,
) -> DuplicateMatch:
    """Decide whether a post repeats one of the owner's items.

    A match suppresses the post permanently, including after the user
    dismissed or rated the item, unless the item's ``expires_at`` has passed;
    then the post creates a fresh item that replaces the expired one.

    :param store: The owner's Inbox item store.
    :param user_id: The owner.
    :param inputs: The post's key inputs.
    :param now: Current epoch seconds.
    """
    for version in (CURRENT_KEY_VERSION, *LEGACY_KEY_VERSIONS):
        existing = store.get_by_dedupe_key(user_id, dedupe_key(inputs, version))
        if existing is None:
            continue
        if existing.expires_at is not None and existing.expires_at <= now:
            return DuplicateMatch(existing=None, expired=existing)
        return DuplicateMatch(existing=existing)
    return DuplicateMatch(existing=None)


def log_post_decision(inputs: DedupeInputs, match: DuplicateMatch) -> None:
    """Log a post's key inputs and outcome so a new formula can be replayed against them."""
    _logger.info(
        "inbox item duplicate check",
        extra={
            "inbox_dedupe_version": CURRENT_KEY_VERSION,
            "inbox_dedupe_kind": inputs.kind,
            "inbox_dedupe_primary_link": inputs.primary_link,
            "inbox_dedupe_topic": inputs.topic,
            "inbox_dedupe_matched_item": match.existing.id if match.existing else None,
            "inbox_dedupe_replaced_expired": match.expired.id if match.expired else None,
        },
    )
