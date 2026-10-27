"""Saved credentials through the real agy builder and both runner dispatch paths."""

import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import yaml

from omnigent.debug_logging import record_to_row
from omnigent.errors import ErrorCode
from omnigent.runner.native.orchestration import (
    NativeLaunchContext,
    _ensure_native_terminal,
    _launch_native_terminal,
)


@pytest.mark.asyncio
@pytest.mark.parametrize("ensure", [False, True], ids=["startup", "reattach"])
@pytest.mark.parametrize("failure", ["missing-secret", "incompatible-url", "malformed-profile"])
async def test_saved_credentials_reach_runner_error_response(
    monkeypatch, tmp_path, caplog, ensure, failure
):
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.launch.agy_binary_path", lambda: "/unused/agy"
    )
    entry = {
        "kind": "gateway",
        "default": ["gemini"],
        "gemini": {
            "base_url": "https://gateway.example/gemini",
            "api_key_ref": "keychain:missing",
        },
    }
    expected = "setup"
    if failure == "incompatible-url":
        entry["gemini"] = {"base_url": "https://api.openai.com/v1", "api_key": "fake"}
        expected = "OpenAI Responses"
    if failure == "malformed-profile":
        entry = {
            "kind": "databricks",
            "profile": "broken",
            "native_gemini": True,
            "default": ["gemini"],
        }
        profile = tmp_path / "databrickscfg"
        profile.write_text("token = private-malformed-secret\n")
        monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(profile))
        expected = "Repair"
    (tmp_path / "config.yaml").write_text(yaml.safe_dump({"providers": {"selected": entry}}))
    registry = SimpleNamespace(
        terminal_registry=None,
        get_terminal_resource=AsyncMock(return_value=None),
        create_terminal=AsyncMock(),
    )
    events = []
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"workspace": str(tmp_path)})
        ),
        base_url="http://server",
    ) as client:
        ctx = NativeLaunchContext(
            "credential-test",
            registry,
            lambda sid, event: events.append(event),
            server_client=client,
        )
        with caplog.at_level(logging.WARNING):
            if ensure:
                response = await _ensure_native_terminal("antigravity", ctx, ensure_locks={})
                assert response.status_code == 412
                error = json.loads(response.body)["error"]
            else:
                assert not await _launch_native_terminal(
                    "antigravity-native", ctx, ensure_locks={}
                )
                error = next(e["error"] for e in events if e.get("status") == "failed")
        assert error["code"] == ErrorCode.HARNESS_NOT_CONFIGURED
        assert expected in error["message"]
        assert "runner log" not in error["message"]
        assert "private-malformed-secret" not in error["message"] + caplog.text
        assert not any(r.exc_info for r in caplog.records)
        record = next(r for r in caplog.records if error["error_id"] in r.getMessage())
        row = record_to_row(record, source="runner")
        assert row["session_id"] == "credential-test"
        assert row["attributes"]["code"] == ErrorCode.HARNESS_NOT_CONFIGURED
        registry.create_terminal.assert_not_called()
