"""
Unit tests for ``sys_session_get_history`` and ``sys_session_close``.

These cover the two tools added in 13a alongside the existing
``sys_session_send`` / ``sys_session_list`` family. The tests build a
real :class:`SqlAlchemyConversationStore` over a temp SQLite DB and
monkeypatch the ``omnigent.runtime`` accessors so the tools see them.

The tasks table has been removed. ``_resolve_parent_conversation_id``
now reads ``ctx.conversation_id`` directly instead of looking up the
task row, so the fixture only needs a parent conversation and a child.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import dataclass

import pytest

from omnigent.entities.conversation import MessageData, NewConversationItem
from omnigent.runtime import pending_elicitations
from omnigent.session_lifecycle import CLOSED_LABEL_KEY, CLOSED_LABEL_VALUE
from omnigent.spec.types import AgentSpec, ExecutorSpec
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.tools.base import ToolContext
from omnigent.tools.builtins.spawn import (
    _CLOSED_TITLE_INFIX,
    _HISTORY_DEFAULT_TAIL,
    _HISTORY_MAX_TAIL,
    SysSessionCloseTool,
    SysSessionGetHistoryTool,
    SysSessionListTool,
    SysSessionSendTool,
)


@dataclass
class _Fixture:
    """
    Bundle of stores + ids + ctx the test cases reuse.

    Built per-test by :func:`session_fixture` so each test gets an
    independent SQLite database, a parent conversation, and a child
    conversation titled ``"researcher:auth"`` parented by the parent
    conversation.

    ``_resolve_parent_conversation_id`` reads ``ctx.conversation_id``
    directly (tasks table removed), so the fixture sets that field
    instead of creating a task row.

    :param conv_store: Conversation store under test.
    :param parent_conv_id: Parent conversation id.
    :param child_conv_id: Child conversation id (titled
        ``"researcher:auth"`` under the parent).
    :param ctx: Pre-built :class:`ToolContext` carrying
        ``conversation_id=parent_conv_id``.
    """

    conv_store: SqlAlchemyConversationStore
    parent_conv_id: str
    child_conv_id: str
    ctx: ToolContext


@pytest.fixture(autouse=True)
def _clean_pending_elicitations_index() -> Iterator[None]:
    """
    Reset the process-global pending-elicitations index around each test.

    ``SysSessionGetHistoryTool`` now reads this index to append parked
    elicitations to its output. Without a reset, an entry recorded by
    one peek test would inflate another's item count (the existing
    ``len(items) == 2`` assertions would break).
    """
    pending_elicitations.reset_for_tests()
    yield
    pending_elicitations.reset_for_tests()


@pytest.fixture()
def session_fixture(
    db_uri: str,
    monkeypatch: pytest.MonkeyPatch,
) -> Iterator[_Fixture]:
    """
    Build the per-test database state and patch the runtime accessors.

    Creates a parent conversation and a child conversation titled
    ``"researcher:auth"`` parented by the parent. Adds two items to the
    child (one user message, one assistant message). Monkeypatches
    ``omnigent.runtime.get_conversation_store`` so the tool's
    late-bound lookups resolve to the test store.

    ``_resolve_parent_conversation_id`` reads ``ctx.conversation_id``
    directly (tasks table removed), so the fixture sets that field on
    the :class:`ToolContext` instead of creating a task row.

    :param db_uri: Per-test SQLite URI from ``tests/conftest.py``.
    :param monkeypatch: pytest fixture for late-bound runtime patches.
    :yields: A :class:`_Fixture` the test reads from.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)

    parent_conv = conv_store.create_conversation(kind="default")
    child_conv = conv_store.create_conversation(
        kind="sub_agent",
        title="researcher:auth",
        parent_conversation_id=parent_conv.id,
    )
    conv_store.append(
        child_conv.id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_test_1",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": "find the auth bug"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_test_1",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": "looking at handlers.py"}],
                    agent="researcher",
                ),
            ),
        ],
    )

    # The tools' invoke() does ``from omnigent.runtime import
    # get_conversation_store`` per call (lazy bind), so patching the
    # runtime module's attribute is sufficient.
    monkeypatch.setattr("omnigent.runtime.get_conversation_store", lambda: conv_store)

    # conversation_id is the canonical parent session id; _resolve_parent_conversation_id
    # reads ctx.conversation_id directly (tasks table removed).
    ctx = ToolContext(
        task_id="task_placeholder",
        agent_id="ag_parent",
        conversation_id=parent_conv.id,
    )

    yield _Fixture(
        conv_store=conv_store,
        parent_conv_id=parent_conv.id,
        child_conv_id=child_conv.id,
        ctx=ctx,
    )


# ── Schema tests ──────────────────────────────────────────


def test_send_schema_advertises_plain_string_and_purpose_object_args() -> None:
    """
    ``sys_session_send`` accepts either the stable string contract or an object.

    This catches the Nessie regression where its policy required
    ``args.purpose`` but the tool schema only advertised ``args`` as a string,
    causing models to emit policy-denied calls for legitimate review helpers.
    Keeping the string branch verifies the shared tool remains compatible with
    non-Nessie agents.
    """
    tool = SysSessionSendTool(
        {"claude": AgentSpec(spec_version=1, name="claude", description="Review helper.")}
    )

    params = tool.get_schema()["function"]["parameters"]
    args_schema = params["properties"]["args"]

    # Only ``args`` is universally required now: the unified tool accepts
    # either (agent + title) named mode OR session_id mode, so agent/title
    # moved out of ``required`` (the handler enforces the one-of split).
    # session_id is advertised as the by-id-mode addressing field.
    assert params["required"] == ["args"]
    assert "session_id" in params["properties"]
    assert args_schema["anyOf"][0] == {"type": "string"}
    object_schema = args_schema["anyOf"][1]
    assert object_schema["type"] == "object"
    # ``model`` is optional dispatch metadata — only ``input`` may be
    # required, or plain-string sends would break.
    assert object_schema["required"] == ["input"]
    assert object_schema["additionalProperties"] is False
    assert set(object_schema["properties"]) == {"input", "purpose", "model", "file_ids"}
    assert "dispatch metadata" in object_schema["properties"]["purpose"]["description"]
    # The model property must say it is create-time-only and optional,
    # so the LLM doesn't attach it to continuation sends.
    model_desc = object_schema["properties"]["model"]["description"]
    assert object_schema["properties"]["model"]["type"] == "string"
    assert "CREATES" in model_desc
    assert "harness default" in model_desc


def _object_branch_props(tool: SysSessionSendTool) -> set[str]:
    """Return the property names of the object branch of ``args``."""
    params = tool.get_schema()["function"]["parameters"]
    object_schema = next(
        b for b in params["properties"]["args"]["anyOf"] if b.get("type") == "object"
    )
    return set(object_schema["properties"])


def test_send_schema_gates_harness_field_behind_allowlist_opt_in() -> None:
    """
    ``args.harness`` is advertised ONLY when a sub-agent opts in.

    Per design D.4 the runtime harness override is allowlist-gated: the
    schema exposes ``harness`` only when at least one declared sub-agent
    declares a non-empty ``executor.config.allowed_harnesses``. A sub-agent
    without that opt-in keeps the base ``{input, purpose, model, file_ids}``
    args object, so the orchestrator never sees a harness knob it cannot use.
    This mirrors the per-child dispatch guard in tool_dispatch.py — the two
    gates must agree on what "opted in" means.
    """
    # Not opted in: a plain sub-agent (no allowed_harnesses) → base schema.
    plain = SysSessionSendTool(
        {"claude": AgentSpec(spec_version=1, name="claude", description="Review helper.")}
    )
    assert _object_branch_props(plain) == {"input", "purpose", "model", "file_ids"}

    # Opted in: a sub-agent whose executor.config.allowed_harnesses declares a
    # non-empty allowlist (the polly/debby `codex`/`opencode` worker shape) →
    # the schema adds the gated `harness` field.
    opted_in_spec = AgentSpec(
        spec_version=1,
        name="codex",
        description="Codex coding sub-agent.",
        executor=ExecutorSpec(
            type="omnigent",
            config={
                "harness": "codex-native",
                "allowed_harnesses": ["codex-native", "opencode-native"],
            },
        ),
    )
    opted_in = SysSessionSendTool({"codex": opted_in_spec})
    assert _object_branch_props(opted_in) == {"input", "purpose", "model", "file_ids", "harness"}
    object_schema = next(
        b
        for b in opted_in.get_schema()["function"]["parameters"]["properties"]["args"]["anyOf"]
        if b.get("type") == "object"
    )
    assert "allowed_harnesses" in object_schema["properties"]["harness"]["description"]
    # additionalProperties stays closed even with the extra gated field, so a
    # spurious arg is still rejected by validation.
    assert object_schema["additionalProperties"] is False

    # Mixed: one opted-in sub-agent among several opts the whole tool's schema
    # in — the dispatch guard still rejects harness for the non-opted children.
    mixed = SysSessionSendTool(
        {
            "claude": AgentSpec(spec_version=1, name="claude", description="Review helper."),
            "codex": opted_in_spec,
        }
    )
    assert _object_branch_props(mixed) == {"input", "purpose", "model", "file_ids", "harness"}


def test_peek_schema_required_fields_and_no_extra_props() -> None:
    """
    The ``sys_session_get_history`` schema requires ``conversation_id``
    and rejects unknown properties.

    A regression here would either let the LLM omit the required
    arg (the tool would error at parse time, but schema-wide
    enforcement catches it earlier) or accept arbitrary extras
    (which the validator must drop, not pass through to the
    handler).
    """
    schema = SysSessionGetHistoryTool().get_schema()
    params = schema["function"]["parameters"]
    assert params["required"] == ["conversation_id"]
    assert params["additionalProperties"] is False
    assert set(params["properties"].keys()) == {"conversation_id", "tail_items"}


def test_peek_schema_tail_items_bounds() -> None:
    """
    ``tail_items`` is integer with ``minimum=1`` and ``maximum=50``.

    The 50 ceiling matches ``_HISTORY_MAX_TAIL`` and prevents the LLM
    from requesting an unbounded slice that would balloon the
    parent's prompt. The 1 floor prevents zero/negative values
    that the handler would otherwise have to special-case.
    """
    tail_schema = SysSessionGetHistoryTool().get_schema()["function"]["parameters"]["properties"][
        "tail_items"
    ]
    assert tail_schema["type"] == "integer"
    assert tail_schema["minimum"] == 1
    assert tail_schema["maximum"] == _HISTORY_MAX_TAIL


def test_close_schema_required_fields_and_no_extra_props() -> None:
    """
    The ``sys_session_close`` schema requires ``conversation_id``
    only — no ``tail_items``, no extras.

    Close has a smaller surface than peek (no slice argument);
    extending the schema later would be additive, but a regression
    that introduced an unintended property would expose the LLM
    to ambiguity and is worth catching.
    """
    schema = SysSessionCloseTool().get_schema()
    params = schema["function"]["parameters"]
    assert params["required"] == ["conversation_id"]
    assert params["additionalProperties"] is False
    assert set(params["properties"].keys()) == {"conversation_id"}


# ── Peek invoke tests ─────────────────────────────────────


def test_peek_returns_items_chronological(session_fixture: _Fixture) -> None:
    """
    Peek returns the child's items in chronological order with
    each one projected through the same activity shape used by
    ``check_task``.

    Two items were inserted (user → assistant). The list_items
    call uses ``order="desc"`` so peek must reverse the result —
    a regression that forgot the reverse would surface here as the
    user message appearing AFTER the assistant.
    """
    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)

    assert payload["conversation_id"] == session_fixture.child_conv_id
    assert payload["agent"] == "researcher"
    assert payload["title"] == "auth"
    items = payload["items"]
    # Two items inserted in the fixture, both should appear.
    assert len(items) == 2

    # Chronological: user first, assistant second. Activity
    # projection sets ``role`` directly from MessageData.role.
    assert items[0]["role"] == "user"
    assert items[0]["content"] == "find the auth bug"
    assert items[1]["role"] == "assistant"
    assert items[1]["content"] == "looking at handlers.py"


def test_peek_surfaces_pending_elicitation_after_stored_items(
    session_fixture: _Fixture,
) -> None:
    """
    A sub-agent parked on an elicitation surfaces in peek output.

    The elicitation never lands in the conversation store (it lives
    only in the pending-elicitations index), so before this fix a
    peek on a sub-agent blocked on ``AskUserQuestion`` ended at the
    last stored message with no sign it needed input — the exact bug
    this fix addresses. Peek must append the index's outstanding prompt after
    the two stored messages.
    """
    pending_elicitations.record_publish(
        session_fixture.child_conv_id,
        {
            "type": "response.elicitation_request",
            "elicitation_id": "elicit_bio",
            "params": {
                "mode": "form",
                "message": "Answer 3 questions on human biology",
                "requestedSchema": {"properties": {"q1": {}, "q2": {}, "q3": {}}},
            },
        },
    )

    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    items = json.loads(raw)["items"]

    # 3 = 2 stored messages + 1 synthesized pending elicitation. If 2,
    # the index isn't being read; the parent stays blind to the prompt.
    assert len(items) == 3
    # The elicitation is the most recent act, so it's appended last —
    # after the user/assistant messages, in chronological order.
    elicit = items[-1]
    assert elicit["type"] == "pending_elicitation"
    assert elicit["elicitation_id"] == "elicit_bio"
    # The prompt text proves the params payload traversed the index →
    # snapshot → projector path, not just a bare "blocked" sentinel.
    assert elicit["prompt"] == "Answer 3 questions on human biology"
    assert elicit["fields"] == ["q1", "q2", "q3"]
    # The stored messages still precede it and are unchanged.
    assert items[0]["role"] == "user"
    assert items[1]["role"] == "assistant"


def test_peek_no_pending_elicitation_when_index_empty(
    session_fixture: _Fixture,
) -> None:
    """
    With nothing parked, peek returns only the stored items.

    Guards the inverse of the surfacing test: peek must not invent a
    pending_elicitation item when the index is empty for the target.
    """
    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    items = json.loads(raw)["items"]
    # Exactly the two fixture messages — no synthesized elicitation.
    assert len(items) == 2
    assert all(item["type"] != "pending_elicitation" for item in items)


def test_peek_default_tail_when_omitted(session_fixture: _Fixture) -> None:
    """
    Omitting ``tail_items`` falls back to ``_HISTORY_DEFAULT_TAIL``.

    The fixture has two items, fewer than the default. A regression
    that used 0 or 1 as the fallback would clip output. The
    assertion uses ``len(items) == 2`` because the fixture only
    inserted two — anything less means the default was wrong.
    """
    assert _HISTORY_DEFAULT_TAIL >= 2, (
        "Test depends on the default exceeding the fixture's item count "
        "so omitting tail_items returns everything."
    )
    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    items = json.loads(raw)["items"]
    assert len(items) == 2


def test_peek_clamps_oversize_tail_items(session_fixture: _Fixture) -> None:
    """
    ``tail_items`` exceeding ``_HISTORY_MAX_TAIL`` is clamped to the
    cap, not rejected.

    The schema's ``maximum`` bound is advisory (LLM providers
    don't all enforce schema validation), so the handler clamps
    explicitly. A regression that dropped the clamp would let a
    misbehaving caller request thousands of items and balloon
    prompt size.

    To genuinely exercise the clamp, this test extends the
    fixture's child conversation with enough extra items that
    the post-clamp list_items request is the limiting factor
    (not the data set size). Specifically: ``_HISTORY_MAX_TAIL + 10``
    extra items, totaling ``_HISTORY_MAX_TAIL + 12`` items in the
    child. Calling peek with ``tail_items = _HISTORY_MAX_TAIL * 20``
    must return exactly ``_HISTORY_MAX_TAIL`` items — proving the
    clamp engaged. Without the clamp the LLM would receive all
    ``_HISTORY_MAX_TAIL + 12`` items.
    """
    extra_count = _HISTORY_MAX_TAIL + 10
    session_fixture.conv_store.append(
        session_fixture.child_conv_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_test_clamp",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": f"item {i}"}],
                    agent="researcher",
                ),
            )
            for i in range(extra_count)
        ],
    )

    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps(
            {
                "conversation_id": session_fixture.child_conv_id,
                "tail_items": _HISTORY_MAX_TAIL * 20,
            },
        ),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert "error" not in payload
    # Exactly the cap. Without the clamp this would be the full
    # _HISTORY_MAX_TAIL + 12 items (= 2 fixture seeds + extras);
    # without the extras seeded above this would be 2 regardless
    # of the clamp. The exact equality is the only assertion shape
    # that catches BOTH a missing clamp and a clamp set to the
    # wrong constant.
    assert len(payload["items"]) == _HISTORY_MAX_TAIL


def test_peek_rejects_non_integer_tail_items(session_fixture: _Fixture) -> None:
    """
    Non-integer ``tail_items`` returns a validation error (not a
    crash).

    A regression that passed the raw value through to ``int()``
    would raise (which the framework would surface as
    ``[llm] (error with no details)``); the handler must catch
    the conversion failure and return an actionable error to the
    LLM.
    """
    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps(
            {
                "conversation_id": session_fixture.child_conv_id,
                "tail_items": "many",
            }
        ),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert "error" in payload
    assert "tail_items" in payload["error"]


def test_peek_unknown_conversation_id_returns_not_found(
    session_fixture: _Fixture,
) -> None:
    """
    Peek for a ``conversation_id`` that doesn't exist returns
    ``session_not_found``.

    A pre-fix regression where the lookup matched the wrong row
    (e.g. by partial id) would surface here as a successful peek
    against the existing fixture child rather than a not-found.
    """
    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": "conv_does_not_exist"}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_not_found"
    assert payload["conversation_id"] == "conv_does_not_exist"


def test_peek_out_of_tree_conversation_is_rejected(
    session_fixture: _Fixture,
) -> None:
    """
    Peek refuses a conversation_id from a different spawn tree.

    The caller's tree is identified by ``root_conversation_id``;
    a child of an unrelated parent has a different root and must
    be rejected with ``session_out_of_tree``. Without this guard,
    any agent could read any other agent's conversation by
    guessing/leaking conversation_ids.
    """
    # Build a totally separate tree: a different parent conversation
    # and a child under it. The child's root_conversation_id is its
    # parent's id, which differs from the fixture's parent_conv_id.
    other_parent = session_fixture.conv_store.create_conversation(kind="default")
    other_child = session_fixture.conv_store.create_conversation(
        kind="sub_agent",
        title="other:secret",
        parent_conversation_id=other_parent.id,
    )

    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": other_child.id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_out_of_tree"
    assert payload["conversation_id"] == other_child.id


def test_peek_top_level_conversation_in_tree_is_rejected(
    session_fixture: _Fixture,
) -> None:
    """
    Peek refuses a top-level conversation_id even when it's in the
    caller's spawn tree.

    The caller's own root conversation passes the tree-membership
    check (its ``root_conversation_id`` equals the caller's
    ``root_id``) but it isn't a sub-agent — its title doesn't
    follow the ``"<agent>:<title>"`` convention. The tool must
    return ``session_not_a_sub_agent`` rather than letting
    ``_agent_title_from_conversation`` raise.

    :param session_fixture: Per-test fixture providing the tool ctx
        and parent conversation id.
    """
    tool = SysSessionGetHistoryTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.parent_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_not_a_sub_agent"
    assert payload["conversation_id"] == session_fixture.parent_conv_id


def test_close_top_level_conversation_in_tree_is_rejected(
    session_fixture: _Fixture,
) -> None:
    """
    Close refuses a top-level conversation_id even when it's in
    the caller's spawn tree.

    Same invariant as the peek case — the LLM cannot tombstone a
    non-sub-agent conversation, so the tool returns
    ``session_not_a_sub_agent`` and leaves the row's title intact.

    :param session_fixture: Per-test fixture providing the tool ctx
        and parent conversation id.
    """
    tool = SysSessionCloseTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.parent_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_not_a_sub_agent"
    assert payload["conversation_id"] == session_fixture.parent_conv_id

    parent_after = session_fixture.conv_store.get_conversation(
        session_fixture.parent_conv_id,
    )
    assert parent_after is not None
    assert _CLOSED_TITLE_INFIX not in (parent_after.title or "")


# ── Close invoke tests ────────────────────────────────────


def test_close_marks_closed_and_tombstones_internal_title(session_fixture: _Fixture) -> None:
    """
    Close marks the child closed and internally tombstones its title.

    The explicit ``omnigent.closed=true`` label is the behavioral
    marker write paths consume. The conv_id title suffix still
    guarantees uniqueness against the partial unique index on
    ``(parent_conversation_id, title)`` even if the same logical
    session is closed multiple times across the parent's lifetime.
    A regression that omitted the suffix would let two closes collide
    on the index.
    """
    tool = SysSessionCloseTool()
    raw = tool.invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload == {
        "closed": True,
        "conversation_id": session_fixture.child_conv_id,
        "agent": "researcher",
        "title": "auth",
    }

    # Inspect the underlying row directly via list_conversations —
    # exercises the same lookup path the next spawn would use.
    children = session_fixture.conv_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=session_fixture.parent_conv_id,
        limit=100,
    )
    titles = [c.title for c in children.data]
    expected = f"researcher:auth{_CLOSED_TITLE_INFIX}{session_fixture.child_conv_id}"
    assert expected in titles, (
        f"expected title {expected!r} in {titles!r} — close did not "
        "rewrite the child's title with the conv_id suffix."
    )
    # And the bare title is gone — proves a follow-up spawn would
    # find no match.
    assert "researcher:auth" not in titles
    refreshed = session_fixture.conv_store.get_conversation(session_fixture.child_conv_id)
    assert refreshed is not None
    assert refreshed.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE


def test_close_then_peek_by_id_still_resolves_tombstoned_row(
    session_fixture: _Fixture,
) -> None:
    """
    After close, peek by conversation_id still resolves the row but
    its title is now tombstoned. Peek by the original
    ``(agent, title)`` lookup (used by send / list) would no
    longer find it.

    The tombstone is non-destructive: peek still works against
    the now-tombstoned conversation_id (the tombstone only
    affects title-based lookups). What we verify here is that
    the title rewrite landed and the result still references the
    same conversation_id, and that the agent/title fields are
    recovered correctly from the tombstoned title.
    """
    SysSessionCloseTool().invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    # The conversation_id remains valid; peek by id still works.
    raw = SysSessionGetHistoryTool().invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert "error" not in payload
    # The agent/title fields come from the post-tombstone title
    # (``"researcher:auth:closed:<id>"``); the helper splits at
    # ``_CLOSED_TITLE_INFIX`` so the bare ``"auth"`` is recovered.
    assert payload["agent"] == "researcher"
    assert payload["title"] == "auth"


def test_close_succeeds_regardless_of_session_state(session_fixture: _Fixture) -> None:
    """
    Close tombstones the child conversation regardless of any live
    session state.

    The tasks table has been removed and the task-based busy check
    (``_busy_check_or_none``) now always returns ``None``. Close
    proceeds unconditionally; live-session awareness is the
    responsibility of the caller.

    :param session_fixture: Pre-built fixture with a child conversation.
    """
    raw = SysSessionCloseTool().invoke(
        json.dumps({"conversation_id": session_fixture.child_conv_id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload.get("closed") is True
    assert payload["conversation_id"] == session_fixture.child_conv_id

    # The title is tombstoned and the closed label is set — proves
    # close ran to completion.
    children = session_fixture.conv_store.list_conversations(
        kind="sub_agent",
        parent_conversation_id=session_fixture.parent_conv_id,
        limit=100,
    )
    titles = [c.title for c in children.data]
    assert "researcher:auth" not in titles
    closed_child = session_fixture.conv_store.get_conversation(session_fixture.child_conv_id)
    assert closed_child is not None
    assert closed_child.labels[CLOSED_LABEL_KEY] == CLOSED_LABEL_VALUE


def test_session_list_skips_label_closed_child_with_original_title(
    session_fixture: _Fixture,
) -> None:
    """
    ``sys_session_list`` treats the closed label as authoritative.

    This covers the behavioral marker independently from the legacy
    title tombstone. If a row has the original human title but is
    labelled closed, it must not reappear as a resumable sub-agent;
    otherwise ``sys_session_send`` by name could keep talking to a
    session the parent explicitly closed.
    """
    session_fixture.conv_store.set_labels(
        session_fixture.child_conv_id,
        {CLOSED_LABEL_KEY: CLOSED_LABEL_VALUE},
    )

    raw = SysSessionListTool().invoke("{}", session_fixture.ctx)
    payload = json.loads(raw)
    assert payload["sub_agents"] == []


def test_close_unknown_conversation_id_returns_not_found(
    session_fixture: _Fixture,
) -> None:
    """
    Close with an unknown ``conversation_id`` returns
    ``session_not_found`` (no DB mutation).
    """
    raw = SysSessionCloseTool().invoke(
        json.dumps({"conversation_id": "conv_ghost"}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_not_found"
    assert payload["conversation_id"] == "conv_ghost"


def test_close_out_of_tree_conversation_is_rejected(
    session_fixture: _Fixture,
) -> None:
    """
    Close refuses a conversation_id from a different spawn tree
    (``session_out_of_tree``) and leaves the row's title intact.

    Without the tree-scope check, any agent could tombstone any
    other agent's session by guessing conversation_ids. The
    regression we care about is silent acceptance — the row
    should remain unchanged on the way out.
    """
    other_parent = session_fixture.conv_store.create_conversation(kind="default")
    other_child = session_fixture.conv_store.create_conversation(
        kind="sub_agent",
        title="other:secret",
        parent_conversation_id=other_parent.id,
    )

    raw = SysSessionCloseTool().invoke(
        json.dumps({"conversation_id": other_child.id}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert payload["error"] == "session_out_of_tree"
    assert payload["conversation_id"] == other_child.id

    # Verify the out-of-tree row's title was not rewritten.
    refreshed = session_fixture.conv_store.get_conversation(other_child.id)
    assert refreshed is not None
    assert refreshed.title == "other:secret"


# ── Argument-parsing edge cases ───────────────────────────


def test_peek_invalid_json_returns_error(session_fixture: _Fixture) -> None:
    """
    Malformed JSON arguments produce an error, not a crash.

    The handler runs ``json.loads`` defensively and surfaces
    parse failures as actionable errors. A regression that let the
    JSONDecodeError propagate would surface as
    ``[llm] (error with no details)``.
    """
    raw = SysSessionGetHistoryTool().invoke(
        "{not valid json",
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert "error" in payload
    assert "invalid arguments" in payload["error"]


def test_close_missing_required_field_returns_error(session_fixture: _Fixture) -> None:
    """
    Missing ``conversation_id`` argument returns a structured
    error naming the missing field.

    The error message must name the field so the LLM can correct
    and retry. A regression that returned a generic message would
    force the LLM to guess.
    """
    raw = SysSessionCloseTool().invoke(
        json.dumps({}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert "error" in payload
    assert "conversation_id" in payload["error"]


def test_peek_empty_conversation_id_returns_error(session_fixture: _Fixture) -> None:
    """
    Empty-string ``conversation_id`` is rejected with an error
    (not silently treated as missing).

    A regression that fell through to ``get_conversation("")``
    would issue a useless DB query and return ``session_not_found``;
    surfacing the validation up front gives the LLM a clearer
    correction signal.
    """
    raw = SysSessionGetHistoryTool().invoke(
        json.dumps({"conversation_id": ""}),
        session_fixture.ctx,
    )
    payload = json.loads(raw)
    assert "error" in payload
    assert "conversation_id" in payload["error"]


# Tree-scoping resolution via the parent task is exercised by
# ``test_peek_returns_items_chronological`` (happy path) and
# ``test_peek_out_of_tree_conversation_is_rejected`` (rejection
# path). No standalone resolver test — the mechanism has no
# observable behavior beyond those two outcomes.
