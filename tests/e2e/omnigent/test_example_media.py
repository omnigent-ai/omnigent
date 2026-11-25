"""Structural test for the media example bundle (examples/media).

Pure spec-load: no LLM, credentials or live MCP endpoint, modeled on
``test_deep_research_example.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.spec import load
from omnigent.spec.types import AgentSpec

_MEDIA_BUNDLE = Path(__file__).resolve().parents[3] / "examples" / "media"


@pytest.fixture(scope="module")
def media_spec() -> AgentSpec:
    """Load and validate the media bundle without needing an API key."""
    return load(_MEDIA_BUNDLE, expand_env=False)


def test_media_limits_the_tensorscale_mcp_server(media_spec: AgentSpec) -> None:
    """Only generation and polling tools are exposed, and no key is committed."""
    assert media_spec.name == "media"
    assert media_spec.sub_agents == []
    [server] = media_spec.mcp_servers
    assert (server.name, server.transport, server.url, server.timeout) == (
        "tensorscale",
        "http",
        "https://mcp.tensorscale.io/mcp",
        280,
    )
    assert server.tools == [
        "list_resolutions",
        "text_to_image",
        "text_to_video",
        "get_task_status",
    ]
    assert server.headers == {"Authorization": "Bearer ${TENSORSCALE_API_KEY}"}


def test_media_runs_on_claude_sdk_without_local_tools(media_spec: AgentSpec) -> None:
    """No model is pinned, and the agent gets no file or shell access."""
    assert media_spec.executor.config.get("harness") == "claude-sdk"
    assert media_spec.executor.model is None
    assert media_spec.os_env is None
