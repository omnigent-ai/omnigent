"""Owner-identity mode of the Databricks Sandbox launcher.

With ``identity="owner"`` every Sandbox REST call and the in-sandbox proxy
bearer use the host owner's connected Databricks token — never the server's
own identity — and an unconnected owner is refused. The SDK client is replaced
by fakes that capture how the launcher builds it, so these tests pin which
host and which credential every request carries.
"""

from __future__ import annotations

from typing import Any

import pytest

from omnigent.onboarding.sandboxes import databricks_sandbox as mod
from omnigent.onboarding.sandboxes.base import OwnerCredentialMissingError
from omnigent.onboarding.sandboxes.databricks_sandbox import DatabricksSandboxLauncher

_HOST = "https://owner-ws.cloud.databricks.com"


class _FakeApiClient:
    """Records each request together with the Authorization header it carried."""

    def __init__(self, config: _FakeConfig) -> None:
        self._config = config
        self.calls: list[dict[str, Any]] = []

    def do(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        auth = self._config.authenticate()["Authorization"]
        self.calls.append({"method": method, "path": path, "auth": auth, **kwargs})
        if method == "GET" and path.startswith("/api/2.0/sandboxes/"):
            return {"status": {"state": "SANDBOX_STATE_RUNNING"}}
        if "/exec-sync" in path:
            return {
                "status": "EXECUTE_COMMAND_STATUS_COMPLETED",
                "exit_code": 0,
                "stdout": "/home/u/.omnigent/proxy-bearer",
            }
        return {}


class _FakeConfig:
    """Captures the SDK ``Config`` kwargs; authenticates through the strategy."""

    built: list[_FakeConfig] = []

    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        strategy = kwargs.get("credentials_strategy")
        self._headers = (
            strategy(self)
            if strategy is not None
            else (lambda: {"Authorization": "Bearer SERVER"})
        )
        _FakeConfig.built.append(self)

    def authenticate(self) -> dict[str, str]:
        return self._headers()


class _FakeWorkspaceClient:
    def __init__(self, *, config: _FakeConfig) -> None:
        self.config = config
        self.api_client = _FakeApiClient(config)


@pytest.fixture(autouse=True)
def _fake_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    import databricks.sdk
    import databricks.sdk.config

    _FakeConfig.built = []
    monkeypatch.setattr(databricks.sdk, "WorkspaceClient", _FakeWorkspaceClient)
    monkeypatch.setattr(databricks.sdk.config, "Config", _FakeConfig)
    monkeypatch.setattr(mod, "_STATE_POLL_S", 0.0)
    # The server's own identity is in the environment, as on Databricks Apps.
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "server-sp")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "server-secret")


class _Resolver:
    """A bound owner resolver whose answer the test controls."""

    def __init__(self, payload: dict[str, object] | None) -> None:
        self.payload = payload
        self.calls = 0

    def __call__(self) -> dict[str, object] | None:
        self.calls += 1
        return self.payload


def _owner_launcher(
    payload: dict[str, object] | None = None, **kwargs: Any
) -> tuple[DatabricksSandboxLauncher, _Resolver]:
    launcher = DatabricksSandboxLauncher(identity="owner", **kwargs)
    resolver = _Resolver(payload)
    launcher.bind_owner_credential(resolver)
    return launcher, resolver


def _api(launcher: DatabricksSandboxLauncher) -> _FakeApiClient:
    return launcher._workspace_client().api_client


def test_identity_selects_the_owner_credential_provider() -> None:
    assert DatabricksSandboxLauncher().owner_credential_provider is None
    assert DatabricksSandboxLauncher(identity="owner").owner_credential_provider == "databricks"


def test_constructor_rejects_owner_with_profile_and_unknown_identity() -> None:
    with pytest.raises(ValueError, match="profile"):
        DatabricksSandboxLauncher(identity="owner", profile="sp")
    with pytest.raises(ValueError, match="identity"):
        DatabricksSandboxLauncher(identity="robot")  # type: ignore[arg-type]


def test_every_call_runs_on_the_owner_workspace_with_the_owner_token() -> None:
    launcher, _ = _owner_launcher({"token": "OWNER-1", "workspace_host": _HOST + "/"})

    sandbox_id = launcher.provision("managed-abc")
    launcher.terminate(sandbox_id)

    (config,) = _FakeConfig.built
    assert config.kwargs["host"] == _HOST
    assert "profile" not in config.kwargs
    calls = _api(launcher).calls
    assert [c["method"] for c in calls][:2] == ["POST", "GET"]
    assert {c["auth"] for c in calls} == {"Bearer OWNER-1"}


def test_proxy_bearer_written_into_the_sandbox_is_the_owner_token() -> None:
    launcher, _ = _owner_launcher({"token": "OWNER-1", "workspace_host": _HOST}, proxy_bearer=True)

    launcher.start_host(
        "sb-1", token="launch", host_id="h", host_name="n", server_url="https://srv.example"
    )

    bearer_writes = [
        c
        for c in _api(launcher).calls
        if "/exec-sync" in c["path"]
        and "OMNIGENT_PROXY_BEARER_VALUE" in (c["body"].get("envs") or {})
    ]
    assert bearer_writes
    assert {c["body"]["envs"]["OMNIGENT_PROXY_BEARER_VALUE"] for c in bearer_writes} == {"OWNER-1"}


def test_owner_token_is_re_resolved_after_the_cache_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launcher, resolver = _owner_launcher({"token": "OWNER-1", "workspace_host": _HOST})
    launcher.is_running("sb-1")
    monkeypatch.setattr(mod, "_OWNER_TOKEN_CACHE_S", 0.0)
    resolver.payload = {"token": "OWNER-2", "workspace_host": _HOST}

    launcher.is_running("sb-1")

    assert [c["auth"] for c in _api(launcher).calls] == ["Bearer OWNER-1", "Bearer OWNER-2"]


def test_unconnected_owner_is_refused_before_any_call() -> None:
    launcher, _ = _owner_launcher(None)

    with pytest.raises(OwnerCredentialMissingError, match="Connect Databricks"):
        launcher.provision("managed-abc")
    with pytest.raises(OwnerCredentialMissingError, match="Connect Databricks"):
        launcher.prepare()

    assert _FakeConfig.built == []


def test_unbound_owner_launcher_names_the_missing_server_config() -> None:
    launcher = DatabricksSandboxLauncher(identity="owner")

    with pytest.raises(OwnerCredentialMissingError, match="OMNIGENT_DATABRICKS_CLIENT_ID"):
        launcher.prepare()
    assert _FakeConfig.built == []


def test_connection_to_another_workspace_is_refused() -> None:
    launcher, _ = _owner_launcher(
        {"token": "OWNER-1", "workspace_host": "https://elsewhere.cloud.databricks.com"},
        workspace_host=_HOST,
    )

    with pytest.raises(OwnerCredentialMissingError, match="elsewhere"):
        launcher.provision("managed-abc")
    assert _FakeConfig.built == []


def test_disconnect_mid_life_surfaces_unwrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    launcher, resolver = _owner_launcher({"token": "OWNER-1", "workspace_host": _HOST})
    launcher.is_running("sb-1")
    monkeypatch.setattr(mod, "_OWNER_TOKEN_CACHE_S", 0.0)
    resolver.payload = None

    with pytest.raises(OwnerCredentialMissingError, match="Connect Databricks"):
        launcher.terminate("sb-1")
    # Status probes keep their "unknown" contract rather than raising.
    assert launcher.is_running("sb-1") is None


def test_server_identity_mode_never_consults_a_resolver() -> None:
    launcher = DatabricksSandboxLauncher(profile="sp")
    resolver = _Resolver({"token": "OWNER-1", "workspace_host": _HOST})
    launcher.bind_owner_credential(resolver)

    launcher.is_running("sb-1")

    (config,) = _FakeConfig.built
    assert config.kwargs["profile"] == "sp"
    assert "credentials_strategy" not in config.kwargs
    assert resolver.calls == 0
