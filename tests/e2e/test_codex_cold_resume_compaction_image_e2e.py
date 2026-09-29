"""E2E: a Codex cold resume must not replay compaction-omitted images as invalid URLs.

Reproduces the reported journey end to end with the real ``codex`` CLI:

1. A codex-native conversation carried an inline screenshot and was compacted.
   The compaction snapshot is persisted through the real storage seam
   (``CompactionData`` validation), which strips the image bytes to the
   ``[image/png content omitted from the compaction snapshot]`` marker while
   the block keeps its Responses ``input_image`` type.
2. Cold resume: the production rollout-refresh path
   (``_ensure_local_codex_resume_rollout``) rebuilds Codex's local rollout from
   the stored session items, served here with the exact
   ``GET /v1/sessions/{id}/items`` page shape.
3. The user sends a text-only message on the resumed conversation
   (``codex exec resume`` -- the non-interactive twin of the TUI resume that
   ``omnigent codex --resume`` launches).

Expected (the regression this guards): the turn completes -- unusable
historical images are replayed as text notices, valid image URLs survive, and
no ``input_image`` block with the omission marker reaches the provider.

Before the fix the marker rides ``replacement_history`` verbatim into Codex's
context, the provider rejects every request with HTTP 400
(``Invalid 'input[N].content[1].image_url'. Expected a valid URL, but got a
value with an invalid format.``), and the resumed conversation is permanently
stuck -- text-only messages included.

Self-contained: the Responses provider is a loopback fake applying OpenAI's
documented ``image_url`` validation, and the Omnigent history API is an httpx
``MockTransport``; the codex CLI, the storage strip seam, and the cold-resume
rollout builder are all real. Requires no server, no credentials, no network.
"""

from __future__ import annotations

import base64
import http.server
import json
import os
import shutil
import struct
import subprocess
import threading
import zlib
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.conversation import CompactionData
from omnigent.harnesses.codex_native.main import _ensure_local_codex_resume_rollout
from tests.e2e._harness_probes import cli_unavailable_reason

_THREAD_ID = "019e96aa-0be2-7343-8d3b-6f914d60936b"
_SESSION_ID = "conv_cold_resume_compaction"
_OMISSION_MARKER_FRAGMENT = "omitted from the compaction snapshot"
_VALID_IMAGE_URL = "https://example.com/screenshot.png"
_CANARY_REPLY = "synthetic canary reply"


def _make_png(width: int = 32, height: int = 32) -> bytes:
    """Build a small, genuinely valid PNG so the fixture is a real image."""

    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + tag
            + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x7f\x3f\xbf" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def _invalid_image_url_paths(body: dict[str, Any]) -> list[str]:
    """Return ``input[i].content[j].image_url`` paths OpenAI would reject.

    Mirrors the Responses API's documented validation: an ``input_image``'s
    ``image_url`` must be an ``http(s)`` URL or a ``data:`` URI.
    """
    paths: list[str] = []
    for i, item in enumerate(body.get("input") or []):
        if not isinstance(item, dict):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for j, block in enumerate(content):
            if not isinstance(block, dict) or block.get("type") != "input_image":
                continue
            url = block.get("image_url")
            ok = isinstance(url, str) and url.startswith(("http://", "https://", "data:"))
            if not ok:
                paths.append(f"input[{i}].content[{j}].image_url")
    return paths


class _LoopbackResponsesProvider(http.server.ThreadingHTTPServer):
    """Loopback Responses provider applying OpenAI's image_url validation.

    Rejects a request carrying an invalid ``input_image.image_url`` with the
    provider's real-world 400 shape; accepts anything else with a scripted
    SSE completion.
    """

    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        super().__init__(("127.0.0.1", 0), _LoopbackHandler)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/v1"


class _LoopbackHandler(http.server.BaseHTTPRequestHandler):
    server: _LoopbackResponsesProvider

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self) -> None:
        # codex polls /models on startup; an empty list keeps it quiet.
        self._send(200, "application/json", json.dumps({"models": []}).encode())

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.requests.append(body)
        bad = _invalid_image_url_paths(body)
        if bad:
            error = {
                "error": {
                    "message": (
                        f"Invalid '{bad[0]}'. Expected a valid URL, but got a "
                        "value with an invalid format."
                    ),
                    "type": "invalid_request_error",
                    "param": bad[0],
                    "code": "invalid_value",
                }
            }
            self._send(400, "application/json", json.dumps(error).encode())
            return
        self._send(200, "text/event-stream", _sse_text_response(_CANARY_REPLY))

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


def _sse_text_response(text: str) -> bytes:
    """Minimal Responses-API SSE stream: created -> message -> completed."""
    message_item = {
        "id": "msg-1",
        "type": "message",
        "role": "assistant",
        "status": "completed",
        "content": [{"type": "output_text", "text": text}],
    }
    completed = {
        "id": "resp-1",
        "object": "response",
        "status": "completed",
        "output": [message_item],
        "usage": {
            "input_tokens": 1,
            "input_tokens_details": None,
            "output_tokens": 1,
            "output_tokens_details": None,
            "total_tokens": 2,
        },
    }
    events: list[tuple[str, dict[str, Any]]] = [
        ("response.created", {"response": {"id": "resp-1"}}),
        ("response.output_item.done", {"item": message_item}),
        ("response.completed", {"response": completed}),
    ]
    return "".join(
        f"event: {evt}\ndata: {json.dumps({'type': evt, **payload})}\n\n"
        for evt, payload in events
    ).encode()


def _stored_session_items() -> list[dict[str, Any]]:
    """Session items as the codex-native forwarder persists them.

    The compaction item's ``compacted_messages`` (Codex's ``compacted`` event
    ``replacement_history``) go through the real ``CompactionData`` storage
    seam, so the inline screenshot's base64 is stripped to the omission marker
    exactly as it is in a stored row.
    """
    data_url = "data:image/png;base64," + base64.b64encode(_make_png()).decode()
    user_with_screenshot = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "Here is a screenshot to remember."},
            {"type": "input_image", "image_url": data_url},
        ],
    }
    user_with_linked_image = {
        "type": "message",
        "role": "user",
        "content": [
            {"type": "input_text", "text": "And a linked image."},
            {"type": "input_image", "image_url": _VALID_IMAGE_URL},
        ],
    }
    assistant_ack = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "Noted both images."}],
    }
    stored = CompactionData.model_validate(
        {
            "summary": "User shared a screenshot and a linked image.",
            "last_item_id": "msg_3",
            "token_count": 42,
            "window_id": "01a070e2-2665-7d62-9b74-973decf239b7",
            "compacted_messages": [
                user_with_screenshot,
                user_with_linked_image,
                assistant_ack,
            ],
        }
    ).model_dump()
    # Precondition, not the assertion under test: storage redaction dropped
    # the base64 payload (the binary strip itself is correct and kept).
    assert data_url not in json.dumps(stored), "storage seam no longer strips base64"
    return [
        {
            "id": "msg_1",
            "type": "message",
            "role": "user",
            "content": user_with_screenshot["content"],
            "response_id": "resp_1",
        },
        {
            "id": "msg_2",
            "type": "message",
            "role": "user",
            "content": user_with_linked_image["content"],
            "response_id": "resp_1",
        },
        {
            "id": "msg_3",
            "type": "message",
            "role": "assistant",
            "content": assistant_ack["content"],
            "response_id": "resp_1",
        },
        {"id": "cmp_1", "type": "compaction", **stored, "response_id": "compact_1"},
    ]


async def _write_cold_resume_rollout(
    items: list[dict[str, Any]], codex_home: Path, workspace: Path
) -> Path:
    """Run the production cold-resume rollout refresh against stored items."""

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/v1/sessions/{_SESSION_ID}/items", request.url
        return httpx.Response(200, json={"data": items, "has_more": False})

    async with httpx.AsyncClient(
        base_url="http://omnigent-server.invalid",
        transport=httpx.MockTransport(handler),
    ) as client:
        return await _ensure_local_codex_resume_rollout(
            client,
            session_id=_SESSION_ID,
            external_session_id=_THREAD_ID,
            codex_home=codex_home,
            workspace=workspace,
            model_provider="loopback",
            codex_path=shutil.which("codex"),
        )


def _codex_subprocess_env(codex_home: Path) -> dict[str, str]:
    """A clean env for the codex CLI: no leaked runner/harness/auth state."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not k.startswith(("OMNIGENT", "RUNNER_", "CODEX", "ANTHROPIC", "DATABRICKS", "OPENAI"))
    }
    env.update(
        {
            "CODEX_HOME": str(codex_home),
            # Keep any corporate proxy out of the loopback provider path.
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
        }
    )
    return env


@pytest.mark.posix_only
@pytest.mark.timeout(300)
async def test_codex_cold_resume_survives_compaction_omitted_screenshot(
    tmp_path: Path,
) -> None:
    """A text-only turn after a cold resume must complete, not 400 forever.

    The compaction snapshot legitimately dropped the screenshot's bytes; the
    replayed history must therefore not present the omission marker to the
    provider as an ``input_image`` URL. Before the fix the marker reaches the
    provider verbatim and every turn on the resumed conversation dies with
    HTTP 400 -- the failure this test reproduces and guards against.
    """
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")

    codex_home = tmp_path / "codex-home"
    workspace = tmp_path / "workspace"
    codex_home.mkdir()
    workspace.mkdir()

    rollout = await _write_cold_resume_rollout(_stored_session_items(), codex_home, workspace)
    assert rollout.exists()

    provider = _LoopbackResponsesProvider()
    threading.Thread(target=provider.serve_forever, daemon=True).start()
    try:
        (codex_home / "config.toml").write_text(
            'model = "mock-model"\n'
            'model_provider = "loopback"\n'
            "[model_providers.loopback]\n"
            'name = "Loopback"\n'
            f'base_url = "{provider.base_url}"\n'
            'wire_api = "responses"\n'
        )
        proc = subprocess.run(
            [
                "codex",
                "exec",
                "resume",
                _THREAD_ID,
                "--skip-git-repo-check",
                "Reply with exactly: pong",
            ],
            cwd=workspace,
            env=_codex_subprocess_env(codex_home),
            capture_output=True,
            text=True,
            timeout=240,
        )
    finally:
        provider.shutdown()
        provider.server_close()

    assert provider.requests, (
        "codex never reached the model provider -- the resume itself failed: "
        f"stdout={proc.stdout[-2000:]!r} stderr={proc.stderr[-2000:]!r}"
    )

    # The omission marker must never reach the provider as an image URL --
    # this is the reported bug: the compaction-stripped block keeps its
    # input_image type and the provider rejects every turn with HTTP 400.
    invalid_paths = [path for body in provider.requests for path in _invalid_image_url_paths(body)]
    marker_blocks = [
        block
        for body in provider.requests
        for item in (body.get("input") or [])
        if isinstance(item, dict) and isinstance(item.get("content"), list)
        for block in item["content"]
        if isinstance(block, dict)
        and block.get("type") == "input_image"
        and _OMISSION_MARKER_FRAGMENT in str(block.get("image_url"))
    ]
    assert not invalid_paths and not marker_blocks, (
        "cold resume replayed compaction-omitted image(s) as invalid "
        f"input_image URLs at {invalid_paths}; blocks={marker_blocks}; "
        f"codex stderr tail: {proc.stderr[-1500:]!r}"
    )

    # The user's text-only turn must complete on the resumed conversation.
    combined_output = proc.stdout + proc.stderr
    assert proc.returncode == 0 and _CANARY_REPLY in combined_output, (
        "text-only turn after cold resume did not complete: "
        f"exit={proc.returncode} stdout={proc.stdout[-2000:]!r} "
        f"stderr={proc.stderr[-2000:]!r}"
    )

    # The valid image reference must survive the replay untouched.
    outgoing = json.dumps(provider.requests)
    assert _VALID_IMAGE_URL in outgoing, (
        "the replay dropped a valid image URL from the compacted history"
    )
