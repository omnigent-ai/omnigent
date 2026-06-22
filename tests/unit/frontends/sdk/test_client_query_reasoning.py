from __future__ import annotations

import pytest
from omnigent_client._client import OmnigentClient


@pytest.mark.asyncio
async def test_client_query_threads_reasoning_to_temporary_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class _FakeSession:
        def __init__(self) -> None:
            self.reasoning_effort = None

        def set_reasoning_effort(self, effort: str | None) -> None:
            captured["effort"] = effort
            self.reasoning_effort = effort

        async def query(self, input: object, *, files: object = None, stream: bool = False) -> str:
            captured["input"] = input
            captured["files"] = files
            captured["stream"] = stream
            return "ok"

    client = OmnigentClient("http://example.invalid")
    fake_session = _FakeSession()
    monkeypatch.setattr(client, "session", lambda **kwargs: fake_session)
    try:
        result = await client.query(
            model="agent",
            input="hi",
            reasoning={"effort": "xhigh"},
        )
    finally:
        await client.close()

    assert result == "ok"
    assert captured["effort"] == "xhigh"
    assert captured["input"] == "hi"
