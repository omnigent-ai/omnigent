"""Exercise the maintenance entry point's bounded progress and store selection."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

from omnigent.db.db_models import SqlConversation, workspace_scope
from omnigent.entities import MessageData, NewConversationItem
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "reconcile_message_watermarks.py"
_SPEC = importlib.util.spec_from_file_location("_reconcile_watermarks_under_test", _SCRIPT)
assert _SPEC is not None and _SPEC.loader is not None
reconcile = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reconcile)


def _message(*, hidden: bool = False) -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id="hidden" if hidden else "visible",
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "maintenance test"}],
            is_meta=hidden,
        ),
    )


def test_cli_resumes_partial_pages_without_skipping_conversations(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation_ids = ["01" * 16, "02" * 16]
    with workspace_scope(17):
        for conversation_id in conversation_ids:
            store.create_conversation(conversation_id=conversation_id)
            store.append(conversation_id, [_message(), _message(hidden=True)])
            with store._session("test_setup") as session:
                row = session.get(SqlConversation, (17, conversation_id))
                assert row is not None
                row.last_message_at = None
                row.last_message_observed_position = None

        cursor: list[int | str] | None = None
        results = []
        for page in range(5):
            command = [
                sys.executable,
                str(_SCRIPT),
                "--storage-location",
                db_uri,
                "--workspace-id",
                "17",
                "--conversation-batch-limit",
                "1",
                "--item-batch-limit",
                "1",
                "--max-pages",
                "1",
            ]
            if cursor is not None:
                command.extend(
                    [
                        "--after-workspace-id",
                        str(cursor[0]),
                        "--after-conversation-id",
                        str(cursor[1]),
                    ]
                )
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            assert result.returncode == (0 if page == 4 else 2), result.stderr
            progress = json.loads(result.stdout)
            results.append(progress)
            cursor = progress["next_after"]
            current = store.get_conversation(conversation_ids[min(page // 2, 1)])
            assert current is not None
            assert current.last_message_at_fresh is (page not in (0, 2))

        assert results == [
            {"complete": False, "next_after": None},
            {"complete": False, "next_after": [17, conversation_ids[0]]},
            {"complete": False, "next_after": [17, conversation_ids[0]]},
            {"complete": False, "next_after": [17, conversation_ids[1]]},
            {"complete": True, "next_after": None},
        ]
        for conversation_id in conversation_ids:
            repaired = store.get_conversation(conversation_id)
            assert repaired is not None
            assert repaired.last_message_at is not None
            assert repaired.last_message_observed_position == 2


def test_cli_factory_uses_the_deployment_decoder(
    db_uri: str, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class WrappedStore(SqlAlchemyConversationStore):
        def _encode_item_data(self, data_json: str) -> str:
            return "wrapped:" + data_json

        def _decode_item_data_batch(self, stored: list[str]) -> list[str]:
            assert all(value.startswith("wrapped:") for value in stored)
            return [value.removeprefix("wrapped:") for value in stored]

    store = WrappedStore(db_uri)
    conversation = store.create_conversation()
    items = store.append(conversation.id, [_message()])
    with store._session("test_setup") as session:
        row = session.get(SqlConversation, (0, conversation.id))
        assert row is not None
        row.last_message_at = None
        row.last_message_observed_position = None

    factory_calls: list[tuple[str, str | None]] = []

    def factory(storage: str, conversations: str | None) -> WrappedStore:
        factory_calls.append((storage, conversations))
        return WrappedStore(storage, conversations)

    factory_module = ModuleType("unread_test_factory")
    monkeypatch.setattr(factory_module, "build", factory, raising=False)
    monkeypatch.setitem(sys.modules, factory_module.__name__, factory_module)
    assert (
        reconcile.main(
            ["--storage-location", db_uri, "--store-factory", "unread_test_factory:build"]
        )
        == 0
    )
    assert factory_calls == [(db_uri, None)]
    assert json.loads(capsys.readouterr().out)["complete"] is True
    repaired = store.get_conversation(conversation.id)
    assert repaired is not None
    assert repaired.last_message_at_fresh is True
    assert repaired.last_message_at == items[0].created_at


@pytest.mark.parametrize(
    "arguments",
    [
        ["--item-batch-limit", "0"],
        ["--max-pages", "-1"],
        ["--after-workspace-id", "0"],
        ["--after-workspace-id", "17", "--after-conversation-id", "01" * 16],
    ],
)
def test_cli_rejects_invalid_bounds_and_cursors(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        reconcile.main(["--storage-location", "unused", *arguments])
    assert error.value.code == 2
