"""Harness-neutral skill catalog and trust-setting routes."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel

from omnigent.server.skill_settings import read_skill_trust, write_skill_trust
from omnigent.spec.skill_registry import SkillRegistry
from omnigent.spec.skill_sources import SkillSourceContext, resolve_all_candidates


class SkillTrustRequest(BaseModel):
    """Body for updating the default discovery trust boundary."""

    value: Literal["current", "all-host"]


def _catalog(include_other_tools: bool) -> SkillRegistry:
    root = Path.cwd().resolve()
    context = SkillSourceContext(
        roots=(root,),
        home=Path.home(),
        skills_filter="all",
        bundle_dir=None,
    )
    return SkillRegistry.from_candidates(
        resolve_all_candidates(context),
        active_provider="claude",
        skill_trust="all-host" if include_other_tools else "current",
    )


def _summary(entry: Any) -> dict[str, Any]:
    candidate = entry.winner
    return {
        "id": entry.canonical_id,
        "name": candidate.invocation_name,
        "description": candidate.skill.description,
        "origin": "built_in" if candidate.location_scope == "bundle" else candidate.location_scope,
        "enabled": True,
        "available": True,
        "has_conflict": bool(entry.shadowed),
        "updated_at": None,
    }


def create_skills_router() -> APIRouter:
    """Build the skill registry API router."""
    router = APIRouter()

    @router.get("/skills")
    async def list_skills(
        include_other_tools: bool | None = Query(default=None),
    ) -> dict[str, Any]:
        configured = read_skill_trust() == "all-host"
        effective = configured if include_other_tools is None else include_other_tools
        visible = _catalog(effective).list()
        hidden_count = 0
        if not effective:
            hidden_count = max(0, len(_catalog(True).list()) - len(visible))
        return {
            "object": "list",
            "data": [_summary(entry) for entry in visible],
            "include_other_tools": effective,
            "hidden_count": hidden_count,
        }

    @router.get("/skills/trust")
    async def get_skill_trust() -> dict[str, Any]:
        value = read_skill_trust()
        return {"value": value, "include_other_tools": value == "all-host"}

    @router.put("/skills/trust")
    async def set_skill_trust(body: SkillTrustRequest) -> dict[str, Any]:
        write_skill_trust(body.value)
        return {"value": body.value, "include_other_tools": body.value == "all-host"}

    @router.get("/skills/{skill_id}")
    async def get_skill(skill_id: str) -> dict[str, Any]:
        registry = _catalog(read_skill_trust() == "all-host")
        entry = registry.get_entry(skill_id)
        if entry is None:
            from fastapi import HTTPException

            raise HTTPException(status_code=404, detail="Skill not found")
        candidate = entry.winner
        result = _summary(entry)
        result.update(
            {
                "content": candidate.skill.content,
                "provenance": {
                    "provider": candidate.provider,
                    "original_path": str(candidate.origin_path),
                    "source_kind": candidate.source_kind,
                    "source_coords": candidate.source_coords,
                    "digest": candidate.tree_digest,
                },
                "selected_winner": candidate.source_coords,
                "conflict_candidates": [item.source_coords for item in entry.shadowed],
                "delivery": {"mode": "automatic"},
            }
        )
        return result

    return router
