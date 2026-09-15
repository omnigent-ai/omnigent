"""UI journey: a SIGTERM'd claude CLI child must not wedge the session.

The claude-sdk executor keeps ONE live ``ClaudeSDKClient`` per session and
reuses it across turns. That client owns a long-lived ``claude`` CLI child
process. If the CLI process is terminated out from under the executor between
turns (a SIGTERM from the OS / a cgroup / an idle-reap / a resource limit, which
exits the CLI with code 143 = 128 + SIGTERM), the executor still holds the now
dead client in its per-session cache. Nothing evicts a cached client between
turns -- eviction only happens on ``close_session``, the in-turn crash boundary,
or a cancelled turn. So the NEXT turn reuses the dead client, and
``ClaudeSDKExecutor.run_turn`` calls ``client.query(...)`` -> the SDK transport's
``write()`` sees ``self._process.returncode is not None`` and raises
``CLIConnectionError("Cannot write to terminated process (exit code: 143)")``.
The executor's top-level error boundary surfaces that verbatim, so the runner
publishes ``turn surfaced to UI as failed ... Claude SDK error: Cannot write to
terminated process (exit code: 143)`` and the user sees an error pill instead of
a reply. The error boundary also records a crash marker, so every later turn in
the session is refused outright — the session is permanently wedged.

Journey driven here, on the real web SPA against a live server + runner and the
real ``claude`` CLI pointed at the mock Anthropic endpoint:

1. start a claude-sdk session (real ``claude`` CLI subprocess)
2. send a message; turn 1 succeeds -> the executor caches a live client whose
   ``claude`` CLI child process is now running
3. that CLI child process is terminated with SIGTERM (exit code 143) -- the
   real-world trigger (OS / cgroup / idle-reap kills the child), reproduced by
   sending the signal to the child pid that appeared for this session
4. send a second message; the executor reuses the cached dead client and tries
   to write the prompt to the terminated CLI
5. observable failure: the second turn surfaces to the UI as failed with an
   error pill reading "Cannot write to terminated process (exit code: 143)"
   instead of the session recovering and answering.

Regression guard: the final assertions require actual recovery -- turn 2 must
produce a fresh assistant reply with no error surfaced. They FAIL on the
unfixed build (the turn errors instead of answering, whatever wording the
dead-transport write surfaces) and pass once the executor detects the dead
client and rebuilds it instead of writing to the corpse.

Usage::

    pytest tests/e2e_ui/chat/test_claude_sdk_terminated_cli_recovery.py -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import signal
import tarfile
import time
import uuid

import httpx
import psutil
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
_ERROR_PILL = '[data-testid="error-pill"]'
_ERROR_CONTENT = '[data-testid="error-message-content"]'

# The exact text the SDK transport raises when a prior turn's CLI child died.
_TERMINATED_TEXT = "Cannot write to terminated process"

# Spec-declared context window; not central to this bug, but the SPA's status
# tray renders more predictably with a known denominator.
_CONTEXT_WINDOW = 200_000


def _build_claude_sdk_bundle(name: str, mock_llm_server_url: str) -> bytes:
    """Build a one-file claude-sdk agent bundle wired at the mock LLM.

    ``executor.auth`` (type api_key + base_url) points the claude CLI's
    ``ANTHROPIC_BASE_URL`` at the mock server, which serves the Anthropic
    ``/v1/messages`` SSE format, so the real ``claude`` CLI subprocess runs
    without reaching a real provider.

    :param name: Agent name (unique per test run).
    :param mock_llm_server_url: Mock server base URL WITHOUT ``/v1``.
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "claude-sdk",
            "model": "claude-sonnet-4-20250514",
            "context_window": _CONTEXT_WINDOW,
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


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_llm_server_url: str) -> str:
    """Create a runner-bound session for a fresh claude-sdk agent.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :param mock_llm_server_url: Mock server base URL (no ``/v1``).
    :returns: The new session id.
    """
    name = f"sdk-term-{uuid.uuid4().hex[:8]}"
    bundle = _build_claude_sdk_bundle(name, mock_llm_server_url)
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
    return session_id


def _claude_cli_pids() -> set[int]:
    """PIDs of live SDK-launched ``claude`` CLI child processes.

    The claude-sdk executor launches the CLI as ``.../claude --output-format
    stream-json --verbose ...``, so a running SDK CLI is uniquely identified by
    the ``stream-json`` output flag on a process named/pathed ``claude``. This
    deliberately does NOT match the CLI the harness might run for other
    purposes; the caller diffs before/after the turn to isolate this session's
    child.
    """
    pids: set[int] = set()
    for proc in psutil.process_iter(["pid", "name", "cmdline"]):
        try:
            name = (proc.info["name"] or "").lower()
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if "stream-json" not in cmd:
            continue
        if name == "claude" or "/claude" in cmd.lower():
            pids.add(proc.info["pid"])
    return pids


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


@pytest.mark.timeout(600)
def test_next_turn_recovers_when_claude_cli_was_terminated(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """A turn after the cached claude CLI was SIGTERM'd must not write to it.

    Turn 1 succeeds and leaves a live cached client + running ``claude`` CLI.
    We SIGTERM that CLI child (exit code 143), then send turn 2. Pre-fix the
    executor reuses the dead client and the SDK raises "Cannot write to
    terminated process (exit code: 143)", surfaced to the UI as an error pill.
    The regression assertion -- that the user does NOT see that error after the
    next turn -- fails on the current build and passes once a dead client is
    detected and rebuilt instead of written to.
    """
    from tests.e2e_ui.conftest import configure_mock_llm

    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_claude_sdk_session(live_server, runner_id, mock_llm_server_url)
        try:
            uid = uuid.uuid4().hex[:6]
            token1 = f"sdkterm-one-{uid}"
            token2 = f"sdkterm-two-{uid}"

            # Turn 1 succeeds (a small ack). The CLI may make more than one
            # internal API call per turn, so seed several identical replies.
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "ack one"}] * 6,
                key=f"sdkterm-turn1-{uid}",
                match=token1,
            )
            # Turn 2 is scripted to succeed too: post-fix the executor rebuilds
            # the dead client and this turn answers. Pre-fix the write never
            # reaches the mock at all -- it fails at the terminated CLI first --
            # so this queue is the post-fix recovery target, not the trigger.
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "ack two"}] * 6,
                key=f"sdkterm-turn2-{uid}",
                match=token2,
            )

            page.goto(f"{live_server}/c/{session_id}")

            # ── Turn 1: succeeds, spawning this session's live claude CLI ──
            baseline_pids = _claude_cli_pids()
            _send(page, f"Say ack. {token1}")
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=180_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=180_000)
            assistant_after_turn1 = page.locator(_ASSISTANT).count()

            # ── Fault injection: terminate this session's CLI child ────────
            # Poll briefly -- the CLI is spawned during the turn's connect and
            # is certainly present now, but psutil enumeration can lag a beat.
            new_pids: set[int] = set()
            deadline = time.time() + 30
            while time.time() < deadline:
                new_pids = _claude_cli_pids() - baseline_pids
                if new_pids:
                    break
                time.sleep(0.5)
            assert new_pids, (
                "expected a claude-sdk CLI child process to be running after turn 1; "
                f"baseline={baseline_pids}, now={_claude_cli_pids()}"
            )
            for pid in new_pids:
                with contextlib.suppress(ProcessLookupError):
                    os.kill(pid, signal.SIGTERM)
            # Wait for each killed child to fully exit (exit code 143 = SIGTERM)
            # so the runner (its parent) reaps it and the transport records a
            # terminated returncode before the next write.
            for pid in new_pids:
                try:
                    proc = psutil.Process(pid)
                    exit_code = proc.wait(timeout=20)
                    print(f"claude CLI pid {pid} exited with {exit_code}")
                except psutil.NoSuchProcess:
                    pass
                except psutil.TimeoutExpired:
                    proc.kill()
            # Give the runner's event loop a moment to reap the child so the
            # SDK transport sees ``returncode is not None`` on the next write.
            time.sleep(2.0)

            # ── Turn 2: the executor must not write to the dead CLI ────────
            _send(page, f"Continue. {token2}")

            # Wait for the second turn to reach a terminal UI state: either the
            # error pill (pre-fix) or a fresh assistant reply (post-fix).
            terminal_deadline = time.time() + 240
            error_present = False
            while time.time() < terminal_deadline:
                error_present = page.locator(_ERROR_PILL).count() > 0
                reply_present = page.locator(_ASSISTANT).count() > assistant_after_turn1
                if error_present or reply_present:
                    break
                page.wait_for_timeout(500)

            # Best-effort: on the current (buggy) build, expand the pill so the
            # recording shows the "Cannot write to terminated process" text.
            # No-op post-fix (no pill), so it never fails the guard below.
            if error_present:
                try:
                    page.locator(_ERROR_PILL).first.click()
                    expect(page.locator(_ERROR_CONTENT)).to_be_visible(timeout=5_000)
                    page.wait_for_timeout(1_500)
                except Exception:  # best-effort surfacing for the recording only
                    pass

            # ── Regression assertions (fail pre-fix, pass once recovered) ──
            # The session must actually recover: turn 2 answers with a fresh
            # assistant reply and nothing errors. Asserting recovery (not just
            # the absence of one specific error string) keeps the guard
            # deterministic: pre-fix the turn always fails, whichever wording
            # the dead-transport write surfaces and whether or not the pill
            # expands.
            expect(page.get_by_text("ack two")).to_be_visible(timeout=60_000)
            expect(page.locator(_ERROR_PILL)).to_have_count(0)
            expect(page.get_by_text(_TERMINATED_TEXT)).to_have_count(0)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
