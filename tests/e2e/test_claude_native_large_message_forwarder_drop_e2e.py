"""End-to-end regression: large (>1 MiB) native Claude responses must not be
silently dropped from the Omnigent web UI.

The bug: in a Claude Code (``claude-native``) session, after a large (>1 MiB)
message the assistant's responses render in the legacy Claude Code TUI but NOT
in the Omnigent web UI. The web UI surfaces a "Harness stream connection
error"; the runner logs an ``httpx.ReadTimeout`` on a POST/PATCH to the managed
server -- the Databricks Apps forwarder path enforces a ~1 MiB gRPC
message-size quota, so an oversized transcript-item POST hangs and times out.

The claude-native forwarder (``forward_claude_transcript_to_session`` ->
``_forward_available_items`` -> ``_post_external_conversation_item``) is the
TUI->Web *display* path: it tails Claude's JSONL transcript and mirrors every
item into the session via ``POST /v1/sessions/{id}/events``. On a
``httpx.ReadTimeout`` the shared delivery classifier
(``post_may_have_been_delivered``) treats the POST as *ambiguously delivered*,
so the forwarder SKIPS the item without retrying -- items are not server-deduped
and a blind retry would duplicate the bubble. The large response is therefore
lost from the web-visible transcript while it stays in the TUI (Claude's own
JSONL), which is exactly the reported symptom.

Scope / harness note: the >1 MiB quota is a deployment-infrastructure limit on
the Databricks Apps managed-server forwarder, not on a local self-spawned
uvicorn server, so the ``tests/e2e_ui`` browser harness cannot impose it and the
web surface cannot be driven to the failure locally. This test reproduces the
same product code path faithfully by driving the REAL forwarder against a
recording HTTP server that MODELS the quota: it hangs (forcing a genuine
``httpx.ReadTimeout``) on any request body larger than 1 MiB, exactly as the
gRPC forwarder does, and commits everything smaller.

The assertion encodes the guarded behavior -- the large assistant response must
reach the web-visible transcript (be delivered as a conversation item) instead
of being silently dropped. The forwarder bounds each item POST under the quota
(capping the mirrored text with an explicit truncation marker; the full text
stays in the native session), so the response's leading content always lands.
"""

from __future__ import annotations

import json
import queue
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, cast

import httpx
import pytest

import omnigent.claude_native_forwarder as forwarder

# The Databricks Apps managed-server forwarder rejects/hangs on gRPC messages
# larger than ~1 MiB. Model that exact boundary.
_QUOTA_LIMIT_BYTES = 1 * 1024 * 1024  # 1 MiB

# The forwarder's per-request client timeout in this test. Kept short so the
# modeled quota hang surfaces as a ReadTimeout quickly.
_CLIENT_TIMEOUT_S = 2.0

# How long the server "hangs" on an oversized body -- longer than the client
# timeout so the forwarder observes a ReadTimeout, but bounded so no handler
# thread outlives the test.
_QUOTA_HANG_S = 5.0


class _QuotaRecordingServer(ThreadingHTTPServer):
    """Records committed POST bodies; models the >1 MiB forwarder quota.

    A request whose body exceeds :data:`_QUOTA_LIMIT_BYTES` is NOT committed and
    the handler blocks past the client's timeout, so the forwarder's POST raises
    ``httpx.ReadTimeout`` -- the observable behavior of the Databricks Apps gRPC
    message-size quota. Smaller requests are recorded and answered ``202``,
    exactly like the real Sessions API.
    """

    daemon_threads = True
    requests: queue.Queue[dict[str, Any]]


class _QuotaHandler(BaseHTTPRequestHandler):
    """Request handler that commits sub-quota bodies and hangs on oversized ones."""

    def log_message(self, format: str, *args: object) -> None:
        del format, args

    def do_POST(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        # Drain the request body off the socket regardless, so the failure is a
        # clean read-timeout-waiting-for-response (matching the reporter's
        # httpx.ReadTimeout), not a write stall.
        raw = self.rfile.read(length)
        if length > _QUOTA_LIMIT_BYTES:
            # Oversized: the managed forwarder's gRPC quota drops it and never
            # answers, so the runner's client times out. Never commit it.
            time.sleep(_QUOTA_HANG_S)
            return
        cast(_QuotaRecordingServer, self.server).requests.put(json.loads(raw.decode("utf-8")))
        self.send_response(202)
        self.end_headers()
        self.wfile.write(b"{}")

    def do_PATCH(self) -> None:
        length = int(self.headers.get("Content-Length", "0"))
        self.rfile.read(length)
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"{}")


@contextmanager
def _quota_server() -> Iterator[tuple[_QuotaRecordingServer, str]]:
    """Start a recording server that models the >1 MiB forwarder quota."""
    server = _QuotaRecordingServer(("127.0.0.1", 0), _QuotaHandler)
    server.requests = queue.Queue()
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    address = server.server_address
    host = str(address[0])
    port = int(address[1])
    try:
        yield server, f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _assistant_text_line(uuid_str: str, text: str) -> str:
    """A Claude JSONL assistant text record (mirrored as a ``message`` item)."""
    return json.dumps(
        {
            "type": "assistant",
            "uuid": uuid_str,
            "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
        }
    )


def _user_text_line(uuid_str: str, text: str) -> str:
    """A Claude JSONL user text record (mirrored as a ``message`` item)."""
    return json.dumps(
        {"type": "user", "uuid": uuid_str, "message": {"role": "user", "content": text}}
    )


def _drain_committed_items(
    server: _QuotaRecordingServer, *, timeout_s: float = 15.0
) -> list[dict[str, Any]]:
    """Collect committed ``external_conversation_item`` POST bodies until idle.

    :param server: The quota-modeling recording server.
    :param timeout_s: Overall budget to keep pulling committed requests.
    :returns: The committed conversation-item POST bodies, in arrival order.
    """
    items: list[dict[str, Any]] = []
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            body = server.requests.get(timeout=0.5)
        except queue.Empty:
            if items:
                break
            continue
        if body.get("type") == "external_conversation_item":
            items.append(body)
    return items


def _item_text(body: dict[str, Any]) -> str:
    """Concatenate the text of a mirrored ``message`` item's content parts."""
    item_data = body.get("data", {}).get("item_data", {})
    content = item_data.get("content", [])
    if not isinstance(content, list):
        return ""
    return " ".join(part.get("text", "") for part in content if isinstance(part, dict))


@pytest.mark.asyncio
async def test_large_native_claude_response_reaches_web_ui(tmp_path: Path) -> None:
    """A >1 MiB Claude response must not vanish from the web UI.

    Drives the real claude-native transcript forwarder over a transcript whose
    middle assistant turn is >1 MiB against a server that models the Databricks
    Apps >1 MiB gRPC quota. The large response must still land as a web-visible
    conversation item instead of being silently dropped after the oversized
    POST times out.
    """
    nonce = uuid.uuid4().hex[:8]
    small_before = f"SMALL-BEFORE-{nonce}"
    large_marker = f"LARGE-RESP-{nonce}"
    small_after = f"SMALL-AFTER-{nonce}"
    # >1 MiB of assistant text so the forwarder's item POST trips the quota.
    large_text = large_marker + ("x" * (_QUOTA_LIMIT_BYTES + 512 * 1024))

    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                _user_text_line("user-1", "reply small before"),
                _assistant_text_line("assistant-small-before", small_before),
                _user_text_line("user-2", "reply with the big payload"),
                _assistant_text_line("assistant-large", large_text),
                _user_text_line("user-3", "reply small after"),
                _assistant_text_line("assistant-small-after", small_after),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    state = forwarder.TranscriptForwardState(
        transcript_path=transcript_path,
        line_cursor=0,
        byte_offset=0,
        cursor_fingerprint=forwarder._jsonl_cursor_fingerprint(transcript_path, 0),
    )
    retry_tracker = forwarder._PostRetryTracker(base_delay_s=0.0, max_delay_s=0.0)
    dedupe = forwarder._ForwardDedupeState()

    with _quota_server() as (server, base_url):
        async with httpx.AsyncClient(base_url=base_url, timeout=_CLIENT_TIMEOUT_S) as client:
            await forwarder._forward_available_items(
                client=client,
                session_id="conv_large_response",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                state=state,
                retry_tracker=retry_tracker,
                dedupe=dedupe,
            )
        committed = _drain_committed_items(server)

    committed_text = "\n".join(_item_text(body) for body in committed)

    # Sanity: the forwarder kept going past the oversized item -- the small
    # response AFTER the large one still reached the web-visible transcript.
    assert small_after in committed_text, (
        "forwarder did not continue past the large item; "
        f"committed item text: {committed_text[:500]!r}"
    )

    # The bug: the >1 MiB assistant response is silently dropped from the web UI
    # (delivered only to the TUI). It MUST reach the web-visible transcript.
    assert large_marker in committed_text, (
        "the >1 MiB Claude response was dropped from the Omnigent web UI (never "
        "delivered as a conversation item) after the forwarder's oversized POST "
        "timed out; it survives only in the legacy Claude Code TUI. committed "
        f"markers present: small_before={small_before in committed_text} "
        f"small_after={small_after in committed_text} large={large_marker in committed_text}"
    )
