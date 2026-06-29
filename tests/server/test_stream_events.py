"""Coverage for the SSE stream-event single source of truth.

Two flavors live here:

1. **Direct unit tests on the union (Part 1)** — fast, pure-Python
   invariants over :data:`omnigent.server.schemas.ServerStreamEvent`.
   They guard the structural shape of the SoT (uniqueness of wire
   types, predicate correctness, every emit-site wire name is in the
   union).

2. **OpenAPI surface (Part 2)** — loads ``openapi.json`` (generated
   by ``scripts/dump_openapi.py``) and verifies the two SSE routes
   surface ``text/event-stream`` content with a schema reference to
   ``ServerStreamEvent``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import TypeAdapter, ValidationError

from omnigent.server.schemas import (
    ServerStreamEvent,
    SessionCreatedEvent,
    SessionModelOptionsEvent,
    SessionSkillsEvent,
    SessionStatusEvent,
    is_known_event,
)

# Module-level adapter — TypeAdapter caches the validator so each
# union ``validate_python`` call has no per-call setup cost.
_ADAPTER: TypeAdapter[ServerStreamEvent] = TypeAdapter(ServerStreamEvent)


# ── Part 1: Direct union unit tests ──────────────────────────────


def test_every_event_class_has_unique_wire_type() -> None:
    """Every event class in the union maps to a distinct wire type."""
    # Pull every variant's ``type`` Literal out of the union.
    # ``ServerStreamEvent`` is ``Annotated[Union[...], Field(...)]``;
    # ``__args__[0]`` is the Union, ``__args__`` of which is the
    # tuple of variant types.
    union = ServerStreamEvent.__args__[0]  # type: ignore[attr-defined]
    types = [
        cls.model_fields["type"].annotation.__args__[0]  # type: ignore[union-attr]
        for cls in union.__args__
    ]
    duplicates = [t for t in types if types.count(t) > 1]
    # A duplicate wire type would mean Pydantic can't dispatch
    # by ``type`` to a single concrete class — the discriminator
    # would refuse the union at class build time, so this test
    # is also a sanity check that Pydantic's construction
    # succeeded.
    assert len(types) == len(set(types)), (
        f"Duplicate wire types in ServerStreamEvent union: {duplicates}. "
        f"Two variants pinning the same Literal would prevent the "
        f"discriminator from dispatching."
    )


def test_is_known_event_accepts_every_union_variant() -> None:
    """is_known_event returns True for every wire type in the union."""
    union = ServerStreamEvent.__args__[0]  # type: ignore[attr-defined]
    for cls in union.__args__:
        wire = cls.model_fields["type"].annotation.__args__[0]  # type: ignore[union-attr]
        # If this fails, ``_KNOWN_EVENT_TYPES`` is out of sync with
        # the union body — ``is_known_event`` would false-positive
        # on legitimate events.
        assert is_known_event(wire), (
            f"is_known_event rejected a defined variant: {wire!r} "
            f"(class {cls.__name__}). _KNOWN_EVENT_TYPES drifted from "
            f"the union."
        )


def test_is_known_event_rejects_unknown_strings() -> None:
    """is_known_event returns False for arbitrary strings."""
    # A True return on arbitrary input would silently accept a
    # truly-drifted emission as a known event.
    assert not is_known_event("not.a.real.event")
    # ``response.output_text`` is close-but-wrong: real wire name
    # is ``response.output_text.delta``. Catches accidental prefix
    # matching.
    assert not is_known_event("response.output_text")
    # Empty string — guards against the antipattern of treating
    # ``""`` as a sentinel for "no type" (project rule 19).
    assert not is_known_event("")


def test_emit_sites_referenced_by_grep_are_all_in_the_union() -> None:
    """
    Every wire-name string literal AP/runtime emits via
    ``session_stream.publish`` is in the union.

    This is a coverage audit: we walk a fixed list of source files
    and verify every ``"type": "<...>"`` literal is a known wire
    name. If a future emit site introduces a new literal without
    registering its event class, this test catches the addition
    statically (no workflow needed).

    Production breakage that causes this test to fail: someone
    publishes ``{"type": "response.something_new", ...}`` without
    adding ``SomethingNewEvent`` to the union.
    """
    import re

    # Scope: every file that publishes onto the SSE stream. Each
    # routes its events through the SSE serializer in
    # routes/sessions.py.
    repo_root = Path(__file__).resolve().parent.parent.parent
    paths = [
        repo_root / "omnigent/runtime/workflow.py",
        repo_root / "omnigent/runtime/compaction.py",
        repo_root / "omnigent/runtime/policies/approval.py",
        repo_root / "omnigent/runtime/llm_retry.py",
        repo_root / "omnigent/runtime/tool_retry.py",
        repo_root / "omnigent/server/routes/sessions.py",
    ]
    pattern = re.compile(r'"type":\s*"(response\.[^"]+|session\.[^"]+)"')
    found: set[str] = set()
    for p in paths:
        if not p.exists():
            continue
        for match in pattern.finditer(p.read_text()):
            found.add(match.group(1))

    unknown = {wire for wire in found if not is_known_event(wire)}
    assert not unknown, (
        f"Wire-name string literals emitted by the runtime/server "
        f"that are NOT registered in ServerStreamEvent: "
        f"{sorted(unknown)}. Either add a typed event subclass to "
        f"omnigent.server.schemas and include it in the "
        f"union, or remove the offending emit site."
    )


# ── Part 2: OpenAPI surface ────────────────────────────────────────


def test_openapi_json_surfaces_sse_routes_with_typed_schema() -> None:
    """openapi.json declares text/event-stream + ServerStreamEvent on SSE routes.

    The spec is generated by ``scripts/dump_openapi.py``; a missing
    file means a developer changed routes/schemas without
    regenerating. We don't run the script in this test (CI would
    re-run it via ``--check``); we only verify the on-disk file
    matches the contract.

    What breaks if this fails: SDK codegen / interactive docs would
    no longer see the SSE event union, regressing the wire contract
    visibility.
    """
    repo_root = Path(__file__).resolve().parent.parent.parent
    openapi_path = repo_root / "openapi.json"
    if not openapi_path.exists():
        pytest.skip(
            "openapi.json not generated yet — run "
            "`scripts/dump_openapi.py` before running this test.",
        )
    spec = json.loads(openapi_path.read_text())
    # OpenAPI 3.2 — ``scripts/dump_openapi.py`` post-processes the
    # FastAPI-emitted 3.1 doc to bump the version + inject the SSE
    # ``itemSchema`` shape. If the version regressed, the SSE
    # ``itemSchema`` keyword wouldn't be valid OAS for the parser.
    assert spec["openapi"].startswith("3.2"), (
        f"openapi.json version is {spec['openapi']!r}; expected 3.2.x. "
        f"The dump script must bump the version when post-processing."
    )

    # The session-keyed SSE route is the canonical surface we
    # maintain typed OpenAPI for. The legacy ``POST /v1/responses``
    # streaming endpoint is intentionally NOT in this list — we are
    # not maintaining accurate OpenAPI docs for the
    # ``/v1/responses`` family since it's being deprecated by the
    # ``/v1/sessions`` migration. See ``scripts/dump_openapi.py``'s
    # ``_SSE_ROUTES`` list (the source of truth driving generation).
    sse_paths = ["/v1/sessions/{session_id}/stream"]
    methods = {"/v1/sessions/{session_id}/stream": "get"}
    for path in sse_paths:
        op = spec["paths"][path][methods[path]]
        content = op["responses"]["200"]["content"]
        assert "text/event-stream" in content, (
            f"{methods[path].upper()} {path}: response 200 is missing "
            f"text/event-stream content. The route's "
            f"``responses=`` decorator argument may have regressed."
        )
        # OpenAPI 3.2's per-event keyword for sequential media
        # types is ``itemSchema``. The dump script rewrites the
        # FastAPI-emitted 3.1 ``schema`` field to ``itemSchema``
        # for SSE responses.
        sse_entry = content["text/event-stream"]
        assert "itemSchema" in sse_entry, (
            f"{methods[path].upper()} {path}: text/event-stream "
            f"missing itemSchema (OAS 3.2 sequential-stream keyword). "
            f"dump_openapi.py post-processing regressed."
        )
        ref = sse_entry["itemSchema"].get("$ref", "")
        assert ref.endswith("/ServerStreamEvent"), (
            f"{methods[path].upper()} {path}: itemSchema does not "
            f"reference ServerStreamEvent (got {ref!r}). The SSE "
            f"event union is the source of truth — schema must "
            f"point at it."
        )

    # The components block must contain a ServerStreamEvent schema
    # that's a oneOf over every variant. Without this, the $ref
    # above would be dangling.
    schemas = spec["components"]["schemas"]
    assert "ServerStreamEvent" in schemas, (
        "components.schemas.ServerStreamEvent is missing. "
        "dump_openapi.py must materialize the discriminated union "
        "as a top-level schema for the $ref to resolve."
    )


# ── Part 4: New event variants from the session-rearchitecture port ──
#
# Direct construction tests (using the real Pydantic types — no
# MagicMock per project rule) for the two events added to support
# the session-rearchitecture spec §3 / §7 flows: ``session.status``
# gains a new ``waiting`` literal, and the new ``session.created``
# event is published onto a parent's stream when a sub-agent spawns.


def test_session_status_event_accepts_waiting_literal() -> None:
    """``session.status`` rejects unknown values but accepts ``waiting``.

    The ``waiting`` literal is the spec-mandated session-status
    value emitted by the runtime when the parent agent loop parks
    on the async-work drain. If a refactor accidentally drops the
    literal, every emitter would silently raise ValidationError at
    runtime — this test catches the drop at static-import time.
    """
    event = SessionStatusEvent(
        type="session.status",
        conversation_id="conv_abc",
        status="waiting",
    )
    # Verify the value round-trips intact through model_dump (the
    # exact path session_loop / workflow use to publish).
    dumped = event.model_dump()
    assert dumped["status"] == "waiting"
    assert dumped["conversation_id"] == "conv_abc"
    assert dumped["type"] == "session.status"

    # All four allowed literals must validate cleanly.
    for status in ("idle", "running", "waiting", "failed"):
        SessionStatusEvent(
            type="session.status",
            conversation_id="conv_abc",
            status=status,  # type: ignore[arg-type]
        )

    # An out-of-set value must raise ValidationError — fail loud
    # rather than silently shipping a non-conforming wire shape
    # (project rule 15).
    with pytest.raises(ValidationError):
        SessionStatusEvent(
            type="session.status",
            conversation_id="conv_abc",
            status="parked",  # type: ignore[arg-type]
        )


def test_session_status_waiting_round_trips_through_union() -> None:
    """A waiting status dict round-trips through the ServerStreamEvent adapter."""
    raw = {
        "type": "session.status",
        "conversation_id": "conv_xyz",
        "status": "waiting",
    }
    parsed = _ADAPTER.validate_python(raw)
    # Discriminator routes the dict to SessionStatusEvent (not some
    # other variant); status is preserved.
    assert isinstance(parsed, SessionStatusEvent)
    assert parsed.status == "waiting"


def test_session_skills_event_round_trips_through_union() -> None:
    """``session.skills`` is a bare nudge that routes via the discriminator.

    ``_load_runner_skills`` publishes this dict (via ``model_dump``) the
    moment the background runner-skills fetch lands. If the variant were
    missing from the union, the SSE serializer's boundary validation
    (``_stream_live_events``) would reject the emit and the web UI would
    never be told to re-read the snapshot — leaving the slash-command
    menu empty. This pins the wire shape (just ``conversation_id``, no
    payload) and the discriminator routing.
    """
    event = SessionSkillsEvent(
        type="session.skills",
        conversation_id="conv_abc",
    )
    dumped = event.model_dump()
    assert dumped == {
        "type": "session.skills",
        "conversation_id": "conv_abc",
        "sequence_number": None,
    }
    parsed = _ADAPTER.validate_python(dumped)
    # Discriminator must route to SessionSkillsEvent, not some other
    # ``session.*`` variant; a misroute would mean a duplicate or
    # shadowed wire type.
    assert isinstance(parsed, SessionSkillsEvent)
    assert parsed.conversation_id == "conv_abc"


def test_session_model_options_event_round_trips_through_union() -> None:
    """``session.model_options`` routes via the discriminator."""
    event = SessionModelOptionsEvent(
        type="session.model_options",
        conversation_id="conv_abc",
    )
    dumped = event.model_dump()
    assert dumped == {
        "type": "session.model_options",
        "conversation_id": "conv_abc",
        "sequence_number": None,
    }
    parsed = _ADAPTER.validate_python(dumped)
    # Discriminator must route to the model-options nudge, otherwise
    # clients would never be told to refetch the cache-warmed snapshot.
    assert isinstance(parsed, SessionModelOptionsEvent)
    assert parsed.conversation_id == "conv_abc"


def test_session_created_event_basic_fields() -> None:
    """``session.created`` carries parent + child + agent ids on the wire."""
    event = SessionCreatedEvent(
        type="session.created",
        conversation_id="conv_parent",
        child_session_id="conv_child",
        agent_id="agent_xyz",
        parent_session_id="conv_parent",
    )
    dumped = event.model_dump()
    # Exact wire fields the spec §3 calls out — assert each one
    # so a typo in any field name regresses the test.
    assert dumped["type"] == "session.created"
    assert dumped["conversation_id"] == "conv_parent"
    assert dumped["child_session_id"] == "conv_child"
    assert dumped["agent_id"] == "agent_xyz"
    assert dumped["parent_session_id"] == "conv_parent"


def test_session_created_event_round_trips_through_union() -> None:
    """A session.created dict is dispatched to SessionCreatedEvent."""
    raw = {
        "type": "session.created",
        "conversation_id": "conv_parent",
        "child_session_id": "conv_child",
        "agent_id": "agent_xyz",
        "parent_session_id": "conv_parent",
    }
    parsed = _ADAPTER.validate_python(raw)
    assert isinstance(parsed, SessionCreatedEvent)
    # Verify the discriminator did NOT misroute to e.g.
    # SessionInputConsumedEvent (both have a ``data``/dict field
    # at the top level) — concrete-class assertion proves dispatch.
    assert parsed.child_session_id == "conv_child"


def test_session_created_event_optional_agent_id() -> None:
    """``agent_id`` defaults to None for back-compat with legacy spawn paths.

    The spec recommends populating ``agent_id`` always, but the
    field is optional on the wire so older runners that haven't
    been updated do not fail validation against the typed union.
    Production code in
    :func:`omnigent.tools.builtins.spawn._publish_session_created_on_parent`
    raises ``ValueError`` when ``agent_id`` is empty — that's the
    fail-loud check at the emit site, not on the schema.
    """
    event = SessionCreatedEvent(
        type="session.created",
        conversation_id="conv_parent",
        child_session_id="conv_child",
    )
    assert event.agent_id is None
    assert event.parent_session_id is None


def test_session_created_event_is_known() -> None:
    """``session.created`` is registered in the union's known-set."""
    # The wire-name audit (test_emit_sites_referenced_by_grep_are_
    # all_in_the_union) relies on ``is_known_event`` — a missed
    # registration would let the new event slip past the gate.
    assert is_known_event("session.created")


def test_publish_session_status_helper_uses_waiting_literal() -> None:
    """``workflow._publish_session_status`` publishes a typed waiting event.

    Verifies the runtime helper added for the session-rearchitecture
    port constructs a real :class:`SessionStatusEvent` (not a raw
    dict) so any future change to the wire shape is enforced by the
    Pydantic model. We capture published payloads via the live
    publish hook on the session_stream module.
    """
    from omnigent.runtime import session_stream as cs
    from omnigent.server.routes.sessions import _publish_status as _publish_session_status

    captured: list[tuple[str, dict[str, Any]]] = []
    real_publish = cs.publish

    def capturing_publish(conversation_id: str, event: dict[str, Any]) -> None:
        captured.append((conversation_id, event))

    cs.publish = capturing_publish  # type: ignore[assignment]
    try:
        _publish_session_status("conv_abc", "waiting")
        _publish_session_status("conv_abc", "running")
    finally:
        cs.publish = real_publish  # type: ignore[assignment]

    assert len(captured) == 2
    waiting_conv, waiting_event = captured[0]
    running_conv, running_event = captured[1]
    assert waiting_conv == "conv_abc"
    assert waiting_event["type"] == "session.status"
    assert waiting_event["status"] == "waiting"
    assert waiting_event["conversation_id"] == "conv_abc"
    assert running_conv == "conv_abc"
    assert running_event["status"] == "running"
    # Each event must round-trip the union adapter — proves the
    # helper produces dicts that the wire-validation gate accepts.
    parsed_w = _ADAPTER.validate_python(waiting_event)
    parsed_r = _ADAPTER.validate_python(running_event)
    assert isinstance(parsed_w, SessionStatusEvent)
    assert isinstance(parsed_r, SessionStatusEvent)


def test_publish_session_status_rejects_unknown_status() -> None:
    """The helper fails loud on out-of-set status values (rule 15)."""
    from omnigent.server.routes.sessions import _publish_status as _publish_session_status

    with pytest.raises(ValidationError):
        _publish_session_status("conv_abc", "bogus")


@pytest.mark.asyncio
async def test_relay_runner_disconnect_does_not_publish_failed_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Runner transport loss ends the relay without poisoning status cache."""
    from omnigent.server.routes import sessions as sessions_module

    published: list[dict[str, Any]] = []
    session_id = "conv_runner_disconnect"
    sessions_module._session_status_cache[session_id] = "running"

    class _BrokenRunnerClient(httpx.AsyncClient):
        def stream(self, *args: Any, **kwargs: Any) -> object:  # type: ignore[override]
            del args, kwargs
            raise httpx.ConnectError("runner tunnel closed")

    monkeypatch.setattr(
        "omnigent.server.routes.sessions.session_stream.publish",
        lambda _session_id, event: published.append(event),
    )
    try:
        await sessions_module._relay_runner_stream(  # type: ignore[arg-type]
            session_id,
            _BrokenRunnerClient(),
            object(),  # type: ignore[arg-type]
        )
    finally:
        cache_after = sessions_module._session_status_cache.get(session_id)
        sessions_module._session_status_cache.pop(session_id, None)

    assert cache_after == "running"
    assert not any(
        event.get("type") == "session.status" and event.get("status") == "failed"
        for event in published
    )


def test_session_created_event_payload_shape() -> None:
    """``SessionCreatedEvent`` validates the payload shape the runner publishes.

    The DBOS-removal cutover folded the standalone
    ``_publish_session_created_on_parent`` helper from
    ``spawn.py`` into the runner's sub-agent dispatch (see
    ``omnigent/runner/tool_dispatch.py::_execute_subagent_tool``).
    The wire-level invariants the helper used to encode are still
    enforced by the model itself — that the event identifies a
    parent-side ``conversation_id``, a ``child_session_id``, the
    ``agent_id``, and the discriminator. Pin them here against the
    typed model so a future renamed/dropped field can't drift past
    the SSE union silently.
    """
    payload = {
        "type": "session.created",
        "conversation_id": "conv_parent",
        "child_session_id": "conv_child",
        "agent_id": "agent_xyz",
        "parent_session_id": "conv_parent",
    }
    parsed = _ADAPTER.validate_python(payload)
    assert isinstance(parsed, SessionCreatedEvent)
    assert parsed.conversation_id == "conv_parent"
    assert parsed.child_session_id == "conv_child"
    assert parsed.agent_id == "agent_xyz"
