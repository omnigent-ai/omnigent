"""Unit tests for the cline-native harness modules."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from omnigent.harnesses.cline_native.bridge import (
    ClineBridgeState,
    build_cline_native_spawn_env,
    clear_bridge_state,
    native_input_ready,
    read_bridge_state,
    write_bridge_state,
)
from omnigent.harnesses.cline_native.items import MirroredItem, hub_event_to_items
from omnigent.harnesses.cline_native.serve import (
    parse_hub_start_json,
    resolve_cline_provider_settings,
    token_fingerprint,
)


def _event(name: str, payload: dict | None = None, session_id: str = "s1") -> dict:
    return {
        "version": "v1",
        "event": name,
        "sessionId": session_id,
        "payload": payload or {},
    }


class TestBridgeState:
    def test_round_trip(self, tmp_path: Path) -> None:
        state = ClineBridgeState(
            ws_url="ws://127.0.0.1:9999/hub",
            auth_token="tok",
            session_id="hub-sess-1",
            pid=123,
            server_url="http://127.0.0.1:6767",
            headers={"Authorization": "Bearer x"},
        )
        write_bridge_state(tmp_path, state)
        assert read_bridge_state(tmp_path) == state

    def test_read_missing_returns_none(self, tmp_path: Path) -> None:
        assert read_bridge_state(tmp_path) is None
        (tmp_path / "bridge_state.json").write_text("{not json")
        assert read_bridge_state(tmp_path) is None
        (tmp_path / "bridge_state.json").write_text('{"ws_url": ""}')
        assert read_bridge_state(tmp_path) is None

    def test_clear(self, tmp_path: Path) -> None:
        write_bridge_state(tmp_path, ClineBridgeState(ws_url="ws://x"))
        clear_bridge_state(tmp_path)
        assert read_bridge_state(tmp_path) is None
        clear_bridge_state(tmp_path)  # idempotent

    def test_input_ready_requires_session_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "omnigent.harnesses.cline_native.bridge.bridge_dir_for_session_id",
            lambda _sid: tmp_path,
        )
        assert native_input_ready("sess", object()) is False
        write_bridge_state(tmp_path, ClineBridgeState(ws_url="ws://x"))
        assert native_input_ready("sess", object()) is False
        write_bridge_state(tmp_path, ClineBridgeState(ws_url="ws://x", session_id="hub-1"))
        assert native_input_ready("sess", object()) is True

    def test_spawn_env_keys_bridge_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            "omnigent.harnesses.cline_native.bridge.bridge_dir_for_session_id",
            lambda sid: tmp_path / sid,
        )
        env = build_cline_native_spawn_env("abc")
        assert env == {"CLINE_NATIVE_BRIDGE_DIR": str(tmp_path / "abc")}


class TestHubStartParse:
    def test_parse(self) -> None:
        raw = json.dumps(
            {
                "url": "ws://127.0.0.1:25463/hub",
                "authToken": "abc123",
                "cwd": "/tmp",
                "platform": "linux",
            }
        )
        ep = parse_hub_start_json(raw)
        assert ep.ws_url == "ws://127.0.0.1:25463/hub"
        assert ep.auth_token == "abc123"

    def test_parse_missing_fields(self) -> None:
        from omnigent.harnesses.cline_native.serve import ClineHubStartError

        with pytest.raises(ClineHubStartError):
            parse_hub_start_json('{"url": "ws://x"}')
        with pytest.raises(ClineHubStartError):
            parse_hub_start_json("{}")


def _write_providers(cline_home: Path, record: dict) -> None:
    path = cline_home / "data" / "settings" / "providers.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record))


class TestProviderSettings:
    def test_reads_last_used_provider_and_model(self, tmp_path: Path) -> None:
        _write_providers(
            tmp_path,
            {
                "lastUsedProvider": "cline-pass",
                "providers": {
                    "cline-pass": {"settings": {"model": "m-1", "auth": {}}},
                    "other": {"settings": {"model": "m-2"}},
                },
            },
        )
        assert resolve_cline_provider_settings(tmp_path) == ("cline-pass", "m-1")

    def test_falls_back_to_first_provider_without_last_used(self, tmp_path: Path) -> None:
        _write_providers(
            tmp_path,
            {"providers": {"only": {"settings": {"model": "m-9"}}}},
        )
        assert resolve_cline_provider_settings(tmp_path) == ("only", "m-9")

    def test_provider_without_model_returns_none_model(self, tmp_path: Path) -> None:
        _write_providers(
            tmp_path,
            {"lastUsedProvider": "p", "providers": {"p": {"settings": {}}}},
        )
        assert resolve_cline_provider_settings(tmp_path) == ("p", None)

    def test_missing_or_malformed_returns_none(self, tmp_path: Path) -> None:
        assert resolve_cline_provider_settings(tmp_path) == (None, None)
        _write_providers(tmp_path, {"providers": {}})
        assert resolve_cline_provider_settings(tmp_path) == (None, None)
        (tmp_path / "data" / "settings" / "providers.json").write_text("{bad")
        assert resolve_cline_provider_settings(tmp_path) == (None, None)

    def test_token_fingerprint_is_stable_and_short(self) -> None:
        fp = token_fingerprint("secret-token")
        assert fp == token_fingerprint("secret-token")
        assert fp != token_fingerprint("other-token")
        assert len(fp) == 8
        assert token_fingerprint("") == "none"


class TestClineAuthConfigured:
    def test_configured_with_auth_settings(self, tmp_path: Path) -> None:
        from omnigent.onboarding.cline_auth import cline_auth_configured

        _write_providers(
            tmp_path,
            {
                "lastUsedProvider": "cline-pass",
                "providers": {"cline-pass": {"settings": {"auth": {"accessToken": "t"}}}},
            },
        )
        assert cline_auth_configured(tmp_path) is True

    def test_configured_with_api_key(self, tmp_path: Path) -> None:
        from omnigent.onboarding.cline_auth import cline_auth_configured

        _write_providers(
            tmp_path,
            {
                "lastUsedProvider": "openrouter",
                "providers": {"openrouter": {"settings": {"apiKey": "k"}}},
            },
        )
        assert cline_auth_configured(tmp_path) is True

    def test_not_configured_without_credentials(self, tmp_path: Path) -> None:
        from omnigent.onboarding.cline_auth import cline_auth_configured

        assert cline_auth_configured(tmp_path) is False
        _write_providers(
            tmp_path,
            {"lastUsedProvider": "p", "providers": {"p": {"settings": {}}}},
        )
        assert cline_auth_configured(tmp_path) is False


class TestItems:
    def test_assistant_finished_maps_message(self) -> None:
        items = hub_event_to_items(_event("assistant.finished", {"text": "hello"}))
        assert items == [MirroredItem(kind="message", role="assistant", text="hello")]

    def test_reasoning_finished_maps_reasoning(self) -> None:
        items = hub_event_to_items(_event("reasoning.finished", {"reasoning": "thinking"}))
        assert items == [MirroredItem(kind="reasoning", role="assistant", text="thinking")]

    def test_tool_started_maps_function_call(self) -> None:
        items = hub_event_to_items(
            _event(
                "tool.started",
                {"toolCallId": "c1", "toolName": "bash", "input": {"cmd": "ls"}},
            )
        )
        assert len(items) == 1
        item = items[0]
        assert item.kind == "function_call"
        assert item.call_id == "c1"
        assert item.name == "bash"
        assert json.loads(item.arguments) == {"cmd": "ls"}

    def test_tool_finished_maps_output(self) -> None:
        items = hub_event_to_items(
            _event(
                "tool.finished",
                {"toolCallId": "c1", "toolName": "bash", "output": "done"},
            )
        )
        assert items == [MirroredItem(kind="function_call_output", call_id="c1", text="done")]

    def test_tool_finished_error_maps_output(self) -> None:
        items = hub_event_to_items(
            _event(
                "tool.finished",
                {"toolCallId": "c1", "error": "boom"},
            )
        )
        assert items == [MirroredItem(kind="function_call_output", call_id="c1", text="boom")]

    def test_unmapped_events_yield_nothing(self) -> None:
        for name in (
            "run.started",
            "run.completed",
            "usage.updated",
            "assistant.delta",
            "approval.requested",
            "session.created",
        ):
            assert hub_event_to_items(_event(name, {"text": "x"})) == []

    def test_empty_text_yields_nothing(self) -> None:
        assert hub_event_to_items(_event("assistant.finished", {"text": ""})) == []
        assert hub_event_to_items(_event("tool.finished", {"toolCallId": "c"})) == []


class TestExecutorHelpers:
    def test_latest_user_text(self) -> None:
        from omnigent.inner.cline_native_executor import _latest_user_text

        assert _latest_user_text([]) == ""
        assert (
            _latest_user_text(
                [
                    {"role": "user", "content": "first"},
                    {"role": "assistant", "content": "mid"},
                    {"role": "user", "content": "latest"},
                ]
            )
            == "latest"
        )
        assert (
            _latest_user_text(
                [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "a"},
                            {"type": "input_text", "text": "b"},
                        ],
                    }
                ]
            )
            == "a\n\nb"
        )


class _FakeWS:
    """Minimal websockets stand-in for ClineHubClient tests."""

    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.incoming: asyncio.Queue = asyncio.Queue()
        self.close_code = None
        self.close_reason = ""
        self._closed: asyncio.Future = asyncio.get_event_loop().create_future()

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    def __aiter__(self):
        return self._gen()

    async def _gen(self):
        while True:
            item = await self.incoming.get()
            if item is _CLOSE:
                return
            yield item

    async def wait_closed(self) -> None:
        await self._closed

    async def close(self) -> None:
        if not self._closed.done():
            self._closed.set_result(None)
            self.close_code = 1000
            self.incoming.put_nowait(_CLOSE)


_CLOSE = object()


def _client_with_fake_ws():
    """A ClineHubClient bound to a fake ws with its recv loop running."""
    import asyncio as _a

    from omnigent.harnesses.cline_native.client import ClineHubClient

    ws = _FakeWS()
    client = ClineHubClient(ws=ws, client_id="test-client")
    client._recv_task = _a.create_task(client._recv_loop())
    return client, ws


def _reply(request_id: str, payload: dict, ok: bool = True) -> str:
    return json.dumps(
        {"kind": "reply", "envelope": {"requestId": request_id, "ok": ok, "payload": payload}}
    )


class TestHubClientCommands:
    async def test_command_waits_without_deadline(self) -> None:
        """timeout_s=None waits for the reply instead of a fixed cap."""
        client, ws = _client_with_fake_ws()
        try:
            task = asyncio.ensure_future(client.command("session.list", {}, timeout_s=None))
            await asyncio.sleep(0.05)
            assert not task.done()
            ws.incoming.put_nowait(_reply("omni-1", {"sessions": []}))
            assert (await task) == {"sessions": []}
        finally:
            await client.close()

    async def test_session_create_sends_provider_and_model(self) -> None:
        """providerId/modelId must reach sessionConfig, metadata, and modelSelection."""
        client, ws = _client_with_fake_ws()
        try:
            task = asyncio.ensure_future(
                client.session_create(
                    workspace_root="/w",
                    cwd="/w",
                    provider="cline-pass",
                    model="m-1",
                )
            )
            await asyncio.sleep(0.05)
            frame = ws.sent[-1]
            payload = frame["envelope"]["payload"]
            assert frame["envelope"]["command"] == "session.create"
            assert payload["sessionConfig"]["providerId"] == "cline-pass"
            assert payload["sessionConfig"]["modelId"] == "m-1"
            assert payload["metadata"]["provider"] == "cline-pass"
            assert payload["metadata"]["model"] == "m-1"
            assert payload["modelSelection"] == {"provider": "cline-pass", "model": "m-1"}
            ws.incoming.put_nowait(
                _reply(frame["envelope"]["requestId"], {"session": {"sessionId": "s-1"}})
            )
            assert (await task) == "s-1"
        finally:
            await client.close()

    async def test_session_attach_and_get(self) -> None:
        client, ws = _client_with_fake_ws()
        try:
            attach = asyncio.ensure_future(client.session_attach("s-1"))
            await asyncio.sleep(0.05)
            frame = ws.sent[-1]
            assert frame["envelope"]["command"] == "session.attach"
            assert frame["envelope"]["payload"] == {"sessionId": "s-1"}
            ws.incoming.put_nowait(
                _reply(
                    frame["envelope"]["requestId"],
                    {"session": {"sessionId": "s-1", "status": "idle"}},
                )
            )
            assert (await attach)["status"] == "idle"

            get = asyncio.ensure_future(client.session_get("s-1"))
            await asyncio.sleep(0.05)
            frame = ws.sent[-1]
            assert frame["envelope"]["command"] == "session.get"
            ws.incoming.put_nowait(
                _reply(
                    frame["envelope"]["requestId"],
                    {"session": {"sessionId": "s-1", "status": "running"}},
                )
            )
            assert (await get)["status"] == "running"
        finally:
            await client.close()


class TestExecutorSessionGating:
    def test_dead_session_status_raises(self) -> None:
        from omnigent.inner.cline_native_executor import _require_live_session

        for status in ("aborted", "terminated", "deleted", "closed"):
            with pytest.raises(Exception, match=status):
                _require_live_session({"status": status}, "s-1")

    def test_live_statuses_pass(self) -> None:
        from omnigent.inner.cline_native_executor import _require_live_session

        for status in ("idle", "running", "completed", ""):
            _require_live_session({"status": status}, "s-1")

    def test_describe_record(self) -> None:
        from omnigent.inner.cline_native_executor import _describe_session_record

        text = _describe_session_record(
            {
                "status": "aborted",
                "participants": [{"clientId": "c"}],
                "metadata": {"provider": "cline-pass", "model": "m-1"},
            }
        )
        assert "'aborted'" in text
        assert "cline-pass" in text
        assert "participants=1" in text


class TestForwarderRefresh:
    async def test_refresh_rewrites_bridge_state_only_when_coords_change(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnigent.harnesses.cline_native.forwarder import _refresh_hub_endpoint
        from omnigent.harnesses.cline_native.serve import ClineHubEndpoint

        write_bridge_state(
            tmp_path,
            ClineBridgeState(
                ws_url="ws://old/hub",
                auth_token="old-tok",
                session_id="s-1",
                workspace="/w",
                cline_command="/bin/cline",
            ),
        )
        endpoint = ClineHubEndpoint(ws_url="ws://new/hub", auth_token="new-tok")
        monkeypatch.setattr(
            "omnigent.harnesses.cline_native.serve.ensure_cline_hub",
            lambda **_kw: _async_return(endpoint),
        )
        result = await _refresh_hub_endpoint(
            cwd="/w", executable="/bin/cline", bridge_dir=tmp_path, session_id="sess-1"
        )
        assert result == endpoint
        state = read_bridge_state(tmp_path)
        assert state is not None
        assert (state.ws_url, state.auth_token) == ("ws://new/hub", "new-tok")
        # Session linkage survives the refresh.
        assert state.session_id == "s-1"

    async def test_refresh_returns_none_when_hub_start_fails(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from omnigent.harnesses.cline_native.forwarder import _refresh_hub_endpoint
        from omnigent.harnesses.cline_native.serve import ClineHubStartError

        def _raise(**_kw):
            raise ClineHubStartError("nope")

        monkeypatch.setattr(
            "omnigent.harnesses.cline_native.serve.ensure_cline_hub", _raise
        )
        result = await _refresh_hub_endpoint(
            cwd="/w", executable="", bridge_dir=tmp_path, session_id="sess-1"
        )
        assert result is None


async def _async_return(value):
    return value
