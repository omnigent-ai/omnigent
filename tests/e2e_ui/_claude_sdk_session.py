"""Create runner-bound Claude SDK sessions against the browser suite's mock model."""

from __future__ import annotations

import uuid

import httpx
import yaml

from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle

CLAUDE_SDK_TEST_MODEL = "claude-sonnet-4-20250514"
CLAUDE_SDK_TEST_CONTEXT_WINDOW = 200_000


def build_claude_sdk_bundle(name: str, mock_llm_server_url: str) -> bytes:
    """Build a claude-sdk agent bundle that talks to the mock endpoint."""
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "claude-sdk",
            "model": CLAUDE_SDK_TEST_MODEL,
            "context_window": CLAUDE_SDK_TEST_CONTEXT_WINDOW,
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_server_url,
            },
        },
    }
    return bundle_files({f"{name}.yaml": yaml.safe_dump(config, sort_keys=False).encode()})


def create_claude_sdk_session(
    base_url: str,
    runner_id: str,
    mock_llm_server_url: str,
    *,
    name_prefix: str,
) -> tuple[str, str]:
    """Create a session bound to ``runner_id``; return its id and unique agent name."""
    name = f"{name_prefix}-{uuid.uuid4().hex[:8]}"
    create_resp = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        build_claude_sdk_bundle(name, mock_llm_server_url),
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    bind_session_runner(httpx.patch, base_url, session_id, runner_id, timeout=10.0)
    return session_id, name
