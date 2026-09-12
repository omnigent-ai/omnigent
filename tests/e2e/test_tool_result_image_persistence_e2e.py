"""E2E regression: tool-result images must not persist as inline base64.

A tool that returns an image (an image read, an MCP server returning an
image block, a computer-use screenshot) produces a ``function_call_output``
conversation item. Compaction snapshots strip inline binary payloads at the
storage seam (``CompactionData.strip_binary_payloads``) and pasted-image
message items are stored by ``file_id`` reference, but a tool result's
``output`` string is persisted verbatim — so every image a tool returns is
written to the conversation store as inline base64 (bounded only by the
1 MiB ``cap_tool_output`` cap), one copy per image, for the life of the
conversation. On a quota-backed managed store an image-heavy conversation
eventually fails to hydrate on open and becomes unopenable; on SQLite or
Postgres it is unbounded store bloat.

This test drives the real user journey against the live server stack:

1. Register an agent whose ``@tool`` function reads a PNG from disk and
   returns it as an Anthropic-style ``image`` content block — the same
   tool-result shape image reads, MCP image tools, and screenshots produce.
2. The (mock) LLM calls the tool; the tool executes for real; the result
   flows through the runner and is persisted as a ``function_call_output``
   conversation item.
3. Read the persisted conversation items back from the store and assert the
   raw base64 payload was not stored inline.

Red while the bug is present (the exact base64 of the PNG is stored inline
in the ``function_call_output`` item); green once tool-result images are
stored by reference or redacted at the persistence seam, the way compaction
snapshots and pasted-image messages already are.

Usage::

    pytest tests/e2e/test_tool_result_image_persistence_e2e.py -v
"""

from __future__ import annotations

import base64
import json
import random
import struct
import uuid
import zlib
from pathlib import Path

import httpx
import pytest
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    poll_session_until_terminal,
    register_dir_agent_with_mock_llm,
    reset_mock_llm,
    send_user_message_to_session,
)

# Marker the fixture tool embeds in its text block. Survives any
# binary-redaction / reference-on-store fix (those only touch binary
# payload fields), so asserting on it proves the tool really ran and
# its non-binary content persisted — the base64 assertion below cannot
# pass vacuously off a tool failure.
_TOOL_OK_MARKER = "read-image-ok"

# The @tool function shipped in the agent bundle (tools/python/, auto-
# discovered and loaded by file path in the server subprocess). It reads
# a real file and returns the canonical image-bearing tool-result shape:
# a text block plus an ``image`` block with a base64 ``source``.
_READ_IMAGE_TOOL_SOURCE = '''"""Image-read tool (e2e fixture): returns an image content block."""

from __future__ import annotations

import base64
import json
from pathlib import Path

from omnigent_client.tools import tool


@tool
def read_image(path: str) -> str:
    """
    Read an image file and return it as tool-result content blocks.

    Mirrors how image-bearing tool results are shaped: a text block
    plus an Anthropic-style ``image`` block whose ``source`` carries
    the base64 payload.

    :param path: Absolute path of the image file to read.
    :returns: JSON-encoded content-block list.
    """
    name = Path(path).name
    data = base64.b64encode(Path(path).read_bytes()).decode("ascii")
    return json.dumps(
        [
            {"type": "text", "text": f"read-image-ok {name}"},
            {
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": "image/png",
                    "data": data,
                },
            },
        ]
    )
'''


def _screenshot_like_png(side: int, seed: int) -> bytes:
    """
    Build a valid PNG with incompressible pixel data, no PIL needed.

    Deterministically seeded random RGB rows make the IDAT payload a
    real ~side*side*3-byte image (incompressible, so the base64 is
    genuine binary bulk), representative of a tool-returned image
    rather than a toy icon. The defect this test guards is
    size-independent: any tool-returned image is persisted inline as
    base64, so a moderate payload reproduces it exactly while keeping
    the turn's tool-result transfer well within limits and the test
    deterministic. (The ticket's quota/bloat *impact* scales with size
    and image count; the missing-redaction *defect* does not.)

    :param side: Width and height in pixels, e.g. ``64``.
    :param seed: RNG seed so reruns produce identical bytes.
    :returns: PNG file bytes.
    """
    rng = random.Random(seed)
    # Each scanline: filter byte 0 + side RGB pixels.
    raw = b"".join(b"\x00" + rng.randbytes(side * 3) for _ in range(side))

    def chunk(tag: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(tag + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + tag + payload + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", side, side, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw, 1))
        + chunk(b"IEND", b"")
    )


def _materialize_image_agent_dir(root: Path) -> Path:
    """
    Write a single-tool agent bundle dir for the image-read journey.

    :param root: Per-test temp dir to create the bundle under.
    :returns: The agent dir (``config.yaml`` + ``tools/python/``).
    """
    agent_dir = root / "image-read-agent"
    tools_dir = agent_dir / "tools" / "python"
    tools_dir.mkdir(parents=True)
    (tools_dir / "read_image.py").write_text(_READ_IMAGE_TOOL_SOURCE)
    config = {
        "spec_version": 1,
        "name": "image-read-tool",
        "description": (
            "Fixture agent for the tool-result image persistence e2e test. "
            "Ships one @tool function under tools/python/ (auto-discovered) "
            "that reads an image file and returns it as an Anthropic-style "
            "image content block."
        ),
        # config.harness is required: without it the runner computes
        # harness="omnigent" and produces no output (see the
        # decorator-tools fixture).
        "executor": {
            "type": "omnigent",
            "model": "mock-model",
            "config": {"harness": "openai-agents"},
        },
        "prompt": (
            "You have a read_image tool. When the user asks you to read an "
            "image file, call read_image with the given path, then confirm "
            "what you read."
        ),
        "os_env": {"type": "caller_process", "cwd": "."},
    }
    (agent_dir / "config.yaml").write_text(yaml.safe_dump(config))
    return agent_dir


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_tool_result_image_not_persisted_as_inline_base64(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    An image returned by a tool must not be stored inline in the item row.

    Full journey: agent's real ``read_image`` tool reads a PNG and returns
    an image content block; the turn completes; the persisted
    ``function_call_output`` row (read back from the conversation store)
    must carry the image by reference/marker, never as raw inline base64.
    """
    model = f"mock-imgread-{uuid.uuid4().hex[:6]}"
    call_id = "call_readimg1"

    png_path = tmp_path / "screenshot.png"
    png_bytes = _screenshot_like_png(side=64, seed=6434)
    png_path.write_bytes(png_bytes)
    expected_b64 = base64.b64encode(png_bytes).decode("ascii")

    reset_mock_llm(mock_llm_server_url)
    agent_name = register_dir_agent_with_mock_llm(
        http_client,
        agent_dir=_materialize_image_agent_dir(tmp_path),
        name=f"imgread-{uuid.uuid4().hex[:6]}",
        model=model,
        mock_llm_base_url=f"{mock_llm_server_url}/v1",
    )

    # Turn 1: LLM calls read_image on the PNG. Turn 2: LLM wraps up.
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": call_id,
                        "name": "read_image",
                        "arguments": json.dumps({"path": str(png_path)}),
                    },
                ],
            },
            {"text": "I read the screenshot."},
        ],
        key=model,
    )

    session_id = create_runner_bound_session(
        http_client, agent_name=agent_name, runner_id=live_runner_id
    )
    response_id = send_user_message_to_session(
        http_client,
        session_id=session_id,
        content=(
            f"Use the read_image tool to read the image at {png_path} and tell me what you read."
        ),
    )
    body = poll_session_until_terminal(
        http_client,
        session_id=session_id,
        response_id=response_id,
        timeout=120,
    )
    assert body["status"] == "completed", (
        f"turn did not complete: status={body.get('status')!r}, error={body.get('error')!r}"
    )

    # Read the persisted conversation items back from the store.
    snap = http_client.get(f"/v1/sessions/{session_id}")
    snap.raise_for_status()
    items = snap.json().get("items", [])
    outputs = [
        item
        for item in items
        if item.get("type") == "function_call_output"
        and isinstance(item.get("data"), dict)
        and item["data"].get("call_id") == call_id
    ]
    assert outputs, (
        "no persisted function_call_output for the read_image call; "
        f"item types: {[item.get('type') for item in items]}"
    )
    output = outputs[-1]["data"].get("output")
    assert isinstance(output, str)

    # Integrity guards (fix-shape independent): the tool ran and its
    # non-binary content persisted. Without these, a tool failure would
    # make the base64 assertion below pass vacuously.
    assert _TOOL_OK_MARKER in output, (
        f"tool did not run cleanly; persisted output: {output[:500]!r}"
    )
    assert not output.startswith("Error:"), f"tool errored: {output[:500]!r}"

    # The bug: the tool-returned image's raw base64 payload is persisted
    # inline in the function_call_output row. It must be stored by
    # file-store reference or redacted to a marker instead — the same
    # treatment compaction snapshots and pasted-image messages get.
    assert expected_b64 not in output, (
        "function_call_output persisted the tool-returned image as inline "
        f"base64 ({len(expected_b64)} base64 chars of a {len(png_bytes)}-byte "
        "PNG stored in the conversation item row)"
    )
    # No other persisted item may carry an inline copy either (e.g. a
    # duplicate metadata mirror of the tool result).
    assert expected_b64 not in json.dumps(items), (
        "a persisted conversation item other than the checked "
        "function_call_output carries the image's inline base64 payload"
    )
