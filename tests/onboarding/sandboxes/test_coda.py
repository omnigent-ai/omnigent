from __future__ import annotations

from types import SimpleNamespace

import click
import pytest

from omnigent.onboarding.sandboxes.coda import CodaProvider, _safe_control_error_detail


class FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.responses: dict[str, dict[str, object]] = {
            "/api/omnigent-host/status": {"ready": True},
            "/api/omnigent-host/lease": {"ok": True},
            "/api/omnigent-host/connect": {"workspace": "/app/python/source_code/session-a"},
            "/api/omnigent-host/disconnect": {"ok": True},
        }

    def __call__(self, method: str, path: str, body: object) -> dict[str, object]:
        self.calls.append((method, path, body))
        return self.responses[path]


def launcher(control: FakeControl) -> CodaProvider:
    return CodaProvider(
        app_name="coda-main",
        app_url="https://coda-main.example.com",
        request_fn=control,
        app_getter=lambda _: SimpleNamespace(compute_status=SimpleNamespace(state="ACTIVE")),
    )


def test_control_error_detail_redacts_credentials() -> None:
    detail = _safe_control_error_detail(
        '{"error":"bad request","host_token":"launch-secret",'
        '"nested":{"authorization":"Bearer x"}}'
    )

    assert "launch-secret" not in detail
    assert "Bearer x" not in detail
    assert detail == (
        '{"error":"bad request","host_token":"<redacted>","nested":{"authorization":"<redacted>"}}'
    )


@pytest.mark.parametrize(
    "raw",
    [
        '{"message":"Authorization: Bearer launch-secret"}',
        '{"error":"token=launch-secret"}',
        '{"details":["host token launch-secret"]}',
        '{"password":"launch-secret"}',
    ],
)
def test_control_error_detail_redacts_credentials_in_values(raw: str) -> None:
    detail = _safe_control_error_detail(raw)

    assert "launch-secret" not in detail
    assert "<redacted>" in detail


def test_capabilities() -> None:
    caps = launcher(FakeControl()).capabilities
    assert caps.cli_bootstrap is False
    assert caps.managed_launch is True
    assert caps.local_port_forward is False
    assert caps.resume_stopped is False
    assert caps.programmatic_terminate is True


def test_prepare_validates_without_mutating() -> None:
    control = FakeControl()
    launcher(control).prepare()
    assert control.calls == [("GET", "/api/omnigent-host/status", None)]


def test_prepare_rejects_inactive_app() -> None:
    provider = CodaProvider(
        app_name="coda-main",
        app_url="https://coda-main.example.com",
        request_fn=FakeControl(),
        app_getter=lambda _: SimpleNamespace(compute_status=SimpleNamespace(state="STOPPED")),
    )
    with pytest.raises(click.ClickException, match="not ACTIVE"):
        provider.prepare()


def test_provision_acquires_fenced_lease() -> None:
    control = FakeControl()
    sandbox_id = launcher(control).provision("managed-abcd")
    assert sandbox_id.startswith("coda:coda-main#")
    method, path, body = control.calls[-1]
    assert (method, path) == ("POST", "/api/omnigent-host/lease")
    assert body["lease_id"] == sandbox_id.rsplit("#", 1)[1]


def test_start_host_posts_identity_and_returns_reported_workspace() -> None:
    control = FakeControl()
    provider = launcher(control)
    sandbox_id = provider.provision("managed-abcd")
    stages: list[str] = []
    workspace = provider.start_host(
        sandbox_id,
        token="launch-token",
        host_id="host_abcd",
        host_name="managed-abcd",
        server_url="https://omnigent.example.com",
        repo_url=None,
        host_config={"providers": {}},
        on_stage=stages.append,
    )
    assert workspace == "/app/python/source_code/session-a"
    assert stages == ["cloning", "starting"]
    _, path, body = control.calls[-1]
    assert path == "/api/omnigent-host/connect"
    assert body["host_token"] == "launch-token"
    assert body["host_id"] == "host_abcd"
    assert body["lease_id"] == sandbox_id.rsplit("#", 1)[1]


def test_terminate_releases_without_app_lifecycle_calls() -> None:
    control = FakeControl()
    provider = launcher(control)
    sandbox_id = provider.provision("managed-abcd")
    provider.terminate(sandbox_id)
    assert control.calls[-1] == (
        "POST",
        "/api/omnigent-host/disconnect",
        {"lease_id": sandbox_id.rsplit("#", 1)[1], "scrub": True},
    )
    forbidden = ("stop", "delete", "deploy", "update", "create-update")
    assert not any(word in path for _, path, _ in control.calls for word in forbidden)


def test_terminate_is_best_effort() -> None:
    def fail(_method: str, _path: str, _body: object) -> dict[str, object]:
        raise click.ClickException("network down")

    provider = CodaProvider(
        app_name="coda-main",
        app_url="https://coda-main.example.com",
        request_fn=fail,
        app_getter=lambda _: None,
    )
    provider.terminate("coda:coda-main#lease-a")
