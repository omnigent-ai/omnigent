"""End-to-end regression: transcript-forwarder retry re-posts must not
duplicate conversation items, and cold-resume transcript synthesis must not
copy pre-existing store duplicates into model context.

Bug
---
``_post_external_conversation_item`` in ``omnigent/claude_native_forwarder.py``
posts each mirrored transcript item as an ``external_conversation_item`` event.
When the host is under load a POST can time out client-side after the server
has already committed the item; the forwarder's retry loop then re-posts the
identical payload. Without a server-side idempotency key each retry created a
new, byte-identical ``conversation_items`` row. The duplicates rendered in the
web UI and — because ``_ensure_local_claude_resume_transcript`` rebuilds
Claude Code's local JSONL verbatim from server items — became real model
context on every cold resume, inflating per-turn input cost.

Two facets
----------
A. **Server-side**: re-posting the same ``external_conversation_item`` (same
   ``source_id``) must persist exactly one row and return the same item id.
B. **Client-side defense in depth**: ``_claude_transcript_records_from_session_items``
   must drop adjacent byte-identical duplicates when synthesizing the resume
   transcript, so a store already corrupted with duplicates stops compounding
   them into context.

Both tests FAIL on the unfixed build (three duplicate rows / three duplicate
records) and PASS once the fix lands.

Usage::

    pytest tests/e2e/test_claude_native_forwarder_retry_dedup_e2e.py -v

No ``--llm-api-key`` or ``--profile`` needed — no LLM is invoked.
"""

from __future__ import annotations

import io
import json
import os
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Return an ephemeral TCP port the OS considers free right now."""
    s = socket.socket()
    s.bind(("", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def _build_minimal_agent_bundle() -> bytes:
    """Return a minimal agent bundle (tar.gz bytes) accepted by POST /v1/sessions."""
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "forwarder-dedup-test",
            "executor": {
                "type": "omnigent",
                "config": {"harness": "openai-agents"},
            },
            "llm": {
                "model": "forwarder-dedup-test",
                "connection": {"api_key": "test-key"},
            },
        }
    ).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(config)
        tf.addfile(info, io.BytesIO(config))
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Fixture: minimal Omnigent server subprocess
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def forwarder_dedup_server() -> Iterator[str]:
    """Start a minimal Omnigent server subprocess; yield its base URL.

    Self-contained: does not use the session-scoped ``live_server`` fixture
    (which needs ``--llm-api-key``).  Uses the same ``omnigent.cli server``
    entrypoint the production server uses, backed by a throw-away SQLite DB.

    :yields: Base URL, e.g. ``"http://127.0.0.1:<port>"``.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmp_root = Path(tempfile.mkdtemp(prefix="forwarder-dedup-e2e-"))
    db_path = tmp_root / "ap.db"
    artifact_dir = tmp_root / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_root / "server.log"

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "stub-not-used"
    # Pin single-user header auth: the test drives headerless requests, and
    # an ambient developer/CI shell exporting OMNIGENT_AUTH_ENABLED (or OIDC
    # vars) would boot the server in login mode and 401 every call.
    env["OMNIGENT_AUTH_PROVIDER"] = "header"
    env["OMNIGENT_LOCAL_SINGLE_USER"] = "1"
    # Strip ambient credentials/config that would alter server behaviour in
    # CI: any Databricks or OIDC setting, any cookie/signing secret, and the
    # specific provider/tunnel vars below.
    for var in list(env):
        if (
            var.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or var.endswith("_SECRET")
            or var
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
            env.pop(var, None)
    # Always prepend the worktree so the server imports the branch under test.
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(_REPO_ROOT)
    )

    log_handle = open(log_path, "w")  # noqa: SIM115 — subprocess holds the FD
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.cli",
            "server",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{db_path}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        cwd=str(_REPO_ROOT),
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )

    try:
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2.0)
                if resp.status_code == 200:
                    break
            except (httpx.ConnectError, httpx.ReadError):
                pass
            time.sleep(0.2)
        else:
            proc.terminate()
            log_handle.close()
            log_text = log_path.read_text(errors="replace")
            raise RuntimeError(
                f"Omnigent server failed to start within 30 s. Log tail:\n{log_text[-2000:]}"
            )
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


@pytest.fixture(scope="module")
def forwarder_dedup_client(forwarder_dedup_server: str) -> Iterator[httpx.Client]:
    """HTTP client pointed at the test server.

    :param forwarder_dedup_server: Base URL from :func:`forwarder_dedup_server`.
    :yields: A configured :class:`httpx.Client`.
    """
    with httpx.Client(base_url=forwarder_dedup_server, timeout=30.0) as client:
        yield client


@pytest.fixture(scope="module")
def forwarder_dedup_session_id(forwarder_dedup_client: httpx.Client) -> str:
    """Create a real session and return its id.

    Uploads a minimal agent bundle so the session row exists in the DB
    (the events route checks session existence before accepting).

    :param forwarder_dedup_client: Live server HTTP client.
    :returns: The session/conversation id string.
    """
    bundle = _build_minimal_agent_bundle()
    resp = forwarder_dedup_client.post(
        "/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code in (200, 201), (
        f"Session create failed {resp.status_code}: {resp.text[:400]}"
    )
    data = resp.json()
    session_id: str = data.get("id") or data.get("session_id") or ""
    assert session_id, f"No session id in response: {data}"
    return session_id


# ---------------------------------------------------------------------------
# Facet A: server-side idempotent persistence keyed on source_id
# ---------------------------------------------------------------------------


def test_external_item_retry_reposts_persist_once(
    forwarder_dedup_client: httpx.Client,
    forwarder_dedup_session_id: str,
) -> None:
    """Re-posting the same ``external_conversation_item`` stores one row.

    Simulates the forwarder's retry loop after a client-side timeout whose
    server disposition is unknown: the identical payload (same ``source_id``
    idempotency key, as the fixed forwarder sends) is POSTed three times.

    The server must treat the retries as re-posts of the committed item:
    every POST returns the same ``item_id`` and the items list contains
    exactly one copy.  On the unfixed build each POST inserted a distinct,
    byte-identical row (this test then fails with sentinel_count == 3).
    """
    payload = {
        "type": "external_conversation_item",
        "data": {
            "item_type": "message",
            "item_data": {
                "role": "user",
                "content": [
                    {
                        "type": "input_text",
                        "text": "retry-duplicate-sentinel-server",
                    }
                ],
            },
            "response_id": "resp_forwarder_retry_facet_a",
            # The forwarder's stable per-item idempotency key, re-sent
            # verbatim on every retry of the same item.
            "source_id": "11111111-2222-3333-4444-555555555555:0:message",
        },
    }

    item_ids: list[str] = []
    for attempt in range(3):
        resp = forwarder_dedup_client.post(
            f"/v1/sessions/{forwarder_dedup_session_id}/events",
            json=payload,
        )
        assert resp.status_code in (200, 202), (
            f"POST attempt {attempt} failed {resp.status_code}: {resp.text[:400]}"
        )
        item_id = resp.json().get("item_id", "")
        assert item_id, f"No item_id in response on attempt {attempt}: {resp.json()}"
        item_ids.append(item_id)

    items_resp = forwarder_dedup_client.get(
        f"/v1/sessions/{forwarder_dedup_session_id}/items",
        params={"limit": 100, "order": "asc"},
    )
    assert items_resp.status_code == 200, (
        f"GET items failed {items_resp.status_code}: {items_resp.text[:400]}"
    )
    items = items_resp.json().get("data", [])

    sentinel = "retry-duplicate-sentinel-server"
    counts = Counter(
        block.get("text", "")
        for item in items
        for block in item.get("content", [])
        if isinstance(block, dict) and block.get("type") == "input_text"
    )
    sentinel_count = counts.get(sentinel, 0)

    assert sentinel_count == 1, (
        f"Expected exactly 1 persisted copy of a retried external item, got "
        f"{sentinel_count}. Multiple copies mean the server appended a new row "
        f"per retry instead of deduplicating on source_id. item_ids: {item_ids}"
    )
    assert len(set(item_ids)) == 1, (
        f"Expected every retry POST to return the same item_id, got {set(item_ids)}. "
        f"Distinct ids mean the server created a new row per POST — the bug."
    )


# ---------------------------------------------------------------------------
# Facet B: resume-transcript synthesis must drop adjacent duplicates
# ---------------------------------------------------------------------------


def test_resume_transcript_drops_adjacent_duplicate_items() -> None:
    """``_claude_transcript_records_from_session_items`` collapses adjacent
    byte-identical items into one record.

    Defense in depth for a store already corrupted with retry duplicates:
    ``_ensure_local_claude_resume_transcript`` rebuilds Claude Code's JSONL
    from server items on cold resume, and without adjacent-duplicate
    filtering every duplicate became real model context on every subsequent
    turn.  On the unfixed build three identical inputs produced three
    records (this test then fails with sentinel_count == 3).
    """
    from omnigent.claude_native import _claude_transcript_records_from_session_items

    duplicate_item = {
        "type": "message",
        "role": "user",
        "content": [
            {
                "type": "input_text",
                "text": "resume-duplicate-sentinel-client",
            }
        ],
    }
    # Simulate three identical store rows (the result of three retry re-posts).
    items = [duplicate_item, duplicate_item, duplicate_item]

    records = _claude_transcript_records_from_session_items(
        items,
        session_id="conv_forwarder_dedup_test",
        external_session_id="12345678-0000-0000-0000-000000000001",
        cwd=Path(tempfile.gettempdir()),
        bridge_dir=Path(tempfile.gettempdir()) / "bridge",
    )

    sentinel = "resume-duplicate-sentinel-client"
    sentinel_count = sum(1 for r in records if sentinel in json.dumps(r))

    assert sentinel_count == 1, (
        f"Expected 1 record from 3 byte-identical adjacent items (adjacent-"
        f"duplicate filtering), got {sentinel_count}. Every surplus record is "
        f"duplicated model context replayed on each resumed turn. "
        f"Records: {records}"
    )
    assert len(records) == 1, (
        f"Expected 1 output record from 3 identical input items, got "
        f"{len(records)}. Records: {records}"
    )
