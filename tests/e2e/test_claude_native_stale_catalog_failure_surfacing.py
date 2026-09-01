"""A claude-native launch against a retired model must not fail silently.

Journey under test: a model catalog is written while a model id is servable →
the model is retired → a claude-native session launches → the provider rejects
the retired id (``model_not_found``) → the failure must reach the operator with
the provider's own reason, and a prompt caught in a paste burst must still be
delivered without a human pressing Enter.

Three behaviors, each observable at a public seam (no live Claude login or
tmux session required):

  1. **Stale catalog convergence** — a catalog entry older than
     ``CATALOG_STALE_AFTER_S`` still serves instantly (availability), but the
     store re-probes in the background and converges, so a retired model id
     cannot pin launches forever. The launch path is told the entry is stale
     so it never adopts the stale default as launch authority.

  2. **Failure-reason surfacing** — when Claude's hook journal records a
     ``StopFailure`` with the provider's ``error`` and the model's own
     ``last_assistant_message``, the forwarder's ``failed`` status POST must
     carry that detail as ``output`` instead of dropping it (which left the
     operator with the generic "Error: native sub-agent turn failed").

  3. **Paste-burst submit survival** — Claude Code can coalesce a rapid stdin
     burst into a paste, folding the submitting Enter into a newline. The
     submit-verify budget must ride out a long burst and keep retrying Enter
     so the message is delivered without manual intervention.
"""

from __future__ import annotations

import asyncio
import json
import os
import queue
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from omnigent import model_catalog_store as store

_RETIRED_ROWS = [
    {
        "id": "claude-fable-5",
        "model": "claude-fable-5",
        "displayName": "Claude Fable 5 (retired)",
        "isDefault": True,
    }
]

_FRESH_ROWS = [
    {
        "id": "claude-sonnet-5",
        "model": "claude-sonnet-5",
        "displayName": "Claude Sonnet 5",
        "isDefault": True,
    }
]


@pytest.fixture(autouse=True)
def _isolated_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Redirect the on-disk catalog store to a throwaway temp directory."""
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    store._inflight.clear()


def _age_entry(harness: str, fingerprint: str, age_s: float) -> None:
    """Back-date a stored catalog entry so it reads as *age_s* old."""
    path = store.catalog_path(harness, fingerprint)
    expired = time.time() - age_s
    os.utime(path, (expired, expired))


# ---------------------------------------------------------------------------
# Behavior 1 — a stale catalog cannot pin a retired model id forever
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_stale_catalog_serves_fallback_then_converges_to_fresh_rows() -> None:
    """A stale entry answers instantly but the store re-probes and converges.

    The retired id may serve one launch as a fallback, but the background
    re-probe must replace it so the retirement cannot stick: the next read
    returns the fresh rows and the retired id is gone.
    """
    harness = "claude-native"
    fingerprint = "stale-retired-fp"
    store.write_catalog(harness, fingerprint, _RETIRED_ROWS)
    _age_entry(harness, fingerprint, store.CATALOG_STALE_AFTER_S + 600)

    probes: list[int] = []

    async def _probe() -> list[dict[str, Any]]:
        probes.append(1)
        return _FRESH_ROWS

    # Availability: the stale entry still answers instantly.
    assert await store.ensure_catalog(harness, fingerprint, _probe) == _RETIRED_ROWS
    # Convergence: the stale hit must have kicked a background re-probe.
    task = store._inflight.get((harness, fingerprint))
    assert task is not None, (
        "a stale catalog hit must kick a background re-probe; without it a "
        "retired model id pins every launch until the cache file is deleted"
    )
    await task
    assert probes == [1]
    refreshed = store.read_catalog(harness, fingerprint)
    assert refreshed == _FRESH_ROWS
    assert all(row.get("id") != "claude-fable-5" for row in refreshed), (
        "the retired model id must not survive the re-probe"
    )
    # The refreshed entry is fresh: later launches serve it with no re-probe.
    assert await store.ensure_catalog(harness, fingerprint, _probe) == _FRESH_ROWS
    assert probes == [1]


def test_stale_catalog_is_reported_stale_to_the_launch_path() -> None:
    """The launch path is told the entry is stale so it never pins its default.

    ``claude_launch_catalog_is_stale`` is the signal the launch orchestration
    reads (before the fetch) to defer a Default launch to the CLI's own
    servable default instead of pinning yesterday's ``isDefault`` row.
    """
    from omnigent.claude_native import claude_catalog_fingerprint, claude_launch_catalog_is_stale

    fingerprint = claude_catalog_fingerprint(None)
    store.write_catalog("claude-native", fingerprint, _RETIRED_ROWS)
    assert claude_launch_catalog_is_stale(None) is False

    _age_entry("claude-native", fingerprint, store.CATALOG_STALE_AFTER_S + 600)
    assert claude_launch_catalog_is_stale(None) is True, (
        "an entry past the TTL must read as stale, or the launch path will "
        "pin a possibly-retired default and hard-fail with model_not_found"
    )


# ---------------------------------------------------------------------------
# Behavior 2 — the StopFailure provider reason reaches the status POST
# ---------------------------------------------------------------------------


class _RecordingHTTPServer(ThreadingHTTPServer):
    """HTTP server that records JSON POST bodies."""

    requests: queue.Queue[dict[str, Any]]


def _handler_factory(requests: queue.Queue[dict[str, Any]]) -> type[BaseHTTPRequestHandler]:
    class _Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            del format, args

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            requests.put({"method": "POST", "path": self.path, "body": body})
            payload = json.dumps({}).encode("utf-8")
            self.send_response(202)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_PATCH(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            payload = json.dumps({}).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    return _Handler


@pytest.mark.asyncio
async def test_stop_failure_provider_reason_reaches_the_status_post(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The forwarder's ``failed`` POST carries the provider's own reason.

    Claude's hook journal records the ``StopFailure`` with ``error`` (the
    provider code, e.g. ``model_not_found``) and ``last_assistant_message``
    (the model's own description). Dropping both leaves the runner posting
    ``failed`` with no output, and the operator sees only the generic
    "Error: native sub-agent turn failed".
    """
    from omnigent.claude_native_bridge import record_hook_event
    from omnigent.claude_native_forwarder import forward_claude_transcript_to_session

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)

    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "StopFailure",
            "session_id": "claude-session",
            "error": "model_not_found",
            "last_assistant_message": (
                "There's an issue with the selected model (claude-fable-5). "
                "It may not exist or you may not have access to it."
            ),
        },
    )

    requests: queue.Queue[dict[str, Any]] = queue.Queue()
    server = _RecordingHTTPServer(("127.0.0.1", 0), _handler_factory(requests))
    server.requests = requests
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address
    base_url = f"http://{host}:{port}"

    task = asyncio.create_task(
        forward_claude_transcript_to_session(
            base_url=base_url,
            headers={},
            session_id="conv_abc",
            bridge_dir=bridge_dir,
            agent_name="claude-native-ui",
            start_at_end=False,
            poll_interval_s=0.01,
        )
    )
    status_data: dict[str, Any] | None = None
    try:
        deadline = time.monotonic() + 10.0
        while time.monotonic() < deadline:
            try:
                request = await asyncio.to_thread(requests.get, True, 0.5)
            except queue.Empty:
                continue
            body = request["body"]
            if body.get("type") == "external_session_status":
                status_data = body.get("data") or {}
                break
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        server.shutdown()
        server.server_close()
        thread.join(timeout=5.0)

    assert status_data is not None, "expected an external_session_status POST"
    assert status_data.get("status") == "failed"
    output = status_data.get("output")
    assert isinstance(output, str) and "model_not_found" in output, (
        f"the failed status must carry the provider's reason so the operator "
        f"sees why the turn died instead of a generic fallback; got output={output!r}"
    )
    assert "selected model" in output, (
        f"the model's own error description must be preserved; got output={output!r}"
    )


# ---------------------------------------------------------------------------
# Behavior 3 — a prompt caught in a long paste burst is still delivered
# ---------------------------------------------------------------------------


class _VirtualClock:
    """Clock whose ``sleep`` advances ``monotonic`` instead of blocking."""

    def __init__(self) -> None:
        self.now = 0.0

    def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return self.now


def _composer_pane(draft: str = "") -> str:
    """Render a pane whose live input box holds *draft*."""
    return f"""\
──────────────────────────────
❯ {draft}
──────────────────────────────
  ? for shortcuts
"""


def test_prompt_survives_a_long_paste_burst_without_manual_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 15s paste burst must not turn a healthy turn into a failed one.

    Claude Code coalesces a rapid stdin burst into a paste; every Enter that
    lands inside the burst becomes a newline in the draft instead of a
    submit. On a loaded host the burst can outlive a short verification
    budget — the turn is then reported failed while the user's draft sits
    intact in the composer, and pressing Enter manually sends it. The
    default budget must ride out the burst and the retry loop must deliver
    the message with no human involved.
    """
    import omnigent.claude_native_bridge as bridge
    from omnigent.claude_native_bridge import inject_user_message, write_tmux_target

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)
    clock = _VirtualClock()
    monkeypatch.setattr("omnigent.claude_native_bridge.time", clock)
    # Pin the budget to the shipped default so an ambient
    # OMNIGENT_CLAUDE_SUBMIT_VERIFY_TIMEOUT_S on the host cannot skew the
    # behavior under test (the module reads the env once at import).
    default_budget = getattr(
        bridge, "_SUBMIT_VERIFY_TIMEOUT_DEFAULT_S", bridge._SUBMIT_VERIFY_TIMEOUT_S
    )
    monkeypatch.setattr("omnigent.claude_native_bridge._SUBMIT_VERIFY_TIMEOUT_S", default_budget)

    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    burst_window_s = 15.0
    prompt = "reproduce the launch failure"
    tui: dict[str, Any] = {"pane": _composer_pane(), "paste_at": None}
    enters: list[float] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """Simulate a TUI mid-paste-burst that swallows Enters for 15s."""
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = _composer_pane(prompt)
            tui["paste_at"] = clock.now
        if cmd[-1] == "Enter":
            enters.append(clock.now)
            paste_at = tui["paste_at"]
            if paste_at is not None and clock.now >= paste_at + burst_window_s:
                tui["pane"] = _composer_pane()  # submitted — input box clears
            # else: folded into the paste burst as a newline — draft stays
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)

    # Behavioral assertion: delivery succeeds with no human Enter. A budget
    # shorter than the burst raises RuntimeError here — the "turn failed but
    # the draft sits unsent until a human presses Enter" symptom.
    inject_user_message(bridge_dir, content=prompt)

    assert tui["pane"] == _composer_pane(), "the draft must have left the input box"
    assert len(enters) >= 2, "the retry loop must have re-sent Enter during the burst"
