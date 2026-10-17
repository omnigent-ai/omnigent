"""Bound metadata writes without activating session ownership behavior."""

from pathlib import Path

import pytest
from sqlalchemy import func, select, update
from sqlalchemy.exc import StatementError

from omnigent.db.compression import encode
from omnigent.db.db_models import SqlConversationMetadata, current_workspace_id
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore


@pytest.fixture(scope="module")
def stored_byte_boundaries() -> tuple[str, str, str]:
    import random
    import string

    source = "".join(
        random.Random(417).choices(
            string.ascii_letters + string.digits + "!#$%&()*+,-./:;<=>?@[]^_{|}~", k=25000
        )
    )
    fitting, oversized = source[:19904], source[:19905]
    fitting_frame, oversized_frame = encode(fitting), encode(oversized)
    assert fitting_frame is not None and len(fitting_frame) == 16_383
    assert oversized_frame is not None and len(oversized_frame) == 16_384
    return fitting, oversized, source


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """
    :returns: A SqlAlchemyConversationStore backed by the test database.
    """
    return SqlAlchemyConversationStore(db_uri)


@pytest.fixture()
def split_db_conversation_store(tmp_path: Path) -> SqlAlchemyConversationStore:
    """
    :returns: A SqlAlchemyConversationStore with two separate SQLite databases
        (Omnigent DB + AP/conversations DB) to exercise split-DB routing.
    """
    omnigent_uri = f"sqlite:///{tmp_path}/omnigent.db"
    conv_uri = f"sqlite:///{tmp_path}/conversations.db"
    return SqlAlchemyConversationStore(omnigent_uri, conv_uri)


@pytest.fixture(params=["conversation_store", "split_db_conversation_store"])
def store(request: pytest.FixtureRequest) -> SqlAlchemyConversationStore:
    return request.getfixturevalue(request.param)


@pytest.mark.parametrize(
    "field",
    [
        "live_status_response_id",
        "live_status_error_labels",
        "runner_offline_decision",
        "terminal_startup_claim",
        "live_status_observation_id",
        "live_status_error_key",
    ],
)
@pytest.mark.parametrize("write_path", ["orm", "core"])
def test_observation_columns_bound_final_stored_bytes(
    store: SqlAlchemyConversationStore,
    stored_byte_boundaries: tuple[str, str, str],
    field: str,
    write_path: str,
) -> None:
    fitting, oversized, _ = stored_byte_boundaries
    is_identifier = field in {"live_status_observation_id", "live_status_error_key"}
    if is_identifier:
        fitting, oversized = "🎉" * 64, "🎉" * 65
    conv = store.create_conversation(runner_id="runner-a")
    key = (current_workspace_id(), conv.id)
    column = getattr(SqlConversationMetadata, field)

    def write(value: str) -> None:
        with store._session_immediate("test_observation_payload_bound") as session:
            if write_path == "orm":
                meta = session.get(SqlConversationMetadata, key)
                assert meta is not None
                setattr(meta, field, value)
                meta.live_status_revision += 1
            else:
                session.execute(
                    update(SqlConversationMetadata)
                    .where(
                        SqlConversationMetadata.workspace_id == key[0],
                        SqlConversationMetadata.id == key[1],
                    )
                    .values(
                        {
                            field: value,
                            "live_status_revision": SqlConversationMetadata.live_status_revision
                            + 1,
                        }
                    )
                )

    write(fitting)
    with store._session("test_read_stored_observation_payload") as session:
        meta = session.get(SqlConversationMetadata, key)
        assert meta is not None and getattr(meta, field) == fitting
        revision = meta.live_status_revision
        assert session.scalar(
            select(func.length(column)).where(
                SqlConversationMetadata.workspace_id == key[0],
                SqlConversationMetadata.id == key[1],
            )
        ) == (64 if is_identifier else 16_383)
    with pytest.raises(StatementError, match=r"stored bytes|64 characters"):
        write(oversized)
    with store._session("test_read_retained_observation_payload") as session:
        meta = session.get(SqlConversationMetadata, key)
        assert meta is not None and getattr(meta, field) == fitting
        assert meta.live_status_revision == revision
