"""Regression: a sub-agent turn must not warn when it already holds the child.

Field symptom (polly dispatching a sub-agent): every turn of a healthy child
session logged

    Sub-agent '<name>' for session <id> did not resolve in the parent spec;
    falling back to the parent spec (child runs with the parent's prompt,
    tools and harness).

even though the sub-agent is declared in the bundle. The warning is a false
positive. Whatever primes ``_session_spec_cache`` first — ``POST
/v1/sessions``, ``_resolve_session_spec_entry`` behind a resource read, or an
earlier turn — resolves the PARENT tree, swaps to the named sub-spec, and
caches the CHILD. The turn path then reads that cached spec and searches it
*again* for the sub-agent name; ``_find_spec_by_name`` only walks
``spec.sub_agents``, and the child has no child of its own, so the lookup
always misses and the warning fires on a session that resolved perfectly.

The distinction matters more now than it did when this was only a warning:
a name that genuinely does not resolve fails the dispatch with
``SUB_AGENT_UNRESOLVED`` rather than booting the child as a clone of its
orchestrator, so a false positive here would break healthy sessions instead
of merely logging at them. The turn path therefore still looks the sub-agent
up unconditionally and still swaps whenever it resolves; only the failure is
gated, suppressed on a miss where the spec in hand already carries the
sub-agent's name.

Gating the LOOKUP on that same name check would be wrong, which is what the
last test pins. A root may legally share its sub-agent's name — the
uniqueness check never compares the root's own name — and
``_find_spec_by_name`` still resolves the child there, so skipping the lookup
drops a swap that would have succeeded and boots the child as a parent clone
with no warning at all.

The reported bundle was polly's ``pi`` worker; the defect is in the warning
gate and is independent of the child's harness, so these tests use the
``claude_code`` / ``claude-native`` child that the rest of the runner suite
already exercises hermetically.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import pytest

from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import (
    _FakeProcessManager,
    _runner_client,
    _ScriptedHarnessClient,
    _sse,
)
from tests.runner.helpers import NullServerClient

PARENT_AGENT_ID = "ag_polly"
CHILD_SESSION_ID = "conv_child_worker"
SUB_AGENT_NAME = "claude_code"
CHILD_HARNESS = "claude-native"
UNRESOLVABLE_SUB_AGENT_NAME = "renamed_worker"
_WARNING_FRAGMENT = "did not resolve in the parent spec"
_UNRESOLVED_CODE = "sub_agent_unresolved"

# A root may legally carry its own sub-agent's name: the uniqueness check
# (``_check_unique_sub_agent_names``) seeds its ``seen`` set empty and only
# walks ``spec.sub_agents``, so the root's name is never compared.
SHADOWED_NAME = "pi"
SHADOWED_SESSION_ID = "conv_child_shadowed"


def _orchestrator_spec_tree() -> AgentSpec:
    """Parent orchestrator (``claude-sdk``) with one declared sub-agent."""
    child = AgentSpec(
        spec_version=1,
        name=SUB_AGENT_NAME,
        executor=ExecutorSpec(type="omnigent", config={"harness": CHILD_HARNESS}),
    )
    return AgentSpec(
        spec_version=1,
        name="polly",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[child],
    )


async def _parent_spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    """Resolve any agent_id to the parent tree, as the live server does."""
    del agent_id, session_id
    return _orchestrator_spec_tree()


class _SubAgentSnapshotServer(NullServerClient):
    """Server client whose session GET carries ``sub_agent_name``.

    :param sub_agent_name: The name the snapshot reports for the child
        session, e.g. ``"claude_code"``.
    """

    def __init__(self, sub_agent_name: str) -> None:
        self._sub_agent_name = sub_agent_name

    class _Resp:
        def __init__(self, payload: dict[str, Any]) -> None:
            self.status_code = 200
            self._payload = payload
            self.text = json.dumps(payload)

        def json(self) -> dict[str, Any]:
            return self._payload

        def raise_for_status(self) -> None:
            return None

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        u = url.rstrip("/")
        if u.endswith(CHILD_SESSION_ID):
            return self._Resp(
                {
                    "agent_id": PARENT_AGENT_ID,
                    "sub_agent_name": self._sub_agent_name,
                    "parent_session_id": "conv_parent_polly",
                    "created_at": 0,
                    "workspace": None,
                }
            )
        if u.endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return super()._Response()


async def _prime_spec_cache_then_turn(
    sub_agent_name: str,
    caplog: pytest.LogCaptureFixture,
) -> tuple[_FakeProcessManager, list[logging.LogRecord]]:
    """Prime the session spec cache, then run one turn; return the turn's logs.

    The resource read stands in for every path that populates
    ``_session_spec_cache`` before the first turn (the live sequence is ``POST
    /v1/sessions``; the web UI's resource panels hit this one). The log
    capture is cleared between the phases so the assertions only see what the
    TURN logged.

    Only resolvable names come through here: priming an unresolvable one now
    fails the resource read outright, which
    ``test_unresolvable_sub_agent_resolution_fails_with_typed_error`` pins.

    :param sub_agent_name: The name the server snapshot reports.
    :param caplog: pytest log capture, cleared between the phases.
    :returns: ``(process_manager, records_logged_during_the_turn)``.
    """
    pm = _FakeProcessManager(
        _ScriptedHarnessClient(
            [
                _sse({"type": "response.created", "response": {"id": "r1"}}),
                _sse({"type": "response.completed", "response": {"id": "r1"}}),
            ]
        )
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_parent_spec_resolver,
        server_client=_SubAgentSnapshotServer(sub_agent_name),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            primed = await client.get(f"/v1/sessions/{CHILD_SESSION_ID}/resources")
            assert primed.status_code == 200, f"{primed.status_code} {primed.text}"

            # Everything above is setup; only the turn is under test.
            caplog.clear()

            turn = await client.post(
                f"/v1/sessions/{CHILD_SESSION_ID}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": PARENT_AGENT_ID,
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert turn.status_code == 202, f"{turn.status_code} {turn.text}"

            for _ in range(300):
                if pm.get_client_calls:
                    break
                await asyncio.sleep(0.01)

        records = list(caplog.records)

    return pm, records


@pytest.mark.asyncio
async def test_declared_sub_agent_turn_does_not_warn_about_resolution(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A turn for a DECLARED sub-agent must log no "did not resolve" warning.

    The spec cache already holds the swapped child spec, so the turn's lookup
    finds nothing to swap — a miss that means the child is in hand, not that
    anything fell back to the parent.
    """
    pm, records = await _prime_spec_cache_then_turn(SUB_AGENT_NAME, caplog)

    spurious = [r for r in records if _WARNING_FRAGMENT in r.getMessage()]
    assert not spurious, (
        "turn for a declared sub-agent logged the unresolved-sub-agent "
        f"warning: {[r.getMessage() for r in spurious]!r}. The cached spec is "
        "already the child; searching it for its own name always misses."
    )

    harnesses = [h for (conv, h, _env) in pm.get_client_calls if conv == CHILD_SESSION_ID]
    assert harnesses, "the turn never asked the process manager for a harness"
    assert all(h == CHILD_HARNESS for h in harnesses), (
        f"turn spawned {harnesses!r} for the sub-agent session; expected only "
        f"{CHILD_HARNESS!r} (the child's own harness)."
    )


def _unresolvable_app() -> tuple[_FakeProcessManager, Any]:
    """Build a runner app for a session naming a sub-agent the tree lacks.

    :returns: ``(process_manager, app)``; the process manager records every
        harness the runner asked to spawn.
    """
    pm = _FakeProcessManager(
        _ScriptedHarnessClient(
            [
                _sse({"type": "response.created", "response": {"id": "r1"}}),
                _sse({"type": "response.completed", "response": {"id": "r1"}}),
            ]
        )
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_parent_spec_resolver,
        server_client=_SubAgentSnapshotServer(UNRESOLVABLE_SUB_AGENT_NAME),  # type: ignore[arg-type]
    )
    return pm, app


@pytest.mark.asyncio
async def test_unresolvable_sub_agent_resolution_fails_with_typed_error() -> None:
    """A name absent from the parent tree must fail, not fall back.

    Spec resolution for a session whose ``sub_agent_name`` names no declared
    child has no spec to return. Returning the parent's runs the child as a
    clone of its orchestrator, so resolution raises instead, and the typed
    ``sub_agent_unresolved`` code is what lets a dispatching parent tell a
    renamed or removed sub-agent from a missing session.
    """
    _pm, app = _unresolvable_app()

    async with _runner_client(app) as client:
        resp = await client.get(f"/v1/sessions/{CHILD_SESSION_ID}/resources")

    assert resp.status_code == 410, (
        f"resolution for an undeclared sub-agent returned {resp.status_code}; "
        "expected 410, the session-lifecycle status its sibling "
        "session_agent_missing uses."
    )
    body = resp.json()
    assert body["error"]["code"] == _UNRESOLVED_CODE, (
        f"expected a typed {_UNRESOLVED_CODE!r} error, got {body!r}"
    )
    assert UNRESOLVABLE_SUB_AGENT_NAME in body["error"]["message"], (
        "the failure must name the sub-agent that was asked for, so the "
        f"parent can act on it: {body['error']['message']!r}"
    )


@pytest.mark.asyncio
async def test_unresolvable_sub_agent_turn_never_spawns_the_parent_harness() -> None:
    """A turn for an undeclared sub-agent must not run on the parent spec.

    The reported symptom: a fan-out of five sub-agents warned five times and
    the parent received three "produced no output" completions plus one
    preamble-only reply, all reported as successes. The turn must fail rather
    than spawn anything, so the parent sees a failure instead of a child that
    was never the agent it asked for.

    No priming here: the turn has to resolve the parent tree fresh, which is
    the state the fallback used to survive in.
    """
    pm, app = _unresolvable_app()

    async with _runner_client(app) as client:
        turn = await client.post(
            f"/v1/sessions/{CHILD_SESSION_ID}/events",
            json={
                "type": "message",
                "role": "user",
                "agent_id": PARENT_AGENT_ID,
                "content": [{"type": "input_text", "text": "hi"}],
            },
        )
        assert turn.status_code == 202, f"{turn.status_code} {turn.text}"

        # The turn runs in the background; give it room to reach (and fail at)
        # harness selection before asserting nothing was spawned.
        for _ in range(300):
            if pm.get_client_calls:
                break
            await asyncio.sleep(0.01)

    spawned = [(conv, h) for (conv, h, _env) in pm.get_client_calls if conv == CHILD_SESSION_ID]
    assert not spawned, (
        f"a turn for an undeclared sub-agent spawned {spawned!r}. Any spawn "
        "here is the parent-clone fallback: the child would run with the "
        "orchestrator's prompt, tools and harness and report success."
    )


def _shadowed_name_spec_tree() -> AgentSpec:
    """A parent whose OWN name equals its sub-agent's name.

    Legal today: ``_check_unique_sub_agent_names`` never adds the root's name
    to the ``seen`` set, so this tree validates clean. ``_find_spec_by_name``
    searches ``spec.sub_agents`` only, so it still resolves the CHILD here —
    the swap must happen, and any name-equality shortcut would skip it.
    """
    child = AgentSpec(
        spec_version=1,
        name=SHADOWED_NAME,
        executor=ExecutorSpec(type="omnigent", config={"harness": CHILD_HARNESS}),
    )
    return AgentSpec(
        spec_version=1,
        name=SHADOWED_NAME,
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-sdk"}),
        sub_agents=[child],
    )


async def _shadowed_spec_resolver(agent_id: str, session_id: str | None = None) -> AgentSpec:
    """Resolve any agent_id to the name-shadowing parent tree."""
    del agent_id, session_id
    return _shadowed_name_spec_tree()


class _ShadowedSnapshotServer(_SubAgentSnapshotServer):
    """Snapshot server for the name-shadowing session id."""

    def __init__(self) -> None:
        super().__init__(SHADOWED_NAME)

    async def get(self, url: str, **kwargs: Any) -> Any:
        del kwargs
        u = url.rstrip("/")
        if u.endswith(SHADOWED_SESSION_ID):
            return self._Resp(
                {
                    "agent_id": PARENT_AGENT_ID,
                    "sub_agent_name": SHADOWED_NAME,
                    "parent_session_id": "conv_parent_polly",
                    "created_at": 0,
                    "workspace": None,
                }
            )
        if u.endswith("/items"):
            return self._Resp({"data": [], "has_more": False})
        return super(_SubAgentSnapshotServer, self)._Response()


@pytest.mark.asyncio
async def test_sub_agent_sharing_the_parent_name_still_swaps_to_the_child(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A parent named after its own sub-agent must still hand over the CHILD.

    Guards the quiet-the-warning fix against a shortcut that skips the swap
    whenever the spec in hand is *named* for the sub-agent: in this legal
    tree the spec in hand is the PARENT and the swap is still required.
    Skipping it boots the child with the parent's prompt, tools and harness —
    the exact silent parent-clone the warning exists to catch, and the child
    of a coordinator parent then re-dispatches into itself.

    No priming here on purpose: the turn must resolve the parent tree fresh
    from the bound ``agent_id`` (the state where the spec in hand really is
    the parent), which is what makes the missing swap observable.
    """
    pm = _FakeProcessManager(
        _ScriptedHarnessClient(
            [
                _sse({"type": "response.created", "response": {"id": "r1"}}),
                _sse({"type": "response.completed", "response": {"id": "r1"}}),
            ]
        )
    )
    app = create_runner_app(
        process_manager=pm,  # type: ignore[arg-type]
        spec_resolver=_shadowed_spec_resolver,
        server_client=_ShadowedSnapshotServer(),  # type: ignore[arg-type]
    )

    with caplog.at_level(logging.WARNING, logger="omnigent.runner.app"):
        async with _runner_client(app) as client:
            turn = await client.post(
                f"/v1/sessions/{SHADOWED_SESSION_ID}/events",
                json={
                    "type": "message",
                    "role": "user",
                    "agent_id": PARENT_AGENT_ID,
                    "content": [{"type": "input_text", "text": "hi"}],
                },
            )
            assert turn.status_code == 202, f"{turn.status_code} {turn.text}"

            for _ in range(300):
                if pm.get_client_calls:
                    break
                await asyncio.sleep(0.01)

        records = list(caplog.records)

    harnesses = [h for (conv, h, _env) in pm.get_client_calls if conv == SHADOWED_SESSION_ID]
    assert harnesses, "the turn never asked the process manager for a harness"
    assert all(h == CHILD_HARNESS for h in harnesses), (
        f"turn spawned {harnesses!r} for a sub-agent whose parent shares its "
        f"name; expected only {CHILD_HARNESS!r}. A 'claude-sdk' spawn means the "
        "swap was skipped and the child is running as a clone of its parent."
    )

    spurious = [r for r in records if _WARNING_FRAGMENT in r.getMessage()]
    assert not spurious, (
        "the name-shadowing tree resolves its sub-agent fine, yet the turn "
        f"logged: {[r.getMessage() for r in spurious]!r}"
    )
