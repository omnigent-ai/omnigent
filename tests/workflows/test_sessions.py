"""Exercise the workflow adapter through the real SDK's HTTP/SSE contract."""

from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest
from omnigent_client import SessionsNamespace

from omnigent.workflows.engine import WorkflowRunner
from omnigent.workflows.execution import open_executor
from omnigent.workflows.models import Workflow
from omnigent.workflows.sessions import SessionExecutor
from omnigent.workflows.state import ExecutionBinding, FeedbackEvent


@pytest.mark.parametrize(
    "placement,executor",
    [
        (
            {
                "kind": "sandbox",
                "provider": "test-provider",
                "workspace": "https://github.com/example/repo",
            },
            SessionExecutor,
        ),
        (
            {
                "kind": "sandbox",
                "provider": "test-provider",
                "workspace": "https://github.com/example/repo",
            },
            None,
        ),
        ({"kind": "host", "host_id": "host_test", "workspace": "/workspace"}, SessionExecutor),
    ],
)
@pytest.mark.parametrize(
    "execution",
    [
        "parallel",
        "profile",
        "provision",
        "persisted-text",
        "native",
        "resume-owner",
        "missing-session",
    ],
)
async def test_sessions_are_isolated_per_member_and_lead_reuses_session(
    workflow: Workflow,
    placement: dict,
    execution: str,
    executor: type[SessionExecutor] | None,
    monkeypatch,
    tmp_path,
) -> None:
    owner = execution in {"resume-owner", "missing-session"}
    provisioning = execution == "provision" and placement["kind"] == "sandbox"
    data = workflow.model_dump()
    if owner:
        data["teams"][0]["strategy"] = "owner"
    for member in data["teams"][0]["members"]:
        member["placement"] = dict(placement)
        if placement["kind"] == "sandbox" and member["id"] == "reviewer":
            member["placement"]["provider"] = "another-provider"
    workflow = Workflow.model_validate(data)
    creates: list[dict] = []
    messages: list[tuple[str, dict]] = []
    gets: list[str] = []
    streams = 0
    persisted_outputs = {}
    native_history = {}
    native_polls = {}
    tokens = []
    clock = 1000.0
    monkeypatch.delenv("OMNIGENT_WORKFLOW_DATABRICKS_PROFILE", raising=False)
    if executor is None:
        monkeypatch.setenv("OMNIGENT_HOST_SLICE_KEY_ENABLED", "1")
        monkeypatch.setenv("OMNIGENT_RUNNER_SLICE_KEY", "host_caller")
        monkeypatch.delenv("OMNIGENT_REMOTE_AUTH_TOKEN", raising=False)
        monkeypatch.setattr("omnigent.session_host._session_hosts", {})
        monkeypatch.setattr("omnigent.cli_auth.load_token", lambda _url: None)
        monkeypatch.setattr(
            "omnigent.cli_auth.load_databricks_workspace_host", lambda _url: "http://test"
        )

        def fresh_token(_source):
            tokens.append(f"token-{len(tokens)}")
            return tokens[-1]

        monkeypatch.setattr(
            "omnigent.chat._ReusedDatabricksTokenSource.current_token", fresh_token
        )
        if execution == "profile":
            from omnigent.inner.databricks_executor import _DatabricksCliToken

            config = tmp_path / "databrickscfg"
            config.write_text("[chosen]\nhost = http://test\nworkspace_id = 1234\n")
            monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(config))
            monkeypatch.setenv("OMNIGENT_WORKFLOW_DATABRICKS_PROFILE", "chosen")
            monkeypatch.setenv("OMNIGENT_REMOTE_AUTH_TOKEN", "stale-env-token")
            monkeypatch.setattr(
                "omnigent.inner.databricks_executor.time", SimpleNamespace(time=lambda: clock)
            )

            def mint(profile, *, now):
                assert profile == "chosen"
                return _DatabricksCliToken(fresh_token(None), now + 3600)

            monkeypatch.setattr(
                "omnigent.inner.databricks_executor._databricks_cli_profile_token", mint
            )

    def respond(request: httpx.Request) -> httpx.Response:
        nonlocal streams, clock
        if executor is None:
            assert request.headers["Authorization"] == f"Bearer {tokens[-1]}"
            if execution == "profile":
                assert request.headers["X-Databricks-Org-Id"] == "1234"
            if request.url.path.endswith(("/events", "/stream")):
                assert "X-Databricks-Omnigent-Slice-Key" not in request.headers
        if request.url.path == "/v1/sessions":
            creates.append(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "id": f"session_{len(creates)}",
                    "agent_id": "ag_test",
                    "host_id": None if provisioning else f"host_session_{len(creates)}",
                    "runner_id": None if provisioning else f"runner_session_{len(creates)}",
                    "status": "idle",
                    "harness": "codex-native" if execution == "native" else None,
                    "created_at": 1,
                    "updated_at": 1,
                },
            )
        if request.url.path.endswith("/events"):
            session_id = request.url.path.split("/")[-2]
            messages.append((session_id, json.loads(request.content)))
            if execution == "profile" and len(messages) == 2:
                clock += 4000
            if execution == "native":
                native_polls[session_id] = 0
            return httpx.Response(200, json={})
        if request.url.path.endswith("/stream"):
            assert execution != "native", "Native bootstrap completion is not a task completion"
            streams += 1
            output = json.dumps(
                {
                    "summary": "evidence",
                    "add_nodes": [],
                    "status": "waiting" if owner and streams == 1 else "ready",
                }
            )
            persisted_outputs[request.url.path.split("/")[-2]] = output
            events = [
                {"type": "session.heartbeat", "session_id": "test"},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "response_test",
                        "status": "completed",
                        "created_at": 1,
                        "model": "test-model",
                        "output": [
                            {
                                "type": "message",
                                "role": "assistant",
                                "content": [
                                    {
                                        "type": "output_text",
                                        "text": output,
                                    },
                                ],
                            }
                        ],
                    },
                },
            ]
            if execution == "persisted-text":
                events[-1]["response"]["output"] = []
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content="".join(
                    f"event: {event['type']}\ndata: {json.dumps(event)}\n\n" for event in events
                ),
            )
        if request.url.path.endswith("/items"):
            session_id = request.url.path.split("/")[-2]
            if execution == "native":
                history = native_history.setdefault(session_id, [])
                if request.url.params["limit"] != "1":
                    native_polls[session_id] += 1
                    poll = native_polls[session_id]
                    if poll == 2:
                        history[:0] = [
                            {
                                "id": f"assistant-{len(messages)}",
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "partial"}],
                            },
                            {
                                "id": f"user-{len(messages)}",
                                "type": "message",
                                "role": "user",
                                "content": [],
                            },
                        ]
                    elif poll == 3:
                        history[0]["content"][0]["text"] = json.dumps({"summary": "evidence"})
                return httpx.Response(200, json={"data": history})
            assert execution == "persisted-text"
            assert request.url.params["order"] == "desc"
            session_id = request.url.path.split("/")[-2]
            return httpx.Response(
                200,
                json={
                    "data": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [
                                {"type": "output_text", "text": persisted_outputs[session_id]}
                            ],
                        },
                        {"type": "message", "role": "user", "content": []},
                        {
                            "type": "message",
                            "role": "assistant",
                            "status": "completed",
                            "content": [{"type": "output_text", "text": "stale output"}],
                        },
                    ]
                },
            )
        if request.method == "GET" and request.url.path.startswith("/v1/sessions/"):
            gets.append(request.url.path.split("/")[-1])
            return httpx.Response(
                404 if execution == "missing-session" else 200,
                json={
                    "id": gets[-1],
                    "agent_id": "ag_test",
                    "host_id": f"host_{gets[-1]}",
                    "runner_id": f"runner_{gets[-1]}",
                    "status": "running"
                    if execution == "native" and native_polls[gets[-1]] in {2, 3}
                    else "idle",
                    "created_at": 1,
                    "updated_at": 1,
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    async def run(member_executor):
        runner = WorkflowRunner(workflow, member_executor)
        await runner.run()
        if owner:
            assert runner.status == "waiting"
            if executor is None:
                monkeypatch.setattr("omnigent.session_host._session_hosts", {})
            runner = WorkflowRunner.restore(
                runner.snapshot(),
                type(member_executor)(member_executor._sessions),
            )
            runner.enqueue(FeedbackEvent(id="continue", node_id="root", message="Continue"))
            await runner.run()
        return runner

    if executor is None:
        from omnigent.cli_auth import open_server_client

        monkeypatch.setattr(
            "omnigent.cli_auth.open_server_client",
            lambda *args, **kwargs: open_server_client(
                *args, **kwargs, transport=httpx.MockTransport(respond)
            ),
        )
        async with open_executor(ExecutionBinding(mode="server", server="http://test")) as execute:
            runner = await run(execute)
        if execution == "profile":
            assert len(tokens) == 2, "Members must share cached credentials and their refresh"
        else:
            assert len(tokens) >= 3
    else:
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            runner = await run(executor(SessionsNamespace(client, "http://test")))
    assert runner.status == ("failed" if execution == "missing-session" else "succeeded"), (
        runner.snapshot()
    )
    assert len(creates) == (1 if owner else 2)
    assert {body["labels"]["omnigent.workflow.member"] for body in creates} == (
        {"lead"} if owner else {"lead", "reviewer"}
    )
    assert all(body["labels"]["omnigent.workflow.run"] == runner.run_id for body in creates)
    assert all(body["workspace"] == placement["workspace"] for body in creates)
    if placement["kind"] == "sandbox":
        assert all(
            body["host_type"] == "managed"
            and body["sandbox_provider"]
            == (
                "another-provider"
                if body["labels"]["omnigent.workflow.member"] == "reviewer"
                else placement["provider"]
            )
            for body in creates
        )
        assert all("host_id" not in body for body in creates)
    else:
        assert all(
            body["host_id"] == "host_test" and "sandbox_provider" not in body for body in creates
        )
    lead_session = runner.nodes["root"].members["lead"].session_id
    assert [session for session, _ in messages].count(lead_session) == (
        1 if execution == "missing-session" else 2
    )
    if not owner:
        reviewer_session = runner.nodes["root"].members["reviewer"].session_id
        assert lead_session != reviewer_session
        assert [session for session, _ in messages].count(reviewer_session) == 1
        assert set(gets) == (
            {lead_session, reviewer_session} if provisioning or execution == "native" else set()
        )
        assert runner.nodes["root"].summary == "evidence"
    else:
        assert gets == [lead_session]


@pytest.mark.parametrize(
    "outcome",
    [
        "failed",
        "incomplete",
        "cancelled",
        "disconnect",
        "timeout",
        "cancel_run",
        "interrupt_failed",
        "launch_failed",
        "launch_timeout",
        "empty_output",
        "native_failed",
        "native_timeout",
        "native_cancel_run",
    ],
)
async def test_unsuccessful_streams_never_release_dependants(
    workflow: Workflow, outcome: str
) -> None:
    native = outcome.startswith("native_")
    outcome = outcome.removeprefix("native_")
    workflow = Workflow.model_validate(
        {
            **workflow.model_dump(),
            "member_timeout_seconds": 0.1 if outcome in {"timeout", "launch_timeout"} else 5,
        }
    )
    opened = asyncio.Event()
    interrupted: list[str] = []

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield (
                b"event: session.heartbeat\n"
                b'data: {"type": "session.heartbeat", "session_id": "test"}\n\n'
            )
            if outcome in {"timeout", "cancel_run"}:
                opened.set()
                await asyncio.Event().wait()
            elif outcome != "disconnect":
                status = "failed" if outcome == "interrupt_failed" else outcome
                response = {
                    "type": f"response.{status}",
                    "response": {
                        "id": "response_test",
                        "status": "completed" if outcome == "empty_output" else status,
                        "created_at": 1,
                        "model": "test-model",
                    },
                }
                if outcome == "empty_output":
                    response["type"] = "response.completed"
                yield f"event: {response['type']}\ndata: {json.dumps(response)}\n\n".encode()

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/sessions":
            member = json.loads(request.content)["labels"]["omnigent.workflow.member"]
            return httpx.Response(
                200,
                json={
                    "id": f"session_{member}",
                    "agent_id": "ag_test",
                    "host_id": None if outcome.startswith("launch_") else f"host_{member}",
                    "runner_id": None if outcome.startswith("launch_") else f"runner_{member}",
                    "status": "idle",
                    "harness": "codex-native" if native else None,
                    "created_at": 1,
                    "updated_at": 1,
                },
            )
        if request.url.path.endswith("/items"):
            if native:
                return httpx.Response(
                    200,
                    json={
                        "data": [
                            {
                                "id": "old-output",
                                "type": "message",
                                "role": "assistant",
                                "status": "completed",
                                "content": [{"type": "output_text", "text": "stale"}],
                            }
                        ]
                    },
                )
            assert outcome == "empty_output"
            return httpx.Response(200, json={"data": []})
        if request.method == "GET" and native:
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.split("/")[-1],
                    "agent_id": "ag_test",
                    "status": "failed" if outcome == "failed" else "idle",
                    "created_at": 1,
                    "last_task_error": {"message": "native failure"}
                    if outcome == "failed"
                    else None,
                },
            )
        if request.method == "GET" and outcome == "launch_failed":
            return httpx.Response(
                200,
                json={
                    "id": request.url.path.split("/")[-1],
                    "agent_id": "ag_test",
                    "status": "idle",
                    "created_at": 1,
                    "sandbox_status": {"stage": "failed", "error": "Sandbox provisioning failed"},
                },
            )
        if request.url.path.endswith("/stream"):
            assert not native
            assert not outcome.startswith("launch_")
            return httpx.Response(
                200, headers={"content-type": "text/event-stream"}, stream=Stream()
            )
        if request.url.path.endswith("/events"):
            if native:
                opened.set()
            if json.loads(request.content)["type"] == "interrupt":
                interrupted.append(request.url.path.split("/")[-2])
                if outcome == "interrupt_failed":
                    return httpx.Response(500, json={"error": "interrupt unavailable"})
            return httpx.Response(200, json={})
        raise AssertionError(str(request.url))

    async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
        runner = WorkflowRunner(
            workflow, SessionExecutor(SessionsNamespace(client, "http://test"))
        )
        if outcome == "cancel_run":
            task = asyncio.create_task(runner.run())
            await opened.wait()
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        else:
            await runner.run()
    assert runner.status == ("cancelled" if outcome == "cancel_run" else "failed")
    if native and outcome == "failed":
        assert "native failure" in runner.nodes["root"].error
    elif outcome in {"failed", "incomplete", "cancelled"}:
        assert f"status '{outcome}'" in runner.nodes["root"].error
    elif outcome == "disconnect":
        assert "without a completed turn" in runner.nodes["root"].error
    elif outcome in {"timeout", "launch_timeout"}:
        assert "TimeoutError" in runner.nodes["root"].error
    elif outcome == "launch_failed":
        assert "Sandbox provisioning failed" in runner.nodes["root"].error
    elif outcome == "empty_output":
        assert "no text in stream or transcript" in runner.nodes["root"].error
    elif outcome == "interrupt_failed":
        assert "status 'failed'" in runner.nodes["root"].error
        assert all(
            "Could not interrupt" in member.cleanup_error
            for member in runner.nodes["root"].members.values()
        )
    assert set(interrupted) == {"session_lead", "session_reviewer"}
    assert all(member.session_id for member in runner.nodes["root"].members.values())
