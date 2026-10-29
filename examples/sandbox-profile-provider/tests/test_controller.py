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


@pytest.mark.parametrize("failure", ["denied", "changed", "gone", "secret", "large", "redirect"])
def test_adapter_bounds_and_sanitizes_responses(monkeypatch, failure):
    monkeypatch.setenv("SANDBOX_CONTROLLER_TOKEN", "service-secret")
    original = httpx.Client

    def dispatch(request):
        assert request.headers["X-Sandbox-User"] == "alice"
        assert request.headers["X-Sandbox-Workspace"] == "0"
        status = {
            "denied": 403,
            "changed": 409,
            "gone": 404,
            "secret": 500,
            "large": 200,
            "redirect": 302,
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
        launcher.list_profiles(SandboxProfileContext(user_id="alice", workspace_id=0))
    assert "private-provider-secret" not in str(caught.value)


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
