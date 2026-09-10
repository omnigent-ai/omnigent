"""E2E: writing a harness credential to a host running omnigent v0.6.0.

The setup dialog's credential form POSTs
``/v1/hosts/{id}/harnesses/{harness}/credential``, which forwards a
``host.store_secret`` frame over the host tunnel. A host daemon on omnigent
v0.6.0 predates that frame kind: its frame loop can't decode it and silently
drops it (v0.6.0 ``omnigent/host/connect.py:_serve_frames``). The reported bug:
the server never checks the host can handle the frame before forwarding, so the
user's save spins for the full 30s store-secret timeout and then fails with a
misleading ``504 "host ... did not respond to store_secret within 30s"`` — as if
the host were flaky, when it is online and healthy but will *never* answer.
Expected: a fast, actionable rejection (the host is too old — update it), not a
30-second dead wait blamed on host responsiveness.

Unlike ``test_harness_credential.py`` (which stubs the credential POST with
``page.route``), these tests must exercise the real server route and the real
tunnel — the bug lives between them. So a **fake v0.6.0 host** connects over the
real host WebSocket tunnel (``/v1/hosts/{id}/tunnel``), sends the hello a 0.6.0
daemon sends, answers only the frame kinds v0.6.0 knew (pings, stat, list_dir),
and silently drops everything else — exactly the production behavior of an
out-of-date machine. The shared ``live_server`` doesn't enable the
``harness_install`` flag, so a dedicated server is spawned with it on.

Covered facets:

- ``test_credential_route_rejects_old_host_fast_and_clearly`` — the credential
  route itself: POSTing a key for a host that can't handle ``store_secret``
  must fail fast with a non-504, non-"did not respond" error (bug: 30s hang,
  then 504 "did not respond to store_secret within 30s").
- ``test_setup_dialog_save_against_old_host_gives_prompt_feedback`` — the user
  journey: in the setup dialog, saving an API key against the old host must
  surface feedback promptly instead of leaving the save spinning ~30s and then
  toasting the misleading timeout message.

The async-in-a-fresh-thread shape is inherited from
``test_windows_workspace_picker.py`` (pytest-asyncio can't start a loop on the
main thread once a sync pytest-playwright test has run in the session).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections.abc import AsyncIterator, Coroutine, Iterator
from typing import Any

import httpx
import pytest
from playwright.async_api import async_playwright, expect

from omnigent.host.frames import (
    HostHelloFrame,
    HostListDirFrame,
    HostListDirResultFrame,
    HostStatFrame,
    HostStatResultFrame,
    encode_host_frame,
)
from omnigent.runner.transports.ws_tunnel.frames import (
    PingFrame,
    PongFrame,
    decode_frame,
    encode_frame,
)
from tests.e2e_ui.conftest import _BUILD_OUTPUT, _REPO_ROOT, _find_free_port

_HOST_NAME = "old-host-0-6-0-e2e"
_HARNESS = "claude-native"
# The server's built-in Claude Code agent — the picker's "Claude Code" harness
# row (see BUILTIN_AGENTS in web/src/lib/agentGrouping.ts). Its readiness on
# the fake old host is "needs-auth", which is what makes the setup dialog
# offer the credential form.
_CLAUDE_AGENT_NAME = "claude-native-ui"

# The dedicated server cold-boots alongside the suite's shared one; give it a
# wider window than the shared fixture's 30s so CI can't flake on the spawn.
_SERVER_BOOT_TIMEOUT_S = 180.0

# The bug is a 30s dead wait (_STORE_SECRET_TIMEOUT_S in
# omnigent/server/routes/hosts.py). "Prompt" feedback means well under that;
# 20s leaves slack for CI scheduling while still failing the buggy build.
_PROMPT_FEEDBACK_S = 20.0


# ── Fake v0.6.0 host ─────────────────────────────────────────────────────────


def _stat_reply(frame: HostStatFrame) -> HostStatResultFrame:
    """Answer ``host.stat`` the way the old host would for any path: exists.

    The journey never browses the filesystem; a permissive stat just keeps any
    incidental recent-workspace validation from wedging the landing screen.
    """
    return HostStatResultFrame(
        request_id=frame.request_id,
        status="ok",
        exists=True,
        type="directory",
        canonical_path=frame.path,
    )


async def _serve_old_host(ws: Any, dropped: list[str]) -> None:
    """Serve frames exactly like a v0.6.0 host daemon.

    v0.6.0 knew hello/readiness/runner/stat/list_dir/worktree/create_dir
    frames. ``host.store_secret``, ``host.detect_credentials`` and
    ``host.model_options`` did not exist yet: ``decode_host_frame`` raises on
    them, the runner-frame fallback also fails, and the frame is silently
    dropped (v0.6.0 ``connect.py:_serve_frames``). Tunnel keepalive pings are
    answered so the host stays "online" throughout.

    :param ws: The connected ``websockets`` client connection.
    :param dropped: Mutated with the ``kind`` of every silently-dropped frame,
        so tests can report whether the server really forwarded a frame the
        host can't handle.
    """
    known_kinds = {"host.stat", "host.list_dir"}
    async for raw in ws:
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            continue
        kind = payload.get("kind") if isinstance(payload, dict) else None
        if kind == "host.stat":
            frame = HostStatFrame(request_id=payload["request_id"], path=payload.get("path", "~"))
            await ws.send(encode_host_frame(_stat_reply(frame)))
            continue
        if kind == "host.list_dir":
            frame = HostListDirFrame(
                request_id=payload["request_id"], path=payload.get("path", "~")
            )
            await ws.send(
                encode_host_frame(
                    HostListDirResultFrame(request_id=frame.request_id, status="ok", entries=[])
                )
            )
            continue
        if isinstance(kind, str) and kind.startswith("host.") and kind not in known_kinds:
            # Unknown to 0.6.0 (store_secret / detect_credentials /
            # model_options / ...): dropped without a reply, like the real
            # old daemon.
            dropped.append(kind)
            continue
        try:
            runner_frame = decode_frame(raw)
        except ValueError:
            continue
        if isinstance(runner_frame, PingFrame):
            await ws.send(encode_frame(PongFrame(ts=runner_frame.ts)))


@contextlib.asynccontextmanager
async def _old_host(base_url: str, dropped: list[str]) -> AsyncIterator[str]:
    """Connect a fake v0.6.0 host to the live server's host tunnel.

    Sends the hello a 0.6.0 daemon sends: ``version="0.6.0"``, wire protocol 1,
    and a readiness map marking claude-native installed-but-unauthenticated —
    the state whose fix is exactly the credential write under test.

    :param base_url: The dedicated server's base URL.
    :param dropped: Passed through to :func:`_serve_old_host`.
    :returns: Async context manager yielding the REST-reported host id.
    """
    import websockets

    host_id = uuid.uuid4().hex
    ws_url = base_url.replace("http://", "ws://") + f"/v1/hosts/{host_id}/tunnel"
    async with websockets.connect(ws_url) as ws:
        await ws.send(
            encode_host_frame(
                HostHelloFrame(
                    version="0.6.0",
                    frame_protocol_version=1,
                    name=_HOST_NAME,
                    runners=[],
                    configured_harnesses={_HARNESS: "needs-auth"},
                )
            )
        )
        serve_task = asyncio.create_task(_serve_old_host(ws, dropped))
        try:
            rest_host_id: str | None = None
            async with httpx.AsyncClient(trust_env=False) as client:
                for _ in range(100):
                    resp = await client.get(f"{base_url}/v1/hosts")
                    hosts = resp.json().get("hosts", [])
                    match = next(
                        (h for h in hosts if h["name"] == _HOST_NAME and h["status"] == "online"),
                        None,
                    )
                    if match is not None:
                        rest_host_id = match["host_id"]
                        break
                    await asyncio.sleep(0.1)
                else:
                    raise AssertionError("fake v0.6.0 host never came online")
            assert rest_host_id is not None
            yield rest_host_id
        finally:
            serve_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await serve_task


# ── Dedicated server (harness_install on) ────────────────────────────────────


@pytest.fixture(scope="module")
def old_host_server(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[str]:
    """Spawn a server with ``OMNIGENT_FEATURES=harness_install`` and yield its URL.

    The suite's shared ``live_server`` doesn't enable the flag, and the
    credential route 404s without it. Mirrors the shared spawn (random port,
    per-module sqlite DB, log dumped into the RuntimeError on a failed boot)
    but starts no runner — the journey needs only a connected host, never an
    agent turn. The built-in Claude Code agent the picker offers is registered
    by the server itself.

    :param built_spa: Ensures the SPA bundle exists before the server mounts it.
    :param tmp_path_factory: Pytest temp dirs for the DB/log/artifacts.
    :returns: The dedicated server's base URL.
    """
    port = _find_free_port()
    server_tmp = tmp_path_factory.mktemp("old_host_server")
    log_path = server_tmp / "server.log"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir(parents=True, exist_ok=True)

    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        # The whole point of the dedicated spawn: the credential route exists.
        "OMNIGENT_FEATURES": "harness_install",
        # Serve the HEAD SPA bundle regardless of what's installed in the venv.
        "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT),
        # No turns run here; strip ambient credentials so none can leak in.
        "ANTHROPIC_API_KEY": "",
        "OPENAI_API_KEY": "mock-key",
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — lives for the Popen; closed in finally
    proc = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "from omnigent.cli import main; main()",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{server_tmp / 'test.db'}",
            "--artifact-location",
            str(artifact_dir),
        ],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + _SERVER_BOOT_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            try:
                # trust_env=False: a CI HTTP(S) proxy must not intercept loopback.
                if httpx.get(f"{base_url}/health", timeout=2, trust_env=False).status_code == 200:
                    ready = True
                    break
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(0.5)
        if not ready:
            log_handle.flush()
            log_text = log_path.read_text() if log_path.exists() else ""
            raise RuntimeError(
                f"old-host server not healthy within {_SERVER_BOOT_TIMEOUT_S:.0f}s on "
                f"{base_url} (last_error={last_error}).\n{log_text[-3000:]}"
            )
        yield base_url
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=10)
        log_handle.close()


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    Same rationale as ``test_windows_workspace_picker.py``: once a
    pytest-playwright sync test has run in the session, pytest-asyncio can't
    start a loop on the main thread. Exceptions re-raise on the calling thread.

    :param coro: The coroutine to run to completion.
    :raises Exception: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, Exception] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except Exception as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


def _video_kwargs() -> dict[str, Any]:
    """Page kwargs that record a video when the recording lane asks for one.

    :returns: ``record_video_dir`` kwargs when ``OMNI_E2E_VIDEO_DIR`` is set
        (the repro/fix recording lanes), else no kwargs.
    """
    video_dir = os.environ.get("OMNI_E2E_VIDEO_DIR")
    return {"record_video_dir": video_dir} if video_dir else {}


# ── Tests ────────────────────────────────────────────────────────────────────


def test_credential_route_rejects_old_host_fast_and_clearly(old_host_server: str) -> None:
    """POSTing a credential for a pre-store_secret host fails fast and clearly.

    The buggy build forwards ``host.store_secret`` to a host that can't decode
    it, waits the full 30s ``_STORE_SECRET_TIMEOUT_S``, and returns
    ``504 "host ... did not respond to store_secret within 30s"`` — misleading,
    because the host is online and will never answer. The route must instead
    reject the write promptly with an error that doesn't blame responsiveness.
    """
    _run_in_fresh_loop(_drive_credential_post(old_host_server))


async def _drive_credential_post(base_url: str) -> None:
    dropped: list[str] = []
    async with (
        _old_host(base_url, dropped) as host_id,
        httpx.AsyncClient(trust_env=False, timeout=60.0) as client,
    ):
        start = time.monotonic()
        resp = await client.post(
            f"{base_url}/v1/hosts/{host_id}/harnesses/{_HARNESS}/credential",
            json={"kind": "key", "secret": "sk-ant-e2e-old-host"},
        )
        elapsed = time.monotonic() - start

        problems: list[str] = []
        if elapsed >= _PROMPT_FEEDBACK_S:
            problems.append(
                f"took {elapsed:.1f}s — the user sits through the full 30s store-secret timeout"
            )
        if resp.status_code == 504:
            problems.append("returned 504 (gateway timeout) for a host that is online")
        if 200 <= resp.status_code < 300:
            problems.append(
                f"returned HTTP {resp.status_code} — a v0.6.0 host cannot write a "
                "credential, so the route must not claim success"
            )
        if "did not respond" in resp.text.lower():
            problems.append(
                f"error blames host responsiveness ({resp.text[:200]!r}) — the host "
                "is healthy; it is too old to know the store_secret frame"
            )
        assert not problems, (
            f"credential write against a v0.6.0 host (dropped frames: {dropped}) "
            f"-> HTTP {resp.status_code} after {elapsed:.1f}s: " + "; ".join(problems)
        )


def test_setup_dialog_save_against_old_host_gives_prompt_feedback(old_host_server: str) -> None:
    """Saving a key in the setup dialog against the old host answers promptly.

    The user journey behind the report: pick the v0.6.0 host, pick a Claude
    agent (needs-auth there), open "Set up" → "Set up auth", paste a key, Save.
    On the buggy build the save spins ~30s with no feedback, then toasts
    ``Couldn't save the credential: host ... did not respond to store_secret
    within 30s``. The UI must surface feedback well before that dead wait, and
    the message must not blame host responsiveness.
    """
    _run_in_fresh_loop(_drive_setup_dialog_save(old_host_server))


async def _drive_setup_dialog_save(base_url: str) -> None:
    dropped: list[str] = []
    async with _old_host(base_url, dropped) as host_id, async_playwright() as pw:
        # The built-in Claude Code agent's id is minted at registration; look
        # it up by its stable name.
        async with httpx.AsyncClient(trust_env=False) as client:
            agents = (await client.get(f"{base_url}/v1/agents")).json().get("data", [])
            agent = next(a for a in agents if a["name"] == _CLAUDE_AGENT_NAME)

        browser = await pw.chromium.launch()
        # Explicit context so a recorded video is finalized on context.close()
        # even when the drive fails mid-way.
        context = await browser.new_context(**_video_kwargs())
        page = await context.new_page()
        try:
            await page.goto(f"{base_url}/")
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=30_000
            )

            # Select the fake v0.6.0 host explicitly.
            await page.get_by_test_id("new-chat-landing-host-chip").click()
            await page.get_by_test_id(f"new-chat-landing-host-{host_id}").click(timeout=15_000)
            # Let the dropdown's exit animation unmount before the next popover
            # (same Radix timing artifact test_windows_workspace_picker.py notes).
            await expect(page.locator('[data-slot="dropdown-menu-content"]')).to_have_count(0)

            # Select the built-in Claude Code agent — needs-auth on this host.
            await page.get_by_test_id("new-chat-landing-agent-select").click()
            agent_option = page.get_by_test_id(f"new-chat-landing-agent-{agent['id']}")
            await expect(agent_option).to_be_visible(timeout=60_000)
            await agent_option.click()

            # "Set up →" opens the dialog; the auth step expands the form.
            setup = page.get_by_test_id("new-chat-landing-harness-setup")
            await expect(setup).to_be_visible(timeout=60_000)
            await setup.click()
            await page.get_by_test_id("harness-setup-add-credential").click(timeout=15_000)
            await expect(page.get_by_test_id("harness-credential-form")).to_be_visible(
                timeout=5_000
            )

            await page.get_by_test_id("harness-credential-key").fill("sk-ant-e2e-old-host")
            await page.get_by_test_id("harness-credential-save").click()

            # The user must get feedback promptly — the buggy build shows
            # nothing until the 30s server timeout lands.
            toast = page.get_by_test_id("toast").first
            await expect(toast).to_be_visible(timeout=int(_PROMPT_FEEDBACK_S * 1000))
            # … and the message must not be the misleading responsiveness blame.
            await expect(toast).not_to_contain_text("did not respond")
        finally:
            await context.close()
            await browser.close()
