# tests/inner/test_opencode_executor.py
import asyncio

import pytest

from omnigent.inner.executor import ExecutorConfig, TextChunk, TurnComplete
from omnigent.inner.opencode_executor import OpenCodeExecutor


class _FakeStream:
    def __init__(self, events):
        self._events = list(events)

    def __aiter__(self):
        return self

    async def __anext__(self):
        await asyncio.sleep(0)
        if not self._events:
            raise StopAsyncIteration
        return self._events.pop(0)

    async def close(self):
        pass


class _Obj:
    def __init__(self, **kw):
        self.__dict__.update(kw)

    def model_dump(self, **kw):
        return self.__dict__


def _part_event(part):
    return _Obj(type="message.part.updated", properties=_Obj(part=_Obj(**part)))


def _idle(session_id):
    return _Obj(type="session.idle", properties=_Obj(session_id=session_id))


@pytest.mark.asyncio
async def test_run_turn_streams_text_and_completes(monkeypatch):
    ex = OpenCodeExecutor()

    # Track chat kwargs so we can assert provider_id + model_id are always set.
    recorded_chat_kwargs: dict = {}

    class _FakeSessionRes:
        async def create(self, **kw):
            return _Obj(id="sess-1")

        async def chat(self, **kw):
            recorded_chat_kwargs.update(kw)
            return _Obj(
                parts=[],
                tokens={"input": 5, "output": 3, "cache": {}},
                model_id="m",
                provider_id="p",
            )

        async def abort(self, **kw):
            return _Obj(ok=True)

    class _FakeEventRes:
        async def list(self):
            return _FakeStream(
                [
                    _part_event({"id": "p1", "type": "text", "text": "Hi"}),
                    _idle("sess-1"),
                ]
            )

    class _FakeAppRes:
        async def providers(self):
            return _Obj(default={"anthropic": "claude-sonnet-4-5"}, providers=[])

    class _FakeClient:
        session = _FakeSessionRes()
        event = _FakeEventRes()
        app = _FakeAppRes()

        async def close(self):
            pass

    async def _fake_ensure_server(self, tools):
        self._server_started = True
        self._client = _FakeClient()

    monkeypatch.setattr(OpenCodeExecutor, "_ensure_server", _fake_ensure_server)

    events = []
    async for e in ex.run_turn(
        [{"role": "user", "content": "hello", "session_id": "k1"}],
        tools=[],
        system_prompt="",
        config=ExecutorConfig(),
    ):
        events.append(e)
    assert any(isinstance(e, TextChunk) and e.text == "Hi" for e in events)
    tc = [e for e in events if isinstance(e, TurnComplete)]
    assert tc and tc[0].response == "Hi"
    assert tc[0].usage == {
        "input_tokens": 5,
        "output_tokens": 3,
        "total_tokens": 8,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
    }
    # Correction (A): session.chat MUST always be called with both provider_id and model_id.
    assert recorded_chat_kwargs.get("provider_id"), (
        f"session.chat was called without a non-empty provider_id; kwargs={recorded_chat_kwargs}"
    )
    assert recorded_chat_kwargs.get("model_id"), (
        f"session.chat was called without a non-empty model_id; kwargs={recorded_chat_kwargs}"
    )


def test_capabilities():
    ex = OpenCodeExecutor()
    assert ex.handles_tools_internally() is True
    assert ex.supports_streaming() is True
    assert ex.supports_live_message_queue() is True


@pytest.mark.asyncio
async def test_ensure_server_exports_opencode_account_key(monkeypatch):
    """HARNESS_OPENCODE_API_KEY is exported to the serve process as OPENCODE_API_KEY.

    The OpenCode Zen / Go account key rides the server's environment (its own
    auth resolves OPENCODE_API_KEY for both the ``opencode`` and
    ``opencode-go`` provider ids), not the OPENCODE_CONFIG_CONTENT provider
    override.
    """
    monkeypatch.setenv("HARNESS_OPENCODE_API_KEY", "oc-secret")
    for var in (
        "HARNESS_OPENCODE_GATEWAY_PROVIDER",
        "HARNESS_OPENCODE_GATEWAY_BASE_URL",
        "HARNESS_OPENCODE_GATEWAY_API_KEY",
        "HARNESS_OPENCODE_MCP_SERVERS",
    ):
        monkeypatch.delenv(var, raising=False)

    class _StubServer:
        client = object()

        def __init__(self):
            self.extra_env: dict | None = None

        async def start(self, *, cwd, extra_env):
            self.extra_env = dict(extra_env)

    ex = OpenCodeExecutor()
    stub = _StubServer()
    ex._server = stub
    await ex._ensure_server([])
    assert stub.extra_env is not None
    assert stub.extra_env["OPENCODE_API_KEY"] == "oc-secret"
