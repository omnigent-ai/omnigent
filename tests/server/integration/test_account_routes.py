"""Account routing is enforced on catalogs, launches and model edits.

A synthetic host and a fake model catalog stand in for real machines; no
account or credential is involved.
"""

from __future__ import annotations

import asyncio

import pytest

from omnigent.host.frames import (
    HostLaunchRunnerFrame,
    HostLaunchRunnerResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    decode_host_frame,
    encode_host_frame,
)
from omnigent.server import host_accounts
from omnigent.server.host_accounts import HostAccountRegistry
from omnigent.server.routes import hosts, sessions
from omnigent.server.routes._sessions import helpers
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.polling import poll_until_async
from tests.server.helpers import build_agent_bundle, create_test_agent
from tests.server.integration.test_session_host_launch import (
    _HOST_ID,
    _WORKSPACE,
    _connect_host,
    _serve_one_launch,
    _serve_one_stop,
)
from tests.server.integration.test_session_host_launch import (
    app as app,
)

pytestmark = pytest.mark.asyncio

detached_stops: list[str] = []

MODEL = "synthetic-route-model"
OTHER_MODEL = "synthetic-route-model-two"
ACCOUNT = "synthetic-openai"


def pending_marker(transition_id: str) -> dict:
    return {
        "id": transition_id,
        "account_id": ACCOUNT,
        "host_id": _HOST_ID,
        "harness": "codex-native",
        "model": MODEL,
        "effort": "high",
    }


@pytest.fixture
def configured(app, monkeypatch):
    """Register one OpenAI route on the synthetic host with a one-model catalog."""
    app.state.account_registry = HostAccountRegistry.model_validate(
        {
            "version": 1,
            "accounts": [
                {
                    "id": ACCOUNT,
                    "label": "Synthetic OpenAI",
                    "provider": "openai",
                    "harness": "codex-native",
                    "placements": [
                        {
                            "host_id": _HOST_ID,
                            "machine_id": "machine-a",
                            "machine_label": "Machine A",
                        }
                    ],
                }
            ],
        }
    )
    probes: list[str] = []

    async def catalog(**kwargs):
        probes.append(kwargs["harness"])
        efforts = [{"reasoningEffort": "high"}, {"reasoningEffort": "low"}]
        return {
            "status": "ok",
            "models": [
                {
                    "id": MODEL,
                    "model": MODEL,
                    "isDefault": True,
                    "supportedReasoningEfforts": efforts,
                },
                {"id": OTHER_MODEL, "model": OTHER_MODEL, "supportedReasoningEfforts": efforts},
            ],
            "routable_models": [MODEL, OTHER_MODEL],
        }

    async def runner_status(*args, **kwargs):
        return "alive"

    async def workspace(**kwargs):
        return _WORKSPACE

    monkeypatch.setattr(host_accounts, "request_host_model_options", catalog)
    monkeypatch.setattr(hosts, "_proxy_model_options", catalog)
    monkeypatch.setattr(sessions, "_query_host_runner_status", runner_status)
    monkeypatch.setattr(
        "omnigent.server.routes._workspace_validation.validate_workspace", workspace
    )
    detached_stops.clear()

    async def record_stop(session_id, host_id, runner_id, registry, **kwargs):
        detached_stops.append(runner_id)
        return True

    # The background stop a move starts for the runner it detaches.
    monkeypatch.setattr(helpers, "_stop_session_host_runner", record_stop)
    return probes


async def make_bound(client, db_uri, harness="codex-native"):
    agent = await create_test_agent(
        client, executor={"type": "omnigent", "config": {"harness": harness}}, include_llm=False
    )
    store = SqlAlchemyConversationStore(db_uri)
    conv = store.create_conversation(
        agent_id=agent["id"],
        host_id=_HOST_ID,
        workspace=_WORKSPACE,
        runner_id="runner-before",
        model_override=MODEL,
        reasoning_effort="high",
    )
    return store, conv


def begin_payload(**overrides) -> dict:
    return {
        "runner_id": "",
        "account_id": ACCOUNT,
        "target_host_id": _HOST_ID,
        "target_harness": "codex-native",
        "target_model": MODEL,
        "target_effort": "high",
        "silent": True,
        **overrides,
    }


async def test_catalog_and_model_edits_reject_undeclared_route_model_effort(
    app, client, db_uri, configured
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        catalog = f"/v1/hosts/{_HOST_ID}/harnesses"
        assert (await client.get(f"{catalog}/claude-native/model-options")).status_code == 400
        assert configured == []
        wrong = await client.get(f"{catalog}/codex-native/model-options?account_id=wrong")
        assert wrong.status_code == 400
        right = await client.get(f"{catalog}/codex-native/model-options?account_id={ACCOUNT}")
        assert right.status_code == 200, right.text
        for payload in [{"model_override": "unsupported"}, {"reasoning_effort": "medium"}]:
            response = await client.patch(f"/v1/sessions/{conv.id}", json=payload)
            assert response.status_code == 400, response.text
        assert store.get_conversation(conv.id).model_override == MODEL
        accepted = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={"model_override": MODEL, "reasoning_effort": "high", "silent": True},
        )
        assert accepted.status_code == 200, accepted.text
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_launch_on_unregistered_harness_is_rejected(app, client, db_uri, configured):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri, harness="claude-native")
        store.clear_runner_id(conv.id)
        response = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners", json={"session_id": conv.id, "workspace": _WORKSPACE}
        )
        assert response.status_code == 400, response.text
        assert store.get_conversation(conv.id).runner_id is None
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_destination_must_keep_the_session_harness_and_registry_must_exist(
    app, client, db_uri, configured
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        other_family = await client.patch(
            f"/v1/sessions/{conv.id}", json=begin_payload(target_harness="claude-native")
        )
        assert other_family.status_code == 400, other_family.text
        assert "keeps its harness" in other_family.text
        no_detach = await client.patch(
            f"/v1/sessions/{conv.id}", json=begin_payload(runner_id="runner-before")
        )
        assert no_detach.status_code == 400, no_detach.text
        assert store.get_conversation(conv.id).runner_id == "runner-before"
        app.state.account_registry = None
        unconfigured = await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())
        assert unconfigured.status_code == 400, unconfigured.text
        assert store.get_conversation(conv.id).account_transition is None
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_unconfigured_registry_changes_nothing(app, client, db_uri):
    comm = await _connect_host(app)
    try:
        _store, conv = await make_bound(client, db_uri)
        response = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={"model_override": "any-model", "reasoning_effort": "low", "silent": True},
        )
        assert response.status_code == 200, response.text
        topology = await client.get("/v1/execution-topology")
        assert topology.json()["configured"] is False
        gated = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={
                "runner_id": "",
                "account_id": ACCOUNT,
                "target_host_id": _HOST_ID,
                "target_harness": "codex-native",
                "target_model": MODEL,
            },
        )
        assert gated.status_code == 400
        stale = await client.patch(f"/v1/sessions/{conv.id}", json={"transition_id": "stale"})
        assert stale.status_code == 409
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_pending_move_blocks_events_runtime_edits_and_automatic_resume(
    app, client, db_uri, configured
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        store.begin_account_transition(
            conv.id, pending_marker("pending"), expected_runner_id=conv.runner_id
        )
        for event in [
            {"type": "message", "data": {"text": "must not run"}},
            {"type": "retry_session", "data": {}},
        ]:
            response = await client.post(f"/v1/sessions/{conv.id}/events", json=event)
            assert response.status_code == 409, response.text
        fresh = store.get_conversation(conv.id)
        with pytest.raises(Exception, match="account change is pending"):
            await sessions._launch_runner_on_host(
                fresh, store, app.state.host_registry, app.state.host_registry.get(_HOST_ID)
            )
        assert store.get_conversation(conv.id).runner_id is None
        # Metadata edits stay available; edits that touch the runtime need the transition id.
        for update in [{"title": "Pending but named"}, {"archived": True}, {"archived": False}]:
            response = await client.patch(f"/v1/sessions/{conv.id}", json=update)
            assert response.status_code == 200, response.text
            assert store.get_conversation(conv.id).account_transition["id"] == "pending"
        no_token = await client.patch(f"/v1/sessions/{conv.id}", json={"model_override": MODEL})
        assert no_token.status_code == 409
        wrong_model = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={"model_override": "other", "transition_id": "pending", "silent": True},
        )
        assert wrong_model.status_code == 409
        fork = await client.post(f"/v1/sessions/{conv.id}/fork", json={})
        assert fork.status_code != 409, fork.text
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_patch_begins_change_and_only_the_recorded_destination_can_finish_it(
    app, client, db_uri, configured
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        response = await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())
        assert response.status_code == 200, response.text
        marker = response.json()["account_transition"]
        assert set(marker) == {
            "id",
            "account_id",
            "host_id",
            "harness",
            "model",
            "effort",
            "workspace",
        }
        stored = store.get_conversation(conv.id).account_transition
        assert stored["previous_runner_id"] == "runner-before"
        assert store.get_conversation(conv.id).runner_id is None
        assert detached_stops == ["runner-before"]
        path = f"/v1/hosts/{_HOST_ID}/runners"
        # Wrong and stale clients are rejected before contacting the host.
        for account_id, transition_id in [("wrong", marker["id"]), (ACCOUNT, "stale")]:
            response = await client.post(
                path,
                json={
                    "session_id": conv.id,
                    "workspace": _WORKSPACE,
                    "account_id": account_id,
                    "transition_id": transition_id,
                },
            )
            assert response.status_code == 409, response.text

        async def serve():
            stopped = await _serve_one_stop(comm)
            frame = await _serve_one_launch(comm, launch_status="launched")
            return stopped, frame

        responder = asyncio.create_task(serve())
        response = await client.post(
            path,
            json={
                "session_id": conv.id,
                "workspace": _WORKSPACE,
                "account_id": ACCOUNT,
                "transition_id": marker["id"],
            },
        )
        stopped, frame = await responder
        assert response.status_code == 200, response.text
        assert stopped == "runner-before"
        assert frame.harness == "codex-native"
        fresh = store.get_conversation(conv.id)
        assert fresh.runner_id != stopped
        assert fresh.account_transition is None
        assert fresh.workspace == _WORKSPACE
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_interrupted_launch_attempt_is_stopped_before_the_retry(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        store.begin_account_transition(
            conv.id, pending_marker("recover"), expected_runner_id=conv.runner_id
        )
        assert store.bind_account_launch(
            conv.id, "recover", "interrupted-attempt", _HOST_ID, _WORKSPACE
        )
        restarted = SqlAlchemyConversationStore(db_uri)
        assert (
            restarted.get_conversation(conv.id).account_transition["launch_runner_id"]
            == "interrupted-attempt"
        )
        stopped = []

        async def stop(session_id, host_id, runner_id, registry, **kwargs):
            stopped.append(runner_id)
            return True

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        responder = asyncio.create_task(_serve_one_launch(comm, launch_status="launched"))
        response = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={
                "session_id": conv.id,
                "workspace": _WORKSPACE,
                "account_id": ACCOUNT,
                "transition_id": "recover",
            },
        )
        frame = await responder
        assert response.status_code == 200, response.text
        assert stopped == ["interrupted-attempt", "runner-before"]
        fresh = restarted.get_conversation(conv.id)
        assert fresh.runner_id not in stopped and fresh.account_transition is None
        assert frame.session_id == conv.id
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_failed_destination_launch_keeps_the_change_for_a_retry(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        marker = (await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())).json()[
            "account_transition"
        ]

        async def stop(session_id, host_id, runner_id, registry, **kwargs):
            return True

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        responder = asyncio.create_task(
            _serve_one_launch(comm, launch_status="failed", launch_error="boom")
        )
        response = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={
                "session_id": conv.id,
                "workspace": _WORKSPACE,
                "account_id": ACCOUNT,
                "transition_id": marker["id"],
            },
        )
        await responder
        assert response.status_code == 502, response.text
        fresh = store.get_conversation(conv.id)
        assert fresh.runner_id is None
        assert fresh.account_transition["id"] == marker["id"]
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


def launch_body(conv, marker: dict, **overrides) -> dict:
    return {
        "session_id": conv.id,
        "workspace": _WORKSPACE,
        "account_id": ACCOUNT,
        "transition_id": marker["id"],
        **overrides,
    }


async def test_move_with_a_new_model_and_effort_end_to_end(app, client, db_uri, configured):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        begun = await client.patch(
            f"/v1/sessions/{conv.id}",
            json=begin_payload(target_model=OTHER_MODEL, target_effort="low"),
        )
        assert begun.status_code == 200, begun.text
        marker = begun.json()["account_transition"]
        assert (marker["model"], marker["effort"]) == (OTHER_MODEL, "low")
        path = f"/v1/hosts/{_HOST_ID}/runners"
        # The runtime still has the old model, so the destination launch is refused.
        early = await client.post(path, json=launch_body(conv, marker))
        assert early.status_code == 409, early.text
        # Step 2 runs while the session is detached and carries the transition id.
        edit = {"model_override": OTHER_MODEL, "reasoning_effort": "low", "silent": True}
        refused = await client.patch(f"/v1/sessions/{conv.id}", json=edit)
        assert refused.status_code == 409, refused.text
        accepted = await client.patch(
            f"/v1/sessions/{conv.id}", json={**edit, "transition_id": marker["id"]}
        )
        assert accepted.status_code == 200, accepted.text
        fresh = store.get_conversation(conv.id)
        assert (fresh.model_override, fresh.reasoning_effort) == (OTHER_MODEL, "low")
        assert fresh.runner_id is None and fresh.account_transition is not None

        async def serve():
            stopped = await _serve_one_stop(comm)
            return stopped, await _serve_one_launch(comm, launch_status="launched")

        responder = asyncio.create_task(serve())
        response = await client.post(path, json=launch_body(conv, marker))
        stopped, _frame = await responder
        assert response.status_code == 200, response.text
        assert stopped == "runner-before"
        done = store.get_conversation(conv.id)
        assert done.account_transition is None
        assert done.runner_id not in {None, "runner-before"}
        assert (done.model_override, done.reasoning_effort) == (OTHER_MODEL, "low")
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_cancel_stops_the_attempt_clears_the_marker_and_frees_the_session(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        marker = (await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())).json()[
            "account_transition"
        ]
        assert store.bind_account_launch(
            conv.id, marker["id"], "half-started", _HOST_ID, _WORKSPACE
        )
        stopped = []

        async def stop(session_id, host_id, runner_id, registry, **kwargs):
            stopped.append(runner_id)
            return True

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        cancel = {"abandon_transition": True, "transition_id": marker["id"]}
        stale = await client.patch(
            f"/v1/sessions/{conv.id}", json={**cancel, "transition_id": "stale"}
        )
        assert stale.status_code == 409, stale.text
        for extra in [{"target_host_id": _HOST_ID}, {"title": "renamed"}, {"archived": True}]:
            mixed = await client.patch(f"/v1/sessions/{conv.id}", json={**cancel, **extra})
            assert mixed.status_code == 400, mixed.text
        untouched = store.get_conversation(conv.id)
        assert untouched.account_transition is not None and untouched.title is None
        assert not untouched.archived
        response = await client.patch(f"/v1/sessions/{conv.id}", json=cancel)
        assert response.status_code == 200, response.text
        assert response.json()["account_transition"] is None
        assert response.json()["runner_id"] is None
        assert stopped == ["half-started"]
        fresh = store.get_conversation(conv.id)
        assert fresh.runner_id is None and fresh.account_transition is None
        again = await client.patch(f"/v1/sessions/{conv.id}", json=cancel)
        assert again.status_code == 409, again.text
        # The session is free: a move to any destination begins at once.
        second = await client.patch(
            f"/v1/sessions/{conv.id}", json=begin_payload(target_model=OTHER_MODEL)
        )
        assert second.status_code == 200, second.text
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_cancel_is_refused_while_the_attempt_will_not_stop(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        marker = (await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())).json()[
            "account_transition"
        ]
        store.bind_account_launch(conv.id, marker["id"], "stuck", _HOST_ID, _WORKSPACE)

        async def stop(*args, **kwargs):
            return False

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        response = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={"abandon_transition": True, "transition_id": marker["id"]},
        )
        assert response.status_code == 409, response.text
        fresh = store.get_conversation(conv.id)
        assert fresh.account_transition["id"] == marker["id"]
        assert fresh.runner_id == "stuck"
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_a_sub_agent_cannot_be_moved_on_its_own(app, client, db_uri, configured):
    comm = await _connect_host(app)
    try:
        store, parent = await make_bound(client, db_uri)
        child = store.create_conversation(
            agent_id=parent.agent_id,
            parent_conversation_id=parent.id,
            host_id=_HOST_ID,
            workspace=_WORKSPACE,
            runner_id=parent.runner_id,
            model_override=MODEL,
            reasoning_effort="high",
        )
        response = await client.patch(f"/v1/sessions/{child.id}", json=begin_payload())
        assert response.status_code == 409, response.text
        assert "move its parent" in response.text
        for conv_id in (parent.id, child.id):
            fresh = store.get_conversation(conv_id)
            assert fresh.runner_id == "runner-before"
            assert fresh.account_transition is None
        assert detached_stops == []
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_a_previous_runner_that_will_not_stop_blocks_a_launch_on_its_host(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        marker = (await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())).json()[
            "account_transition"
        ]

        async def stop(*args, **kwargs):
            return False

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        response = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners", json=launch_body(conv, marker)
        )
        assert response.status_code == 409, response.text
        assert "Previous runner" in response.text
        fresh = store.get_conversation(conv.id)
        assert fresh.runner_id is None
        assert fresh.account_transition["id"] == marker["id"]
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_stray_bind_and_two_launches_cannot_strand_a_pending_move(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        marker = (await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())).json()[
            "account_transition"
        ]
        # A launch that read the session before the move began binds a runner afterwards.
        assert store.set_runner_id(conv.id, "stray") is False
        plain = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners",
            json={"session_id": conv.id, "workspace": _WORKSPACE},
        )
        assert plain.status_code == 409, plain.text
        assert store.get_conversation(conv.id).runner_id is None

        async def stop(*args, **kwargs):
            return True

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        responder = asyncio.create_task(_serve_one_launch(comm, launch_status="launched"))
        path = f"/v1/hosts/{_HOST_ID}/runners"
        results = await asyncio.gather(
            client.post(path, json=launch_body(conv, marker)),
            client.post(path, json=launch_body(conv, marker)),
        )
        await responder
        codes = sorted(result.status_code for result in results)
        assert codes[0] == 200 and codes[1] in {400, 409}, [r.text for r in results]
        fresh = store.get_conversation(conv.id)
        assert fresh.account_transition is None
        assert fresh.runner_id not in {None, "stray", "runner-before"}
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_create_gates_reject_unregistered_native_routes(app, client, db_uri, configured):
    comm = await _connect_host(app)
    try:
        executor = {"type": "omnigent", "config": {"harness": "claude-native"}}
        agent = await create_test_agent(client, executor=executor, include_llm=False)
        from_agent = await client.post(
            "/v1/sessions",
            json={"agent_id": agent["id"], "host_id": _HOST_ID, "workspace": _WORKSPACE},
        )
        assert from_agent.status_code == 400, from_agent.text
        bundle = build_agent_bundle("gated", executor=executor, include_llm=False)
        import json

        from_bundle = await client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({"host_id": _HOST_ID, "workspace": _WORKSPACE})},
            files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        )
        assert from_bundle.status_code == 400, from_bundle.text
        assert "not registered" in from_bundle.text and "not registered" in from_agent.text
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_cancel_succeeds_when_the_failed_attempt_is_already_unbound_and_its_host_is_gone(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    store, conv = await make_bound(client, db_uri)
    marker = (await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())).json()[
        "account_transition"
    ]
    assert store.bind_account_launch(conv.id, marker["id"], "failed-attempt", _HOST_ID, _WORKSPACE)
    # A failed launch unbinds the attempt but keeps the marker's record of it.
    store.clear_host_binding(conv.id, expected_runner_id="failed-attempt")
    stored = store.get_conversation(conv.id)
    assert stored.runner_id is None
    assert stored.account_transition["launch_runner_id"] == "failed-attempt"
    await comm.send_input({"type": "websocket.disconnect", "code": 1000})
    await comm.wait()
    assert app.state.host_registry.get(_HOST_ID) is None

    async def never(*args, **kwargs):
        raise AssertionError("an unbound attempt needs no confirmed stop")

    monkeypatch.setattr(sessions, "_stop_session_host_runner", never)
    response = await client.patch(
        f"/v1/sessions/{conv.id}",
        json={"abandon_transition": True, "transition_id": marker["id"]},
    )
    assert response.status_code == 200, response.text
    fresh = store.get_conversation(conv.id)
    assert fresh.account_transition is None and fresh.runner_id is None
    # With the marker gone nothing blocks the session's events any more.
    event = await client.post(
        f"/v1/sessions/{conv.id}/events", json={"type": "message", "data": {"text": "hi"}}
    )
    assert "account change is pending" not in event.text


async def test_cancel_restores_the_model_and_effort_from_before_the_move(
    app, client, db_uri, configured
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        marker = (
            await client.patch(
                f"/v1/sessions/{conv.id}",
                json=begin_payload(target_model=OTHER_MODEL, target_effort="low"),
            )
        ).json()["account_transition"]
        step_two = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={
                "model_override": OTHER_MODEL,
                "reasoning_effort": "low",
                "transition_id": marker["id"],
                "silent": True,
            },
        )
        assert step_two.status_code == 200, step_two.text
        assert store.get_conversation(conv.id).model_override == OTHER_MODEL
        response = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={"abandon_transition": True, "transition_id": marker["id"]},
        )
        assert response.status_code == 200, response.text
        fresh = store.get_conversation(conv.id)
        assert (fresh.model_override, fresh.reasoning_effort) == (MODEL, "high")
        assert fresh.account_transition is None
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_cancel_while_the_destination_is_starting_stops_the_launched_runner(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        marker = (await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())).json()[
            "account_transition"
        ]
        confirmed = []

        async def stop(session_id, host_id, runner_id, registry, **kwargs):
            confirmed.append(runner_id)
            return True

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        detached_stops.clear()
        cancelled = []

        async def host_that_is_cancelled_mid_launch():
            for _ in range(40):
                output = await comm.receive_output(timeout=30)
                if output["type"] != "websocket.send":
                    continue
                frame = decode_host_frame(output["text"])
                if isinstance(frame, HostStatFrame):
                    reply = HostStatResultFrame(
                        request_id=frame.request_id,
                        status="ok",
                        exists=True,
                        type="directory",
                        canonical_path=frame.path,
                    )
                    await comm.send_input(
                        {"type": "websocket.receive", "text": encode_host_frame(reply)}
                    )
                elif isinstance(frame, HostLaunchRunnerFrame):
                    # The user cancels after the launch frame went out, before the ack.
                    cancelled.append(
                        await client.patch(
                            f"/v1/sessions/{conv.id}",
                            json={"abandon_transition": True, "transition_id": marker["id"]},
                        )
                    )
                    result = HostLaunchRunnerResultFrame(
                        request_id=frame.request_id, status="launched", runner_id="from_host"
                    )
                    await comm.send_input(
                        {"type": "websocket.receive", "text": encode_host_frame(result)}
                    )
                    return
            raise AssertionError("no launch frame")

        responder = asyncio.create_task(host_that_is_cancelled_mid_launch())
        response = await client.post(
            f"/v1/hosts/{_HOST_ID}/runners", json=launch_body(conv, marker)
        )
        await responder
        assert cancelled[0].status_code == 200, cancelled[0].text
        assert response.status_code == 409, response.text
        await poll_until_async(lambda: detached_stops)
        assert len(detached_stops) == 1
        assert detached_stops[0].startswith("runner_token_")
        assert "runner-before" not in detached_stops
        fresh = store.get_conversation(conv.id)
        assert fresh.account_transition is None and fresh.runner_id is None
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_a_session_without_a_host_binding_cannot_be_moved(app, client, db_uri, configured):
    comm = await _connect_host(app)
    try:
        agent = await create_test_agent(
            client,
            executor={"type": "omnigent", "config": {"harness": "codex-native"}},
            include_llm=False,
        )
        store = SqlAlchemyConversationStore(db_uri)
        conv = store.create_conversation(
            agent_id=agent["id"], runner_id="cli-runner", model_override=MODEL
        )
        response = await client.patch(f"/v1/sessions/{conv.id}", json=begin_payload())
        assert response.status_code == 409, response.text
        assert "no host binding" in response.text
        assert store.get_conversation(conv.id).runner_id == "cli-runner"
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()


async def test_cancel_keeps_the_destination_settings_when_an_attempt_left_the_session_there(
    app, client, db_uri, configured, monkeypatch
):
    comm = await _connect_host(app)
    try:
        store, conv = await make_bound(client, db_uri)
        # The session starts on another host and moves to the registered one.
        old_host = "f" * 32
        store.set_host_id(conv.id, old_host, _WORKSPACE)
        marker = (
            await client.patch(
                f"/v1/sessions/{conv.id}",
                json=begin_payload(target_model=OTHER_MODEL, target_effort="low"),
            )
        ).json()["account_transition"]
        assert store.get_conversation(conv.id).account_transition["previous_host_id"] == old_host
        edit = {"model_override": OTHER_MODEL, "reasoning_effort": "low", "silent": True}
        step_two = await client.patch(
            f"/v1/sessions/{conv.id}", json={**edit, "transition_id": marker["id"]}
        )
        assert step_two.status_code == 200, step_two.text
        # The attempt leaves the session bound to the destination host.
        assert store.bind_account_launch(conv.id, marker["id"], "attempt-1", _HOST_ID, _WORKSPACE)

        async def stop(*args, **kwargs):
            return True

        monkeypatch.setattr(sessions, "_stop_session_host_runner", stop)
        response = await client.patch(
            f"/v1/sessions/{conv.id}",
            json={"abandon_transition": True, "transition_id": marker["id"]},
        )
        assert response.status_code == 200, response.text
        fresh = store.get_conversation(conv.id)
        assert fresh.account_transition is None and fresh.host_id == _HOST_ID
        assert (fresh.model_override, fresh.reasoning_effort) == (OTHER_MODEL, "low")
    finally:
        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait()
