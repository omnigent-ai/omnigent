import json
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from sqlalchemy import update

from omnigent.native.session_todos import validate_session_todos
from omnigent.stores.conversation_store import sqlalchemy_store as store_module

TODOS = [{"content": "Verify the example", "status": "in_progress", "activeForm": "Verifying"}]


def test_plan_validation_is_canonical_and_bounded():
    assert validate_session_todos([{**TODOS[0], "unknown": "discard"}]) == TODOS
    assert validate_session_todos([{**TODOS[0], "status": []}]) == []
    invalid = (
        TODOS * 101,
        [{**TODOS[0], "content": "x" * 4097}],
        [{**TODOS[0], "content": "字" * 4096, "activeForm": "字" * 4096}] * 11,
    )
    for value in invalid:
        with pytest.raises(ValueError):
            validate_session_todos(value)


def test_sqlite_writers_preserve_policy_and_plan(tmp_path, monkeypatch):
    store = store_module.SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'concurrent.db'}")
    conv = store.create_conversation(title="Concurrent Plan fixture")
    entered = [threading.Event(), threading.Event()]
    release, decode = threading.Event(), store_module._decode_session_state

    def coordinated_decode(value: str | None) -> tuple[dict[str, object], list[dict[str, object]]]:
        state, todos = decode(value)
        writer = int(threading.current_thread().name.rsplit("_", 1)[1])
        entered[writer].set()
        assert writer != 0 or release.wait(10)
        return state, todos

    monkeypatch.setattr(store_module, "_decode_session_state", coordinated_decode)

    with ThreadPoolExecutor(max_workers=2, thread_name_prefix="writer") as executor:
        policy_write = executor.submit(store.set_session_state, conv.id, {"policy_counter": 1})
        assert entered[0].wait(10)
        plan_write = executor.submit(store.set_session_todos, conv.id, TODOS)
        blocked = not entered[1].wait(0.5)
        release.set()
        assert blocked
        assert policy_write.result(timeout=10) is None and plan_write.result(timeout=10)

    monkeypatch.setattr(store_module, "_decode_session_state", decode)
    reloaded = store.get_conversation(conv.id)
    assert reloaded and reloaded.session_state == {"policy_counter": 1}
    assert reloaded.session_todos == TODOS


def test_legacy_session_state_rows_read_and_upgrade_cleanly(tmp_path):
    """Rows persisted before Plan storage existed keep their policy state."""
    store = store_module.SqlAlchemyConversationStore(f"sqlite:///{tmp_path / 'legacy.db'}")
    conv = store.create_conversation(title="Legacy fixture")
    # A pre-upgrade server wrote the policy dict alone, with json.dumps defaults.
    legacy_state = {"policy_counter": 7, "flags": {"beta": True}}
    with store._session("test_seed_legacy_session_state") as session:
        session.execute(
            update(store_module.SqlConversationMetadata)
            .where(store_module.SqlConversationMetadata.id == conv.id)
            .values(session_state=json.dumps(legacy_state))
        )

    loaded = store.get_conversation(conv.id)
    assert loaded and loaded.session_state == legacy_state
    assert loaded.session_todos == []

    # The first new-format Plan write must not disturb legacy policy keys.
    assert store.set_session_todos(conv.id, TODOS)
    upgraded = store.get_conversation(conv.id)
    assert upgraded and upgraded.session_state == legacy_state
    assert upgraded.session_todos == TODOS

    # Clearing the Plan leaves the row policy-only again, as before the upgrade.
    assert store.set_session_todos(conv.id, [])
    cleared = store.get_conversation(conv.id)
    assert cleared and cleared.session_state == legacy_state
    assert cleared.session_todos == []
