"""Dispatch tests for the Nimble Extract-backed ``web_fetch`` path.

These lock in the expectations behind the ``fetch_provider: nimble`` branch in
``_execute_web_fetch_tool``: with a URL and the nimble provider selected, the
call resolves to Nimble Extract (no sub-agent spawn); otherwise the existing
``__web_researcher`` sub-agent path is preserved (zero regression).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

from omnigent.runner.tool_dispatch import (
    _ALL_LOCAL_TOOLS,
    _execute_web_fetch_tool,
    _web_fetch_config_from_spec,
    should_dispatch_locally,
)


def _spec_with_web_fetch(config: dict[str, str]) -> SimpleNamespace:
    """Minimal agent_spec stub: a single ``web_fetch`` builtin carrying ``config``."""
    return SimpleNamespace(
        tools=SimpleNamespace(
            builtins=[SimpleNamespace(name="web_fetch", config=config)],
        ),
    )


def test_web_fetch_is_runner_local() -> None:
    """``web_fetch`` dispatches locally (regression guard for the base wiring)."""
    assert "web_fetch" in _ALL_LOCAL_TOOLS
    assert should_dispatch_locally("web_fetch") is True


def test_config_from_spec_reads_nimble_config() -> None:
    """The config helper returns the ``web_fetch`` builtin's config dict."""
    spec = _spec_with_web_fetch({"fetch_provider": "nimble", "api_key": "k"})
    assert _web_fetch_config_from_spec(spec) == {"fetch_provider": "nimble", "api_key": "k"}


def test_config_from_spec_empty_when_absent() -> None:
    """No ``web_fetch`` entry → empty config."""
    spec = SimpleNamespace(tools=SimpleNamespace(builtins=[]))
    assert _web_fetch_config_from_spec(spec) == {}


def test_nimble_path_extracts_when_url_present() -> None:
    """fetch_provider=nimble + a URL → Nimble Extract, and NO researcher spawn."""
    spec = _spec_with_web_fetch({"fetch_provider": "nimble", "api_key": "k"})
    fake = MagicMock()
    fake.json.return_value = {
        "data": {"markdown": "# Hello"},
        "url": "https://example.com",
        "status": "success",
    }
    with (
        patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post,
        patch(
            "omnigent.runner.tool_dispatch._execute_subagent_tool",
            new=AsyncMock(return_value="SUBAGENT"),
        ) as mock_sub,
    ):
        mock_post.return_value = fake
        result = asyncio.run(
            _execute_web_fetch_tool(
                {"query": "hi", "url": "https://example.com"},
                server_client=None,
                conversation_id="c",
                agent_spec=spec,
                task_id="t",
            )
        )

    assert mock_post.call_count == 1, "nimble path must call Nimble Extract"
    assert mock_sub.call_count == 0, "nimble path must not spawn the researcher sub-agent"
    assert "# Hello" in result


def test_nimble_path_falls_back_to_subagent_without_url() -> None:
    """fetch_provider=nimble but no URL → researcher sub-agent (nothing to extract)."""
    spec = _spec_with_web_fetch({"fetch_provider": "nimble", "api_key": "k"})
    with (
        patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post,
        patch(
            "omnigent.runner.tool_dispatch._execute_subagent_tool",
            new=AsyncMock(return_value="SUBAGENT"),
        ) as mock_sub,
    ):
        result = asyncio.run(
            _execute_web_fetch_tool(
                {"query": "hi"},
                server_client=None,
                conversation_id="c",
                agent_spec=spec,
                task_id="t",
            )
        )

    assert mock_post.call_count == 0, "no URL → must not call Extract"
    assert mock_sub.call_count == 1
    assert result == "SUBAGENT"


def test_no_provider_preserves_subagent_path() -> None:
    """No ``fetch_provider`` → existing researcher path, even with a URL (zero regression)."""
    spec = _spec_with_web_fetch({})
    with (
        patch("omnigent.tools.builtins.web_fetch_nimble.httpx.post") as mock_post,
        patch(
            "omnigent.runner.tool_dispatch._execute_subagent_tool",
            new=AsyncMock(return_value="SUBAGENT"),
        ) as mock_sub,
    ):
        result = asyncio.run(
            _execute_web_fetch_tool(
                {"query": "hi", "url": "https://example.com"},
                server_client=None,
                conversation_id="c",
                agent_spec=spec,
                task_id="t",
            )
        )

    assert mock_post.call_count == 0, "no provider → must not call Extract"
    assert mock_sub.call_count == 1
    assert result == "SUBAGENT"
