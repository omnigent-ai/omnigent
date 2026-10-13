"""
Integration test: ``LEVEL_READ`` callers get policy verdicts without session mutation.

Uses a real ``SqlAlchemyPermissionStore`` so ``UnifiedAuthProvider``
resolves ``X-Forwarded-Email`` headers to permission levels. An owner
creates a session with a label-writing policy, then a ``LEVEL_READ``
collaborator evaluates the same session. The collaborator receives the
verdict (ALLOW with ``set_labels`` populated), but the session's
persisted labels are unchanged afterward. A second test confirms that
an ``LEVEL_EDIT`` caller still persists as before.

Uses the shared ``mock_llm`` / ``runtime_init`` / ``db_uri`` fixtures
from ``tests/server/conftest.py``.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from omnigent.runtime import get_caps
from omnigent.runtime.caps import RuntimeCaps
from omnigent.server.auth import LEVEL_EDIT, LEVEL_READ
from omnigent.spec.types import FunctionPolicySpec, FunctionRef, Phase
from omnigent.stores.conversation_store.sqlalchemy_store import (
    SqlAlchemyConversationStore,
)
from omnigent.stores.permission_store.sqlalchemy_store import SqlAlchemyPermissionStore
from tests.server.helpers import create_test_agent

pytestmark = pytest.mark.asyncio


# ── Policy callable ──────────────────────────────────────────────


def _allow_and_label(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that ALLOWs every event and writes a label.

    Always returns ``set_labels: {"evaluated": "true"}`` so we can
    verify whether the label was actually persisted.

    :param event: V0 event dict.
    :returns: ALLOW with ``set_labels``.
    """
    return {
        "result": "ALLOW",
        "set_labels": {"evaluated": "true"},
    }


def _ask_on_request(event: dict[str, Any]) -> dict[str, Any]:
    """
    Policy that demands approval (ASK) for every event.

    Used to exercise the endpoint's server-side ASK park on the REQUEST
    phase — the phase a native session's ``UserPromptSubmit`` hook hits.

    :param event: V0 event dict.
    :returns: ASK with a reason.
    """
    return {
        "result": "ASK",
        "reason": "Prompt requires approval",
    }


# ── Helpers ──────────────────────────────────────────────────────

OWNER = "owner@example.com"
READER = "reader@example.com"
EDITOR = "editor@example.com"


def _tool_call_request(tool_name: str = "Bash") -> dict[str, Any]:
    """
    Build a PHASE_TOOL_CALL EvaluationRequest.

    :param tool_name: Tool name, e.g. ``"Bash"``.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_TOOL_CALL",
            "target": "",
            "data": {"name": tool_name, "arguments": {}},
            "context": {},
        },
    }


def _request_request(text: str = "do the thing") -> dict[str, Any]:
    """
    Build a PHASE_REQUEST EvaluationRequest (the UserPromptSubmit shape).

    :param text: The user prompt text.
    :returns: EvaluationRequest JSON dict.
    """
    return {
        "event": {
            "type": "PHASE_REQUEST",
            "target": "",
            "data": {"text": text},
            "context": {},
        },
    }


async def _create_session_as(
    client: httpx.AsyncClient,
    user: str,
    agent_id: str,
) -> str:
    """
    Create a session via the API as the given user.

    :param client: Test HTTP client.
    :param user: User email for ``X-Forwarded-Email``.
    :param agent_id: Agent to bind.
    :returns: New session id.
    """
    resp = await client.post(
        "/v1/sessions",
        json={"agent_id": agent_id},
        headers={"X-Forwarded-Email": user},
    )
    assert resp.status_code == 201, f"create session failed: {resp.status_code} {resp.text}"
    return resp.json()["id"]


def _grant_access(db_uri: str, user: str, session_id: str, level: int) -> None:
    """
    Grant a permission level to a user on a session.

    :param db_uri: SQLite connection URI.
    :param user: User email.
    :param session_id: Session (conversation) id.
    :param level: Permission level, e.g. ``LEVEL_READ``.
    """
    perm_store = SqlAlchemyPermissionStore(db_uri)
    perm_store.ensure_user(user)
    perm_store.grant(user, session_id, level)


def _get_labels(db_uri: str, session_id: str) -> dict[str, str]:
    """
    Read persisted labels from the conversation store.

    :param db_uri: SQLite connection URI.
    :param session_id: Session (conversation) id.
    :returns: The conversation's label dict.
    """
    conv_store = SqlAlchemyConversationStore(db_uri)
    conv = conv_store.get_conversation(session_id)
    assert conv is not None, f"session {session_id!r} not found"
    return dict(conv.labels)


# ── Tests ────────────────────────────────────────────────────────


async def test_read_only_caller_gets_verdict_but_no_label_mutation(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """
    A LEVEL_READ collaborator receives the policy verdict but session
    labels are NOT persisted.

    The label-writing policy returns ``set_labels: {"evaluated": "true"}``.
    The LEVEL_READ caller sees ALLOW in the response, but the session's
    persisted ``conversation_labels`` remains empty afterward. This
    verifies the ``read_only`` guard in the route and the engine.
    """
    labeling_policy = FunctionPolicySpec(
        name="admin__labeler",
        on=None,
        function=FunctionRef(path=f"{__name__}._allow_and_label"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[labeling_policy],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )

    # Owner creates the agent and session.
    agent = await create_test_agent(auth_client, user=OWNER)
    session_id = await _create_session_as(auth_client, OWNER, agent["id"])

    # Grant LEVEL_READ to reader.
    _grant_access(db_uri, READER, session_id, LEVEL_READ)

    # Snapshot labels before.
    labels_before = _get_labels(db_uri, session_id)
    assert labels_before.get("evaluated") is None, "label should not exist before evaluation"

    # Reader evaluates — should get ALLOW but no persistence.
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Read"),
        headers={"X-Forwarded-Email": READER},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_ALLOW"

    # Labels must NOT have been persisted.
    labels_after = _get_labels(db_uri, session_id)
    assert labels_after.get("evaluated") is None, (
        "LEVEL_READ caller must not mutate session labels via policy evaluate"
    )


async def test_edit_caller_persists_labels_as_before(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """
    A LEVEL_EDIT (or higher) caller's policy evaluation still persists labels.

    Ensures the ``read_only`` guard only fires for sub-edit levels and
    does not regress the normal write path.
    """
    labeling_policy = FunctionPolicySpec(
        name="admin__labeler",
        on=None,
        function=FunctionRef(path=f"{__name__}._allow_and_label"),
    )
    original_caps = get_caps()
    patched_caps = RuntimeCaps(
        execution_timeout=original_caps.execution_timeout,
        default_policies=[labeling_policy],
    )
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: patched_caps,
    )

    # Owner creates the agent and session.
    agent = await create_test_agent(auth_client, user=OWNER)
    session_id = await _create_session_as(auth_client, OWNER, agent["id"])

    # Grant LEVEL_EDIT to editor.
    _grant_access(db_uri, EDITOR, session_id, LEVEL_EDIT)

    # Editor evaluates — should persist labels.
    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_tool_call_request("Read"),
        headers={"X-Forwarded-Email": EDITOR},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["result"] == "POLICY_ACTION_ALLOW"

    # Labels SHOULD have been persisted.
    labels_after = _get_labels(db_uri, session_id)
    assert labels_after.get("evaluated") == "true", (
        "LEVEL_EDIT caller's policy evaluation must still persist labels"
    )


async def test_request_phase_ask_parks_server_side_and_collapses_to_allow(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """
    A REQUEST-phase ASK is parked server-side and collapses to a hard verdict.

    This is the path a native session's ``UserPromptSubmit`` hook takes: it
    POSTs a PHASE_REQUEST event and must NEVER see raw ASK (the hook has no
    ASK primitive — it can only block or allow). The endpoint holds the gate
    via ``_hold_native_ask_gate`` and returns ALLOW on approve / DENY on
    decline. Before REQUEST was added to the endpoint's ASK-park phases, this
    returned a raw ``POLICY_ACTION_ASK`` the hook couldn't act on safely.
    """
    held: dict[str, Any] = {}

    async def _fake_hold(_request: Any, **kwargs: Any) -> bool:
        held["phase"] = kwargs["phase"]
        return True  # human approved

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._hold_native_ask_gate",
        _fake_hold,
    )
    ask_policy = FunctionPolicySpec(
        name="admin__ask",
        on=None,
        function=FunctionRef(path=f"{__name__}._ask_on_request"),
    )
    original_caps = get_caps()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: RuntimeCaps(
            execution_timeout=original_caps.execution_timeout,
            default_policies=[ask_policy],
        ),
    )

    agent = await create_test_agent(auth_client, user=OWNER)
    session_id = await _create_session_as(auth_client, OWNER, agent["id"])

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_request_request("delete prod"),
        headers={"X-Forwarded-Email": OWNER},
    )
    assert resp.status_code == 200, resp.text
    # The gate was entered AT the REQUEST phase (not skipped as a raw ASK).
    assert held.get("phase") == Phase.REQUEST
    # Approval collapses the ASK to a hard ALLOW — the hook never sees ASK.
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"


async def test_request_phase_ask_decline_collapses_to_deny(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """
    A declined / timed-out REQUEST-phase ASK collapses to DENY (fail closed).

    The companion to the approve case: when the human declines (or the park
    times out / disconnects), the endpoint returns DENY so the native hook
    blocks the prompt rather than letting it through.
    """

    async def _fake_hold(_request: Any, **_kwargs: Any) -> bool:
        return False  # declined / timeout

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._hold_native_ask_gate",
        _fake_hold,
    )
    ask_policy = FunctionPolicySpec(
        name="admin__ask",
        on=None,
        function=FunctionRef(path=f"{__name__}._ask_on_request"),
    )
    original_caps = get_caps()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: RuntimeCaps(
            execution_timeout=original_caps.execution_timeout,
            default_policies=[ask_policy],
        ),
    )

    agent = await create_test_agent(auth_client, user=OWNER)
    session_id = await _create_session_as(auth_client, OWNER, agent["id"])

    resp = await auth_client.post(
        f"/v1/sessions/{session_id}/policies/evaluate",
        json=_request_request("delete prod"),
        headers={"X-Forwarded-Email": OWNER},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["result"] == "POLICY_ACTION_DENY"


async def test_request_phase_skips_gate_when_web_prompt_pending(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """
    A REQUEST-phase eval is skipped (ALLOW) when a web prompt is in flight.

    A native session's ``UserPromptSubmit`` hook posts ``PHASE_REQUEST`` for
    every prompt, but a web-UI prompt was already gated server-side by
    ``_evaluate_input_policy`` before injection. The presence of a
    ``pending_inputs`` entry marks the prompt as web-origin, so the endpoint
    short-circuits to ALLOW rather than re-gating (which would double-prompt
    the human). If the dedup regressed, the ASK policy below would enter the
    gate instead of returning a clean ALLOW.
    """
    from omnigent.runtime import pending_inputs

    # If the dedup is bypassed and the gate runs, this records the failure.
    gate_ran = {"called": False}

    async def _fail_hold(_request: Any, **_kwargs: Any) -> bool:
        gate_ran["called"] = True
        return True

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._hold_native_ask_gate",
        _fail_hold,
    )
    ask_policy = FunctionPolicySpec(
        name="admin__ask",
        on=None,
        function=FunctionRef(path=f"{__name__}._ask_on_request"),
    )
    original_caps = get_caps()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: RuntimeCaps(
            execution_timeout=original_caps.execution_timeout,
            default_policies=[ask_policy],
        ),
    )

    agent = await create_test_agent(auth_client, user=OWNER)
    session_id = await _create_session_as(auth_client, OWNER, agent["id"])

    # Mark a web-composer prompt as in flight (recorded at POST /events for a
    # native session, before the runner forward).
    pending_inputs.record(session_id, [{"type": "input_text", "text": "delete prod"}])
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_request_request("delete prod"),
            headers={"X-Forwarded-Email": OWNER},
        )
    finally:
        pending_inputs.reset_for_tests()

    assert resp.status_code == 200, resp.text
    # Skipped → clean ALLOW, and the ASK gate was never entered.
    assert resp.json()["result"] == "POLICY_ACTION_ALLOW"
    assert gate_ran["called"] is False, "dedup must skip the gate when a web prompt is pending"


async def test_request_phase_gate_runs_when_the_only_queued_web_prompt_was_interrupted(
    auth_client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
    db_uri: str,
) -> None:
    """
    A cancelled web prompt does not exempt a later prompt from the request-phase gate.

    The dedup reads a queued entry as "a web prompt is in flight". After the
    person pressed Stop that entry is a cancelled message, so a prompt typed
    directly in the TUI afterwards, which has no other request-phase gate, must
    still be evaluated. If the interrupted entry counted, the ASK policy below
    would be skipped and the prompt let through.
    """
    from omnigent.runtime import pending_inputs

    held: dict[str, Any] = {}

    async def _hold_and_decline(_request: Any, **kwargs: Any) -> bool:
        held["phase"] = kwargs["phase"]
        return False  # declined

    monkeypatch.setattr(
        "omnigent.server.routes.sessions._hold_native_ask_gate",
        _hold_and_decline,
    )
    ask_policy = FunctionPolicySpec(
        name="admin__ask",
        on=None,
        function=FunctionRef(path=f"{__name__}._ask_on_request"),
    )
    original_caps = get_caps()
    monkeypatch.setattr(
        "omnigent.server.routes.sessions.get_caps",
        lambda: RuntimeCaps(
            execution_timeout=original_caps.execution_timeout,
            default_policies=[ask_policy],
        ),
    )

    agent = await create_test_agent(auth_client, user=OWNER)
    session_id = await _create_session_as(auth_client, OWNER, agent["id"])

    cancelled = pending_inputs.record(session_id, [{"type": "input_text", "text": "delete prod"}])
    pending_inputs.mark_interrupted(session_id, [cancelled])
    try:
        resp = await auth_client.post(
            f"/v1/sessions/{session_id}/policies/evaluate",
            json=_request_request("typed in the terminal"),
            headers={"X-Forwarded-Email": OWNER},
        )
    finally:
        pending_inputs.reset_for_tests()

    assert resp.status_code == 200, resp.text
    # The gate was entered at the REQUEST phase and its decline applied.
    assert held.get("phase") == Phase.REQUEST
    assert resp.json()["result"] == "POLICY_ACTION_DENY"
