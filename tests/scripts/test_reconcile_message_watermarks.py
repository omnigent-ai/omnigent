"""Exercise the maintenance entry point's external bounded checkpoints."""

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


def _message(*, hidden: bool = False, response_id: str = "message") -> NewConversationItem:
    return NewConversationItem(
        type="message",
        response_id=response_id,
        data=MessageData(
            role="user",
            content=[{"type": "input_text", "text": "maintenance test"}],
            is_meta=hidden,
        ),
    )


def _run_page(
    db_uri: str,
    *,
    workspace_id: int,
    cursor: dict[str, object] | None,
) -> tuple[int, dict[str, object]]:
    command = [
        sys.executable,
        str(_SCRIPT),
        "--storage-location",
        db_uri,
        "--workspace-id",
        str(workspace_id),
        "--item-batch-limit",
        "1",
        "--max-pages",
        "1",
    ]
    if cursor is not None:
        command.extend(["--cursor", json.dumps(cursor, separators=(",", ":"))])
    result = subprocess.run(command, capture_output=True, text=True, timeout=30)
    assert not result.stderr, result.stderr
    return result.returncode, json.loads(result.stdout)


def test_cli_resumes_partial_pages_without_skipping_conversations(db_uri: str) -> None:
    store = SqlAlchemyConversationStore(db_uri)
    conversation_ids = ["01" * 16, "02" * 16]
    with workspace_scope(17):
        for conversation_id in conversation_ids:
            store.create_conversation(conversation_id=conversation_id)
            store.append(
                conversation_id,
                [
                    _message(response_id=f"visible-{conversation_id}"),
                    _message(hidden=True, response_id=f"hidden-{conversation_id}"),
                ],
            )
            with store._session("test_setup") as session:
                row = session.get(SqlConversation, (17, conversation_id))
                assert row is not None
                row.last_message_at = 999

        cursor: dict[str, object] | None = None
        progress: list[dict[str, object]] = []
        for _ in range(10):
            code, result = _run_page(db_uri, workspace_id=17, cursor=cursor)
            progress.append(result)
            if result["complete"]:
                assert code == 0
                break
            assert code == 2
            cursor = result["next_cursor"]  # type: ignore[assignment]
        else:
            raise AssertionError("maintenance command did not finish")

        assert progress[-1] == {"complete": True, "next_cursor": None}
        for conversation_id in conversation_ids:
            repaired = store.get_conversation(conversation_id)
            assert repaired is not None
            assert repaired.last_message_at is not None
            assert repaired.last_message_at != 999


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
        row.last_message_at = 999

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
    output = capsys.readouterr().out.strip().splitlines()
    assert json.loads(output[-1])["complete"] is True
    repaired = store.get_conversation(conversation.id)
    assert repaired is not None
    assert repaired.last_message_at == items[0].created_at


@pytest.mark.parametrize(
    "arguments",
    [
        ["--item-batch-limit", "0"],
        ["--max-pages", "-1"],
        ["--cursor", "not-json"],
        [
            "--workspace-id",
            "17",
            "--cursor",
            '{"workspace_id":18,"conversation_id":null,"item_position":null,"max_visible_message_at":null}',
        ],
        [
            "--cursor",
            '{"workspace_id":0,"conversation_id":"01010101010101010101010101010101","item_position":-1,"max_visible_message_at":null}',
        ],
    ],
)
def test_cli_rejects_invalid_bounds_and_cursors(arguments: list[str]) -> None:
    with pytest.raises(SystemExit) as error:
        reconcile.main(["--storage-location", "unused", *arguments])
    assert error.value.code == 2
