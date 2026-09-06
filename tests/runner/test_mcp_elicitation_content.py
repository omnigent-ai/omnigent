"""An MCP server gets the answer the person gave, not one the schema implies.

``elicitation/create`` is how an MCP server asks for something it cannot
decide alone — "which environment?", "what should I name the branch?". The
server declares the shape in ``requestedSchema`` and expects that shape back
in ``ElicitResult.content``.

The web UI already collects those answers: a schema with an ``answer`` enum
renders option buttons that post ``{"answer": "<label>"}``. What the runner
did with them is what these tests pin — the verdict registry carried a bare
bool, so the answer was dropped on arrival and the accept was filled in from
the schema instead. A person choosing "prod" from three options had "dev"
sent on their behalf, because auto-fill takes the first enum value.
"""

from __future__ import annotations

import asyncio
from typing import Any, cast

import pytest
from mcp.types import ElicitRequestFormParams

from omnigent.runner import pending_approvals
from omnigent.runner.mcp_manager import RunnerMcpManager
from omnigent.tools._elicitation_schema import build_accept_content_from_schema

ELICIT_ID = "elicit_env_choice"
SESSION = "conv_mcp_elicit"

#: What an MCP server asking a real question sends.
ENV_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {"answer": {"type": "string", "enum": ["dev", "staging", "prod"]}},
}


@pytest.fixture(autouse=True)
def _clean_registry() -> object:
    """The verdict registry is process-global; keep this module's ids out."""
    pending_approvals.reset_for_tests()
    yield
    pending_approvals.reset_for_tests()


async def _park_then(
    resolve: Any,
) -> pending_approvals.Verdict:
    """Park on a verdict and deliver *resolve* once the Future is registered."""

    async def _deliver() -> None:
        for _ in range(200):
            if ELICIT_ID in pending_approvals._pending:
                break
            await asyncio.sleep(0.001)
        resolve()

    waiter = asyncio.ensure_future(
        pending_approvals.wait_for_user_verdict(
            elicitation_id=ELICIT_ID,
            conversation_id=SESSION,
            publish_event=lambda _s, _e: None,
            timeout_seconds=5.0,
        )
    )
    await _deliver()
    return await waiter


async def test_the_chosen_option_reaches_the_waiting_caller() -> None:
    """The whole point: "prod" was chosen, so "prod" is what comes back.

    Without this the caller only learned that *someone* accepted, and had to
    invent the field the server asked for.
    """
    verdict = await _park_then(
        lambda: pending_approvals.resolve(ELICIT_ID, True, {"answer": "prod"})
    )

    assert verdict.approved is True
    assert verdict.content == {"answer": "prod"}


async def test_auto_fill_would_have_sent_a_different_answer() -> None:
    """Names the damage, so a revert reads as a behaviour change.

    The schema fallback picks the first enum value. That is a reasonable
    guess for a surface that collected nothing, and the wrong answer whenever
    a person actually chose.
    """
    auto_filled = build_accept_content_from_schema(ENV_SCHEMA)

    assert auto_filled == {"answer": "dev"}

    verdict = await _park_then(
        lambda: pending_approvals.resolve(ELICIT_ID, True, {"answer": "prod"})
    )

    assert verdict.content != auto_filled


async def test_a_bare_approval_carries_no_content() -> None:
    """A yes/no card collects no fields, and must not invent any.

    ``None`` is the signal that lets the MCP caller fall back to the schema
    auto-fill rather than sending an empty map the server's schema rejects.
    """
    verdict = await _park_then(lambda: pending_approvals.resolve(ELICIT_ID, True))

    assert verdict.approved is True
    assert verdict.content is None


async def test_a_decline_is_a_decline_whatever_was_typed() -> None:
    """The registry reports the verdict faithfully; refusal is the caller's job.

    ``_elicit`` returns a bare ``ElicitResult(action="decline")`` on a false
    verdict, so anything typed before the refusal never reaches the server —
    pinned end-to-end in ``test_a_declined_elicitation_sends_no_content``.
    """
    verdict = await _park_then(
        lambda: pending_approvals.resolve(ELICIT_ID, False, {"answer": "prod"})
    )

    assert verdict.approved is False


async def test_a_timeout_is_a_decline_with_no_content() -> None:
    """Nobody answered, so there is no answer to carry."""
    verdict = await pending_approvals.wait_for_user_verdict(
        elicitation_id=ELICIT_ID,
        conversation_id=SESSION,
        publish_event=lambda _s, _e: None,
        timeout_seconds=0.01,
    )

    assert verdict.approved is False
    assert verdict.content is None


async def test_multi_field_and_free_form_answers_survive() -> None:
    """MCP allows several properties, and values the schema cannot guess.

    ``build_accept_content_from_schema`` gives up on exactly these (it
    returns ``None`` for a free-form string), which is why the person's own
    answer has to be the thing that travels.
    """
    typed: dict[str, Any] = {"branch": "release/2.4", "notify": True, "reviewers": ["ana", "kai"]}

    verdict = await _park_then(lambda: pending_approvals.resolve(ELICIT_ID, True, typed))

    free_form = {"type": "object", "properties": {"branch": {"type": "string"}}}

    assert verdict.content == typed
    assert build_accept_content_from_schema(free_form) is None


class _StubServerClient:
    """Enough of ``httpx.AsyncClient`` for the elicitation callback's POST."""

    async def post(self, url: str, json: Any = None, timeout: float = 30.0) -> Any:
        class _Resp:
            @staticmethod
            def raise_for_status() -> None:
                return None

            @staticmethod
            def json() -> dict[str, str]:
                return {"elicitation_id": ELICIT_ID}

        return _Resp()


async def _elicit_with(resolve: Any) -> Any:
    """Drive the real inline-elicitation callback, delivering *resolve* mid-park."""
    manager = RunnerMcpManager(server_client=cast(Any, _StubServerClient()))
    callback = manager._build_elicitation_callback()
    params = ElicitRequestFormParams(
        message="Which environment?", requestedSchema=cast(Any, ENV_SCHEMA)
    )

    task = asyncio.ensure_future(callback(SESSION, params))
    for _ in range(500):
        if ELICIT_ID in pending_approvals._pending:
            break
        await asyncio.sleep(0.001)
    resolve()
    return await task


async def test_the_server_receives_the_option_the_person_picked() -> None:
    """End to end through the real callback: "prod" chosen, "prod" sent.

    This is the behaviour the fix exists for. Before it, the callback
    discarded the verdict's content and auto-filled ``{"answer": "dev"}``
    from the schema — the first enum value — so the server acted on an
    environment nobody selected.
    """
    result = await _elicit_with(
        lambda: pending_approvals.resolve(ELICIT_ID, True, {"answer": "prod"})
    )

    assert result.action == "accept"
    assert result.content == {"answer": "prod"}


async def test_a_bare_approval_still_falls_back_to_the_schema() -> None:
    """A yes/no surface collects nothing, so the guess is better than nothing.

    Keeps the REPL and the binary approve card working: the server asked for
    a field and must get one, even when the surface had no way to ask.
    """
    result = await _elicit_with(lambda: pending_approvals.resolve(ELICIT_ID, True))

    assert result.action == "accept"
    assert result.content == build_accept_content_from_schema(ENV_SCHEMA)


async def test_a_declined_elicitation_sends_no_content() -> None:
    """Refusal is refusal — nothing typed beforehand travels with it."""
    result = await _elicit_with(
        lambda: pending_approvals.resolve(ELICIT_ID, False, {"answer": "prod"})
    )

    assert result.action == "decline"
    assert result.content is None


async def test_the_runner_events_endpoint_carries_the_content() -> None:
    """The regression boundary, exercised through the real HTTP handler.

    Every other test here calls ``resolve`` directly, which is precisely the
    line the old handler never reached with content. Posting the approval the
    way the Omnigent server posts it is what proves the handler forwards it.
    """
    import httpx

    from omnigent.runner import create_runner_app
    from tests.runner.conftest import _FakeProcessManager, _ScriptedHarnessClient
    from tests.runner.helpers import NullServerClient

    app = create_runner_app(
        process_manager=cast(Any, _FakeProcessManager(_ScriptedHarnessClient([]))),
        server_client=cast(Any, NullServerClient()),
    )
    parked = pending_approvals.register(ELICIT_ID)
    try:
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://runner") as client:
            resp = await client.post(
                f"/v1/sessions/{SESSION}/events",
                json={
                    "type": "approval",
                    "data": {
                        "elicitation_id": ELICIT_ID,
                        "action": "accept",
                        "content": {"answer": "prod"},
                    },
                },
            )
            assert resp.status_code in (200, 204)
        assert parked.done(), "the approval event never reached the registry"
        assert parked.result() == pending_approvals.Verdict(
            approved=True, content={"answer": "prod"}
        )
    finally:
        pending_approvals.cleanup(ELICIT_ID)


async def _elicit_with_schema(schema: dict[str, Any], resolve: Any) -> Any:
    """Drive the real inline callback against *schema*, resolving mid-park."""
    manager = RunnerMcpManager(server_client=cast(Any, _StubServerClient()))
    callback = manager._build_elicitation_callback()
    params = ElicitRequestFormParams(
        message="Name the release branch", requestedSchema=cast(Any, schema)
    )

    task = asyncio.ensure_future(callback(SESSION, params))
    for _ in range(500):
        if ELICIT_ID in pending_approvals._pending:
            break
        await asyncio.sleep(0.001)
    resolve()
    return await task


async def test_an_unanswerable_schema_declines_rather_than_inventing() -> None:
    """A required free-form field nobody filled in must not become an accept.

    ``build_accept_content_from_schema`` cannot guess a string, so the old
    fallback produced ``accept`` with no content at all — an answer that
    violates the very schema the server published. Declining is the outcome
    the server already has a path for.
    """
    result = await _elicit_with_schema(
        {
            "type": "object",
            "properties": {"branch": {"type": "string"}},
            "required": ["branch"],
        },
        lambda: pending_approvals.resolve(ELICIT_ID, True),
    )

    assert result.action == "decline"


async def test_optional_fields_still_accept_without_an_answer() -> None:
    """A schema whose fields are all optional legally accepts no content.

    Only a ``required`` list makes an empty accept malformed — converting an
    optional-field consent into a decline would invert the person's answer.
    """
    result = await _elicit_with_schema(
        {"type": "object", "properties": {"note": {"type": "string"}}},
        lambda: pending_approvals.resolve(ELICIT_ID, True),
    )

    assert result.action == "accept"


async def test_an_answer_outside_the_enum_declines_rather_than_substituting() -> None:
    """Content arrives from a browser, so it is checked, not trusted.

    A value the schema never offered must not reach the server — and
    substituting a schema guess would repeat the original bug (the server
    acting on a value nobody chose), so the accept fails closed instead.
    """
    result = await _elicit_with(
        lambda: pending_approvals.resolve(ELICIT_ID, True, {"answer": "production"})
    )

    assert result.action == "decline"


async def test_a_wrongly_typed_answer_declines_rather_than_substituting() -> None:
    """A numeric answer to a string field fails closed, not into a guess."""
    result = await _elicit_with(lambda: pending_approvals.resolve(ELICIT_ID, True, {"answer": 1}))

    assert result.action == "decline"


# ── Proxy (MRTR) path ────────────────────────────────────────────────────────


def _mrtr_request(schema: dict[str, Any] | None) -> dict[str, Any]:
    """Build an ``inputRequests`` entry like the Omnigent server sends."""
    params: dict[str, Any] = {"message": "Which environment?", "mode": "form"}
    if schema is not None:
        params["requestedSchema"] = schema
    return {"method": "elicitation/create", "params": params}


def test_proxy_input_response_carries_the_conforming_answer() -> None:
    """The MRTR retry forwards the person's answer when it fits the schema."""
    from omnigent.runner.proxy_mcp_manager import _input_response

    verdict = pending_approvals.Verdict(approved=True, content={"answer": "prod"})

    entry = _input_response(verdict, _mrtr_request(ENV_SCHEMA))

    assert entry == {"action": "accept", "content": {"answer": "prod"}}


def test_proxy_input_response_declines_a_nonconforming_answer() -> None:
    """Browser content that fails schema validation never crosses the wire."""
    from omnigent.runner.proxy_mcp_manager import _input_response

    verdict = pending_approvals.Verdict(
        approved=True, content={"answer": "prod", "extra": "smuggled"}
    )

    entry = _input_response(verdict, _mrtr_request(ENV_SCHEMA))

    assert entry == {"action": "decline"}


def test_proxy_input_response_bare_accept_and_decline() -> None:
    """No content means a bare accept; a refusal is a decline either way."""
    from omnigent.runner.proxy_mcp_manager import _input_response

    accept = _input_response(pending_approvals.Verdict(approved=True), _mrtr_request(ENV_SCHEMA))
    decline = _input_response(pending_approvals.Verdict(approved=False), _mrtr_request(ENV_SCHEMA))

    assert accept == {"action": "accept"}
    assert decline == {"action": "decline"}


def test_proxy_bare_accept_auto_fills_a_consent_shaped_required_schema() -> None:
    """A content-less approve against a fillable required schema still accepts.

    Mirrors the inline path's fallback order: the surface collected nothing
    (a bare approve card, the REPL's y/n prompt), so a schema the auto-fill
    can answer — like the policy-ASK ``{"approved": boolean}`` — is filled
    rather than declined. Declining here inverted the person's yes.
    """
    from omnigent.runner.proxy_mcp_manager import _input_response

    policy_ask_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"approved": {"type": "boolean"}},
        "required": ["approved"],
    }

    entry = _input_response(
        pending_approvals.Verdict(approved=True), _mrtr_request(policy_ask_schema)
    )

    assert entry == {"action": "accept", "content": {"approved": True}}


def test_proxy_bare_accept_declines_when_required_fields_cannot_be_filled() -> None:
    """A content-less approve against an unanswerable required schema declines.

    Mirrors the inline path's required-aware gate: the auto-fill cannot guess
    a free-form string, and forwarding ``{"action": "accept"}`` with no
    content for a schema whose fields are ``required`` is malformed — the
    server rejects it and the MRTR retry loop spins ("Approval loop
    exceeded") — so the proxy declines instead of sending a body the
    server's own schema will refuse.
    """
    from omnigent.runner.proxy_mcp_manager import _input_response

    required_schema: dict[str, Any] = {
        "type": "object",
        "properties": {"branch": {"type": "string"}},
        "required": ["branch"],
    }

    entry = _input_response(
        pending_approvals.Verdict(approved=True), _mrtr_request(required_schema)
    )

    assert entry == {"action": "decline"}


# ── Validator tightening (browser→server trust boundary) ────────────────────


def _validate(content: dict[str, Any], prop: dict[str, Any]) -> dict[str, Any] | None:
    """Validate one-field content against a one-property schema."""
    from omnigent.tools._elicitation_schema import validate_content_against_schema

    schema = {"type": "object", "properties": {"field": prop}}
    return validate_content_against_schema(cast(Any, {"field": content["field"]}), schema)


def test_a_nullable_union_field_rejects_the_wrong_type() -> None:
    """``anyOf: [string, null]`` (a ``str | None`` field) is not a free pass.

    A union declares its types per branch; an integer satisfies neither, so
    the answer must fail closed rather than skate past a missing top-level
    ``type``.
    """
    nullable_str = {"anyOf": [{"type": "string"}, {"type": "null"}]}

    assert _validate({"field": 123}, nullable_str) is None
    assert _validate({"field": "note"}, nullable_str) == {"field": "note"}
    assert _validate({"field": None}, nullable_str) == {"field": None}


def test_numeric_and_length_bounds_are_enforced() -> None:
    """``maximum`` / ``minLength`` are part of what the server asked for."""
    assert _validate({"field": 999}, {"type": "integer", "maximum": 100}) is None
    assert _validate({"field": 42}, {"type": "integer", "maximum": 100}) == {"field": 42}
    assert _validate({"field": "ab"}, {"type": "string", "minLength": 5}) is None
    assert _validate({"field": "abcde"}, {"type": "string", "minLength": 5}) == {"field": "abcde"}


def test_a_property_level_const_is_enforced() -> None:
    """A bare ``const`` pins the only acceptable answer."""
    assert _validate({"field": "dev"}, {"type": "string", "const": "prod"}) is None
    assert _validate({"field": "prod"}, {"type": "string", "const": "prod"}) == {"field": "prod"}


def test_array_items_enum_and_bounds_are_enforced() -> None:
    """A list answer may not smuggle members the array's ``items`` exclude."""
    prop: dict[str, Any] = {
        "type": "array",
        "items": {"type": "string", "enum": ["dev", "prod"]},
        "maxItems": 2,
    }

    assert _validate({"field": ["prod", "smuggled"]}, prop) is None
    assert _validate({"field": ["dev", "prod", "dev"]}, prop) is None
    assert _validate({"field": ["prod"]}, prop) == {"field": ["prod"]}
