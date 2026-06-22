"""
Tests for the built-in Google Workspace policies
(:mod:`omnigent.policies.builtins.google`) — the three sibling factories
``gdrive_policy`` / ``gmail_policy`` / ``gcalendar_policy`` in one module.

Layers per policy:

- **Layer 1** — direct callable: verb / allowlist gating, created-ID tracking,
  MCP-prefix-agnostic matching, fail-closed on unknown same-service tools, and
  abstention on other services (the composition guarantee).
- **Layer 2** — spec resolution through :func:`resolve_function_policy`.
- **Layer 3** — created-ID roundtrip via a real :class:`PolicyEngine` + SQLite
  store (proves ``session_state`` persistence across an engine rebuild).
- **Layer 4** — registry discovery: all three are one ``POLICY_REGISTRY`` with
  three factory entries, validated against their schemas.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from omnigent.policies.builtins.google import (
    CREATED_DRAFTS_STATE_KEY,
    CREATED_FILES_STATE_KEY,
    gcalendar_policy,
    gdrive_policy,
    gmail_policy,
)
from omnigent.policies.function import FunctionPolicy, resolve_function_policy
from omnigent.policies.registry import get_registry, load_registry, validate_factory_params
from omnigent.policies.types import EvaluationContext
from omnigent.runtime.policies.engine import PolicyEngine
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase, PolicyAction
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests.unit.policies.builtins.helpers import tool_call_event as tc
from tests.unit.policies.builtins.helpers import tool_result_event as tr

_DRIVE_HANDLER = "omnigent.policies.builtins.google.gdrive_policy"
_GMAIL_HANDLER = "omnigent.policies.builtins.google.gmail_policy"
_CAL_HANDLER = "omnigent.policies.builtins.google.gcalendar_policy"
_DOC_ID = "1AbCdefGHIjklMNOpqrSTUvwxyz0123456789"
_DOC_URL = f"https://docs.google.com/document/d/{_DOC_ID}/edit?tab=t.0"


@pytest.fixture()
def conversation_store(db_uri: str) -> SqlAlchemyConversationStore:
    """
    Conversation store backed by a per-test SQLite DB.

    :param db_uri: Root-conftest fixture providing a migrated SQLite URI.
    :returns: A real store for exercising session_state persistence.
    """
    return SqlAlchemyConversationStore(db_uri)


def _engine(
    store: SqlAlchemyConversationStore,
    conv_id: str,
    state: dict[str, Any],
    handler: str,
    arguments: dict[str, Any],
) -> PolicyEngine:
    """
    Build a fresh :class:`PolicyEngine` over a single google builtin policy.

    Mirrors the per-evaluation rebuild ``build_policy_engine`` does in the server.

    :param store: Backing conversation store.
    :param conv_id: Conversation to bind to.
    :param state: Seed ``session_state``.
    :param handler: Dotted path of the policy factory.
    :param arguments: Factory kwargs (non-empty so the factory is invoked).
    :returns: A ready engine.
    """
    spec = FunctionPolicySpec(
        name="google_guard",
        on=None,
        function=FunctionRef(path=handler, arguments=arguments),
    )
    return PolicyEngine(
        policies=[resolve_function_policy(spec)],
        label_defs={},
        ask_timeout=30,
        conversation_id=conv_id,
        initial_labels={},
        initial_session_state=state,
        conversation_store=store,
    )


# ══════════════════════════════════════════════════════════════════════════════
# gdrive_policy
# ══════════════════════════════════════════════════════════════════════════════


def test_drive_read_all_allows_any_read() -> None:
    """``read_all=True`` (default) abstains on reads.

    A non-None result would mean the permissive default wrongly gates reads.
    """
    assert (
        gdrive_policy(read_all=True)(tc("mcp__google__docs_document_get", {"document_id": "x"}))
        is None
    )


@pytest.mark.parametrize("prefix", ["mcp__google__", "google__"])
def test_drive_restricted_read_allowlisted_prefix_agnostic(prefix: str) -> None:
    """Restricted read of an allowlisted ID abstains, for either server prefix.

    Proves canonical matching is MCP-agnostic — the same allowlist works against
    the standard ``mcp__google__*`` and the Databricks ``google__*``.
    """
    policy = gdrive_policy(read_all=False, read_files=[_DOC_ID])
    assert policy(tc(f"{prefix}docs_document_get", {"document_id": _DOC_ID})) is None


def test_drive_restricted_read_accepts_url_allowlist_entry() -> None:
    """A URL in ``read_files`` matches a call targeting the bare ID."""
    policy = gdrive_policy(read_all=False, read_files=[_DOC_URL])
    assert policy(tc("mcp__google__docs_document_get", {"document_id": _DOC_ID})) is None


def test_drive_restricted_read_denies_non_allowlisted() -> None:
    """Restricted read of a non-allowlisted ID is denied (the core guarantee)."""
    policy = gdrive_policy(read_all=False, read_files=[_DOC_ID])
    result = policy(tc("mcp__google__docs_document_get", {"document_id": "other"}))
    assert result is not None and result["result"] == "DENY"


def test_drive_restricted_read_denies_unscopeable_search() -> None:
    """A search (no target ID) fails closed in restricted-read mode."""
    policy = gdrive_policy(read_all=False, read_files=[_DOC_ID])
    result = policy(tc("mcp__google__drive_search", {"query": "secret"}))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "tool",
    [
        "mcp__google__docs_document_create",
        "mcp__google__drive_file_create",
        "mcp__google__sheets_spreadsheet_create",
        "mcp__google__slides_presentation_create",
    ],
)
def test_drive_create_gated_by_allow_create(tool: str) -> None:
    """Create tools (incl. Slides) are allowed only when ``allow_create`` is set.

    Slides in the list proves presentations are treated as Drive files, not split
    into a separate policy.
    """
    assert gdrive_policy(allow_create=True)(tc(tool, {"title": "t"})) is None
    denied = gdrive_policy(allow_create=False)(tc(tool, {"title": "t"}))
    assert denied is not None and denied["result"] == "DENY"


def test_drive_write_to_created_file_allowed() -> None:
    """A write to a file recorded as created this session is allowed."""
    policy = gdrive_policy()
    result = policy(
        tc(
            "mcp__google__drive_file_update",
            {"file_id": "1New"},
            {CREATED_FILES_STATE_KEY: ["1New"]},
        )
    )
    assert result is None


def test_drive_write_to_uncreated_file_denied() -> None:
    """A write to a file the agent did not create (nor allowlisted) is denied."""
    policy = gdrive_policy()
    result = policy(
        tc(
            "mcp__google__drive_file_update",
            {"file_id": "1Foreign"},
            {CREATED_FILES_STATE_KEY: ["1New"]},
        )
    )
    assert result is not None and result["result"] == "DENY"


def test_drive_write_files_allowlist_permits_uncreated() -> None:
    """A pre-approved ``write_files`` ID is writable without creating it."""
    policy = gdrive_policy(write_files=[_DOC_ID])
    assert policy(tc("mcp__google__drive_file_update", {"file_id": _DOC_ID})) is None


def test_drive_write_without_target_denied() -> None:
    """A write with no identifiable target file is denied (unscopeable)."""
    policy = gdrive_policy()
    result = policy(tc("mcp__google__drive_file_update", {"foo": "bar"}))
    assert result is not None and result["result"] == "DENY"


def test_drive_comment_on_created_file_allowed_else_denied() -> None:
    """Commenting is allowed on a created file, denied on a random one."""
    policy = gdrive_policy()
    allowed = policy(
        tc(
            "mcp__google__drive_comment_create",
            {"file_id": "1New"},
            {CREATED_FILES_STATE_KEY: ["1New"]},
        )
    )
    denied = policy(tc("mcp__google__drive_comment_create", {"file_id": "1Other"}))
    assert allowed is None
    assert denied is not None and denied["result"] == "DENY"


def test_drive_create_result_appends_file_id() -> None:
    """A create result (server ``{"result": <json-str>}``) appends the new ID."""
    result = gdrive_policy()(tr("mcp__google__docs_document_create", '{"documentId": "1New"}'))
    assert result is not None
    assert result["result"] == "ALLOW"
    assert result["state_updates"] == [
        {"key": CREATED_FILES_STATE_KEY, "action": "append", "value": "1New"}
    ]


def test_drive_create_result_depth_bounded() -> None:
    """A pathologically deep create-result payload is scanned without crashing.

    Failure (a ``RecursionError``) would mean a crafted MCP response with a
    deeply-nested structure could crash the policy engine. The shallow ID is
    still collected; an ID buried below the depth bound is simply not scanned
    (safe — not tracking a created file only makes later writes to it fail
    closed).
    """
    nested: Any = {"documentId": "buried-too-deep"}
    for _ in range(100):  # far beyond _MAX_RESULT_SCAN_DEPTH
        nested = {"child": nested}
    payload = json.dumps({"documentId": "shallow", "deep": nested})
    result = gdrive_policy()(tr("mcp__google__docs_document_create", payload))
    assert result is not None
    # Only the shallow ID is tracked; the deeply-buried one is beyond the
    # scan bound. Crucially, no RecursionError was raised.
    assert result["state_updates"] == [
        {"key": CREATED_FILES_STATE_KEY, "action": "append", "value": "shallow"}
    ]


def test_drive_create_result_dedupes_tracked_id() -> None:
    """An already-tracked created ID produces no redundant append."""
    result = gdrive_policy()(
        tr(
            "mcp__google__docs_document_create",
            '{"documentId": "1New"}',
            {CREATED_FILES_STATE_KEY: ["1New"]},
        )
    )
    assert result is None


def test_drive_unknown_tool_fails_closed() -> None:
    """An unrecognized Drive-namespaced tool is denied (fail closed)."""
    result = gdrive_policy()(tc("mcp__google__drive_file_trash", {"file_id": "1Q"}))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "tool",
    ["mcp__google__gmail_message_send", "mcp__google__calendar_event_create", "mcp__slack__post"],
)
def test_drive_abstains_on_non_drive_tools(tool: str) -> None:
    """Gmail, Calendar, and non-Google tools are abstained on (isolation)."""
    assert gdrive_policy(read_all=False)(tc(tool, {})) is None


@pytest.mark.asyncio
async def test_drive_resolve_from_spec() -> None:
    """gdrive_policy resolves and runs through ``resolve_function_policy``."""
    spec = FunctionPolicySpec(
        name="g",
        on=None,
        function=FunctionRef(
            path=_DRIVE_HANDLER, arguments={"read_all": False, "read_files": [_DOC_ID]}
        ),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    denied = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="mcp__google__docs_document_get",
            content={"name": "mcp__google__docs_document_get", "arguments": {"document_id": "no"}},
        ),
        {},
    )
    assert denied.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_drive_created_file_roundtrip(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A file created in one turn is writable in a later turn via persisted
    ``session_state``; an untracked file is not.

    Proves the created ID survives an engine rebuild because it was persisted to
    the store (the closure-based approach would fail this).
    """
    conv = conversation_store.create_conversation()

    engine1 = _engine(conversation_store, conv.id, {}, _DRIVE_HANDLER, {"allow_create": True})
    create_result = await engine1.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_RESULT,
            tool_name="mcp__google__docs_document_create",
            content={"result": '{"documentId": "1Created"}'},
        )
    )
    assert create_result.action == PolicyAction.ALLOW
    reloaded = conversation_store.get_conversation(conv.id)
    assert reloaded is not None
    assert reloaded.session_state.get(CREATED_FILES_STATE_KEY) == ["1Created"]

    engine2 = _engine(
        conversation_store,
        conv.id,
        dict(reloaded.session_state),
        _DRIVE_HANDLER,
        {"allow_create": True},
    )
    write_created = await engine2.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="mcp__google__drive_file_update",
            content={
                "name": "mcp__google__drive_file_update",
                "arguments": {"file_id": "1Created"},
            },
        )
    )
    assert write_created.action == PolicyAction.ALLOW
    write_foreign = await engine2.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="mcp__google__drive_file_update",
            content={
                "name": "mcp__google__drive_file_update",
                "arguments": {"file_id": "1Foreign"},
            },
        )
    )
    assert write_foreign.action == PolicyAction.DENY


# ══════════════════════════════════════════════════════════════════════════════
# gmail_policy
# ══════════════════════════════════════════════════════════════════════════════


def test_gmail_read_gated_by_allow_read() -> None:
    """Reading mail is allowed by default, denied when ``allow_read=False``."""
    assert gmail_policy(allow_read=True)(tc("mcp__google__gmail_search", {"q": "x"})) is None
    denied = gmail_policy(allow_read=False)(
        tc("mcp__google__gmail_message_get", {"message_id": "m"})
    )
    assert denied is not None and denied["result"] == "DENY"


def test_gmail_send_denied_by_default() -> None:
    """Sending mail is denied by default — the draft-but-don't-send guardrail.

    Failure means an agent could send email with no human in the loop.
    """
    denied = gmail_policy()(tc("mcp__google__gmail_message_send", {"to": "a@b.com"}))
    assert denied is not None and denied["result"] == "DENY"


def test_gmail_send_allowed_when_enabled() -> None:
    """``allow_send=True`` permits sending."""
    assert (
        gmail_policy(allow_send=True)(tc("mcp__google__gmail_message_send", {"to": "a@b.com"}))
        is None
    )


def test_gmail_draft_create_gated() -> None:
    """Draft creation is gated by ``allow_drafts`` (default on)."""
    assert gmail_policy(allow_drafts=True)(tc("mcp__google__gmail_draft_create", {})) is None
    denied = gmail_policy(allow_drafts=False)(tc("mcp__google__gmail_draft_create", {}))
    assert denied is not None and denied["result"] == "DENY"


def test_gmail_draft_edit_restricted_to_created() -> None:
    """Draft updates are allowed only for drafts created this session."""
    policy = gmail_policy()
    allowed = policy(
        tc(
            "mcp__google__gmail_draft_update",
            {"draft_id": "d1"},
            {CREATED_DRAFTS_STATE_KEY: ["d1"]},
        )
    )
    denied = policy(tc("mcp__google__gmail_draft_update", {"draft_id": "d2"}))
    assert allowed is None
    assert denied is not None and denied["result"] == "DENY"


def test_gmail_modify_denied_by_default() -> None:
    """Message/thread modification is denied by default, allowed when enabled."""
    denied = gmail_policy()(tc("mcp__google__gmail_message_modify", {"message_id": "m"}))
    assert denied is not None and denied["result"] == "DENY"
    assert (
        gmail_policy(allow_modify=True)(tc("mcp__google__gmail_thread_modify", {"thread_id": "t"}))
        is None
    )


def test_gmail_draft_create_result_appends_draft_id() -> None:
    """A draft-create result appends the new draft ID under the draft key."""
    result = gmail_policy()(tr("mcp__google__gmail_draft_create", '{"id": "d9"}'))
    assert result is not None
    assert result["result"] == "ALLOW"
    assert result["state_updates"] == [
        {"key": CREATED_DRAFTS_STATE_KEY, "action": "append", "value": "d9"}
    ]


def test_gmail_unknown_tool_fails_closed() -> None:
    """An unrecognized Gmail-namespaced tool is denied (fail closed)."""
    result = gmail_policy()(tc("mcp__google__gmail_settings_update", {}))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "tool",
    ["mcp__google__drive_file_get", "mcp__google__calendar_event_list", "mcp__slack__post"],
)
def test_gmail_abstains_on_non_gmail_tools(tool: str) -> None:
    """Drive, Calendar, and non-Google tools are abstained on (isolation)."""
    assert gmail_policy()(tc(tool, {})) is None


@pytest.mark.asyncio
async def test_gmail_resolve_from_spec() -> None:
    """gmail_policy resolves and runs through ``resolve_function_policy``."""
    spec = FunctionPolicySpec(
        name="g",
        on=None,
        function=FunctionRef(path=_GMAIL_HANDLER, arguments={"allow_send": False}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    denied = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="mcp__google__gmail_message_send",
            content={"name": "mcp__google__gmail_message_send", "arguments": {"to": "a@b.com"}},
        ),
        {},
    )
    assert denied.action == PolicyAction.DENY


@pytest.mark.asyncio
async def test_gmail_created_draft_roundtrip(
    conversation_store: SqlAlchemyConversationStore,
) -> None:
    """
    A draft created in one turn is editable in a later turn via persisted
    ``session_state``; an untracked draft is not.
    """
    conv = conversation_store.create_conversation()

    engine1 = _engine(conversation_store, conv.id, {}, _GMAIL_HANDLER, {"allow_drafts": True})
    create_result = await engine1.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_RESULT,
            tool_name="mcp__google__gmail_draft_create",
            content={"result": '{"id": "draft-1"}'},
        )
    )
    assert create_result.action == PolicyAction.ALLOW
    reloaded = conversation_store.get_conversation(conv.id)
    assert reloaded is not None
    assert reloaded.session_state.get(CREATED_DRAFTS_STATE_KEY) == ["draft-1"]

    engine2 = _engine(
        conversation_store,
        conv.id,
        dict(reloaded.session_state),
        _GMAIL_HANDLER,
        {"allow_drafts": True},
    )
    edit_own = await engine2.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="mcp__google__gmail_draft_update",
            content={
                "name": "mcp__google__gmail_draft_update",
                "arguments": {"draft_id": "draft-1"},
            },
        )
    )
    assert edit_own.action == PolicyAction.ALLOW
    edit_other = await engine2.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="mcp__google__gmail_draft_update",
            content={
                "name": "mcp__google__gmail_draft_update",
                "arguments": {"draft_id": "draft-9"},
            },
        )
    )
    assert edit_other.action == PolicyAction.DENY


# ══════════════════════════════════════════════════════════════════════════════
# gcalendar_policy
# ══════════════════════════════════════════════════════════════════════════════


def test_cal_read_gated_by_allow_read() -> None:
    """Reading the calendar is allowed by default, denied when off."""
    assert gcalendar_policy(allow_read=True)(tc("mcp__google__calendar_event_list", {})) is None
    denied = gcalendar_policy(allow_read=False)(
        tc("mcp__google__calendar_event_get", {"event_id": "e"})
    )
    assert denied is not None and denied["result"] == "DENY"


def test_cal_create_denied_by_default() -> None:
    """Event/calendar creation is denied by default (read-only posture)."""
    denied = gcalendar_policy()(tc("mcp__google__calendar_event_create", {"summary": "Mtg"}))
    assert denied is not None and denied["result"] == "DENY"


def test_cal_create_allowed_when_enabled() -> None:
    """``allow_create_events=True`` permits event creation."""
    assert (
        gcalendar_policy(allow_create_events=True)(
            tc("mcp__google__calendar_event_create", {"summary": "M"})
        )
        is None
    )


@pytest.mark.parametrize("tool", ["calendar_event_update", "calendar_event_delete"])
def test_cal_modify_gated_by_allow_modify(tool: str) -> None:
    """Updating / deleting events is denied by default, allowed when enabled."""
    denied = gcalendar_policy()(tc(f"mcp__google__{tool}", {"event_id": "e"}))
    assert denied is not None and denied["result"] == "DENY"
    assert (
        gcalendar_policy(allow_modify_events=True)(tc(f"mcp__google__{tool}", {"event_id": "e"}))
        is None
    )


def test_cal_unknown_tool_fails_closed() -> None:
    """An unrecognized Calendar-namespaced tool is denied (fail closed)."""
    result = gcalendar_policy()(tc("mcp__google__calendar_acl_insert", {}))
    assert result is not None and result["result"] == "DENY"


@pytest.mark.parametrize(
    "tool",
    ["mcp__google__drive_file_get", "mcp__google__gmail_search", "mcp__slack__post"],
)
def test_cal_abstains_on_non_calendar_tools(tool: str) -> None:
    """Drive, Gmail, and non-Google tools are abstained on (isolation)."""
    assert gcalendar_policy(allow_read=False)(tc(tool, {})) is None


@pytest.mark.asyncio
async def test_cal_resolve_from_spec() -> None:
    """gcalendar_policy resolves and runs through ``resolve_function_policy``."""
    spec = FunctionPolicySpec(
        name="g",
        on=None,
        function=FunctionRef(path=_CAL_HANDLER, arguments={"allow_create_events": False}),
    )
    policy: FunctionPolicy = resolve_function_policy(spec)
    denied = await policy.evaluate(
        EvaluationContext(
            phase=Phase.TOOL_CALL,
            tool_name="mcp__google__calendar_event_create",
            content={"name": "mcp__google__calendar_event_create", "arguments": {"summary": "x"}},
        ),
        {},
    )
    assert denied.action == PolicyAction.DENY


# ══════════════════════════════════════════════════════════════════════════════
# Registry — one POLICY_REGISTRY, three factory entries
# ══════════════════════════════════════════════════════════════════════════════


@pytest.mark.parametrize("handler", [_DRIVE_HANDLER, _GMAIL_HANDLER, _CAL_HANDLER])
def test_registry_discovers_all_three(handler: str) -> None:
    """All three google policies are discovered as factory entries.

    Failure means a policy is not browsable via GET /v1/policy-registry and its
    params won't be validated on attach.
    """
    load_registry()
    by_handler = {e.handler: e for e in get_registry()}
    assert handler in by_handler
    assert by_handler[handler].kind == "factory"
    assert by_handler[handler].params_schema is not None


@pytest.mark.parametrize(
    "handler,good,bad_key,bad_type",
    [
        (
            _DRIVE_HANDLER,
            {"read_all": False, "read_files": [_DOC_ID]},
            {"bogus": 1},
            {"read_all": "yes"},
        ),
        (_GMAIL_HANDLER, {"allow_send": True}, {"bogus": 1}, {"allow_send": "nope"}),
        (_CAL_HANDLER, {"allow_read": True}, {"bogus": 1}, {"allow_read": "nope"}),
    ],
)
def test_registry_validates_factory_params(
    handler: str,
    good: dict[str, Any],
    bad_key: dict[str, Any],
    bad_type: dict[str, Any],
) -> None:
    """Each schema accepts valid params and rejects unknown keys / wrong types."""
    load_registry()
    assert validate_factory_params(handler, good) is None
    err_unknown = validate_factory_params(handler, bad_key)
    assert err_unknown is not None and "bogus" in err_unknown
    assert validate_factory_params(handler, bad_type) is not None
