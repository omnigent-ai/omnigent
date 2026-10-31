"""Projects are marked by an emoji (``config["icon"]``) or a web theme palette id
(``config["color"]``); a project created with neither gets the least-used color."""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from omnigent.stores.project_store import ProjectStore
from omnigent.theme_palettes_generated import THEME_PALETTE_IDS


def least_used_project_color(used_colors: Iterable[object]) -> str:
    # min() keeps the first of equal counts, so ties go to palette order.
    counts = Counter(used_colors)
    return min(THEME_PALETTE_IDS, key=lambda color: counts[color])


def with_project_color(
    project_store: ProjectStore, user_id: str | None, config: dict[str, Any]
) -> dict[str, Any]:
    if config.get("icon") or config.get("color") in THEME_PALETTE_IDS:
        return dict(config)
    projects = project_store.list(user_id=user_id)
    color = least_used_project_color(project.config.get("color") for project in projects)
    return {**config, "color": color}
