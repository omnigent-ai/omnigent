"""Harness-neutral skill catalog and trust-setting routes."""

from __future__ import annotations

import asyncio
import logging
import urllib.parse
from pathlib import Path
from typing import TYPE_CHECKING, Any, Literal, cast

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.server.auth import LEVEL_READ, AuthProvider
from omnigent.server.routes._auth_helpers import get_user_id, require_access
from omnigent.server.skill_settings import read_skill_trust, write_skill_trust
from omnigent.stores import ConversationStore
from omnigent.stores.permission_store import PermissionStore

if TYPE_CHECKING:
    from omnigent.runner.routing import RunnerRouter

_logger = logging.getLogger(__name__)

# How many registered agents to enumerate for the all-agent aggregation. A
# generous bound that keeps a pathological roster from ballooning the catalog.
_MAX_AGGREGATED_AGENTS = 200


class SkillTrustRequest(BaseModel):
    """Body for updating the default discovery trust boundary."""

    value: Literal["current", "all-host"]


class _AgentBundleAggregator:
    """
    Server-owned browse aggregation of every registered agent's bundle skills.

    The bound runner is authoritative for a session's LOCAL + Omnigent + bound
    agent catalog. This aggregator adds the OTHER registered agents' bundled
    skills to the browse payload (browse-only — execution/`load_skill` is
    untouched). It resolves each agent's spec + extracted bundle dir via the
    two-tier ``AgentCache`` and enumerates bundle candidates with
    ``registry_for_spec`` (bundle-only: no host/local discovery). Serialized
    entries are cached per ``bundle_location`` (which embeds the content
    revision) so a catalog build never re-serializes an unchanged bundle.
    """

    def __init__(self, agent_store: Any, agent_cache: Any) -> None:
        self._agent_store = agent_store
        self._agent_cache = agent_cache
        # bundle_location -> (summaries, {skill_id: bundle_dir}, {skill_id: detail}).
        self._cache: dict[
            str, tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]
        ] = {}

    def _agents(self) -> list[Any]:
        """The registered template agents (built-ins) to aggregate, bounded."""
        try:
            page = self._agent_store.list(limit=_MAX_AGGREGATED_AGENTS)
            return list(page.data)
        except Exception:  # noqa: BLE001 - aggregation must never break the catalog
            _logger.warning("Skill aggregation: agent_store.list failed", exc_info=True)
            return []

    def _bundle_entries(
        self, agent: Any
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
        """
        Serialized browse summaries + {skill_id: bundle_dir} + {skill_id: detail}
        for one agent's bundle skills. Cached by bundle_location; best-effort.
        """
        loc = getattr(agent, "bundle_location", None)
        if not loc:
            return [], {}, {}
        cached = self._cache.get(loc)
        if cached is not None:
            return cached
        result = self._build_bundle_entries(agent, loc)
        self._cache[loc] = result
        return result

    def _build_bundle_entries(
        self, agent: Any, loc: str
    ) -> tuple[list[dict[str, Any]], dict[str, str], dict[str, dict[str, Any]]]:
        from omnigent.spec.skill_registry import canonical_skill_id, display_path
        from omnigent.spec.skill_sources import registry_for_spec

        try:
            loaded = self._agent_cache.load(agent.id, loc)
        except Exception:  # noqa: BLE001 - a bad/absent bundle must not break others
            _logger.warning(
                "Skill aggregation: could not load bundle for agent %s", agent.id, exc_info=True
            )
            return [], {}, {}
        spec = loaded.spec
        workdir = loaded.workdir
        # Bundle-only: pass no host roots so only the spec's bundled skills are
        # enumerated (local/host discovery belongs to the bound runner).
        registry = registry_for_spec(
            spec,
            roots=(),
            home=Path("/nonexistent-aggregation-home"),
            bundle_dir=workdir,
            harness=getattr(getattr(spec, "executor", None), "harness_kind", None),
            skill_trust="all-host",
        )
        summaries: list[dict[str, Any]] = []
        dirs: dict[str, str] = {}
        details: dict[str, dict[str, Any]] = {}
        agent_name = getattr(agent, "name", None) or getattr(spec, "name", None)
        for entry in registry.list():
            cand = entry.winner
            if cand.ownership != "agent":
                # A platform (omnigent) skill injected into the bundle is already
                # surfaced by the bound runner's catalog; skip to avoid dupes.
                continue
            # Browse identity namespaced by AGENT ID so two agents with the same
            # display name (or same skill name) stay distinct browse entries.
            browse_id = f"agent:{agent.id}:{canonical_skill_id(cand)}"
            summary = {
                "id": browse_id,
                "name": cand.invocation_name,
                "description": cand.skill.description,
                "origin": "built_in",
                "ownership": "agent",
                "agent_name": agent_name,
                "agent_id": agent.id,
                "display_path": display_path(cand),
                "enabled": True,
                "available": True,
                "has_conflict": bool(entry.shadowed),
                "updated_at": None,
            }
            summaries.append(summary)
            dirs[browse_id] = str(cand.origin_path)
            details[browse_id] = {
                **summary,
                "content": cand.skill.content,
                "provenance": {
                    "provider": cand.provider,
                    "original_path": str(cand.origin_path),
                    "source_kind": cand.source_kind,
                    "source_coords": cand.source_coords,
                    "digest": cand.tree_digest,
                },
                "selected_winner": cand.source_coords,
                "conflict_candidates": [item.source_coords for item in entry.shadowed],
                "delivery": {"mode": "automatic"},
            }
        return summaries, dirs, details

    def aggregate_into(
        self, payload: dict[str, Any], bound_agent_id: str | None
    ) -> dict[str, Any]:
        """
        Merge all-agent bundle entries into a bound-runner catalog payload.

        Tags availability on every row (`invokable_in_current_session` +
        `required_agent_*`), dedupes the bound agent's bundle skills (the runner
        already lists them), and appends the other agents' bundle skills.
        """
        data = payload.get("data")
        if not isinstance(data, list):
            return payload

        agents = self._agents()
        bound_name: str | None = None
        # Tag the runner-provided rows first: everything the bound runner lists is
        # invokable in this session (its own bundle + local + omnigent).
        for row in data:
            if isinstance(row, dict):
                row.setdefault("invokable_in_current_session", True)
                row.setdefault(
                    "agent_id", bound_agent_id if row.get("ownership") == "agent" else None
                )
                row.setdefault("required_agent_id", None)
                row.setdefault("required_agent_name", None)

        # Names already present from the bound agent (dedupe target).
        for agent in agents:
            if bound_agent_id is not None and agent.id == bound_agent_id:
                bound_name = getattr(agent, "name", None)

        merged = list(data)
        for agent in agents:
            if bound_agent_id is not None and agent.id == bound_agent_id:
                # The bound agent's bundle is already in the runner payload; just
                # stamp its rows with the resolved agent id/name + invokable.
                for row in merged:
                    if isinstance(row, dict) and row.get("ownership") == "agent":
                        row["agent_id"] = agent.id
                        row.setdefault("agent_name", getattr(agent, "name", None))
                continue
            summaries, _dirs, _details = self._bundle_entries(agent)
            for s in summaries:
                s = dict(s)
                s["invokable_in_current_session"] = False
                s["required_agent_id"] = agent.id
                s["required_agent_name"] = getattr(agent, "name", None)
                merged.append(s)

        _ = bound_name  # reserved for future heading use; roster resolved above.
        payload["data"] = merged
        return payload

    def agent_for(self, skill_id: str) -> Any | None:
        """Resolve the owning agent of an aggregated ``agent:<id>:…`` browse id."""
        if not skill_id.startswith("agent:") or self._agent_store is None:
            return None
        parts = skill_id.split(":", 2)  # agent:<agent_id>:<canonical_skill_id>
        if len(parts) != 3:
            return None
        return self._agent_store.get(parts[1])

    def bundle_dir_for(self, skill_id: str) -> str | None:
        """Resolve a non-bound aggregated browse id to its bundle skill dir."""
        agent = self.agent_for(skill_id)
        if agent is None:
            return None
        _summaries, dirs, _details = self._bundle_entries(agent)
        return dirs.get(skill_id)

    def detail_for(
        self, skill_id: str, *, required_agent_id: str, agent_name: str | None
    ) -> dict[str, Any] | None:
        """Full detail dict for a non-bound aggregated skill (browse-only)."""
        agent = self.agent_for(skill_id)
        if agent is None:
            return None
        _summaries, _dirs, details = self._bundle_entries(agent)
        detail = details.get(skill_id)
        if detail is None:
            return None
        detail = dict(detail)
        detail["invokable_in_current_session"] = False
        detail["required_agent_id"] = required_agent_id
        detail["required_agent_name"] = agent_name
        return detail


def create_skills_router(
    conversation_store: ConversationStore,
    runner_router: RunnerRouter | None = None,
    auth_provider: AuthProvider | None = None,
    permission_store: PermissionStore | None = None,
    agent_store: Any | None = None,
    agent_cache: Any | None = None,
) -> APIRouter:
    """Build the skill registry API router.

    :param agent_store: Roster of registered agents (with ``bundle_location``);
        drives the server-owned all-agent browse aggregation. ``None`` disables
        aggregation (only the bound-runner catalog is returned).
    :param agent_cache: Two-tier agent cache used to resolve each agent's spec +
        extracted bundle dir server-side (for bundle skill enumeration + safe
        non-bound bundle file browsing).
    """
    router = APIRouter()
    # Server-owned browse aggregation of every registered agent's bundle skills.
    # Bounded cache keyed by bundle_location (content revision) so catalog builds
    # don't re-serialize a bundle on every request.
    aggregator = (
        _AgentBundleAggregator(agent_store, agent_cache)
        if agent_store is not None and agent_cache is not None
        else None
    )

    async def _runner_payload(
        request: Request,
        session_id: str,
        path: str,
        *,
        include_other_tools: bool | None,
        extra_params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Authorize and proxy one catalog request to the bound runner."""
        user_id = get_user_id(request, auth_provider)
        await require_access(
            user_id,
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        if runner_router is not None:
            routed = runner_router.client_for_session_resources(session_id)
            runner_client = routed.client
        else:
            from omnigent.runtime import get_runner_client

            runner_client = cast("httpx.AsyncClient | None", get_runner_client())
        if runner_client is None:
            raise OmnigentError(
                f"No runner is available for session {session_id!r}",
                code=ErrorCode.RUNNER_UNAVAILABLE,
            )
        merged: dict[str, str] = {} if extra_params is None else dict(extra_params)
        if include_other_tools is not None:
            merged["include_other_tools"] = str(include_other_tools).lower()
        # Preserve the "no query string" contract (params=None) when there is
        # nothing to send, so existing catalog/detail proxying is byte-identical.
        params = merged or None
        try:
            response = await runner_client.get(path, params=params, timeout=10.0)
            payload = response.json()
        except (httpx.HTTPError, ConnectionError, ValueError) as exc:
            raise OmnigentError(
                f"Runner failed to resolve skills for session {session_id!r}: {exc}",
                code=ErrorCode.INTERNAL_ERROR,
            ) from exc
        if not isinstance(payload, dict):
            raise OmnigentError(
                f"Runner returned malformed skills for session {session_id!r}",
                code=ErrorCode.INTERNAL_ERROR,
            )
        if response.status_code == 404:
            raise HTTPException(status_code=404, detail=payload.get("detail", "Skill not found"))
        # Preserve the runner's client-error status (e.g. a 400 path-traversal
        # refusal or a 413 oversized-file rejection from the file endpoints)
        # rather than masking every non-200 as a 500 — the client needs the real
        # reason to render the right non-preview / error state.
        if 400 <= response.status_code < 500:
            raise HTTPException(
                status_code=response.status_code,
                detail=payload.get("detail", "Skill request rejected"),
            )
        if response.status_code != 200:
            raise OmnigentError(
                f"Runner failed to resolve skills for session {session_id!r}: "
                f"HTTP {response.status_code}",
                code=ErrorCode.INTERNAL_ERROR,
            )
        return payload

    def _bound_agent_id(session_id: str) -> str | None:
        """The template agent bound to this session, if resolvable."""
        try:
            conv = conversation_store.get(session_id)
        except Exception:  # noqa: BLE001 - aggregation tagging is best-effort
            return None
        return getattr(conv, "agent_id", None) if conv is not None else None

    @router.get("/skills")
    async def list_skills(
        request: Request,
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        payload = await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog",
            include_other_tools=include_other_tools,
        )
        # Server-owned aggregation: fold in every OTHER registered agent's
        # bundle skills (browse-only). Runs off the event loop (bundle
        # extraction + FS walk are blocking) and never breaks the base catalog.
        if aggregator is not None:
            bound = _bound_agent_id(session_id)
            try:
                payload = await asyncio.to_thread(aggregator.aggregate_into, payload, bound)
            except Exception:  # noqa: BLE001 - degrade to the bound-runner catalog
                _logger.warning("Skill aggregation failed for %s", session_id, exc_info=True)
        return payload

    @router.get("/skills/trust")
    async def get_skill_trust() -> dict[str, Any]:
        value = read_skill_trust()
        return {"value": value, "include_other_tools": value == "all-host"}

    @router.put("/skills/trust")
    async def set_skill_trust(body: SkillTrustRequest) -> dict[str, Any]:
        write_skill_trust(body.value)
        return {"value": body.value, "include_other_tools": body.value == "all-host"}

    @router.get("/skills/{skill_id}")
    async def get_skill(
        request: Request,
        skill_id: str,
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        # A non-bound aggregated skill is served server-side (the runner has no
        # such id). Its detail is browse-only (invokable=false + required agent).
        if aggregator is not None and skill_id.startswith("agent:"):
            await require_access(
                get_user_id(request, auth_provider),
                session_id,
                LEVEL_READ,
                permission_store,
                conversation_store,
            )
            agent = aggregator.agent_for(skill_id)
            if agent is None:
                raise HTTPException(status_code=404, detail="Skill not found")
            detail = await asyncio.to_thread(
                aggregator.detail_for,
                skill_id,
                required_agent_id=agent.id,
                agent_name=getattr(agent, "name", None),
            )
            if detail is None:
                raise HTTPException(status_code=404, detail="Skill not found")
            return detail
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        quoted_skill_id = urllib.parse.quote(skill_id, safe="")
        return await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog/{quoted_skill_id}",
            include_other_tools=include_other_tools,
        )

    @router.get("/skills/{skill_id}/files")
    async def list_skill_files(
        request: Request,
        skill_id: str,
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        """List a skill's resource tree.

        An aggregated non-bound-agent skill (``agent:<id>:…``) is served
        SERVER-side from that agent's bundle dir (safe walk); everything else
        proxies to the bound runner.
        """
        served = await _aggregated_files(request, session_id, skill_id)
        if served is not None:
            return served
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        quoted_skill_id = urllib.parse.quote(skill_id, safe="")
        return await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog/{quoted_skill_id}/files",
            include_other_tools=include_other_tools,
        )

    @router.get("/skills/{skill_id}/file")
    async def read_skill_file_route(
        request: Request,
        skill_id: str,
        path: str = Query(),
        session_id: str = Query(),
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        """Read one skill file (server-side for aggregated ids, else via runner)."""
        served = await _aggregated_file(request, session_id, skill_id, path)
        if served is not None:
            return served
        quoted_session_id = urllib.parse.quote(session_id, safe="")
        quoted_skill_id = urllib.parse.quote(skill_id, safe="")
        extra = {"path": path}
        return await _runner_payload(
            request,
            session_id,
            f"/v1/sessions/{quoted_session_id}/skills/catalog/{quoted_skill_id}/file",
            include_other_tools=include_other_tools,
            extra_params=extra,
        )

    async def _aggregated_files(
        request: Request, session_id: str, skill_id: str
    ) -> dict[str, Any] | None:
        """Serve a non-bound aggregated skill's tree server-side, or None."""
        if aggregator is None or not skill_id.startswith("agent:"):
            return None
        await require_access(
            get_user_id(request, auth_provider),
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        bundle_dir = aggregator.bundle_dir_for(skill_id)
        if bundle_dir is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        from omnigent.spec.skill_files import list_skill_tree

        nodes = await asyncio.to_thread(list_skill_tree, Path(bundle_dir))
        return {
            "object": "list",
            "data": [{"path": n.path, "kind": n.kind, "size": n.size} for n in nodes],
        }

    async def _aggregated_file(
        request: Request, session_id: str, skill_id: str, path: str
    ) -> dict[str, Any] | None:
        """Read one file from a non-bound aggregated skill's bundle, or None."""
        if aggregator is None or not skill_id.startswith("agent:"):
            return None
        await require_access(
            get_user_id(request, auth_provider),
            session_id,
            LEVEL_READ,
            permission_store,
            conversation_store,
        )
        bundle_dir = aggregator.bundle_dir_for(skill_id)
        if bundle_dir is None:
            raise HTTPException(status_code=404, detail="Skill not found")
        from omnigent.spec.skill_files import SkillFileError, read_skill_file

        try:
            content = await asyncio.to_thread(read_skill_file, Path(bundle_dir), path)
        except SkillFileError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.message) from exc
        return {
            "path": content.path,
            "size": content.size,
            "is_text": content.is_text,
            "too_large": content.too_large,
            "text": content.text,
        }

    return router
