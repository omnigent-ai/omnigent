"""Tests for the C1 CoDA managed-host provider foundation."""

from __future__ import annotations

from types import SimpleNamespace

import click
import pytest

from omnigent.onboarding.sandboxes.coda import (
    CodaProvider,
    validate_coda_app_url,
)


class _FakeControl:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str, object]] = []
        self.responses: dict[str, dict[str, object]] = {
            "/api/omnigent-host/status": {"ready": True},
            "/api/omnigent-host/lease": {},
            "/api/omnigent-host/connect": {"workspace": "/app/python/source_code"},
            "/api/omnigent-host/disconnect": {},
        }

    def __call__(self, method: str, path: str, body: object) -> dict[str, object]:
        self.calls.append((method, path, body))
        return self.responses[path]


def _provider(control: _FakeControl) -> CodaProvider:
    return CodaProvider(
        app_name="synthetic-coda",
        app_url="https://synthetic-coda.aws.databricksapps.com/",
        request_fn=control,
        app_getter=lambda _name: SimpleNamespace(compute_status=SimpleNamespace(state="ACTIVE")),
    )


def test_capabilities_are_managed_only() -> None:
    capabilities = _provider(_FakeControl()).capabilities

    assert capabilities.cli_bootstrap is False
    assert capabilities.managed_launch is True
    assert capabilities.local_port_forward is False
    assert capabilities.resume_stopped is False
    assert capabilities.programmatic_terminate is True


@pytest.mark.parametrize(
    "app_url",
    [
        "plain text",
        "http://synthetic-coda.aws.databricksapps.com",
        "https://synthetic-coda.aws.notdatabricksapps.com",
        "https://synthetic-coda.aws.databricksapps.com.attacker.test",
        "https://user:password@synthetic-coda.aws.databricksapps.com",
        "https://synthetic-coda.aws.databricksapps.com:443",
        "https://synthetic-coda.aws.databricksapps.com/path",
        "https://synthetic-coda.aws.databricksapps.com/?query=1",
        "https://synthetic-coda.aws.databricksapps.com/#fragment",
        "https://databricksapps.com",
    ],
)
def test_coda_app_url_requires_trusted_https_shape(app_url: str) -> None:
    with pytest.raises(ValueError, match="CoDA app_url"):
        validate_coda_app_url(app_url)
    with pytest.raises(ValueError, match="CoDA app_url"):
        CodaProvider(app_name="synthetic-coda", app_url=app_url)


def test_prepare_checks_compute_then_control_plane_without_mutation() -> None:
    control = _FakeControl()

    _provider(control).prepare()

    assert control.calls == [("GET", "/api/omnigent-host/status", None)]


@pytest.mark.parametrize("ready", [None, False, "true", 1])
def test_prepare_requires_boolean_ready_true(ready: object) -> None:
    control = _FakeControl()
    control.responses["/api/omnigent-host/status"] = {"ready": ready}

    with pytest.raises(click.ClickException, match="not ready"):
        _provider(control).prepare()


def test_prepare_rejects_non_active_app_without_control_request() -> None:
    control = _FakeControl()
    provider = CodaProvider(
        app_name="synthetic-coda",
        app_url="https://synthetic-coda.aws.databricksapps.com",
        request_fn=control,
        app_getter=lambda _name: SimpleNamespace(compute_status=SimpleNamespace(state="STOPPED")),
    )

    with pytest.raises(click.ClickException, match="not ACTIVE"):
        provider.prepare()
    assert control.calls == []


def test_provision_acquires_fenced_lease() -> None:
    control = _FakeControl()
    sandbox_id = _provider(control).provision("managed-synthetic")

    method, path, body = control.calls[-1]
    assert (method, path) == ("POST", "/api/omnigent-host/lease")
    assert sandbox_id.startswith("coda:synthetic-coda#")
    assert isinstance(body, dict)
    assert body["action"] == "acquire"
    assert body["app_name"] == "synthetic-coda"
    assert body["host_name"] == "managed-synthetic"
    assert body["lease_id"] == sandbox_id.rsplit("#", 1)[1]


def test_start_host_posts_identity_and_returns_absolute_workspace() -> None:
    control = _FakeControl()
    provider = _provider(control)
    sandbox_id = provider.provision("managed-synthetic")
    stages: list[str] = []

    workspace = provider.start_host(
        sandbox_id,
        token="launch-token",
        host_id="host-synthetic",
        host_name="managed-synthetic",
        server_url="https://server.example.test",
        repo_url="https://git.example.test/repo",
        repo_branch="main",
        repo_name="repo",
        host_config={"providers": {}},
        on_stage=stages.append,
    )

    assert workspace == "/app/python/source_code"
    assert stages == ["cloning", "starting"]
    _, path, body = control.calls[-1]
    assert path == "/api/omnigent-host/connect"
    assert body == {
        "server_url": "https://server.example.test",
        "host_token": "launch-token",
        "host_id": "host-synthetic",
        "host_name": "managed-synthetic",
        "host_config": {"providers": {}},
        "repo_url": "https://git.example.test/repo",
        "repo_branch": "main",
        "repo_name": "repo",
        "lease_id": sandbox_id.rsplit("#", 1)[1],
    }


def test_start_host_rejects_mismatched_and_relative_workspace() -> None:
    control = _FakeControl()
    provider = _provider(control)

    with pytest.raises(click.ClickException, match="different app"):
        provider.start_host(
            "coda:other-app#lease",
            token="token",
            host_id="host",
            host_name="host",
            server_url="https://server.example.test",
        )

    control.responses["/api/omnigent-host/connect"] = {"workspace": "relative"}
    with pytest.raises(click.ClickException, match="absolute workspace"):
        provider.start_host(
            "coda:synthetic-coda#lease",
            token="token",
            host_id="host",
            host_name="host",
            server_url="https://server.example.test",
        )


def test_terminate_disconnects_and_never_manages_app_lifecycle() -> None:
    control = _FakeControl()
    provider = _provider(control)

    provider.terminate("coda:synthetic-coda#lease")

    assert control.calls == [
        (
            "POST",
            "/api/omnigent-host/disconnect",
            {"lease_id": "lease", "scrub": True},
        )
    ]


def test_terminate_is_best_effort_and_bad_ids_are_rejected() -> None:
    def fail(_method: str, _path: str, _body: object) -> dict[str, object]:
        raise click.ClickException("network down")

    provider = CodaProvider(
        app_name="synthetic-coda",
        app_url="https://synthetic-coda.aws.databricksapps.com",
        request_fn=fail,
        app_getter=lambda _name: None,
    )
    provider.terminate("coda:synthetic-coda#lease")

    with pytest.raises(click.ClickException, match="invalid CoDA sandbox id"):
        provider.terminate("not-coda")
