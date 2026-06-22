"""Unit test: ``OmnigentClient.query(..., model_override=...)`` threads to Session.

Mirrors :mod:`tests.unit.frontends.sdk.test_client_query_reasoning`. Confirms
that the public one-shot SDK surface (``client.query``) honors the
``model_override`` parameter by calling
:meth:`Session.set_model_override` on the temporary session it
constructs internally — same pattern Corey's effort PR used for the
``reasoning`` kwarg.
"""

from __future__ import annotations

import pytest
from omnigent_client._client import OmnigentClient


@pytest.mark.asyncio
async def test_client_query_threads_model_override_to_temporary_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``client.query(..., model_override="X")`` calls session.set_model_override("X").

    If this regresses, non-REPL Python clients that want a one-shot
    model override would have to drop down to ``client.responses.stream``
    directly — breaking parity with how ``reasoning`` is exposed today.
    """
    captured: dict[str, object] = {}

    class _FakeSession:
        """Concrete stub recording every setter call.

        Real class (not :class:`MagicMock`) so an unexpected attribute
        access fails loudly — see ``.claude/skills/omnigent-testing``
        rule 3.
        """

        def __init__(self) -> None:
            self.reasoning_effort = None
            self.model_override = None

        def set_reasoning_effort(self, effort: str | None) -> None:
            captured["effort"] = effort
            self.reasoning_effort = effort

        def set_model_override(self, model: str | None) -> None:
            captured["model_override"] = model
            self.model_override = model

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
            model_override="openai/gpt-5.4-mini",
        )
    finally:
        await client.close()

    # The query completed normally — proves we actually reached
    # session.query past the setter calls.
    assert result == "ok"
    # The override was forwarded onto the temporary session BEFORE
    # the query() call. Asserting on the captured dict catches both
    # "setter never called" and "setter called with wrong value".
    assert captured["model_override"] == "openai/gpt-5.4-mini"
    assert captured["input"] == "hi"


@pytest.mark.asyncio
async def test_client_query_omits_model_override_when_unset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No ``model_override`` kwarg → set_model_override is never called.

    Confirms the opposite direction: ``client.query`` without
    ``model_override`` must not invoke the setter at all (which
    would be a no-op for ``None`` but would still indicate a wire
    inconsistency).
    """
    setter_calls: list[str | None] = []

    class _FakeSession:
        def __init__(self) -> None:
            self.reasoning_effort = None
            self.model_override = None

        def set_reasoning_effort(self, effort: str | None) -> None:
            self.reasoning_effort = effort

        def set_model_override(self, model: str | None) -> None:
            setter_calls.append(model)
            self.model_override = model

        async def query(self, input: object, *, files: object = None, stream: bool = False) -> str:
            return "ok"

    client = OmnigentClient("http://example.invalid")
    fake_session = _FakeSession()
    monkeypatch.setattr(client, "session", lambda **kwargs: fake_session)
    try:
        await client.query(model="agent", input="hi")
    finally:
        await client.close()

    # Setter never called when the caller didn't pass model_override.
    # If `setter_calls == [None]` instead, it means client.query is
    # eagerly invoking the setter even with no override — that's a
    # subtle inconsistency vs how `reasoning` is gated.
    assert setter_calls == []
