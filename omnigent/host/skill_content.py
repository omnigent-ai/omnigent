"""Read the displayed skill's body using the same home scope as the inventory."""

from pathlib import Path

from omnigent.spec.skill_sources import resolve_harness_skills, skill_source_context_from_env

MAX_SKILL_CONTENT_BYTES = 256 * 1024


def read_skill_content(harness: str, name: str) -> dict[str, str | bool]:
    ctx = skill_source_context_from_env(roots=(Path.home(),), harness=harness)
    for skill in resolve_harness_skills(ctx, harness):
        if skill.name == name:
            content = skill.content.encode("utf-8")
            return {
                "name": skill.name,
                "description": skill.description[:8192],
                "content": content[:MAX_SKILL_CONTENT_BYTES].decode("utf-8", errors="ignore"),
                "truncated": len(content) > MAX_SKILL_CONTENT_BYTES,
            }
    raise LookupError("skill unavailable")
