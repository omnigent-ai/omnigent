"""Entity domain object.

An *Entity* is a reusable building block authored in the web UI — a named
instruction that can be wired into a flow (job) as a step. Examples: the Jira
actions ("Fetch ticket body & metadata", "Post an update to the ticket"). When
a flow uses an entity as a step, the entity's ``instruction`` text is folded
into the flow's rendered narrative.

The backend stores entities verbatim; it never interprets the instruction.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Entity:
    """
    A reusable, named instruction wired into flows as a step.

    :param id: Unique entity identifier, e.g. ``"ent_0f1a2b3c..."``.
    :param created_at: Unix epoch seconds of creation.
    :param updated_at: Unix epoch seconds of the last update.
    :param title: Human-readable title shown on the flow step.
    :param instruction: The instruction text folded into a flow's narrative
        when this entity is used as a step.
    :param created_by: Owning user id, or ``None`` in single-user mode.
    :param group_id: The :class:`~omnigent.entities.entity_group.EntityGroup`
        this entity belongs to, or ``None`` if ungrouped. Built-in entities
        reference a built-in group id (e.g. ``"grp_builtin_jira"``).
    """

    id: str
    created_at: int
    updated_at: int
    title: str
    instruction: str
    created_by: str | None = None
    group_id: str | None = None
