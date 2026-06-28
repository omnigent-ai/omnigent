"""EntityGroup domain object.

An *EntityGroup* organizes :class:`~omnigent.entities.entity.Entity` building
blocks into named, icon-bearing categories shown in the flow builder's step
picker (e.g. "Jira", "GitHub"). Built-in groups (Jira/GitHub and their actions)
are code-owned and read-only — see :mod:`omnigent.entities.builtins`; users can
also create their own groups and upload a custom icon.

A group's icon is either a bundled inline-SVG component (built-ins, referenced
by ``icon_key`` such as ``"jira"``) or an uploaded image (user groups, stored in
the artifact store under ``icon_artifact_key`` and served by a route). The two
are mutually exclusive by construction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class EntityGroup:
    """
    A named, icon-bearing category for entities.

    :param id: Unique group identifier, e.g. ``"grp_0f1a2b3c..."``. Built-in
        groups use reserved ids like ``"grp_builtin_jira"``.
    :param created_at: Unix epoch seconds of creation.
    :param updated_at: Unix epoch seconds of the last update.
    :param name: Human-readable group name shown in the picker.
    :param icon_key: Key of a bundled icon component (built-ins only), e.g.
        ``"jira"`` / ``"github"``; ``None`` for user groups.
    :param icon_artifact_key: Artifact-store key of an uploaded icon image
        (user groups only); ``None`` for built-ins.
    :param icon_content_type: MIME type of the uploaded icon, e.g.
        ``"image/png"``; ``None`` when there is no uploaded icon.
    :param created_by: Owning user id, or ``None`` in single-user mode / for
        built-ins.
    """

    id: str
    created_at: int
    updated_at: int
    name: str
    icon_key: str | None = None
    icon_artifact_key: str | None = None
    icon_content_type: str | None = None
    created_by: str | None = None
