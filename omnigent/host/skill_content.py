"""Read the displayed skill's body using the same home scope as the inventory."""

from dataclasses import replace
from pathlib import Path

from omnigent.host.plugins import MAX_PLUGIN_NAME, _plugin_skills, _text
from omnigent.spec.skill_sources import (
    _plugin_install_paths,
    resolve_harness_skills,
    skill_source_context_from_env,
)

MAX_SKILL_CONTENT_BYTES = 256 * 1024


def read_skill_content(
    harness: str, name: str, source_id: str | None = None
) -> dict[str, str | bool]:
    ctx = skill_source_context_from_env(roots=(Path.home(),), harness=harness)
    if source_id is None:
        skills = resolve_harness_skills(ctx, harness)
    else:
        if harness != "claude-native":
            raise LookupError("plugin skill unavailable")
        # Installed files remain readable when their plugin or slash command is disabled.
        skills = [
            replace(
                skill,
                name=(
                    f"{_text(key.partition('@')[0], MAX_PLUGIN_NAME)}:"
                    f"{_text(skill.name, MAX_PLUGIN_NAME)}"
                ),
            )
            for key, directory in _plugin_install_paths(
                replace(ctx, roots=(), is_native=True)
            ).items()
            for asset_id, skill in _plugin_skills(key, directory)
            if asset_id == source_id
        ]
    for skill in skills:
        if skill.name == name:
            content = skill.content.encode("utf-8")
            return {
                "name": skill.name,
                "description": skill.description[:8192],
                "content": content[:MAX_SKILL_CONTENT_BYTES].decode("utf-8", errors="ignore"),
                "truncated": len(content) > MAX_SKILL_CONTENT_BYTES,
            }
    raise LookupError("skill unavailable")
