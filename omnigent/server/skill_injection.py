"""Fold a session owner's installed skills into the agent bundle a runner fetches.

The runner discovers ``skills/<name>/SKILL.md`` inside the directory of the
spec it runs (the bundle root, or ``agents/<dir>/`` for a child), so adding
installed skills under that directory needs no runner change. One bad
tarball drops only itself; exceeding the delivery bounds is an error the
caller sees rather than a skill silently missing from the menu.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass

from omnigent.entities import Agent, InstalledSkill
from omnigent.runtime.agent_cache import AgentCache
from omnigent.skills.git_source import ancestor_paths
from omnigent.skills.limits import (
    MAX_DELIVERY_BYTES,
    MAX_DELIVERY_ENTRIES,
    MAX_INSTALLED_PER_OWNER,
    MAX_PAGE,
    RUNNER_UNPACK_BYTES,
    RUNNER_UNPACK_ENTRIES,
)
from omnigent.skills.overlay import (
    SkillTarballError,
    UnpackedSize,
    overlay_skills_within,
    validate_skill_tarball,
)
from omnigent.skills.policy import DEFAULT_POLICY, SkillsMarketplacePolicy
from omnigent.spec.types import AgentSpec
from omnigent.spec.validator import is_valid_skill_name
from omnigent.stores import ArtifactStore
from omnigent.stores.skill_store import SkillStore

_logger = logging.getLogger(__name__)

# A user's own rows plus the org's, each capped at install time.
MAX_DELIVERED_SKILLS = 2 * MAX_INSTALLED_PER_OWNER
# Served bundles kept in memory across runner fetches.
OVERLAY_CACHE_BYTES = 256 * 1024 * 1024


class SkillDeliveryError(Exception):
    """The installed skills exceed what one session can be given; the message is user-facing."""


@dataclass(frozen=True)
class SessionTarget:
    """The spec a session runs and where its files live inside the bundle.

    :param spec: The parsed spec, or ``None`` when the bundle could not be loaded.
    :param prefix: ``""`` for the root agent, else ``agents/<dir>/`` per level of nesting.
    """

    spec: AgentSpec | None
    prefix: str


def bundle_spec(agent_cache: AgentCache | None, agent: Agent) -> AgentSpec | None:
    """The agent's parsed spec, for its ``skills:`` filter; ``None`` when unavailable."""
    if agent_cache is None:
        return None
    try:
        return agent_cache.load(
            agent.id, agent.bundle_location, expand_env=agent.operator_authored
        ).spec
    except Exception:  # noqa: BLE001 — the filter is advisory; the bundle still ships
        _logger.warning("could not load spec for agent %s to filter skills", agent.id)
        return None


def session_target(spec: AgentSpec | None, sub_agent_name: str | None) -> SessionTarget:
    """Resolve the child a session runs, by declared name at any depth, and its directory.

    An unknown name falls back to the root, as the runner does.
    """
    if spec is None or not sub_agent_name:
        return SessionTarget(spec, "")
    found = _find_sub_agent(spec, sub_agent_name, "")
    return found if found is not None else SessionTarget(spec, "")


def _find_sub_agent(spec: AgentSpec, name: str, prefix: str) -> SessionTarget | None:
    for child in spec.sub_agents:
        child_prefix = f"{prefix}agents/{child.source_rel_dir or child.name}/"
        if child.name == name:
            return SessionTarget(child, child_prefix)
        nested = _find_sub_agent(child, name, child_prefix)
        if nested is not None:
            return nested
    return None


def skill_allowed(spec: AgentSpec | None, name: str) -> bool:
    """Apply the agent spec's ``skills:`` filter the way host skills honour it."""
    skills_filter = getattr(spec, "skills_filter", "all") if spec is not None else "all"
    if skills_filter == "none":
        return False
    if isinstance(skills_filter, list):
        return name in skills_filter
    return True


class _OverlayCache:
    """Served bundles by everything that decides their bytes, least recently used out first.

    A runner fetches a session's bundle on every start and reconnect; the
    merge decompresses the agent bundle and every skill, so it is done once
    per distinct input set. The key holds the agent bundle's digest and each
    skill's row id and artifact key, which is unique to one write, so any
    change to the bundle or to the installed set is a different entry.
    """

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max_bytes
        self._entries: OrderedDict[tuple[object, ...], bytes] = OrderedDict()
        self._bytes = 0
        self._lock = threading.Lock()

    def get(self, key: tuple[object, ...]) -> bytes | None:
        with self._lock:
            value = self._entries.get(key)
            if value is not None:
                self._entries.move_to_end(key)
            return value

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes = 0

    def put(self, key: tuple[object, ...], value: bytes) -> None:
        if len(value) > self._max_bytes // 4:
            return
        with self._lock:
            old = self._entries.pop(key, None)
            if old is not None:
                self._bytes -= len(old)
            self._entries[key] = value
            self._bytes += len(value)
            while self._bytes > self._max_bytes:
                _, evicted = self._entries.popitem(last=False)
                self._bytes -= len(evicted)


_overlay_cache = _OverlayCache(OVERLAY_CACHE_BYTES)


def with_installed_skills(
    bundle_bytes: bytes,
    *,
    owner_id: str | None,
    skill_store: SkillStore,
    artifact_store: ArtifactStore,
    target: SessionTarget | None = None,
    policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
) -> bytes:
    """Return the bundle with the owner's enabled installed skills under the target's ``skills/``.

    The target spec's own skills, by their canonical parsed names, always win
    a collision; a user-scoped install wins over an org-scoped one of the same
    name. A tarball that fails validation drops only itself, and so does a
    skill whose repository the server's policy no longer allows.

    A name the runner's spec validator refuses is skipped, since one invalid
    skill would fail the whole spec. When the agent's own files leave too
    little room under what a runner unpacks, the skills that do not fit are
    left out with a warning rather than failing the session. The result is
    cached per input set (see :class:`_OverlayCache`).

    :param target: The spec the session runs and its directory in the bundle;
        ``None`` means the root agent with no filter.
    :raises SkillDeliveryError: When the skills exceed :data:`MAX_DELIVERED_SKILLS`,
        :data:`MAX_DELIVERY_BYTES` or :data:`MAX_DELIVERY_ENTRIES` together.
    """
    target = target or SessionTarget(None, "")
    installed = effective_installed_skills(skill_store, owner_id, policy=policy)
    reserved = {skill.name for skill in getattr(target.spec, "skills", None) or []}
    selected: list[InstalledSkill] = []
    for skill in installed:
        if not skill_allowed(target.spec, skill.name) or skill.name in reserved:
            continue
        if not is_valid_skill_name(skill.name):
            _logger.warning("skipping installed skill %r (%s): invalid name", skill.name, skill.id)
            continue
        selected.append(skill)
    if not selected:
        return bundle_bytes
    # Rows carry their unpacked size, so the backstop needs no artifact read.
    total = UnpackedSize(0, 0)
    for skill in selected:
        total += UnpackedSize(skill.bundle_bytes, skill.bundle_entries)
    if total.bytes > MAX_DELIVERY_BYTES or total.entries > MAX_DELIVERY_ENTRIES:
        raise SkillDeliveryError(
            f"Installed skills exceed {MAX_DELIVERY_BYTES // (1024 * 1024)} MB or "
            f"{MAX_DELIVERY_ENTRIES} files together; disable or remove some in "
            "Settings → Skills."
        )
    limit = UnpackedSize(RUNNER_UNPACK_BYTES, RUNNER_UNPACK_ENTRIES)
    key = (
        hashlib.sha256(bundle_bytes).hexdigest(),
        target.prefix,
        tuple(sorted(reserved)),
        tuple((skill.id, skill.name, skill.bundle_location) for skill in selected),
        limit,
    )
    cached = _overlay_cache.get(key)
    if cached is not None:
        return cached
    accepted: list[tuple[str, bytes]] = []
    for skill in selected:
        try:
            tarball = artifact_store.get(skill.bundle_location)
            validate_skill_tarball(tarball)
        except (SkillTarballError, OSError, KeyError, ValueError):
            _logger.warning(
                "skipping installed skill %s (%s): unreadable tarball",
                skill.name,
                skill.id,
                exc_info=True,
            )
            continue
        accepted.append((skill.name, tarball))
    try:
        merged, skipped = overlay_skills_within(
            bundle_bytes, accepted, reserved=reserved, prefix=target.prefix, limit=limit
        )
    except SkillTarballError:
        _logger.warning(
            "could not overlay installed skills; serving the bundle as is", exc_info=True
        )
        return bundle_bytes
    if skipped:
        _logger.warning(
            "left installed skills %s out of a session's bundle: with the agent's own "
            "files they exceed what a runner unpacks (%d MB, %d files)",
            ", ".join(skipped),
            RUNNER_UNPACK_BYTES // (1024 * 1024),
            RUNNER_UNPACK_ENTRIES,
        )
    _overlay_cache.put(key, merged)
    return merged


def effective_installed_skills(
    skill_store: SkillStore,
    owner_id: str | None,
    *,
    policy: SkillsMarketplacePolicy = DEFAULT_POLICY,
) -> list[InstalledSkill]:
    """The owner's enabled installed skills, one per name, user scope winning over org.

    A skill from a repository outside the policy's ``allowed_sources`` is left
    out, and with ``allow_url_install`` off so is one from no listed source,
    so tightening the policy stops serving it. Reads at most
    :data:`MAX_DELIVERED_SKILLS` rows, two full pages.

    :raises SkillDeliveryError: When more rows than that are visible.
    """
    by_name: dict[str, InstalledSkill] = {}
    # With URL installs off, only skills from a listed source are served, so
    # turning the switch off also stops serving what was installed by URL.
    listed = None if policy.allow_url_install else listed_sources(skill_store)
    after: str | None = None
    seen = 0
    while True:
        page = skill_store.list_for_user(owner_id, limit=MAX_PAGE, after=after)
        for skill in page.data:
            if not skill.enabled or not policy.allows(skill.source_url):
                continue
            if listed is not None and not any(
                (skill.source_url, skill.ref, path) in listed
                for path in ancestor_paths(skill.path)
            ):
                continue
            current = by_name.get(skill.name)
            if current is None or (current.scope == "org" and skill.scope == "user"):
                by_name[skill.name] = skill
        seen += len(page.data)
        if seen > MAX_DELIVERED_SKILLS:
            raise SkillDeliveryError(
                f"More than {MAX_DELIVERED_SKILLS} installed skills are visible to this "
                "session; remove some in Settings → Skills."
            )
        if not page.has_more or page.last_id is None:
            break
        after = page.last_id
    return list(by_name.values())


def listed_sources(skill_store: SkillStore) -> set[tuple[str, str | None, str]]:
    """Every browsable source as ``(url, ref, path)``, read page by page."""
    listed: set[tuple[str, str | None, str]] = set()
    after: str | None = None
    while True:
        page = skill_store.list_sources(limit=MAX_PAGE, after=after)
        listed.update((source.url, source.ref, source.path) for source in page.data)
        if not page.has_more or page.last_id is None:
            return listed
        after = page.last_id
