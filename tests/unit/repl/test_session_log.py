"""
Tests for :mod:`omnigent.repl._session_log` — the JSON dump
helper that ports the legacy ``--log`` flag to Omnigent mode.

Two layers:

1. **Path composition** — :func:`default_log_path` produces a
   stable, sortable path with the expected components.
2. **Store-backed dump** —
   :func:`write_session_log_from_store` against a real
   :class:`SqlAlchemyConversationStore`. Covers the happy
   path (one user item + one assistant item dump correctly),
   pagination (>page_size items all show up), and the
   missing-conversation guard.

The SDK-backed :func:`write_session_log` runs in the same
pagination loop just driven by an HTTP client; it's exercised
by the REPL flow's e2e coverage rather than mocked here, since
mocking OmnigentClient + responses correctly is heavier than
the value of testing it twice.
"""

from __future__ import annotations

import json
from pathlib import Path

from omnigent.entities import (
    FunctionCallOutputData,
    MessageData,
    NewConversationItem,
)
from omnigent.repl._session_log import (
    DEFAULT_LOG_DIR,
    default_log_path,
    write_session_log_from_store,
)
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)

# ── 1. Path composition ──────────────────────────────────


def test_default_log_path_uses_default_dir_when_none() -> None:
    """
    ``log_dir=None`` resolves to ``~/.omnigent/logs/`` — the same
    directory the legacy non-AP path writes to. Keeps the
    user's mental model consistent across paths.
    """
    path = default_log_path("conv_abc1234567890def_extra", None)
    assert path.parent == DEFAULT_LOG_DIR, (
        f"Expected the parent to be {DEFAULT_LOG_DIR}, got "
        f"{path.parent}. If different, the legacy --log location "
        f"changed and migrating users will hunt for missing files."
    )


def test_default_log_path_filename_shape(tmp_path: Path) -> None:
    """
    Filename is ``{YYYYMMDD-HHMMSS}-{conv_short}.json``. We don't
    pin the exact timestamp (race against the clock) but verify
    the structure: 15-char timestamp + dash + 16-char conv-short
    + ``.json``.
    """
    path = default_log_path("conv_abc1234567890def_extra", tmp_path)
    name = path.name
    assert name.endswith(".json"), (
        f"Expected .json suffix, got {name!r}. Readers grep for this extension to find logs."
    )
    parts = name[:-5].split("-", 2)  # strip ".json", split into [date, time, slug]
    assert len(parts) == 3, (
        f"Filename must split into [date, time, conv-slug] on dashes, "
        f"got {parts!r} from {name!r}. Slug truncation may have "
        f"merged with the timestamp."
    )
    assert len(parts[0]) == 8, f"YYYYMMDD prefix should be 8 chars, got {parts[0]!r}."
    assert len(parts[1]) == 6, f"HHMMSS should be 6 chars, got {parts[1]!r}."
    # The conv slug is the FIRST 16 chars of the conversation id.
    assert parts[2] == "conv_abc12345678", (
        f"Conv slug should be the first 16 chars of the conversation "
        f"id, got {parts[2]!r}. If different, the slug truncation "
        f"length changed (we rely on it to keep filenames short)."
    )


def test_default_log_path_strips_path_separators(tmp_path: Path) -> None:
    """
    Defensive: a conversation id that somehow contains a ``/``
    must not produce a file path that escapes the log directory.
    Mirrors the legacy ``session.id.replace("/", "_")`` defense in
    ``omnigent/inner/cli.py::_default_session_log_path``.
    """
    path = default_log_path("conv/with/slashes", tmp_path)
    assert "/" not in path.name, (
        f"The slug component of the filename must not contain "
        f"directory separators, got {path.name!r}. Otherwise an "
        f"adversarial conversation id could write outside log_dir."
    )


# ── 2. Store-backed dump ─────────────────────────────────


def test_write_session_log_from_store_dumps_basic_conversation(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    Happy path: one user message + one assistant message land in
    the dump, the JSON parses, and the AP-native shape is intact.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation(title="hello")

    conv_store.append(
        conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_test_user_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Hello agent"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_test_assistant_1",
                data=MessageData(
                    role="assistant",
                    agent="resume_test",
                    content=[{"type": "output_text", "text": "Hello user"}],
                ),
            ),
        ],
    )

    path = write_session_log_from_store(
        conv_store, conv.id, agent_name="resume_test", log_dir=tmp_path
    )

    assert path.exists(), f"Expected log file at {path}, but it wasn't written."
    payload = json.loads(path.read_text())

    assert payload["version"] == 1, (
        f"Schema version must be 1 — readers gate on it. Got {payload.get('version')!r}."
    )
    assert payload["format"] == "omnigent-conversation"
    assert payload["agent_name"] == "resume_test"
    assert payload["conversation"]["id"] == conv.id
    assert payload["conversation"]["title"] == "hello"
    assert len(payload["conversation"]["items"]) == 2, (
        f"Expected exactly 2 items (one user, one assistant), got "
        f"{len(payload['conversation']['items'])}: "
        f"{payload['conversation']['items']!r}. If <2, pagination "
        f"truncated; if >2, an item leaked from another conversation."
    )
    user_item, assistant_item = payload["conversation"]["items"]
    assert user_item["data"]["role"] == "user"
    assert assistant_item["data"]["role"] == "assistant"


def test_write_session_log_from_store_pages_long_conversations(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    Verify the pagination loop walks past the per-call cap (100).
    Without it, a long conversation would silently truncate to the
    first page and the user would lose history in their log file.

    Append 150 items (50 over the cap) and assert the dump captures
    all of them in order.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.create_conversation()

    items_to_append = [
        NewConversationItem(
            type="message",
            response_id=f"resp_test_{i:03d}",
            data=MessageData(
                role="user",
                content=[{"type": "input_text", "text": f"msg-{i:03d}"}],
            ),
        )
        for i in range(150)
    ]
    # Append in batches; the store doesn't accept all 150 at once
    # cleanly under SQLite (variable bind limits).
    for chunk_start in range(0, len(items_to_append), 50):
        conv_store.append(conv.id, items_to_append[chunk_start : chunk_start + 50])

    path = write_session_log_from_store(
        conv_store, conv.id, agent_name="paginating_agent", log_dir=tmp_path
    )
    payload = json.loads(path.read_text())

    items = payload["conversation"]["items"]
    assert len(items) == 150, (
        f"Expected 150 items dumped, got {len(items)}. If 100, the "
        f"pagination loop stopped at the first server page and "
        f"truncated the history. If <100, the page-size logic is "
        f"under-fetching."
    )
    # Spot-check chronological order: first item is msg-000, last
    # is msg-149. If reversed, the helper is asking for desc order
    # somewhere or the cursor advance walks backwards.
    first_text = items[0]["data"]["content"][0]["text"]
    last_text = items[-1]["data"]["content"][0]["text"]
    assert first_text == "msg-000", (
        f"Items must be chronological (oldest first). Got "
        f"first={first_text!r}, expected 'msg-000'."
    )
    assert last_text == "msg-149", (
        f"Last item should be 'msg-149' (most recent), got {last_text!r}."
    )


# ── 3. Sub-agent children walk ────────────────────────────


def test_write_session_log_walks_sub_agent_children(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    Sub-agent spawns are persisted as ``function_call_output``
    items whose ``output`` decodes to a ``sys_session_send``
    handle (``{"kind": "sub_agent", "conversation_id": "...",
    ...}``). The dump walks those handles, recurses into each
    child conversation, and embeds the result under
    ``conversation.children``.

    Without this walk, a supervisor agent's log captures the
    user's transcript with the supervisor but loses every
    sub-agent's work — the legacy non-AP mode log includes
    them via ``session._agent_sessions`` recursion in
    ``_session_log_dict``, so this is the parity equivalent.

    Test shape: a parent conversation with
    - one user message,
    - one function_call_output that decodes to a sub_agent
      handle pointing at a child conversation,
    a child conversation with one assistant message of its own.
    The dump's ``children[0].items[0]`` should be the child's
    assistant message.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = conv_store.create_conversation(title="parent")
    child = conv_store.create_conversation(title="child sub-agent")

    # Append a real spawn-output to the parent so the dump's
    # walker will discover the child via the same parser the
    # Ctrl+O overlay uses.
    handle = {
        "task_id": "tsk_child_a1",
        "conversation_id": child.id,
        "kind": "sub_agent",
        "type": "worker",
        "name": "fib_worker",
        "status": "in_progress",
    }
    conv_store.append(
        parent.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_parent_user",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "Spawn me a worker"}],
                ),
            ),
            NewConversationItem(
                type="function_call_output",
                response_id="resp_parent_spawn",
                data=FunctionCallOutputData(
                    call_id="call_spawn_1",
                    output=json.dumps(handle),
                ),
            ),
        ],
    )
    conv_store.append(
        child.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_child_done",
                data=MessageData(
                    role="assistant",
                    agent="fib_worker",
                    content=[{"type": "output_text", "text": "fib(10) = 55"}],
                ),
            ),
        ],
    )

    path = write_session_log_from_store(
        conv_store, parent.id, agent_name="supervisor", log_dir=tmp_path
    )
    payload = json.loads(path.read_text())

    children = payload["conversation"]["children"]
    assert len(children) == 1, (
        f"Expected one child conversation discovered via the spawn "
        f"handle, got {len(children)}: {children!r}. If 0, the parser "
        f"didn't recognize the sub_agent handle (check that the JSON "
        f"shape still matches `_parse_sub_agent_handle` in "
        f"omnigent/repl/_repl.py — the format the spawn tool "
        f"persists may have changed)."
    )
    child_node = children[0]
    assert child_node["id"] == child.id, (
        f"Child node id should be the spawn handle's conversation_id, "
        f"got {child_node['id']!r}, expected {child.id!r}."
    )
    assert len(child_node["items"]) == 1, (
        f"Expected child's items to be dumped (1 assistant message), "
        f"got {len(child_node['items'])}: {child_node['items']!r}. "
        f"If 0, the child fetch failed silently."
    )
    assert child_node["items"][0]["data"]["content"][0]["text"] == "fib(10) = 55"
    # Recursion terminator: the child has no spawn items, so its
    # children list is empty.
    assert child_node["children"] == [], (
        f"Child has no spawn items, so its children list should be "
        f"empty, got {child_node['children']!r}. If non-empty, the "
        f"walker is finding spawns where there aren't any."
    )


def test_write_session_log_dedupes_repeated_spawns_to_same_child(
    db_uri: str,
    tmp_path: Path,
) -> None:
    """
    A supervisor that calls ``sys_session_send`` multiple times to
    the same child (the continuation pattern — first call to spawn,
    subsequent calls to send follow-up messages) emits multiple
    function_call_output items all carrying the SAME
    conversation_id. The walker must dedupe so the child appears
    once, not once per send.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    parent = conv_store.create_conversation()
    child = conv_store.create_conversation()

    handle = {
        "task_id": "tsk_dup",
        "conversation_id": child.id,
        "kind": "sub_agent",
        "type": "worker",
        "name": "dup_worker",
        "status": "in_progress",
    }
    conv_store.append(
        parent.id,
        [
            NewConversationItem(
                type="function_call_output",
                response_id=f"resp_dup_{i}",
                data=FunctionCallOutputData(
                    call_id=f"call_dup_{i}",
                    output=json.dumps(handle),
                ),
            )
            for i in range(3)
        ],
    )

    path = write_session_log_from_store(
        conv_store, parent.id, agent_name="supervisor", log_dir=tmp_path
    )
    payload = json.loads(path.read_text())
    children = payload["conversation"]["children"]
    assert len(children) == 1, (
        f"Three spawn outputs to the same conversation_id should "
        f"dedupe to one child entry, got {len(children)}. If 3, the "
        f"continuation pattern would balloon the log file with "
        f"redundant nested copies of the same conversation."
    )
