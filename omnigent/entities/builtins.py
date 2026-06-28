"""Code-owned, read-only built-in entity groups and entities.

The flow builder's step picker shows built-in integration groups (Jira, GitHub)
and their actions in addition to whatever the user has created. These built-ins
live in code rather than the database: they always exist, are identical for every
user, and cannot be edited or deleted. The route layer merges them ahead of the
per-user DB rows when listing groups/entities.

Built-in ids use a reserved ``grp_builtin_`` / ``ent_builtin_`` prefix. The
``builtin`` infix contains non-hex characters, so these ids can never collide
with the ``grp_<hex>`` / ``ent_<hex>`` ids minted by
:func:`~omnigent.db.utils.generate_entity_group_id` /
:func:`~omnigent.db.utils.generate_entity_id`.

The reserved ids are a stable contract: a saved flow step references an entity by
id, so renaming or re-keying a built-in would orphan existing steps. Treat them
as append-only.
"""

from __future__ import annotations

from omnigent.entities.entity import Entity
from omnigent.entities.entity_group import EntityGroup

_BUILTIN_GROUP_PREFIX = "grp_builtin_"
_BUILTIN_ENTITY_PREFIX = "ent_builtin_"

# Built-in groups, in the order they should appear in the picker (before any
# user-created groups). icon_key maps to a bundled icon component on the client.
BUILTIN_GROUPS: tuple[EntityGroup, ...] = (
    EntityGroup(
        id="grp_builtin_jira",
        created_at=0,
        updated_at=0,
        name="Jira",
        icon_key="jira",
    ),
    EntityGroup(
        id="grp_builtin_github",
        created_at=0,
        updated_at=0,
        name="GitHub",
        icon_key="github",
    ),
)

# Built-in entities (actions), grouped via ``group_id``. Order is preserved
# within each group in the picker.
BUILTIN_ENTITIES: tuple[Entity, ...] = (
    Entity(
        id="ent_builtin_jira_get_ticket",
        created_at=0,
        updated_at=0,
        title="Get ticket information",
        instruction="Fetch the Jira ticket's description, status, assignee, and fields.",
        group_id="grp_builtin_jira",
    ),
    Entity(
        id="ent_builtin_jira_post_comment",
        created_at=0,
        updated_at=0,
        title="Post comment",
        instruction="Post a comment or update to the Jira ticket.",
        group_id="grp_builtin_jira",
    ),
    Entity(
        id="ent_builtin_jira_close_ticket",
        created_at=0,
        updated_at=0,
        title="Close ticket",
        instruction="Transition the Jira ticket to a closed/done state.",
        group_id="grp_builtin_jira",
    ),
    Entity(
        id="ent_builtin_github_open_pr",
        created_at=0,
        updated_at=0,
        title="Open PR",
        instruction="Open a GitHub pull request for the changes.",
        group_id="grp_builtin_github",
    ),
)

_BUILTIN_GROUPS_BY_ID = {g.id: g for g in BUILTIN_GROUPS}
_BUILTIN_ENTITIES_BY_ID = {e.id: e for e in BUILTIN_ENTITIES}


def is_builtin_group_id(group_id: str) -> bool:
    """Whether ``group_id`` names a code-owned built-in group."""
    return group_id.startswith(_BUILTIN_GROUP_PREFIX)


def is_builtin_entity_id(entity_id: str) -> bool:
    """Whether ``entity_id`` names a code-owned built-in entity."""
    return entity_id.startswith(_BUILTIN_ENTITY_PREFIX)


def builtin_groups() -> list[EntityGroup]:
    """Return all built-in groups, in display order."""
    return list(BUILTIN_GROUPS)


def builtin_entities() -> list[Entity]:
    """Return all built-in entities, in display order."""
    return list(BUILTIN_ENTITIES)


def get_builtin_group(group_id: str) -> EntityGroup | None:
    """Return the built-in group with this id, or ``None``."""
    return _BUILTIN_GROUPS_BY_ID.get(group_id)


def get_builtin_entity(entity_id: str) -> Entity | None:
    """Return the built-in entity with this id, or ``None``."""
    return _BUILTIN_ENTITIES_BY_ID.get(entity_id)
