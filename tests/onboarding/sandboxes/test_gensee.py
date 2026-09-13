"""Tests for the built-in Gensee sandbox provider."""

from __future__ import annotations

import json
import traceback
from collections.abc import Callable

import httpx
import pytest

from omnigent.onboarding.sandboxes.gensee import (
    GenseeControlError,
    GenseeSandboxLauncher,
    _allocation_id,
)
from omnigent.onboarding.sandboxes.types import RepoWorkspace


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def monotonic(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def _launcher(
    monkeypatch: pytest.MonkeyPatch,
    handler: Callable[[httpx.Request], httpx.Response],
    **kwargs: object,
) -> GenseeSandboxLauncher:
    monkeypatch.setenv("GENSEE_CONTROLLER_API_TOKEN", "controller-secret")
    options: dict[str, object] = {
        "endpoint": "https://sandbox.example.com/control",
        "retry_timeout_s": 0,
        "_transport": httpx.MockTransport(handler),
    }
    options.update(kwargs)
    return GenseeSandboxLauncher(
        **options,
    )


def _json(request: httpx.Request) -> dict[str, object]:
    value = json.loads(request.content)
    assert isinstance(value, dict)
    return value


def test_capabilities_are_managed_only() -> None:
    capabilities = GenseeSandboxLauncher().capabilities

    assert capabilities.managed_launch is True
    assert capabilities.programmatic_terminate is True
    assert capabilities.cli_bootstrap is False
    assert capabilities.resume_stopped is False
    assert capabilities.file_copy is False


def test_prepare_checks_credentials_and_public_readiness(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url == "https://sandbox.example.com/control/readyz"
        assert "authorization" not in request.headers
        return httpx.Response(200, json={"ok": True})

    _launcher(monkeypatch, handler).prepare()


def test_prepare_rejects_missing_api_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GENSEE_CONTROLLER_API_TOKEN", raising=False)
    launcher = GenseeSandboxLauncher(
        _transport=httpx.MockTransport(lambda _request: pytest.fail("unexpected request"))
    )

    with pytest.raises(GenseeControlError, match="GENSEE_CONTROLLER_API_TOKEN"):
        launcher.prepare()


def test_prepare_rejects_unready_controller(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher = _launcher(
        monkeypatch,
        lambda _request: httpx.Response(200, json={"ok": False}),
    )

    with pytest.raises(GenseeControlError, match="controller is not ready"):
        launcher.prepare()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"endpoint": "http://sandbox.example.com"}, "HTTPS URL"),
        ({"endpoint": "https://user@sandbox.example.com"}, "without credentials"),
        ({"endpoint": "https://sandbox.example.com:0"}, "port is out of range"),
        ({"endpoint": "https://sandbox.example.com:not-a-port"}, "invalid.*endpoint"),
        ({"api_token_env": "NOT-AN-ENV"}, "environment variable name"),
        ({"workspace_root": "relative"}, "absolute non-root"),
        ({"workspace_root": "/tmp/../workspace"}, "absolute non-root"),
        ({"workspace_root": "/workspace\0suffix"}, "NUL bytes"),
        ({"operation_timeout_s": 0}, "positive number"),
        ({"retry_timeout_s": -1}, "non-negative number"),
        ({"env": ["GOOD", "NOT-GOOD"]}, "environment variable name"),
        ({"env": ["DUPLICATE", "DUPLICATE"]}, "duplicate"),
    ],
)
def test_configuration_rejects_unsafe_values(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        GenseeSandboxLauncher(**kwargs)


def test_provision_uses_unique_ascii_allocation_ids(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payloads: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.headers["authorization"] == "Bearer controller-secret"
        payload = _json(request)
        payloads.append(payload)
        return httpx.Response(
            200,
            json={"allocation": {"allocation_id": payload["allocation_id"]}},
        )

    launcher = _launcher(monkeypatch, handler)
    first = launcher.provision("Managed Session 日本語")
    second = launcher.provision("Managed Session 日本語")

    assert first != second
    first_id = _allocation_id(first)
    assert first_id.isascii()
    assert len(first_id) <= 33
    assert payloads[0]["user_id"] == first_id
    assert payloads[0]["idempotency_key"] == f"allocate-{first_id}"


def test_start_host_uses_secret_store_and_waits_for_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_API_KEY", "model-secret")
    requests: list[tuple[str, dict[str, object]]] = []
    operation_polls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal operation_polls
        payload = _json(request) if request.content else {}
        requests.append((request.url.path, payload))
        if request.url.path.endswith("/v1/sandbox-secrets:put"):
            return httpx.Response(200, json={"accepted": True})
        if request.url.path.endswith("/v1/sandbox-operations:submit"):
            return httpx.Response(200, json={"operation": {"state": "running"}})
        operation_polls += 1
        return httpx.Response(
            200,
            json={
                "operation": {
                    "state": "succeeded",
                    "response": {"result": {"runtime": {"workspace": "/workspace/repo"}}},
                }
            },
        )

    stages: list[str] = []
    launcher = _launcher(
        monkeypatch,
        handler,
        poll_interval_s=0.001,
        env=["OPENAI_API_KEY"],
    )
    workspace = launcher.start_host(
        "gensee+controller:///managed-1",
        token="host-secret",
        host_id="host-1",
        host_name="managed-1",
        server_url="https://omnigent.example.com",
        repos=[
            RepoWorkspace(
                url="https://github.com/example/repo.git",
                branch="main",
                repo_name="repo",
            )
        ],
        host_config={"providers": {}},
        on_stage=stages.append,
    )

    assert workspace == "/workspace/repo"
    assert stages == ["cloning", "starting"]
    assert operation_polls == 1
    secret_payload = requests[0][1]
    secret = json.loads(str(secret_payload["token"]))
    assert secret == {
        "environment": {"OPENAI_API_KEY": "model-secret"},
        "host_token": "host-secret",
        "version": 1,
    }
    assert all("host-secret" not in json.dumps(payload) for _, payload in requests[1:])
    operation_payload = requests[1][1]
    operation_id = operation_payload["operation_id"]
    assert isinstance(operation_id, str)
    assert operation_id.startswith("operation-")
    assert operation_payload == {
        "protocol_version": 1,
        "allocation_id": "managed-1",
        "operation_id": operation_id,
        "idempotency_key": operation_id,
        "action": "start_omnigent_host",
        "parameters": operation_payload["parameters"],
    }
    parameters = operation_payload["parameters"]
    assert isinstance(parameters, dict)
    assert parameters == {
        "secret_id": secret_payload["secret_id"],
        "workspace": "/mnt/gensee-tclone/workspaces/managed-1",
        "host_id": "host-1",
        "host_name": "managed-1",
        "server_url": "https://omnigent.example.com",
        "repo_url": "https://github.com/example/repo.git",
        "repo_branch": "main",
        "repo_name": "repo",
        "host_config": {"providers": {}},
    }


def test_start_host_rejects_multiple_repositories_before_request(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher(
        monkeypatch,
        lambda _request: pytest.fail("multiple repositories must fail before a request"),
    )

    with pytest.raises(GenseeControlError, match="at most one repository"):
        launcher.start_host(
            "gensee+controller:///managed-1",
            token="host-secret",
            host_id="host-1",
            host_name="managed-1",
            server_url="https://omnigent.example.com",
            repos=[
                RepoWorkspace(url="https://github.com/example/a.git", branch=None, repo_name="a"),
                RepoWorkspace(url="https://github.com/example/b.git", branch=None, repo_name="b"),
            ],
        )


def test_start_host_rejects_missing_configured_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher(
        monkeypatch,
        lambda _request: pytest.fail("missing environment must fail before a request"),
        env=["MISSING_MODEL_API_KEY"],
    )

    with pytest.raises(GenseeControlError, match="MISSING_MODEL_API_KEY"):
        launcher.start_host(
            "gensee+controller:///managed-1",
            token="host-secret",
            host_id="host-1",
            host_name="managed-1",
            server_url="https://omnigent.example.com",
        )


def test_start_host_surfaces_controller_operation_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/sandbox-secrets:put"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(
            200,
            json={
                "operation": {
                    "state": "failed",
                    "response": {"error": {"message": "image unavailable"}},
                }
            },
        )

    launcher = _launcher(monkeypatch, handler)
    with pytest.raises(GenseeControlError, match="image unavailable"):
        launcher.start_host(
            "gensee+controller:///managed-1",
            token="host-secret",
            host_id="host-1",
            host_name="managed-1",
            server_url="https://omnigent.example.com",
        )


def test_start_host_surfaces_cancelled_operation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/sandbox-secrets:put"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(
            200,
            json={
                "operation": {
                    "state": "cancelled",
                    "response": {"error": {"message": "allocation released"}},
                }
            },
        )

    launcher = _launcher(monkeypatch, handler)
    with pytest.raises(GenseeControlError, match="allocation released"):
        launcher.start_host(
            "gensee+controller:///managed-1",
            token="host-secret",
            host_id="host-1",
            host_name="managed-1",
            server_url="https://omnigent.example.com",
        )


@pytest.mark.parametrize(
    "operation",
    [
        {"state": "succeeded"},
        {"state": "succeeded", "response": {}},
        {"state": "succeeded", "response": {"result": {}}},
        {"state": "succeeded", "response": {"result": {"runtime": {}}}},
    ],
)
def test_start_host_rejects_malformed_success_response(
    monkeypatch: pytest.MonkeyPatch,
    operation: dict[str, object],
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/v1/sandbox-secrets:put"):
            return httpx.Response(200, json={"accepted": True})
        return httpx.Response(200, json={"operation": operation})

    launcher = _launcher(monkeypatch, handler)
    with pytest.raises(GenseeControlError):
        launcher.start_host(
            "gensee+controller:///managed-1",
            token="host-secret",
            host_id="host-1",
            host_name="managed-1",
            server_url="https://omnigent.example.com",
        )


def test_operation_timeout_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        assert request.method == "POST", "must not poll after the deadline"
        return httpx.Response(200, json={"operation": {"state": "running"}})

    launcher = _launcher(monkeypatch, handler, operation_timeout_s=1, poll_interval_s=2)

    with pytest.raises(GenseeControlError, match=r"operation .* timed out"):
        launcher._submit_and_wait("managed-1", "start_omnigent_host", {})

    assert clock.now == 1
    assert requests == ["POST"]


@pytest.mark.parametrize("slow_method", ["POST", "GET"])
def test_operation_rejects_success_received_after_deadline(
    monkeypatch: pytest.MonkeyPatch, slow_method: str
) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)

    def handler(request: httpx.Request) -> httpx.Response:
        expected_timeout = 1 if request.method == "POST" else 0.75
        assert request.extensions["timeout"] == dict.fromkeys(
            ("connect", "read", "write", "pool"), expected_timeout
        )
        if request.method == slow_method:
            clock.sleep(40)
            return httpx.Response(200, json={"operation": {"state": "succeeded"}})
        return httpx.Response(200, json={"operation": {"state": "running"}})

    launcher = _launcher(monkeypatch, handler, operation_timeout_s=1, poll_interval_s=0.25)
    with pytest.raises(GenseeControlError, match=r"operation .* timed out"):
        launcher._submit_and_wait("managed-1", "start_omnigent_host", {})


def test_submission_and_polling_share_one_deadline(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request.method)
        assert request.method == "POST", "submission must consume the operation budget"
        clock.sleep(0.75)
        return httpx.Response(200, json={"operation": {"state": "running"}})

    launcher = _launcher(monkeypatch, handler, operation_timeout_s=1, poll_interval_s=0.5)
    with pytest.raises(GenseeControlError, match=r"operation .* timed out"):
        launcher._submit_and_wait("managed-1", "start_omnigent_host", {})
    assert clock.now == 1
    assert requests == ["POST"]


@pytest.mark.parametrize("failure", ["transport", "http"])
def test_operation_deadline_caps_request_retries(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if failure == "transport":
            raise httpx.ReadTimeout("controller-secret", request=request)
        return httpx.Response(503, text="unavailable")

    launcher = _launcher(monkeypatch, handler, operation_timeout_s=2, retry_timeout_s=60)
    with pytest.raises(GenseeControlError, match=r"operation .* timed out") as caught:
        launcher._submit_and_wait("managed-1", "start_omnigent_host", {})

    assert clock.now == 2
    assert len(requests) == 2
    assert requests[0].extensions["timeout"]["read"] == 2
    assert requests[1].extensions["timeout"]["read"] == 1
    assert requests[0].content == requests[1].content
    assert "controller-secret" not in "".join(traceback.format_exception(caught.value))


def test_request_can_succeed_within_remaining_operation_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        if request.method == "POST":
            return httpx.Response(200, json={"operation": {"state": "running"}})
        clock.sleep(0.25)
        return httpx.Response(200, json={"operation": {"state": "succeeded"}})

    launcher = _launcher(
        monkeypatch, handler, operation_timeout_s=2, poll_interval_s=0.25, retry_timeout_s=60
    )
    operation = launcher._submit_and_wait("managed-1", "start_omnigent_host", {})

    assert operation["state"] == "succeeded"
    assert clock.now == 1.5
    assert [request.extensions["timeout"]["read"] for request in requests] == [2, 1, 0.75]


@pytest.mark.parametrize(
    ("allocation_state", "observed_state", "ready", "expected"),
    [
        ("active", "running", True, True),
        ("active", "running", False, None),
        ("active", "provisioning", False, None),
        ("active", "stopped", False, False),
        ("released", "deleted", False, False),
    ],
)
def test_is_running_requires_runtime_agent_readiness(
    monkeypatch: pytest.MonkeyPatch,
    allocation_state: str,
    observed_state: str,
    ready: bool,
    expected: bool | None,
) -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "allocation": {
                    "state": allocation_state,
                    "runtime": {"observed_state": observed_state, "ready": ready},
                }
            },
        )

    launcher = _launcher(monkeypatch, handler)
    assert launcher.is_running("gensee+controller:///managed-1") is expected


def test_terminate_is_repeatable_and_sends_idempotent_release(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(_json(request))
        return httpx.Response(200, json={"allocation": {"state": "released"}})

    launcher = _launcher(monkeypatch, handler)
    launcher.terminate("gensee+controller:///managed-1")
    launcher.terminate("gensee+controller:///managed-1")

    expected = {
        "protocol_version": 1,
        "allocation_id": "managed-1",
        "idempotency_key": "release-managed-1",
    }
    assert seen == [expected, expected]


def test_terminate_accepts_an_already_absent_allocation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher = _launcher(
        monkeypatch,
        lambda _request: httpx.Response(404, json={"error": {"message": "not found"}}),
    )
    launcher.terminate("gensee+controller:///managed-1")


@pytest.mark.parametrize("failure", ["timeout", "http", "invalid-json", "non-object"])
def test_provision_releases_allocation_after_ambiguous_failure(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, _json(request)))
        if request.url.path.endswith(":release"):
            return httpx.Response(200, json={"allocation": {"state": "released"}})
        if failure == "timeout":
            raise httpx.ReadTimeout("response lost after creation", request=request)
        if failure == "http":
            return httpx.Response(503)
        if failure == "invalid-json":
            return httpx.Response(200, text="not-json")
        return httpx.Response(200, json=[])

    launcher = _launcher(monkeypatch, handler)
    with pytest.raises(GenseeControlError):
        launcher.provision("managed-1")

    assert len(requests) == 2
    allocation_id = requests[0][1]["allocation_id"]
    assert requests[1] == (
        "/control/v1/sandbox-allocations:release",
        {
            "protocol_version": 1,
            "allocation_id": allocation_id,
            "idempotency_key": f"release-{allocation_id}",
        },
    )


def test_provision_reports_allocation_id_when_cleanup_also_fails(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    allocation_id = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal allocation_id
        allocation_id = str(_json(request)["allocation_id"])
        if request.url.path.endswith(":allocate"):
            raise httpx.ReadTimeout("controller-secret", request=request)
        return httpx.Response(503, json={"error": {"message": "controller-secret"}})

    launcher = _launcher(monkeypatch, handler)
    with pytest.raises(GenseeControlError, match="cleanup failed") as caught:
        launcher.provision("managed-1")

    assert allocation_id and allocation_id in caught.value.message
    assert "request failed" in caught.value.message
    assert "HTTP 503" in caught.value.message
    assert allocation_id in caplog.text
    assert "controller-secret" not in caplog.text
    assert "controller-secret" not in "".join(traceback.format_exception(caught.value))


def test_provision_retry_reuses_allocation_without_releasing_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)
    requests: list[dict[str, object]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith(":allocate")
        payload = _json(request)
        requests.append(payload)
        if len(requests) == 1:
            raise httpx.ReadTimeout("response lost after creation", request=request)
        return httpx.Response(
            200, json={"allocation": {"allocation_id": payload["allocation_id"]}}
        )

    launcher = _launcher(monkeypatch, handler, retry_timeout_s=5)
    sandbox_id = launcher.provision("managed-1")

    assert len(requests) == 2
    assert requests[0] == requests[1]
    assert _allocation_id(sandbox_id) == requests[0]["allocation_id"]


@pytest.mark.parametrize("first_failure", ["timeout", "http"])
@pytest.mark.parametrize("retry_status", [409, 429])
def test_provision_cleans_up_when_rejected_retry_follows_ambiguous_failure(
    monkeypatch: pytest.MonkeyPatch, first_failure: str, retry_status: int
) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append((request.url.path, _json(request)))
        if request.url.path.endswith(":release"):
            return httpx.Response(200, json={"allocation": {"state": "released"}})
        if len(requests) == 1:
            if first_failure == "timeout":
                raise httpx.ReadTimeout("response lost after creation", request=request)
            return httpx.Response(503)
        return httpx.Response(retry_status)

    launcher = _launcher(monkeypatch, handler, retry_timeout_s=5)
    with pytest.raises(GenseeControlError, match=f"HTTP {retry_status}") as caught:
        launcher.provision("managed-1")

    assert len(requests) == 3
    assert requests[0] == requests[1]
    allocation_id = requests[0][1]["allocation_id"]
    assert requests[2][0] == "/control/v1/sandbox-allocations:release"
    assert requests[2][1]["allocation_id"] == allocation_id
    assert str(allocation_id) in caught.value.message


def test_provision_releases_requested_allocation_after_mismatched_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests: list[tuple[str, dict[str, object]]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        payload = _json(request)
        requests.append((request.url.path, payload))
        if request.url.path.endswith(":allocate"):
            return httpx.Response(
                200,
                json={"allocation": {"allocation_id": "INVALID_ID"}},
            )
        return httpx.Response(200, json={"allocation": {"state": "released"}})

    launcher = _launcher(monkeypatch, handler)
    with pytest.raises(GenseeControlError, match="unexpected allocation ID"):
        launcher.provision("managed-1")

    requested_id = requests[0][1]["allocation_id"]
    assert requests[1] == (
        "/control/v1/sandbox-allocations:release",
        {
            "protocol_version": 1,
            "allocation_id": requested_id,
            "idempotency_key": f"release-{requested_id}",
        },
    )


@pytest.mark.parametrize(
    "failure", ["validation", "http-message", "html", "transport", "operation"]
)
def test_start_host_errors_do_not_expose_credentials(
    monkeypatch: pytest.MonkeyPatch, failure: str
) -> None:
    dummy_api_key = 'model-"secret\\with\nnewline-\u0394'
    monkeypatch.setenv("OPENAI_API_KEY", dummy_api_key)
    reflected_input = ""

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal reflected_input
        if request.url.path.endswith(":put"):
            reflected_input = request.content.decode()
            if failure == "operation":
                return httpx.Response(200, json={"accepted": True})
        detail = f"invalid launch: controller-secret; {dummy_api_key}; {reflected_input}"
        if failure == "validation":
            return httpx.Response(422, json={"detail": [{"msg": "bad input", "input": detail}]})
        if failure == "http-message":
            return httpx.Response(422, json={"error": {"message": detail, "input": detail}})
        if failure == "html":
            return httpx.Response(400, text=f"<html>{detail}</html>")
        if failure == "transport":
            raise httpx.ConnectError(detail, request=request)
        return httpx.Response(
            200,
            json={"operation": {"state": "failed", "response": {"error": {"message": detail}}}},
        )

    launcher = _launcher(monkeypatch, handler, env=["OPENAI_API_KEY"])
    with pytest.raises(GenseeControlError) as caught:
        launcher.start_host(
            "gensee+controller:///managed-1",
            token="host-secret",
            host_id="host-1",
            host_name="managed-1",
            server_url="https://omnigent.example.com",
        )

    rendered = "".join(traceback.format_exception(caught.value))
    for secret in ("controller-secret", "host-secret", dummy_api_key):
        assert secret not in rendered
        escaped = json.dumps(secret)[1:-1]
        assert escaped not in rendered
        assert json.dumps(escaped)[1:-1] not in rendered
    if failure in {"http-message", "transport", "operation"}:
        assert "invalid launch" in caught.value.message
        assert "[redacted]" in caught.value.message
    else:
        assert "invalid launch" not in caught.value.message
        assert "HTTP" in caught.value.message


def test_error_redaction_happens_before_truncation(monkeypatch: pytest.MonkeyPatch) -> None:
    dummy_api_key = "model-secret-" + "s" * 5000
    monkeypatch.setenv("OPENAI_API_KEY", dummy_api_key)
    launcher = _launcher(
        monkeypatch,
        lambda _request: httpx.Response(
            400,
            json={"error": {"message": f"invalid value: {dummy_api_key} trailing diagnostic"}},
        ),
        env=["OPENAI_API_KEY"],
    )
    with pytest.raises(GenseeControlError) as caught:
        launcher.start_host(
            "gensee+controller:///managed-1",
            token="host-secret",
            host_id="host-1",
            host_name="managed-1",
            server_url="https://omnigent.example.com",
        )
    assert "model-secret-" not in caught.value.message
    assert "[redacted] trailing diagnostic" in caught.value.message


def test_transient_response_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, text="temporarily unavailable")
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time.sleep", lambda _s: None)
    launcher = _launcher(monkeypatch, handler, retry_timeout_s=5)
    launcher.prepare()

    assert attempts == 2


def test_transport_error_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time.sleep", lambda _s: None)
    launcher = _launcher(monkeypatch, handler, retry_timeout_s=5)
    launcher.prepare()

    assert attempts == 2


def test_transport_error_is_wrapped_after_retry_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection reset", request=request)

    launcher = _launcher(monkeypatch, handler)
    with pytest.raises(GenseeControlError, match="request failed: connection reset"):
        launcher.prepare()


def test_nontransient_error_is_not_retried_and_body_is_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        if request.url.path.endswith(":release"):
            return httpx.Response(404)
        attempts += 1
        return httpx.Response(409, content=b"x" * 5000)

    launcher = _launcher(monkeypatch, handler, retry_timeout_s=5)
    with pytest.raises(GenseeControlError) as caught:
        launcher.provision("managed-1")

    assert attempts == 1
    assert "HTTP 409" in caught.value.message
    assert len(caught.value.message) < 4200


def test_retry_budget_does_not_start_an_attempt_after_its_deadline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock()
    monkeypatch.setattr("omnigent.onboarding.sandboxes.gensee.time", clock)
    attempts = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503)

    launcher = _launcher(monkeypatch, handler, retry_timeout_s=1)
    with pytest.raises(GenseeControlError, match="HTTP 503"):
        launcher.prepare()
    assert attempts == 1
    assert clock.now == 1


@pytest.mark.parametrize(
    "sandbox_id",
    [
        "not-gensee:///managed-1",
        "gensee+controller://host/managed-1",
        "gensee+controller:///../managed-1",
        "gensee+controller:///managed_1",
        "gensee+controller:///",
    ],
)
def test_invalid_sandbox_ids_fail_before_request(
    monkeypatch: pytest.MonkeyPatch, sandbox_id: str
) -> None:
    launcher = _launcher(
        monkeypatch,
        lambda _request: pytest.fail("invalid IDs must not reach the controller"),
    )

    with pytest.raises(GenseeControlError, match="invalid Gensee sandbox ID"):
        launcher.terminate(sandbox_id)


@pytest.mark.parametrize(
    "response",
    [
        httpx.Response(200, text="not-json"),
        httpx.Response(200, json=[]),
        httpx.Response(200, json={"unexpected": {}}),
        httpx.Response(200, json={"allocation": {"allocation_id": ""}}),
    ],
)
def test_malformed_controller_responses_fail_closed(
    monkeypatch: pytest.MonkeyPatch, response: httpx.Response
) -> None:
    launcher = _launcher(monkeypatch, lambda _request: response)

    with pytest.raises(GenseeControlError):
        launcher.provision("managed-1")
