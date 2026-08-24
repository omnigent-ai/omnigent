"""Unit coverage for stable multi-directory session scopes."""

from __future__ import annotations

import pytest

from omnigent.session_directories import (
    DEFAULT_DIRECTORY_ID,
    MAX_SESSION_DIRECTORIES,
    SessionDirectory,
    build_session_directories,
    decode_session_directories,
    encode_session_directories,
    replace_default_directory,
    replace_directory_nickname,
    select_session_directories,
    validate_session_directories,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)


def _directory(index: int, path: str | None = None) -> SessionDirectory:
    """Build one deterministic additional directory for a test."""
    return SessionDirectory(f"dir_{index:032x}", path or f"/repo/{index}")


def test_encode_decode_round_trip_and_legacy_workspace_fallback() -> None:
    """Stored ids round-trip while old workspace-only rows gain ``default``."""
    values = (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/main"),
        SessionDirectory(f"dir_{1:032x}", "/repo/1", nickname="Shared API"),
    )

    encoded = encode_session_directories(values)

    assert encoded is not None
    assert decode_session_directories(encoded, workspace="/repo/main") == values
    assert decode_session_directories(None, workspace="/repo/legacy") == (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/legacy"),
    )
    assert decode_session_directories(None, workspace="   ") == ()


def test_directory_nicknames_override_display_names_and_can_be_cleared() -> None:
    """Nicknames are optional metadata; clearing restores stable defaults."""
    values = (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/main"),
        _directory(1, "/repo/shared"),
    )

    assert values[0].environment_name == "Working folder"
    assert values[1].environment_name == "shared"

    renamed = replace_directory_nickname(values, values[1].id, "Shared services")
    assert renamed[1].nickname == "Shared services"
    assert renamed[1].name == "Shared services"
    assert renamed[1].environment_name == "Shared services"

    cleared = replace_directory_nickname(renamed, values[1].id, None)
    assert cleared[1].nickname is None
    assert cleared[1].environment_name == "shared"

    with pytest.raises(ValueError, match="not attached"):
        replace_directory_nickname(values, f"dir_{99:032x}", "Missing")

    with pytest.raises(ValueError, match="must not be blank"):
        replace_directory_nickname(values, values[1].id, "   ")


def test_build_uses_requested_alias_basename_for_canonicalized_directory() -> None:
    """A symlinked input keeps its picked basename as the default title."""
    directories = build_session_directories(
        "/repo/main",
        ["/mnt/repos/shared-services"],
        requested_additional_paths=["/repo/shared"],
    )

    assert directories[1].path == "/mnt/repos/shared-services"
    assert directories[1].nickname == "shared"
    assert directories[1].environment_name == "shared"


def test_child_scope_inherits_all_or_an_explicit_subset_in_parent_order() -> None:
    """Omitted, empty, and subset scopes have distinct stable semantics."""
    values = (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/main"),
        _directory(1),
        _directory(2),
    )

    assert select_session_directories(values, None) == values
    assert select_session_directories(values, []) == ()
    assert select_session_directories(values, [values[2].id, values[0].id]) == (
        values[0],
        values[2],
    )
    with pytest.raises(ValueError, match="outside the parent scope"):
        select_session_directories(values[1:], [DEFAULT_DIRECTORY_ID])


def test_directory_set_rejects_duplicate_paths_and_more_than_sixteen_roots() -> None:
    """Canonical path uniqueness and the root-count bound are enforced."""
    with pytest.raises(ValueError, match="paths must be unique"):
        validate_session_directories((_directory(1, "/same"), _directory(2, "/same")))

    too_many = tuple(_directory(index) for index in range(MAX_SESSION_DIRECTORIES + 1))
    with pytest.raises(ValueError, match="at most 16"):
        validate_session_directories(too_many)


def test_replacing_default_workspace_preserves_additional_ids() -> None:
    """A managed/fork host bind changes only the primary root path."""
    extra = _directory(1)
    assert replace_default_directory((extra,), "/new/main") == (
        SessionDirectory(DEFAULT_DIRECTORY_ID, "/new/main"),
        extra,
    )


def test_store_round_trips_stable_directories_and_host_rebinds(db_uri: str) -> None:
    """SQL metadata retains ids and keeps ``workspace`` consistent."""
    store = SqlAlchemyConversationStore(db_uri)
    directories = build_session_directories("/repo/main", ["/repo/shared"])
    created = store.create_conversation(
        workspace="/repo/main",
        directories=directories,
    )

    fetched = store.get_conversation(created.id)
    assert fetched is not None
    assert fetched.directories == directories

    renamed = store.set_directory_nickname(created.id, directories[1].id, "Shared services")
    assert renamed.directories[1].nickname == "Shared services"
    fetched_after_rename = store.get_conversation(created.id)
    assert fetched_after_rename is not None
    assert fetched_after_rename.directories[1].nickname == "Shared services"

    rebound = store.set_host_id(created.id, "1" * 32, workspace="/repo/rebound")
    assert rebound.workspace == "/repo/rebound"
    assert rebound.directories[0] == SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/rebound")
    assert rebound.directories[1].nickname == "Shared services"

    cleared = store.clear_host_binding(created.id)
    assert cleared.workspace is None
    assert cleared.directories == ()


def test_store_rejects_mismatched_default_directory_and_workspace(db_uri: str) -> None:
    """Invalid metadata never lands as a row that fails during hydration."""
    store = SqlAlchemyConversationStore(db_uri)

    with pytest.raises(ValueError, match="must match workspace"):
        store.create_conversation(
            workspace="/repo/main",
            directories=(SessionDirectory(DEFAULT_DIRECTORY_ID, "/repo/other"),),
        )
