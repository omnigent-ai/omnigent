"""End-to-end regression: Claude ``StopFailure`` categories are lost before a
session's failure is reported.

When a Claude-native turn's model call fails (bad key, unknown model, rate
limit), Claude Code fires a ``StopFailure`` hook whose payload carries an
``error`` category and a ``last_assistant_message`` explaining the cause. That
this is the REAL hook shape was confirmed by driving the real ``claude`` binary
against a mock Anthropic endpoint: a 401 yields
``{"hook_event_name":"StopFailure","error":"authentication_failed",
"last_assistant_message":"Invalid API key ..."}`` and a 404 on the model yields
``error":"model_not_found"``.

Three facets, each driving real product code, each failing on the unfixed build
and expected to pass once the cause is preserved end to end:

A. ``_hook_record_from_jsonl_record`` drops the ``error`` category: the returned
   ``ClaudeHookRecord`` carries no field holding ``authentication_failed`` /
   ``model_not_found`` / ``rate_limit``.
B. ``forward_claude_transcript_to_session`` posts the ``StopFailure`` -> failed
   edge as ``{"status": "failed"}`` with no ``output``/detail, so the server has
   nothing to surface and logs ``session turn failed ...: no detail``.
C. The server labels any wire-supplied failure ``output`` as ``codex_turn_error``
   even for a Claude wrapper, so a Claude session's error reads "Codex ran into
   an error during this turn."

This mirrors the report's own reproduction ("with the real hook reader and
forwarder and an HTTP mock"). The interactive Claude-native web journey cannot
run in CI (the TUI requires an OAuth login), so facets A/B drive the real hook
reader + real forwarder against an HTTP mock, and facet C drives a real
``omnigent server`` subprocess.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_stopfailure_detail_loss_e2e.py -v
"""

from __future__ import annotations

import asyncio
import io
import json
import os
import queue
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.harnesses.claude_native.bridge import (
    _hook_record_from_jsonl_record,
    _JsonlRecord,
    record_hook_event,
)
from omnigent.harnesses.claude_native.forwarder import forward_claude_transcript_to_session

_REPO_ROOT = Path(__file__).resolve().parents[2]

# The exact StopFailure categories Claude Code emits, verified against the real
# ``claude`` binary driven at a failing model endpoint.
_STOPFAILURE_CATEGORIES = ("authentication_failed", "model_not_found", "rate_limit")

# The real-shape StopFailure hook payload the recorder writes to hooks.jsonl.
_LAST_ASSISTANT_MESSAGE = "Invalid API key · Fix external API key"


def _stopfailure_payload(category: str) -> dict[str, object]:
    """Return the StopFailure hook payload Claude Code writes for *category*."""
    return {
        "session_id": "claude-session-uuid",
        "hook_event_name": "StopFailure",
        "error": category,
        "last_assistant_message": _LAST_ASSISTANT_MESSAGE,
    }


# ---------------------------------------------------------------------------
# Facet A: the hook reader drops the StopFailure error category
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", _STOPFAILURE_CATEGORIES)
def test_stopfailure_hook_record_preserves_error_category(category: str) -> None:
    """The hook record must retain the StopFailure ``error`` category.

    Builds the envelope exactly as :func:`record_hook_event` writes it and runs
    the real :func:`_hook_record_from_jsonl_record`. On the unfixed build the
    resulting ``ClaudeHookRecord`` has no field carrying the category, so it is
    unrecoverable downstream.
    """
    envelope = {"recorded_at": 1.0, "payload": _stopfailure_payload(category)}
    record = _hook_record_from_jsonl_record(
        _JsonlRecord(
            line_number=1,
            byte_offset=0,
            next_byte_offset=len(json.dumps(envelope)),
            text=json.dumps(envelope),
        )
    )

    assert record.event_name == "StopFailure"
    carriers = [
        name
        for name in dir(record)
        if not name.startswith("_") and getattr(record, name, None) == category
    ]
    assert carriers, (
        f"StopFailure error category {category!r} was dropped: no field on the "
        f"hook record carries it (the category is lost before failure reporting)."
    )


# ---------------------------------------------------------------------------
# Facet B: the forwarder posts failed with no detail
# ---------------------------------------------------------------------------


class _RecordingServer(HTTPServer):
    """HTTP server that records POSTed event bodies onto ``requests``."""

    requests: queue.Queue[dict[str, Any]]


def _recording_handler(requests: queue.Queue[dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    """Return a handler that queues POST bodies and 202s every request."""

    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            del args

        def _drain(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            self.rfile.read(length)

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {"_raw": raw.decode("utf-8", "replace")}
            requests.put({"method": "POST", "path": self.path, "body": body})
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def do_PATCH(self) -> None:
            self._drain()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

    return _Handler


def test_stopfailure_forward_attaches_failure_detail(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The failed edge the forwarder posts must carry the failure detail.

    Seeds a real-shape ``hooks.jsonl`` (SessionStart + StopFailure with an
    ``error`` category and ``last_assistant_message``) through the real
    :func:`record_hook_event`, then runs the real forwarder against a recording
    HTTP mock. On the unfixed build the ``StopFailure`` -> ``failed`` edge posts
    ``data == {"status": "failed"}`` with no ``output``, so the server has no
    cause to surface.
    """
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.harnesses.claude_native.bridge._BRIDGE_ROOT", tmp_path)

    bridge_dir = tmp_path / "bridge"
    transcript = tmp_path / "session.jsonl"
    transcript.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session-uuid",
            "transcript_path": str(transcript),
        },
    )
    record_hook_event(bridge_dir, _stopfailure_payload("authentication_failed"))

    requests: queue.Queue[dict[str, Any]] = queue.Queue()
    server = _RecordingServer(("127.0.0.1", 0), _recording_handler(requests))
    server.requests = requests
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    async def _drive() -> dict[str, Any] | None:
        task = asyncio.create_task(
            forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id="conv_stopfailure",
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.01,
            )
        )
        try:
            deadline = asyncio.get_event_loop().time() + 10.0
            while asyncio.get_event_loop().time() < deadline:
                try:
                    req = await asyncio.to_thread(requests.get, True, 0.5)
                except queue.Empty:
                    continue
                body = req.get("body", {})
                data = body.get("data", {}) if isinstance(body, dict) else {}
                if (
                    body.get("type") == "external_session_status"
                    and data.get("status") == "failed"
                ):
                    return body
            return None
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    try:
        failed_event = asyncio.run(_drive())
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert failed_event is not None, "the forwarder never posted a failed status edge"
    data = failed_event["data"]
    detail = data.get("output")
    assert isinstance(detail, str) and detail.strip(), (
        f"StopFailure -> failed edge carried no detail: {json.dumps(data)}. The "
        f"failure category/reason from the hook is dropped, so the server logs "
        f"'session turn failed ...: no detail'."
    )


# ---------------------------------------------------------------------------
# Facet C: a Claude wrapper's failure is mislabeled codex_turn_error
# ---------------------------------------------------------------------------


def _find_free_port() -> int:
    """Return an ephemeral TCP port the OS considers free right now."""
    s = socket.socket()
    s.bind(("", 0))
    port: int = s.getsockname()[1]
    s.close()
    return port


def _build_claude_native_agent_bundle() -> bytes:
    """Return a minimal claude-native agent bundle (tar.gz bytes)."""
    config = yaml.dump(
        {
            "spec_version": 1,
            "name": "stopfailure-attribution-test",
            "executor": {"type": "omnigent", "config": {"harness": "claude-native"}},
            "llm": {
                "model": "stopfailure-attribution-test",
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


@pytest.fixture(scope="module")
def stopfailure_server() -> Iterator[str]:
    """Start a minimal Omnigent server subprocess; yield its base URL.

    Self-contained (does not need ``--llm-api-key``): the same
    ``omnigent.cli server`` entrypoint the production server uses, backed by a
    throw-away SQLite DB.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    tmp_root = Path(tempfile.mkdtemp(prefix="stopfailure-e2e-"))
    db_path = tmp_root / "ap.db"
    artifact_dir = tmp_root / "artifacts"
    artifact_dir.mkdir()
    log_path = tmp_root / "server.log"

    env = os.environ.copy()
    env["OPENAI_API_KEY"] = "stub-not-used"
    env["OMNIGENT_AUTH_PROVIDER"] = "header"
    env["OMNIGENT_LOCAL_SINGLE_USER"] = "1"
    for var in list(env):
        if (
            var.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or var.endswith("_SECRET")
            or var
            in ("ANTHROPIC_API_KEY", "OMNIGENT_AUTH_ENABLED", "OMNIGENT_RUNNER_TUNNEL_TOKEN")
        ):
            env.pop(var, None)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = (
        f"{_REPO_ROOT}{os.pathsep}{existing_pp}" if existing_pp else str(_REPO_ROOT)
    )

    log_handle = open(log_path, "w")  # noqa: SIM115 - subprocess holds the FD
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
        deadline = time.monotonic() + 60.0
        while time.monotonic() < deadline:
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2.0, trust_env=False)
                if resp.status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if proc.poll() is not None:
                break
            time.sleep(0.2)
        else:
            proc.terminate()
            log_handle.close()
            raise RuntimeError(
                "Omnigent server failed to start within 60s. Log tail:\n"
                + log_path.read_text(errors="replace")[-2000:]
            )
        if proc.poll() is not None:
            log_handle.close()
            raise RuntimeError(
                f"server exited early (code {proc.returncode}); log tail:\n"
                + log_path.read_text(errors="replace")[-2000:]
            )
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
        log_handle.close()


def test_claude_wire_failure_not_labelled_codex(stopfailure_server: str) -> None:
    """A Claude wrapper's wire failure detail must not be labelled codex_turn_error.

    Creates a real claude-native session and posts a ``failed``
    ``external_session_status`` carrying a Claude failure ``output`` (the same
    path a fixed forwarder would use). On the unfixed build the server labels it
    ``codex_turn_error`` regardless of harness, so the UI reads "Codex ran into
    an error during this turn." for a Claude session.
    """
    with httpx.Client(base_url=stopfailure_server, timeout=30.0, trust_env=False) as client:
        create = client.post(
            "/v1/sessions",
            data={"metadata": json.dumps({})},
            files={
                "bundle": (
                    "agent.tar.gz",
                    _build_claude_native_agent_bundle(),
                    "application/gzip",
                )
            },
        )
        assert create.status_code in (200, 201), (
            f"session create failed {create.status_code}: {create.text[:400]}"
        )
        session_id = create.json().get("id") or create.json().get("session_id")
        assert session_id, f"no session id in response: {create.json()}"

        status_resp = client.post(
            f"/v1/sessions/{session_id}/events",
            json={
                "type": "external_session_status",
                "data": {"status": "failed", "output": _LAST_ASSISTANT_MESSAGE},
            },
        )
        assert status_resp.status_code in (200, 202), (
            f"failed edge POST rejected {status_resp.status_code}: {status_resp.text[:400]}"
        )

        error: dict[str, Any] | None = None
        deadline = time.monotonic() + 30.0
        while time.monotonic() < deadline:
            snap = client.get(f"/v1/sessions/{session_id}")
            if snap.status_code == 200:
                error = snap.json().get("last_task_error")
                if error is not None:
                    break
            time.sleep(0.5)

    assert error is not None, "the failed edge produced no last_task_error"
    assert error.get("message", "").strip() == _LAST_ASSISTANT_MESSAGE
    assert error.get("code") != "codex_turn_error", (
        f"a claude-native session's failure was labelled {error.get('code')!r}: "
        f"the server attributes any wire failure detail to Codex regardless of "
        f"harness, so the UI misreports a Claude error as a Codex one."
    )
