"""Public HTTP adapter, durable controller assignment and bounded failure behavior."""

import importlib.util
import json
from dataclasses import replace
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from omnigent.community.sandbox.profile_demo.controller import (
    ControllerConfig,
    ControllerSandboxLauncher,
)
from omnigent.onboarding.sandboxes.base import SandboxGoneError
from omnigent.onboarding.sandboxes.profiles import (
    SandboxProfileAccessDenied,
    SandboxProfileChanged,
    SandboxProfileContext,
)
from omnigent.onboarding.sandboxes.types import SandboxConfigError

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "profile_controller_simulator", ROOT / "controller_server.py"
)
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_durable_assignment_survives_catalog_update_and_restart(tmp_path, monkeypatch):
    monkeypatch.setenv("SANDBOX_CONTROLLER_TOKEN", "test-service-token")
    catalog = tmp_path / "catalog.json"
    catalog.write_text((ROOT / "catalog.json").read_text())
    state = tmp_path / "controller.json"
    server = TestClient(module.create_controller(state, catalog))
    original = httpx.Client

    def dispatch(request):
        return server.request(
            request.method,
            request.url.raw_path.decode(),
            headers=dict(request.headers),
            content=request.content,
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(dispatch), **kwargs),
    )
    config = ControllerConfig(url="http://localhost")
    launcher = ControllerSandboxLauncher(config=config)
    caller = SandboxProfileContext(user_id="alice", workspace_id=0)
    chosen = launcher.list_profiles(caller)[0]
    launcher.select_profile(caller, chosen)
    sandbox_id = launcher.provision("example")
    payload = json.loads(catalog.read_text())
    payload["profiles"][0]["config"]["image"] = "example.test/updated:2"
    catalog.write_text(json.dumps(payload))
    server = TestClient(module.create_controller(state, catalog))
    fresh = ControllerSandboxLauncher(config=config)
    scoped = replace(caller, sandbox_id=sandbox_id)
    assert fresh.list_profiles(scoped) == [chosen]
    assert fresh.list_profiles(caller)[0].revision != chosen.revision
    fresh.select_profile(scoped, chosen)
    fresh.resume(sandbox_id)
    with pytest.raises(SandboxProfileChanged):
        fresh.select_profile(scoped, chosen.model_copy(update={"revision": "wrong"}))
    with pytest.raises(SandboxProfileAccessDenied):
        fresh.list_profiles(replace(scoped, user_id="bob"))
    payload["profiles"][0]["users"] = []
    catalog.write_text(json.dumps(payload))
    with pytest.raises(SandboxProfileAccessDenied):
        fresh.select_profile(scoped, chosen)
    # Cleanup remains available after access revocation.
    assert fresh.is_running(sandbox_id) is True
    fresh.terminate(sandbox_id)
    assert fresh.is_running(sandbox_id) is False
    assert json.loads(state.read_text()) == {}


@pytest.mark.parametrize(
    "failure",
    [
        "denied",
        "changed",
        "gone",
        "secret",
        "large",
        "redirect",
        "timeout",
        "connect",
        "invalid-json",
        "invalid-shape",
        "invalid-profile",
        "missing-profiles",
        "invalid-assignment",
        "invalid-selection",
    ],
)
def test_adapter_bounds_and_sanitizes_responses(monkeypatch, failure):
    monkeypatch.setenv("SANDBOX_CONTROLLER_TOKEN", "service-secret")
    original = httpx.Client

    def dispatch(request):
        assert request.headers["X-Sandbox-User"] == "alice"
        assert request.headers["X-Sandbox-Workspace"] == "0"
        if failure in ("timeout", "connect"):
            error = httpx.ReadTimeout if failure == "timeout" else httpx.ConnectError
            raise error("private-provider-secret", request=request)
        invalid_payloads = {
            "invalid-shape": ["private-provider-secret"],
            "invalid-profile": {"profiles": [{"name": "private-provider-secret"}]},
            "missing-profiles": {"private": "private-provider-secret"},
            "invalid-assignment": {"sandbox_profile": {"name": "private-provider-secret"}},
            "invalid-selection": {"profile": {"name": "private-provider-secret"}},
        }
        if failure in invalid_payloads:
            return httpx.Response(200, json=invalid_payloads[failure])
        status = {
            "denied": 403,
            "changed": 409,
            "gone": 404,
            "secret": 500,
            "large": 200,
            "redirect": 302,
            "invalid-json": 200,
        }[failure]
        return httpx.Response(
            status,
            content=b"x" * (1024 * 1024 + 1) if failure == "large" else b"private-provider-secret",
            headers={"location": "https://elsewhere.invalid"},
        )

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(dispatch), **kwargs),
    )
    launcher = ControllerSandboxLauncher(config=ControllerConfig(url="http://localhost"))
    error = {
        "denied": SandboxProfileAccessDenied,
        "changed": SandboxProfileChanged,
        "gone": SandboxGoneError,
    }.get(failure, SandboxConfigError)
    with pytest.raises(error) as caught:
        context = SandboxProfileContext(user_id="alice", workspace_id=0)
        if failure == "invalid-selection":
            from omnigent.onboarding.sandboxes.profiles import ProviderSandboxProfile

            launcher.select_profile(
                context, ProviderSandboxProfile(id="build", name="Build", revision="r1")
            )
        else:
            launcher.list_profiles(
                replace(context, sandbox_id="sandbox")
                if failure == "invalid-assignment"
                else context
            )
    assert "private-provider-secret" not in str(caught.value)


@pytest.mark.parametrize(
    "field,value",
    [
        ("workspace", None),
        ("workspace", ""),
        ("workspace", {"private-provider-secret": "path"}),
        ("running", "false"),
        ("running", 1),
    ],
)
def test_controller_rejects_invalid_lifecycle_response(monkeypatch, field, value):
    from omnigent.onboarding.sandboxes.profiles import ProviderSandboxProfile

    monkeypatch.setenv("SANDBOX_CONTROLLER_TOKEN", "test-service-token")
    original = httpx.Client
    profile = ProviderSandboxProfile(id="build", name="Build", revision="1")

    def dispatch(request):
        payload = (
            {"profile": profile.model_dump()}
            if request.url.path.startswith("/profiles/")
            else {field: value}
        )
        return httpx.Response(200, json=payload)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(dispatch), **kwargs),
    )
    launcher = ControllerSandboxLauncher(config=ControllerConfig(url="http://localhost"))
    launcher.select_profile(SandboxProfileContext(user_id="alice", workspace_id=0), profile)
    with pytest.raises(SandboxConfigError, match="Controller returned an invalid") as caught:
        if field == "workspace":
            launcher.start_host(
                "sandbox",
                token="host-secret",
                host_id="host",
                host_name="host",
                server_url="http://server",
            )
        else:
            launcher.is_running("sandbox")
    assert "private-provider-secret" not in str(caught.value)
    assert "host-secret" not in str(caught.value)


def test_controller_requires_safe_url_and_explicit_selection():
    for url in [
        "http://remote.example",
        "https://token@remote.example",
        "https://remote.example?token=secret",
    ]:
        with pytest.raises(ValueError):
            ControllerConfig(url=url)
    launcher = ControllerSandboxLauncher(config=ControllerConfig(url="https://controller.example"))
    with pytest.raises(SandboxConfigError, match="Select a sandbox profile"):
        launcher.provision("unselected")


@pytest.mark.parametrize("profile_result", ["missing", "malformed", "mismatch"])
@pytest.mark.parametrize("cleanup", ["success", "failure", "timeout"])
def test_provision_cleans_up_invalid_assignment(monkeypatch, caplog, profile_result, cleanup):
    monkeypatch.setenv("SANDBOX_CONTROLLER_TOKEN", "test-service-token")
    original = httpx.Client
    profile = {"id": "build", "name": "Build", "revision": "1"}
    allocated = set()

    def dispatch(request):
        if request.method == "GET":
            return httpx.Response(200, json={"profiles": [profile], "profile": profile})
        if request.method == "POST":
            assert request.url.path == "/sandboxes"
            allocated.add("sb-123")
            result = {"sandbox_id": "sb-123"}
            if profile_result == "malformed":
                result["sandbox_profile"] = {"id": "build"}
            elif profile_result == "mismatch":
                result["sandbox_profile"] = {**profile, "revision": "2"}
            return httpx.Response(200, json=result)
        assert request.method == "DELETE" and request.url.path == "/sandboxes/sb-123"
        if cleanup == "failure":
            return httpx.Response(503)
        if cleanup == "timeout":
            raise httpx.ReadTimeout("cleanup timeout")
        allocated.remove("sb-123")
        return httpx.Response(204)

    monkeypatch.setattr(
        httpx,
        "Client",
        lambda **kwargs: original(transport=httpx.MockTransport(dispatch), **kwargs),
    )
    launcher = ControllerSandboxLauncher(config=ControllerConfig(url="http://localhost"))
    caller = SandboxProfileContext(user_id="alice", workspace_id=0)
    launcher.select_profile(caller, launcher.list_profiles(caller)[0])
    error = SandboxProfileChanged if profile_result == "mismatch" else SandboxConfigError
    with pytest.raises(error):
        launcher.provision("example")
    assert allocated == (set() if cleanup == "success" else {"sb-123"})
    if cleanup != "success":
        assert "Controller cleanup failed for sandbox 'sb-123'" in caplog.text
        assert "test-service-token" not in caplog.text
