"""Create runner-bound Claude SDK sessions against the browser suite's mock model."""

from __future__ import annotations

import io
import json
import tarfile
import uuid

import httpx
import yaml

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
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def create_claude_sdk_session(
    base_url: str,
    runner_id: str,
    mock_llm_server_url: str,
    *,
    name_prefix: str,
) -> tuple[str, str]:
    """Create a session bound to ``runner_id``; return its id and unique agent name."""
    name = f"{name_prefix}-{uuid.uuid4().hex[:8]}"
    bundle = build_claude_sdk_bundle(name, mock_llm_server_url)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id, name
