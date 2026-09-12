"""Quota burst-policy proxy contract tests."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from omnigent.errors import OmnigentError
from omnigent.server.routes import _quota_controller, quota_config


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(quota_config.create_quota_config_router(), prefix="/v1")
    return app


def _policy() -> dict[str, object]:
    return {
        "initial_burst_factor": 2.0,
        "max_burst_factor": None,
        "adaptive_enabled": True,
        "current_burst_factors": {"personal": 1.75},
    }


def _request(method: str, **kwargs: Any) -> httpx.Response:
    async def send() -> httpx.Response:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=_app()), base_url="http://test"
        ) as client:
            return await client.request(method, "/v1/quota/config", **kwargs)

    return asyncio.run(send())


def test_get_returns_authoritative_controller_policy(monkeypatch: Any) -> None:
    async def proxy(
        method: str, path: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        assert (method, path, body) == ("GET", "/v1/burst-policy", None)
        return _policy()

    monkeypatch.setattr(quota_config, "proxy", proxy)
    response = _request("GET")
    assert response.status_code == 200
    assert response.json() == _policy()


def test_patch_translates_to_admin_put_with_server_idempotency_key(monkeypatch: Any) -> None:
    seen: dict[str, object] = {}

    async def proxy(
        method: str, path: str, body: dict[str, object] | None = None
    ) -> dict[str, object]:
        seen.update({"method": method, "path": path, "body": body})
        return {**_policy(), "max_burst_factor": 2.5, "adaptive_enabled": False}

    monkeypatch.setattr(quota_config, "proxy", proxy)
    response = _request(
        "PATCH",
        json={"max_burst_factor": 2.5, "adaptive_enabled": False},
    )
    assert response.status_code == 200
    assert seen["method"] == "PUT"
    assert seen["path"] == "/v1/burst-policy"
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["max_burst_factor"] == 2.5
    assert body["adaptive_enabled"] is False
    assert isinstance(body["idempotency_key"], str)
    assert body["idempotency_key"].startswith("omnigent-")


def test_patch_rejects_burst_below_one() -> None:
    response = _request(
        "PATCH",
        json={"max_burst_factor": 0.5, "adaptive_enabled": True},
    )
    assert response.status_code == 422


def test_controller_token_file_must_be_owner_only(monkeypatch: Any, tmp_path: Path) -> None:
    token_file = tmp_path / "controller-token"
    token_file.write_text("secret\n", encoding="utf-8")
    token_file.chmod(0o644)
    monkeypatch.setenv("LLMQ_CONTROLLER_TOKEN_FILE", str(token_file))
    with pytest.raises(OmnigentError, match="credentials are unavailable"):
        _quota_controller.read_controller_token()

    token_file.chmod(0o600)
    assert _quota_controller.read_controller_token() == "secret"


def test_controller_token_file_rejects_links(monkeypatch: Any, tmp_path: Path) -> None:
    token_file = tmp_path / "controller-token"
    token_file.write_text("secret\n", encoding="utf-8")
    token_file.chmod(0o600)
    hardlink = tmp_path / "hardlink"
    hardlink.hardlink_to(token_file)
    monkeypatch.setenv("LLMQ_CONTROLLER_TOKEN_FILE", str(hardlink))
    with pytest.raises(OmnigentError, match="credentials are unavailable"):
        _quota_controller.read_controller_token()

    symlink = tmp_path / "symlink"
    symlink.symlink_to(token_file)
    monkeypatch.setenv("LLMQ_CONTROLLER_TOKEN_FILE", str(symlink))
    with pytest.raises(OmnigentError, match="credentials are unavailable"):
        _quota_controller.read_controller_token()


@pytest.mark.parametrize("factor", [0.5, float("inf"), float("nan")])
def test_controller_response_rejects_invalid_current_factor(factor: float) -> None:
    with pytest.raises(OmnigentError, match="Invalid quota controller response"):
        quota_config._response({**_policy(), "current_burst_factors": {"personal": factor}})
