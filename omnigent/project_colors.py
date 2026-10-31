"""Project colors: each project is marked by an emoji or a theme palette color.

``config["icon"]`` holds an emoji and ``config["color"]`` a palette id from
the web UI's themes. A project created with neither gets the palette color
its owner's projects use least.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from typing import Any

from omnigent.stores.project_store import ProjectStore
from omnigent.theme_palettes_generated import THEME_PALETTE_IDS


def least_used_project_color(used_colors: Iterable[object]) -> str:
    """Return the palette id used least, breaking ties in palette order.

    :param used_colors: Stored ``config["color"]`` values; non-palette values are ignored.
    :returns: A palette id from :data:`THEME_PALETTE_IDS`.
    """
    counts = Counter(used_colors)
    return min(THEME_PALETTE_IDS, key=lambda color: counts[color])


def recommended_project_color(project_store: ProjectStore, user_id: str | None) -> str:
    """Return the color a new project of ``user_id`` would be assigned.

    :param project_store: Store holding the owner's projects.
    :param user_id: The owner, or ``None`` in single-user mode.
    :returns: A palette id.
    """
    projects = project_store.list(user_id=user_id)
    return least_used_project_color(project.config.get("color") for project in projects)


def with_project_color(
    project_store: ProjectStore, user_id: str | None, config: dict[str, Any]
) -> dict[str, Any]:
    """Return ``config``, adding the recommended color when it has no emoji or color.

    :param project_store: Store holding the owner's projects.
    :param user_id: The owner, or ``None`` in single-user mode.
    :param config: The new project's requested config.
    :returns: A copy of ``config``.
    """
    if config.get("icon") or config.get("color") in THEME_PALETTE_IDS:
        return dict(config)
    return {**config, "color": recommended_project_color(project_store, user_id)}
