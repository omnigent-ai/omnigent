"""Tests for the typed SSE event payloads.

Each event model wraps a wire shape that the legacy raw-dict emit
sites in workflow.py / approval.py / responses.py produce today.
The tests below verify (a) the typed model constructs and dumps to
the same shape the legacy emits use, (b) the discriminated union
dispatches by ``type``, (c) loose-by-default ``extra="ignore"``
forward compatibility, and (d) MCP-style ``extra="allow"`` on the
elicitation params block.

The event models live in :mod:`omnigent.server.schemas`;
this module only references the request/response schemas in
:mod:`omnigent.server.schemas` for the embedded ``ResponseObject``.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import TypeAdapter, ValidationError

from omnigent.server.schemas import (
    CancelledEvent,
    CompletedEvent,
    CreatedEvent,
    CreateResponseRequest,
    ElicitationRequestEvent,
    ElicitationRequestParams,
    FailedEvent,
    HeartbeatEvent,
    IncompleteEvent,
    InProgressEvent,
    OutputItemDoneEvent,
    OutputTextDeltaEvent,
    QueuedEvent,
    ReasoningStartedEvent,
    ReasoningSummaryTextDeltaEvent,
    ReasoningTextDeltaEvent,
    ResponseObject,
    ServerStreamEvent,
    SessionHeartbeatEvent,
)

# ── Round-trip serialization ──────────────────────────────────


def test_output_text_delta_roundtrip() -> None:
    """OutputTextDeltaEvent dumps exactly the legacy raw-dict shape."""
    event = OutputTextDeltaEvent(type="response.output_text.delta", delta="Hello")
    # exclude_none drops the unset sequence_number — matches the
    # producer-side raw dict at workflow.py:818.
    assert event.model_dump(exclude_none=True) == {
        "type": "response.output_text.delta",
        "delta": "Hello",
    }


def test_reasoning_started_roundtrip() -> None:
    """ReasoningStartedEvent has no fields beyond type."""
    event = ReasoningStartedEvent(type="response.reasoning.started")
    assert event.model_dump(exclude_none=True) == {
        "type": "response.reasoning.started",
    }


def test_reasoning_text_delta_roundtrip() -> None:
    event = ReasoningTextDeltaEvent(type="response.reasoning_text.delta", delta="Considering")
    assert event.model_dump(exclude_none=True) == {
        "type": "response.reasoning_text.delta",
        "delta": "Considering",
    }


def test_reasoning_summary_text_delta_roundtrip() -> None:
    event = ReasoningSummaryTextDeltaEvent(
        type="response.reasoning_summary_text.delta", delta="Will use search"
    )
    assert event.model_dump(exclude_none=True) == {
        "type": "response.reasoning_summary_text.delta",
        "delta": "Will use search",
    }


def test_output_item_done_roundtrip() -> None:
    """OutputItemDoneEvent passes the inner item dict through verbatim."""
    item = {
        "id": "fc_abc123",
        "type": "function_call",
        "status": "action_required",
        "name": "search.web",
        "arguments": '{"q": "foo"}',
        "call_id": "call_xyz",
    }
    event = OutputItemDoneEvent(type="response.output_item.done", item=item)
    assert event.model_dump(exclude_none=True) == {
        "type": "response.output_item.done",
        "item": item,
    }


def test_heartbeat_roundtrip_minimal() -> None:
    """
    HeartbeatEvent without timing metadata round-trips to just the
    type — older AP→harness pairs that pre-date the field addition
    must keep parsing cleanly.

    What breaks if this fails: a harness that emits the legacy
    ``HeartbeatEvent(type="response.heartbeat")`` shape would
    fail validation at the AP-side consumer, killing every turn
    those harnesses run.
    """
    event = HeartbeatEvent(type="response.heartbeat")
    # exclude_none drops the timing fields — wire stays minimal
    # for legacy emitters.
    assert event.model_dump(exclude_none=True) == {
        "type": "response.heartbeat",
    }
    assert event.server_time is None
    assert event.last_event_seq is None


def test_heartbeat_roundtrip_with_timing_metadata() -> None:
    """
    HeartbeatEvent populated with ``server_time`` and
    ``last_event_seq`` round-trips both fields verbatim per
    contract §Heartbeats.

    What breaks if this fails: consumers can't detect clock
    drift (no server_time) or dropped events (no
    last_event_seq), regressing the contract's dead-detection
    promise.
    """
    event = HeartbeatEvent(
        type="response.heartbeat",
        server_time="2026-04-27T15:30:00Z",
        last_event_seq=42,
    )
    assert event.model_dump(exclude_none=True) == {
        "type": "response.heartbeat",
        "server_time": "2026-04-27T15:30:00Z",
        "last_event_seq": 42,
    }
    # Round-trip through JSON to guarantee the wire shape
    # matches the contract — pydantic's serialization could
    # silently rename these on a future bump and only a JSON
    # round-trip would catch it.
    raw = event.model_dump_json(exclude_none=True)
    assert '"server_time":"2026-04-27T15:30:00Z"' in raw
    assert '"last_event_seq":42' in raw


def test_session_heartbeat_roundtrip_minimal() -> None:
    """
    SessionHeartbeatEvent without server_time round-trips to just the type.

    What breaks if this fails: the session-stream route emits the
    minimal ``{"type": "session.heartbeat"}`` shape from the
    pub-sub layer on idle, and the SDK / external consumers must
    parse it cleanly. A regression here makes idle-stream keepalive
    fail validation at the route's wire boundary.
    """
    event = SessionHeartbeatEvent(type="session.heartbeat")
    assert event.model_dump(exclude_none=True) == {
        "type": "session.heartbeat",
    }
    assert event.server_time is None


def test_elicitation_request_roundtrip() -> None:
    """ElicitationRequestEvent matches approval.py:175 wire shape verbatim."""
    params = ElicitationRequestParams(
        mode="form",
        message="Approve running 'rm -rf /tmp/cache'?",
        requestedSchema={
            "type": "object",
            "properties": {"approve": {"type": "boolean"}},
        },
        phase="pre_tool_use",
        policy_name="approve_shell_commands",
        content_preview="rm -rf /tmp/cache",
    )
    event = ElicitationRequestEvent(
        type="response.elicitation_request",
        elicitation_id="elicit_abc123",
        params=params,
    )
    dumped = event.model_dump(exclude_none=True)
    # method is the MCP standard literal — kept default-on so producers
    # don't have to re-state it but parsers see it explicitly.
    assert dumped == {
        "type": "response.elicitation_request",
        "elicitation_id": "elicit_abc123",
        "method": "elicitation/create",
        "params": {
            "mode": "form",
            "message": "Approve running 'rm -rf /tmp/cache'?",
            "requestedSchema": {
                "type": "object",
                "properties": {"approve": {"type": "boolean"}},
            },
            "phase": "pre_tool_use",
            "policy_name": "approve_shell_commands",
            "content_preview": "rm -rf /tmp/cache",
        },
    }


@pytest.fixture
def sample_response_object() -> ResponseObject:
    """Minimal ResponseObject for terminal-event tests."""
    return ResponseObject(
        id="resp_abc123",
        status="completed",
        model="research-agent",
        created_at=1234567890,
    )


@pytest.mark.parametrize(
    "event_class,type_literal,status_value",
    [
        # Initial events
        (CreatedEvent, "response.created", "queued"),
        (QueuedEvent, "response.queued", "queued"),
        (InProgressEvent, "response.in_progress", "in_progress"),
        # Terminal events
        (CompletedEvent, "response.completed", "completed"),
        (FailedEvent, "response.failed", "failed"),
        (CancelledEvent, "response.cancelled", "cancelled"),
        (IncompleteEvent, "response.incomplete", "incomplete"),
    ],
)
def test_response_envelope_event_carries_real_response_object(
    event_class: type,
    type_literal: str,
    status_value: str,
    sample_response_object: ResponseObject,
) -> None:
    """Initial + terminal envelope events embed a real ResponseObject.

    Covers both initial (created / queued / in_progress) and
    terminal (completed / failed / cancelled / incomplete) event
    variants — they all wrap a :class:`ResponseObject`. Catches a
    regression where the ``response`` field was loosened to
    ``dict[str, Any]`` — the typed contract requires
    :class:`ResponseObject` so consumers can read structured fields
    without a second parse step.
    """
    response = sample_response_object.model_copy(update={"status": status_value})
    event = event_class(type=type_literal, response=response)
    # response field must be a real ResponseObject, not a dict — so
    # downstream consumers see typed field access.
    assert isinstance(event.response, ResponseObject)
    assert event.response.status == status_value
    # model_dump produces the canonical wire shape with response nested.
    dumped = event.model_dump(exclude_none=True)
    assert dumped["type"] == type_literal
    assert dumped["response"]["id"] == "resp_abc123"
    assert dumped["response"]["status"] == status_value


# ── Discriminated union dispatch ──────────────────────────────


@pytest.mark.parametrize(
    "raw_dict,expected_class",
    [
        (
            {"type": "response.output_text.delta", "delta": "x"},
            OutputTextDeltaEvent,
        ),
        ({"type": "response.reasoning.started"}, ReasoningStartedEvent),
        (
            {"type": "response.reasoning_text.delta", "delta": "x"},
            ReasoningTextDeltaEvent,
        ),
        (
            {"type": "response.reasoning_summary_text.delta", "delta": "x"},
            ReasoningSummaryTextDeltaEvent,
        ),
        (
            {"type": "response.output_item.done", "item": {"id": "x"}},
            OutputItemDoneEvent,
        ),
        ({"type": "response.heartbeat"}, HeartbeatEvent),
        ({"type": "session.heartbeat"}, SessionHeartbeatEvent),
        (
            {
                "type": "response.elicitation_request",
                "elicitation_id": "e1",
                "params": {"message": "ok?"},
            },
            ElicitationRequestEvent,
        ),
    ],
)
def test_response_stream_event_dispatches_to_concrete_class(
    raw_dict: dict[str, Any], expected_class: type
) -> None:
    """TypeAdapter routes raw dicts to the right typed model via type."""
    adapter: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)
    parsed = adapter.validate_python(raw_dict)
    # type(parsed) must equal expected_class — if the discriminator
    # changes (or a variant gets removed from the union), this flips
    # to a different class or raises.
    assert type(parsed) is expected_class


@pytest.mark.parametrize(
    "type_literal,expected_class,status_value",
    [
        # Initial events
        ("response.created", CreatedEvent, "queued"),
        ("response.queued", QueuedEvent, "queued"),
        ("response.in_progress", InProgressEvent, "in_progress"),
        # Terminal events
        ("response.completed", CompletedEvent, "completed"),
        ("response.failed", FailedEvent, "failed"),
        ("response.cancelled", CancelledEvent, "cancelled"),
        ("response.incomplete", IncompleteEvent, "incomplete"),
    ],
)
def test_response_stream_event_dispatches_envelope_events(
    type_literal: str,
    expected_class: type,
    status_value: str,
    sample_response_object: ResponseObject,
) -> None:
    """Each envelope event variant dispatches to its concrete class.

    Covers initial (created / queued / in_progress) and terminal
    (completed / failed / cancelled / incomplete) — they all share
    the same wire shape (``{"type", "response"}``) and only the
    type discriminator distinguishes them. Catches a regression
    where any envelope variant gets removed from the union (the
    dispatch flips to ValidationError) or its discriminator
    literal drifts (the dispatch picks the wrong class).
    """
    response = sample_response_object.model_copy(update={"status": status_value})
    adapter: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)
    raw = {
        "type": type_literal,
        "response": response.model_dump(),
    }
    parsed = adapter.validate_python(raw)
    assert type(parsed) is expected_class
    # Narrow ``parsed`` to the 7-class envelope union so mypy sees
    # the .response field; the type(parsed) check above proves the
    # exact variant.
    assert isinstance(
        parsed,
        (
            CreatedEvent,
            QueuedEvent,
            InProgressEvent,
            CompletedEvent,
            FailedEvent,
            CancelledEvent,
            IncompleteEvent,
        ),
    )
    # response field round-trips back to a ResponseObject — proves
    # the union didn't downgrade it to a dict.
    assert isinstance(parsed.response, ResponseObject)
    assert parsed.response.id == "resp_abc123"
    assert parsed.response.status == status_value


def test_response_stream_event_rejects_unknown_type() -> None:
    """Discriminated union fails loud on type values it doesn't know.

    A ``response.something_invented`` event indicates either a typo on
    the producer side or a contract version skew where the consumer
    is older than the producer. Either way, fail loud rather than
    silently dropping the event.
    """
    adapter: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)
    with pytest.raises(ValidationError):
        adapter.validate_python({"type": "response.something_invented"})


# ── Required-field validation ─────────────────────────────────


@pytest.mark.parametrize(
    "event_class,kwargs",
    [
        # delta is required for the text/reasoning delta events —
        # producing one of these without text means the producer is
        # broken; fail loud at parse time.
        (OutputTextDeltaEvent, {"type": "response.output_text.delta"}),
        (
            ReasoningTextDeltaEvent,
            {"type": "response.reasoning_text.delta"},
        ),
        (
            ReasoningSummaryTextDeltaEvent,
            {"type": "response.reasoning_summary_text.delta"},
        ),
        # item is required for output_item.done — without it, consumers
        # have nothing to render.
        (OutputItemDoneEvent, {"type": "response.output_item.done"}),
        # ElicitationRequestEvent needs both correlation id and params
        # (consumers can't reply without the id; can't render without
        # the params).
        (
            ElicitationRequestEvent,
            {
                "type": "response.elicitation_request",
                "elicitation_id": "e1",
            },
        ),
        (
            ElicitationRequestEvent,
            {
                "type": "response.elicitation_request",
                "params": {"message": "?"},
            },
        ),
        # Terminal events require a response object — that's the
        # entire payload, not just a nice-to-have.
        (CompletedEvent, {"type": "response.completed"}),
        (FailedEvent, {"type": "response.failed"}),
    ],
)
def test_event_missing_required_field_fails_loud(
    event_class: type, kwargs: dict[str, Any]
) -> None:
    """Each event raises ValidationError on a missing required field.

    Catches regressions where a load-bearing field gets a default of
    ``None`` (which would silently accept malformed events). Per the
    contract's loose-by-default discipline, fields the receiver
    actually uses MUST be required.
    """
    with pytest.raises(ValidationError):
        event_class(**kwargs)


def test_elicitation_request_params_requires_message() -> None:
    """The MCP-standard ``message`` field is required (no implicit empty)."""
    with pytest.raises(ValidationError):
        ElicitationRequestParams()  # type: ignore[call-arg]


# ── Loose-by-default forward compatibility ────────────────────


@pytest.mark.parametrize(
    "event_class,base_kwargs",
    [
        (
            OutputTextDeltaEvent,
            {"type": "response.output_text.delta", "delta": "x"},
        ),
        (HeartbeatEvent, {"type": "response.heartbeat"}),
        (
            OutputItemDoneEvent,
            {"type": "response.output_item.done", "item": {"id": "i"}},
        ),
    ],
)
def test_event_silently_drops_unknown_fields(
    event_class: type, base_kwargs: dict[str, Any]
) -> None:
    """Forward-compat: unknown top-level fields are silently dropped.

    This is the v1 validation discipline (extra="ignore" on event
    models) — newer producers can add fields without breaking older
    parsers. Required for the contract's "version skew doesn't
    break harnesses" guarantee.
    """
    parsed = event_class(**base_kwargs, future_field="surprise")
    dumped = parsed.model_dump(exclude_none=True)
    # The unknown field is dropped — not preserved, not raised.
    assert "future_field" not in dumped


def test_elicitation_request_params_preserves_unknown_fields() -> None:
    """ElicitationRequestParams uses extra="allow" to mirror MCP.

    MCP's ElicitRequestParams allows arbitrary extras under params
    (the spec uses extra="allow") so MCP servers can attach context.
    Our params model preserves the same behavior so an MCP server's
    elicitation/create call traversing harness → Omnigent → client doesn't
    lose fields the MCP server attached.
    """
    params = ElicitationRequestParams(
        message="hi",
        # mcp_specific_field is NOT declared on the model — extra="allow"
        # should preserve it through model_dump.
        mcp_specific_field="server-defined-context",  # type: ignore[call-arg]
    )
    dumped = params.model_dump(exclude_none=True)
    # If extra were "ignore", this would fail (field stripped).
    assert dumped["mcp_specific_field"] == "server-defined-context"


# ── Sequence-number ambient field ─────────────────────────────


def test_sequence_number_defaults_to_none() -> None:
    """Producers leave sequence_number unset; AP's serializer assigns it.

    Verifies the producer-side contract — the field exists for
    consumers but doesn't burden producers with assigning it.
    """
    event = HeartbeatEvent(type="response.heartbeat")
    assert event.sequence_number is None
    # exclude_none drops it from the wire shape on the producer side
    # (matches the legacy raw-dict emit behavior).
    assert "sequence_number" not in event.model_dump(exclude_none=True)


def test_sequence_number_included_when_assigned() -> None:
    """Once assigned, sequence_number rides on the wire shape."""
    event = HeartbeatEvent(type="response.heartbeat", sequence_number=42)
    dumped = event.model_dump(exclude_none=True)
    assert dumped["sequence_number"] == 42


# ── CreateResponseRequest model validator ─────────────────────


def test_create_response_request_model_required_for_fresh_conversation() -> None:
    """
    model=None without previous_response_id raises ValidationError (→ 422).

    The validator enforces that fresh conversations must name an agent.
    Without it, downstream code would fail deep inside the handler with
    an obscure error rather than a clean 422 at the API boundary.

    What breaks if this fails: a caller can POST ``{input: "hi"}`` with
    no model, bypass Pydantic, and reach the route handler with
    ``req.model=None`` — the handler then has no agent to resolve and
    raises a 400 runtime error or hits a None-dereference.
    """
    with pytest.raises(ValidationError):
        CreateResponseRequest(input="hi")


@pytest.mark.parametrize(
    "kwargs",
    [
        # Fresh conversation with explicit model — the normal path.
        {"input": "hi", "model": "my-agent"},
        # Continuation turn: model omitted because server resolves agent
        # from the prior task. This is the idle-injection path.
        {"input": "hi", "previous_response_id": "resp_abc123"},
        # Continuation turn with model explicitly repeated — also valid.
        {
            "input": "hi",
            "model": "my-agent",
            "previous_response_id": "resp_abc123",
        },
    ],
)
def test_create_response_request_model_validator_accepts_valid_payloads(
    kwargs: dict[str, Any],
) -> None:
    """
    CreateResponseRequest constructs without error for all valid payload
    shapes: fresh turn with model, continuation without model, and
    continuation with model repeated.

    What breaks if this fails: valid client requests are rejected with
    422 — either fresh turns fail because model is treated as always
    required, or continuation turns fail because model=None is always
    rejected.
    """
    req = CreateResponseRequest(**kwargs)
    assert req.input == "hi"
    # model and previous_response_id carry through verbatim — Pydantic
    # must not strip or coerce either field during validation.
    if "model" in kwargs:
        assert req.model == kwargs["model"]
    if "previous_response_id" in kwargs:
        assert req.previous_response_id == kwargs["previous_response_id"]


def test_session_create_git_requires_host_id() -> None:
    """``git`` without ``host_id`` is rejected at validation (422).

    Worktree creation needs a host; failing in the model means the
    route returns 422 instead of reaching the worktree path and
    failing late. If this validator is dropped, the request would
    validate and the error would surface deeper in the create flow.
    """
    from omnigent.server.schemas import SessionCreateRequest, SessionGitOptions

    with pytest.raises(ValidationError, match="git worktree creation requires host_id"):
        SessionCreateRequest(
            agent_id="ag_x",
            git=SessionGitOptions(branch_name="feature/x"),
        )


def test_session_create_git_with_host_id_ok() -> None:
    """``git`` with ``host_id`` validates cleanly."""
    from omnigent.server.schemas import SessionCreateRequest, SessionGitOptions

    req = SessionCreateRequest(
        agent_id="ag_x",
        host_id="host_abc",
        workspace="/repo",
        git=SessionGitOptions(branch_name="feature/x"),
    )
    assert req.git is not None
    assert req.git.branch_name == "feature/x"


def test_session_create_host_type_defaults_external() -> None:
    """
    ``host_type`` defaults to ``"external"`` — the pre-existing
    contract for every client that doesn't send the field (backcompat).
    """
    from omnigent.server.schemas import SessionCreateRequest

    req = SessionCreateRequest(agent_id="ag_x")
    assert req.host_type == "external"


def test_session_create_managed_rejects_host_id() -> None:
    """
    ``host_type="managed"`` + caller-supplied ``host_id`` is a
    contradiction (the server provisions the host) — must 422 at
    validation instead of silently ignoring the caller's host.
    """
    from omnigent.server.schemas import SessionCreateRequest

    with pytest.raises(ValidationError, match="host_id must not be set"):
        SessionCreateRequest(agent_id="ag_x", host_type="managed", host_id="host_abc")


def test_session_create_managed_rejects_path_workspace() -> None:
    """
    ``host_type="managed"`` + a PATH workspace is a contradiction —
    the sandbox doesn't exist yet, so there is no filesystem to point
    at. Managed workspaces are repository URLs; must 422 at
    validation with the URL form named.
    """
    from omnigent.server.schemas import SessionCreateRequest

    with pytest.raises(ValidationError, match="takes a git repository URL"):
        SessionCreateRequest(agent_id="ag_x", host_type="managed", workspace="/tmp/w")


@pytest.mark.parametrize(
    "workspace",
    [
        "https://github.com/org/repo",
        "https://github.com/org/repo.git#release-1.2",
        "git@github.com:org/repo.git",
    ],
)
def test_session_create_managed_accepts_repo_url_workspace(workspace: str) -> None:
    """
    ``host_type="managed"`` accepts the ``<repo>[#<branch>]`` workspace
    forms — the value passes through verbatim for the launch path to
    parse and clone.
    """
    from omnigent.server.schemas import SessionCreateRequest

    req = SessionCreateRequest(agent_id="ag_x", host_type="managed", workspace=workspace)
    assert req.workspace == workspace


@pytest.mark.parametrize(
    ("workspace", "expected_fragment"),
    [
        # Commit SHA fragment → detached HEAD; the message routes the
        # caller toward branches.
        ("https://github.com/org/repo#" + "a" * 40, "not a commit SHA"),
        # Empty fragment.
        ("https://github.com/org/repo#", "must name a branch"),
        # Bare shorthand is UI sugar, not API surface.
        ("org/repo", "not a supported repository URL"),
    ],
)
def test_session_create_managed_rejects_malformed_repo_workspace(
    workspace: str, expected_fragment: str
) -> None:
    """
    Malformed repository workspaces 422 at validation (with the parse
    error embedded) instead of failing mid-provision inside a
    half-launched sandbox.
    """
    from omnigent.server.schemas import SessionCreateRequest

    with pytest.raises(ValidationError, match="") as exc:
        SessionCreateRequest(agent_id="ag_x", host_type="managed", workspace=workspace)
    assert expected_fragment in str(exc.value)


def test_session_create_external_rejects_repo_url_workspace() -> None:
    """
    A repository-URL workspace on an EXTERNAL host is rejected: there,
    ``workspace`` is an absolute path on the host — silently treating
    the URL as a path would fail later in workspace validation with a
    confusing "no such directory".
    """
    from omnigent.server.schemas import SessionCreateRequest

    with pytest.raises(ValidationError, match="requires host_type 'managed'"):
        SessionCreateRequest(
            agent_id="ag_x",
            host_id="host_abc",
            workspace="https://github.com/org/repo",
        )


@pytest.mark.parametrize("status", ["idle", "running", "waiting", "failed"])
def test_session_response_status_accepts_canonical_set(status: str) -> None:
    """
    ``SessionResponse.status`` accepts the full canonical lifecycle set,
    including ``"waiting"``.

    The wire ``session.status`` event already models ``"waiting"`` (a turn
    parked on background work / sub-agents). The REST snapshot collapses
    ``"waiting"`` -> ``"running"`` on every current read path, so the value
    does not normally reach this model — but a server >= 0.3.0 is documented
    (``server/API.md``) to serialize the canonical set, and keeping the
    Literal a strict subset means any future or alternate-backend path that
    forwards the raw status would 500 on serialization. Widening keeps the
    model a superset of what the runtime can produce.
    """
    from omnigent.server.schemas import SessionResponse

    resp = SessionResponse(id="conv_x", agent_id="ag_x", status=status, created_at=0)
    assert resp.status == status
    assert resp.model_dump()["status"] == status


@pytest.mark.parametrize("status", ["idle", "running", "waiting", "failed"])
def test_session_list_item_status_accepts_canonical_set(status: str) -> None:
    """``SessionListItem.status`` accepts the same canonical set as the snapshot."""
    from omnigent.server.schemas import SessionListItem

    item = SessionListItem(id="conv_x", agent_id="ag_x", status=status, created_at=0, updated_at=0)
    assert item.status == status


def test_session_response_status_rejects_unknown_value() -> None:
    """A status outside the canonical set still fails loud (fail-closed wire shape)."""
    from omnigent.server.schemas import SessionResponse

    with pytest.raises(ValidationError):
        SessionResponse(id="conv_x", agent_id="ag_x", status="launching", created_at=0)
