"""Tests for SqlAlchemyEntityGroupStore + entity group_id wiring."""

from __future__ import annotations

from omnigent.stores.entity_group_store.sqlalchemy_store import SqlAlchemyEntityGroupStore
from omnigent.stores.entity_store.sqlalchemy_store import SqlAlchemyEntityStore


def test_create_and_get_group(entity_group_store: SqlAlchemyEntityGroupStore) -> None:
    """A created group is fetchable; timestamps stamped equal."""
    g = entity_group_store.create_group(name="Deploy", created_by="u1")
    assert g.id.startswith("grp_")
    assert g.created_at == g.updated_at
    fetched = entity_group_store.get_group(g.id)
    assert fetched is not None and fetched.name == "Deploy"


def test_list_groups_scoped_by_owner(entity_group_store: SqlAlchemyEntityGroupStore) -> None:
    """``created_by`` filters the listing; ``None`` returns all."""
    entity_group_store.create_group(name="A", created_by="u1")
    entity_group_store.create_group(name="B", created_by="u2")
    assert [g.name for g in entity_group_store.list_groups(created_by="u1")] == ["A"]
    assert len(entity_group_store.list_groups()) == 2


def test_update_group_icon_fields(entity_group_store: SqlAlchemyEntityGroupStore) -> None:
    """Updating icon artifact key + content type round-trips."""
    g = entity_group_store.create_group(name="G")
    updated = entity_group_store.update_group(
        g.id, icon_artifact_key="entity-group-icons/" + g.id, icon_content_type="image/png"
    )
    assert updated is not None
    assert updated.icon_artifact_key == "entity-group-icons/" + g.id
    assert updated.icon_content_type == "image/png"


def test_delete_group_ungroups_member_entities(
    entity_group_store: SqlAlchemyEntityGroupStore,
    entity_store: SqlAlchemyEntityStore,
) -> None:
    """Deleting a group nulls the group_id of entities that referenced it."""
    g = entity_group_store.create_group(name="G", created_by="u1")
    e = entity_store.create_entity(title="E", instruction="i", created_by="u1", group_id=g.id)
    assert entity_store.get_entity(e.id).group_id == g.id

    assert entity_group_store.delete_group(g.id) is True
    assert entity_group_store.get_group(g.id) is None
    # The entity survives but is now ungrouped.
    survivor = entity_store.get_entity(e.id)
    assert survivor is not None and survivor.group_id is None


def test_entity_group_id_round_trips_and_clears(entity_store: SqlAlchemyEntityStore) -> None:
    """Entity group_id persists on create, patches, and clears on empty string."""
    e = entity_store.create_entity(title="E", instruction="i", group_id="grp_x")
    assert e.group_id == "grp_x"
    moved = entity_store.update_entity(e.id, group_id="grp_y")
    assert moved is not None and moved.group_id == "grp_y"
    # None leaves it unchanged.
    same = entity_store.update_entity(e.id, title="renamed")
    assert same is not None and same.group_id == "grp_y"
    # Empty string clears it.
    cleared = entity_store.update_entity(e.id, group_id="")
    assert cleared is not None and cleared.group_id is None
