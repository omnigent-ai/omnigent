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


async def test_an_unanswerable_schema_declines_rather_than_inventing() -> None:
    """A free-form field nobody filled in must not become a bare accept.

    ``build_accept_content_from_schema`` cannot guess a string, so the old
    fallback produced ``accept`` with no content at all — an answer that
    violates the very schema the server published. Declining is the outcome
    the server already has a path for.
    """
    manager = RunnerMcpManager(server_client=cast(Any, _StubServerClient()))
    callback = manager._build_elicitation_callback()
    params = ElicitRequestFormParams(
        message="Name the release branch",
        requestedSchema=cast(
            Any, {"type": "object", "properties": {"branch": {"type": "string"}}}
        ),
    )

    task = asyncio.ensure_future(callback(SESSION, params))
    for _ in range(500):
        if ELICIT_ID in pending_approvals._pending:
            break
        await asyncio.sleep(0.001)
    pending_approvals.resolve(ELICIT_ID, True)
    result = await task

    assert result.action == "decline"


async def test_an_answer_outside_the_enum_is_not_forwarded() -> None:
    """Content arrives from a browser, so it is checked, not trusted.

    A value the schema never offered would make the server act on something
    it declared impossible; falling back keeps the wire body inside the
    contract the server published.
    """
    result = await _elicit_with(
        lambda: pending_approvals.resolve(ELICIT_ID, True, {"answer": "production"})
    )

    assert result.action == "accept"
    assert result.content == build_accept_content_from_schema(ENV_SCHEMA)
