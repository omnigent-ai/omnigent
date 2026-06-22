"""Tests for llms.adapters.databricks — payload building and validation."""

from typing import Any

from omnigent.llms.adapters.databricks import DatabricksAdapter


def test_stream_options_stripped_from_streaming_payload() -> None:
    """
    Databricks model serving rejects ``stream_options`` with 400.

    The base ``OpenAICompatibleAdapter._build_payload`` always injects
    ``stream_options: {include_usage: true}`` when ``stream=True``.
    ``DatabricksAdapter`` must remove it before the request is sent.
    """
    adapter = DatabricksAdapter()
    payload: dict[str, Any] = adapter._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model="databricks-kimi-k2-6",
        tools=None,
        stream=True,
        extra={},
    )
    assert "stream_options" not in payload
    assert payload["stream"] is True


def test_non_streaming_payload_has_no_stream_options() -> None:
    """Non-streaming payloads never had stream_options; confirm still clean."""
    adapter = DatabricksAdapter()
    payload: dict[str, Any] = adapter._build_payload(
        messages=[{"role": "user", "content": "hi"}],
        model="databricks-kimi-k2-6",
        tools=None,
        stream=False,
        extra={},
    )
    assert "stream_options" not in payload
    assert "stream" not in payload


def test_missing_base_url_raises_when_no_auto_resolve(monkeypatch: Any) -> None:
    """
    When ``connection_params`` has no ``base_url`` and auto-resolution from
    ``~/.databrickscfg`` also fails, ``chat_completions`` raises
    ``OmnigentError``.
    """
    import asyncio

    from omnigent.errors import OmnigentError
    from omnigent.llms.adapters import databricks as adapter_mod

    def _raise(profile: Any) -> None:
        raise OSError("Could not resolve Databricks workspace credentials.")

    monkeypatch.setattr(adapter_mod, "resolve_databricks_workspace", _raise)

    adapter = DatabricksAdapter()

    async def call() -> None:
        await adapter.chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            model="databricks-kimi-k2-6",
            tools=None,
            stream=False,
            extra={},
            connection_params={"api_key": "tok"},  # base_url absent, no auto-resolve
        )

    try:
        asyncio.run(call())
        raise AssertionError("Expected OmnigentError was not raised")
    except OmnigentError as exc:
        assert "Could not resolve" in str(exc)


def test_auto_resolve_used_when_no_connection_params(monkeypatch: Any) -> None:
    """
    When ``connection_params`` is absent, the adapter calls
    :func:`~omnigent.runtime.credentials.databricks.resolve_databricks_workspace`
    and uses the result.

    We don't make a real HTTP call here — we just verify that the resolved
    credentials are forwarded to the parent ``chat_completions``.
    """
    import asyncio

    from omnigent.llms.adapters import databricks as adapter_mod
    from omnigent.runtime.credentials.databricks import WorkspaceCreds

    monkeypatch.setattr(
        adapter_mod,
        "resolve_databricks_workspace",
        lambda profile: WorkspaceCreds(host="https://example.databricks.com", token="dapi-tok"),
    )

    captured: list[dict[str, Any]] = []

    async def _fake_parent(
        self: Any,
        messages: Any,
        model: Any,
        tools: Any,
        stream: Any,
        extra: Any,
        *,
        connection_params: Any = None,
        timeout: Any = None,
    ) -> dict[str, Any]:
        captured.append({"connection_params": connection_params})
        return {}

    from omnigent.llms.adapters.openai import OpenAICompatibleAdapter

    monkeypatch.setattr(OpenAICompatibleAdapter, "chat_completions", _fake_parent)

    adapter = DatabricksAdapter()
    asyncio.run(
        adapter.chat_completions(
            messages=[{"role": "user", "content": "hi"}],
            model="databricks-kimi-k2-6",
            tools=None,
            stream=False,
            extra={},
            connection_params=None,
        )
    )

    assert len(captured) == 1
    assert (
        captured[0]["connection_params"]["base_url"]
        == "https://example.databricks.com/serving-endpoints"
    )
    assert captured[0]["connection_params"]["api_key"] == "dapi-tok"
