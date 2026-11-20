"""Tests for exporting the owner's connected Claude subscription in a managed sandbox."""

from __future__ import annotations

import httpx
import pytest

from omnigent.host import claude_credential as cc
from omnigent.host import credential_broker as broker
from omnigent.host.identity import HOST_TOKEN_ENV_VAR, MANAGED_HOST_TOKEN_HEADER


class _Resp:
    def __init__(self, status_code: int, payload: dict | None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


@pytest.fixture(autouse=True)
def _managed_sandbox(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("IS_SANDBOX", "1")
    monkeypatch.setenv(HOST_TOKEN_ENV_VAR, "host-tok")
    monkeypatch.delenv(cc.CLAUDE_OAUTH_TOKEN_ENV_VAR, raising=False)
    monkeypatch.setattr(broker, "_RETRY_BACKOFF_S", (0.0, 0.0))


def _serve(monkeypatch: pytest.MonkeyPatch, *responses: object) -> list[dict]:
    calls: list[dict] = []
    remaining = list(responses)

    def fake_get(url: str, headers: dict, timeout: float):
        calls.append({"url": url, "headers": headers})
        response = remaining.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    monkeypatch.setattr(broker.httpx, "get", fake_get)
    return calls


def test_exports_the_owners_token_from_the_broker(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(cc.CLAUDE_OAUTH_TOKEN_ENV_VAR, "sk-ant-oat01-image-default")
    calls = _serve(
        monkeypatch,
        httpx.ConnectError("blip"),
        _Resp(
            200, {"connected": True, "owner": "alice@example.com", "token": "sk-ant-oat01-alice"}
        ),
    )
    assert cc.configure_host_claude("https://omni.example/", "host-1") is True
    assert cc.os.environ[cc.CLAUDE_OAUTH_TOKEN_ENV_VAR] == "sk-ant-oat01-alice"
    assert calls[-1]["url"] == "https://omni.example/v1/hosts/host-1/credentials/claude"
    assert calls[-1]["headers"][MANAGED_HOST_TOKEN_HEADER] == "host-tok"


@pytest.mark.parametrize(
    "response",
    [
        _Resp(200, {"connected": False}),
        _Resp(200, {"connected": True, "token": ""}),
        _Resp(404, None),
        _Resp(401, None),
    ],
)
def test_leaves_the_environment_alone_without_a_connection(
    monkeypatch: pytest.MonkeyPatch, response: _Resp
) -> None:
    _serve(monkeypatch, response)
    assert cc.configure_host_claude("https://omni.example", "host-1") is False
    assert cc.CLAUDE_OAUTH_TOKEN_ENV_VAR not in cc.os.environ


@pytest.mark.parametrize("unset", ["IS_SANDBOX", HOST_TOKEN_ENV_VAR])
def test_noop_outside_a_managed_sandbox(monkeypatch: pytest.MonkeyPatch, unset: str) -> None:
    monkeypatch.delenv(unset)
    calls = _serve(monkeypatch)
    assert cc.configure_host_claude("https://omni.example", "host-1") is False
    assert calls == []
    assert cc.CLAUDE_OAUTH_TOKEN_ENV_VAR not in cc.os.environ
