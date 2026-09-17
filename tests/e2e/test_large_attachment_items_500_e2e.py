"""E2E regression test: a large inline attachment makes a session's
``GET /v1/sessions/<id>/items`` fail with HTTP 500, so the conversation can no
longer be opened.

Reported production journey (Databricks Apps / ``prod-aws-us-west-2``): a user
attaches a large PDF (>1 MiB). The attachment bytes are persisted **inline**
(base64) inside the conversation item, so a single item can be several MB. On
session load the server decrypts a page of items by sending them to the mas-java
CMK sidecar over gRPC; the request frame exceeds mas-java's **1 MiB** Armeria
inbound limit and is rejected with ``RESOURCE_EXHAUSTED``::

    Unhandled exception - RPC terminated with RESOURCE_EXHAUSTED.
    Frame size 2036419 exceeds maximum: 1048576

The ``_InactiveRpcError`` propagates unhandled through FastAPI ->
``GET /sessions/{id}/items`` returns HTTP 500. The failure is deterministic:
every load of that conversation 500s, so the session becomes unopenable while
the write path (upload + turn) succeeded cleanly.

Environment fidelity — this is a **stand-in** reproduction. The failing seam
(``databricks_mysql_conversation_store._decode_item_data_batch`` -> CMK decrypt
gRPC -> mas-java Armeria 1 MiB frame limit) lives in Databricks deployment code
that is not part of ``omnigent-ai/omnigent`` or this build: here the conversation
store's ``_decode_item_data_batch`` is identity (plaintext, no gRPC, no size
limit), so driving the genuine user journey never 500s. To exercise the same
user-observable failure against the real read route + exception handler, the
spawned server bootstraps ``_decode_item_data_batch`` to reject a page whose
combined payload exceeds the mas-java 1 MiB inbound limit — standing in for the
CMK sidecar's frame-size rejection. This mirrors the accepted pattern in
``tests/e2e/test_claude_native_cold_resume_items_500_e2e.py``.

Desired behavior (asserted): a session that holds a large attachment must still
load — ``GET /sessions/{id}/items`` must NOT return HTTP 500, so a single
oversized item can no longer make the whole conversation permanently
unreadable. On the buggy build the read path 500s, so this test FAILS with the
returned status + body in the failure message.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_large_attachment_items_500_e2e.py -v
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

import httpx

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy in the environment; every HTTP call in
# this test targets 127.0.0.1, so bypass proxy autodetection entirely.
_http = httpx.Client(trust_env=False)

# The runner imports ``omnigent_client`` / ``omnigent_ui_sdk``; in a worktree
# they resolve from sdks/, in an installed venv from site-packages.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# The mas-java CMK sidecar's Armeria inbound message limit — the value in the
# production error ("Frame size 2036419 exceeds maximum: 1048576").
_MAS_JAVA_FRAME_LIMIT = 1048576

# Bootstrap for the spawned server: monkeypatch the conversation store so a
# page whose combined payload exceeds the mas-java 1 MiB inbound frame limit
# raises at the decode seam the ticket fingers (``_decode_item_data_batch``) —
# standing in for the Databricks CMK sidecar's gRPC RESOURCE_EXHAUSTED. The
# REAL read route and REAL app-level exception handler then produce the
# production 500 body. Smaller pages decode unchanged (identity), so a session
# without a large attachment keeps loading. This mirrors the exact signature
# observed on the deployment without needing the mas-java / Postgres stack.
_SERVER_BOOTSTRAP = f"""
import omnigent.stores.conversation_store.sqlalchemy_store as _s

_LIMIT = {_MAS_JAVA_FRAME_LIMIT}
_orig = _s.SqlAlchemyConversationStore._decode_item_data_batch


class _ResourceExhausted(RuntimeError):
    pass


def _framed_decode(self, stored):
    frame = sum(len(s) for s in stored)
    if frame > _LIMIT:
        raise _ResourceExhausted(
            "RPC terminated with RESOURCE_EXHAUSTED. "
            f"Frame size {{frame}} exceeds maximum: {{_LIMIT}}"
        )
    return _orig(self, stored)


_s.SqlAlchemyConversationStore._decode_item_data_batch = _framed_decode

from omnigent.cli import main

main()
"""

# A minimal agent bundle. Non-``config.yaml`` arcname routes through the
# omnigent compat adapter (translates ``executor.harness`` -> the full spec)
# so no ``spec_version`` is needed.
_AGENT_YAML = """\
name: hello_world
prompt: You are a helpful assistant.

executor:
  model: gpt-4o-mini
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    env.update(extra)
    return env


def _terminate(proc: subprocess.Popen[bytes] | None) -> None:
    """Best-effort SIGTERM -> SIGKILL teardown for a spawned process."""
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
    """Poll *url* until it returns 200 or *deadline* (monotonic) passes."""
    last = "not polled"
    while time.monotonic() < deadline:
        try:
            if _http.get(url, timeout=2.0).status_code == 200:
                return
            last = "non-200"
        except httpx.HTTPError as exc:
            last = f"{type(exc).__name__}: {exc}"
        time.sleep(_POLL_S)
    raise AssertionError(f"{url} never became healthy: {last}")


def _build_agent_bundle() -> bytes:
    """Build a gzipped tarball carrying :data:`_AGENT_YAML`.

    :returns: ``.tar.gz`` bytes ready for the multipart create.
    """
    import io
    import tarfile

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = _AGENT_YAML.encode()
        info = tarfile.TarInfo("hello_world.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _create_session(base_url: str) -> str:
    """Create a plain session via multipart ``POST /v1/sessions``.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _build_agent_bundle(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _make_pdf(size_bytes: int) -> bytes:
    """Build a valid, ``size_bytes``-large single-page PDF.

    The bulk is a padded PDF comment so the whole thing stays a well-formed
    ``application/pdf`` the upload route accepts.

    :param size_bytes: Target total size, e.g. ``1_600_000``.
    :returns: The PDF bytes.
    """
    head = (
        b"%PDF-1.4\n"
        b"1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 612 792]>>endobj\n"
    )
    tail = b"trailer<</Root 1 0 R>>\n%%EOF\n"
    pad = size_bytes - len(head) - len(tail) - 2
    return head + b"%" + (b"a" * max(pad, 0)) + b"\n" + tail


def _append_items(database_uri: str, session_id: str, items: list) -> None:
    """Append conversation items straight into the spawned server's store.

    Direct store writes are the same seeding pattern the e2e suites use — there
    is no REST bulk-append.

    :param database_uri: The spawned server's SQLite URI.
    :param session_id: Conversation to append to.
    :param items: ``NewConversationItem`` objects to persist.
    """
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    store = SqlAlchemyConversationStore(database_uri)
    store.append(session_id, items)


def _small_text_items(count: int) -> list:
    """Build *count* small message items (a normal chatty transcript)."""
    from omnigent.entities import MessageData, NewConversationItem

    out = []
    for i in range(count):
        role = "user" if i % 2 == 0 else "assistant"
        out.append(
            NewConversationItem(
                type="message",
                response_id=f"resp_{i // 2}",
                data=MessageData(
                    role=role,
                    content=[
                        {
                            "type": "input_text" if role == "user" else "output_text",
                            "text": f"turn {i}",
                        }
                    ],
                    agent="hello_world" if role == "assistant" else None,
                ),
            )
        )
    return out


def _inline_attachment_item(pdf_bytes: bytes, filename: str) -> list:
    """Build a user message whose content inlines *pdf_bytes* as base64.

    Mirrors the persisted state the ticket documents: the attachment bytes are
    stored inline (base64) inside the conversation item, so a single item is
    several MB and is re-read on every session load.

    :param pdf_bytes: The attachment payload.
    :param filename: The attachment file name.
    :returns: A one-element list holding the ``NewConversationItem``.
    """
    from omnigent.entities import MessageData, NewConversationItem

    encoded = base64.b64encode(pdf_bytes).decode()
    return [
        NewConversationItem(
            type="message",
            response_id="resp_attach",
            data=MessageData(
                role="user",
                content=[
                    {"type": "input_text", "text": "Please review this PDF."},
                    {
                        "type": "input_file",
                        "filename": filename,
                        "file_data": f"data:application/pdf;base64,{encoded}",
                    },
                ],
                agent=None,
            ),
        )
    ]


def test_large_attachment_keeps_session_loadable(tmp_path: Path) -> None:
    """A session holding a large inline attachment must still load its items.

    Journey (the reporter's): a user attaches a large PDF (>1 MiB) to a
    conversation and later reloads the page. The attachment persists inline
    (base64) in the item, and the session-load decrypt of that page exceeds the
    mas-java 1 MiB frame limit.

    Expected: ``GET /sessions/{id}/items`` still serves the conversation (no
    HTTP 500), so one oversized item can not make the whole session unopenable.
    Buggy behavior: the decode overflow propagates unhandled and the read route
    returns HTTP 500, so the conversation can no longer be opened.

    :param tmp_path: Per-test temp dir (server DB + artifacts).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"

    server_log = (tmp_path / "server.log").open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    try:
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                _SERVER_BOOTSTRAP,
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                database_uri,
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)

        session_id = _create_session(base_url)

        # A normal transcript loads fine — the read path is healthy and the
        # failure below is specific to the large attachment.
        _append_items(database_uri, session_id, _small_text_items(6))
        baseline = _http.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "asc"},
            timeout=60.0,
        )
        assert baseline.status_code == 200, (
            "a session without a large attachment must load; "
            f"got {baseline.status_code}: {baseline.text[:500]}"
        )
        assert len(baseline.json()["data"]) == 6

        # The user attaches a large PDF (>1 MiB) — the real upload route. The
        # write path succeeds cleanly, exactly as in the incident.
        pdf = _make_pdf(1_600_000)
        upload = _http.post(
            f"{base_url}/v1/sessions/{session_id}/resources/files",
            files={"file": ("incident.pdf", pdf, "application/pdf")},
            timeout=60.0,
        )
        assert upload.status_code == 201, (
            f"upload should succeed; got {upload.status_code}: {upload.text[:500]}"
        )

        # The attachment persists inline (base64) in a conversation item — the
        # state the read path re-decrypts on every session load.
        _append_items(
            database_uri,
            session_id,
            _inline_attachment_item(pdf, "incident.pdf"),
        )

        # THE RELOAD: the user re-opens the session. On the buggy build the
        # oversized page busts the frame limit at the decode seam and the read
        # route returns HTTP 500, so the conversation can no longer be opened.
        reload = _http.get(
            f"{base_url}/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "asc"},
            timeout=60.0,
        )
        assert reload.status_code != 500, (
            "session with a large inline attachment became unopenable: "
            f"GET /items returned {reload.status_code}: {reload.text[:500]}. "
            "A single oversized item must not make the whole session unreadable."
        )
    finally:
        _terminate(server_proc)
        server_log.close()
