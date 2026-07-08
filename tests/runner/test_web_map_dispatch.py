"""Dispatch tests for the ``web_map`` builtin (runner-local Nimble Map).

Lock in that web_map is runner-local (so a non-OpenAI model's call resolves to
``WebMapTool.invoke`` → Nimble ``/v1/map``) and not advertised to native harnesses.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _NATIVE_RELAY_BUILTIN_TOOLS,
    _execute_web_map_tool,
    _web_map_config_from_spec,
    should_dispatch_locally,
)


def _spec_with_web_map(config: dict[str, str]) -> SimpleNamespace:
    """Minimal agent_spec stub: a single ``web_map`` builtin carrying ``config``."""
    return SimpleNamespace(
        tools=SimpleNamespace(
            builtins=[SimpleNamespace(name="web_map", config=config)],
        ),
    )


def test_web_map_is_runner_local() -> None:
    """``web_map`` dispatches locally, like ``web_search`` / ``web_fetch``."""
    assert "web_map" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("web_map") is True


def test_web_map_not_relayed_to_native_harnesses() -> None:
    """Native harnesses use their own tooling; web_map is not relayed."""
    assert "web_map" not in _NATIVE_RELAY_BUILTIN_TOOLS


def test_config_from_spec_reads_web_map_config() -> None:
    """The config helper returns the ``web_map`` builtin's config dict."""
    spec = _spec_with_web_map({"api_key": "k", "limit": "10"})
    assert _web_map_config_from_spec(spec) == {"api_key": "k", "limit": "10"}


def test_config_from_spec_empty_when_absent() -> None:
    """No ``web_map`` entry → empty config."""
    spec = SimpleNamespace(tools=SimpleNamespace(builtins=[]))
    assert _web_map_config_from_spec(spec) == {}


def test_dispatch_calls_nimble_map() -> None:
    """The dispatch handler builds WebMapTool from spec config and calls Nimble Map."""
    spec = _spec_with_web_map({"api_key": "k"})
    fake = MagicMock()
    fake.json.return_value = {
        "links": [{"url": "https://example.com/a"}],
        "success": True,
        "task_id": "t",
    }
    with patch("omnigent.tools.builtins.web_map.httpx.post") as mock_post:
        mock_post.return_value = fake
        result = asyncio.run(
            _execute_web_map_tool(
                {"url": "https://example.com"},
                agent_spec=spec,
                conversation_id="c",
            )
        )

    assert mock_post.call_count == 1, "dispatch must call Nimble Map"
    assert "https://example.com/a" in result
    assert mock_post.call_args.kwargs["headers"]["X-Client-Source"] == "omnigent"
