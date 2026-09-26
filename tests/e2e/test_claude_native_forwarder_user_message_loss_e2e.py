"""E2E regression: a flaky forwarder POST must not lose a user message from the
web conversation store while the terminal keeps it.

Guarded bug
-----------
On a claude-native session (Databricks Apps deployment) "some user messages are
lost on the web UI (conv store). I can continue the conversation on terminal
view but web view is completely broken." The live terminal (the real Claude TUI)
shows the full conversation; the web chat view -- which renders from the
canonical conversation store (``GET /v1/sessions/<id>/items``) -- is missing some
user messages the user actually sent.

Mechanism (the seam this drives)
--------------------------------
Only the transcript forwarder mirrors Claude's JSONL transcript into the
conversation store. In ``_forward_available_items`` each transcript item is
POSTed as an ``external_conversation_item``; when that POST fails with an
*ambiguous* transport error (the request was sent but no response was seen --
e.g. a read timeout on the remote forwarder->server hop), the forwarder cannot
tell whether the server committed the item. It used to **skip the item and
advance its cursor without retrying** to avoid double-posting a bubble; if the
server had in fact NOT committed it, that user message was then permanently
absent from the conversation store -- the reported "some user messages lost on
web / terminal fine" desync. The POST carries a ``source_id`` idempotency key
the server dedupes on, so the forwarder must instead hold the cursor and
re-post: a retry of a committed item is a no-op, never a duplicate bubble.

Environment fidelity
--------------------
The report is against a **Databricks Apps** deployment, where the forwarder
posts to a remote server over an authenticated (OAuth) connection that can flake
mid-session. This test is a **stand-in**: a local single-user ``omnigent server``
subprocess with the flaky forwarder->server POST injected as a fault at the
exact production seam. The message-loss BEHAVIOUR being asserted is product code
and environment-independent, but the triggering flaky POST is induced here
rather than arising from the real Databricks network -- so this reproduces the
*likely mechanism*, not the reported environment itself.

This drives the REAL user path: a real ``omnigent server`` subprocess (so the
real ``POST /v1/sessions/<id>/events`` route and the real commit path run), a
real claude-native session, and the real
``forward_claude_transcript_to_session`` loop tailing a seeded Claude JSONL
transcript whose user records use the shape a live Claude 2.1.236 CLI writes.

Desired behavior (asserted): every user message the user sent reaches the
conversation store, even across a single flaky POST. Buggy behavior: the middle
user message whose POST flaked once is silently skipped and never re-posted, so
``GET /v1/sessions/<id>/items`` is missing it -- this test FAILS with the missing
marker in the message, while the seeded transcript (the terminal's source) still
contains it.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_claude_native_forwarder_user_message_loss_e2e.py -v

No ``--llm-api-key`` / ``--profile`` needed -- no LLM is invoked.
"""

from __future__ import annotations

import asyncio
import contextlib
import io
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

import httpx
import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]

# CI shells can carry an egress proxy; every HTTP call here targets 127.0.0.1.
_http = httpx.Client(trust_env=False)

# The spawned server resolves worktree imports from the repo root and the SDKs.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# Plain server launch -- no store monkeypatch. The loss lives entirely in the
# forwarder's ambiguous-failure skip; the server's real commit path is intact.
_SERVER_BOOTSTRAP = "from omnigent.cli import main\n\nmain()\n"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 0.5

# A three-turn conversation. The user sent all three; the forwarder mirrors each
# user + assistant record as one ``external_conversation_item``. Distinct markers
# let the assertion count how many of each landed in the conversation store.
_USER_ONE = "marker-user-one-before-the-flaky-post"
_ASSISTANT_ONE = "marker-assistant-one"
_USER_TWO = "marker-user-two-hit-by-the-flaky-post"
_ASSISTANT_TWO = "marker-assistant-two"
_USER_THREE = "marker-user-three-after-the-flaky-post"
_ASSISTANT_THREE = "marker-assistant-three"


def _find_free_port() -> int:
    """Grab an ephemeral port for the spawned server."""
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
    """Subprocess env with worktree imports and no proxy/credentials in the way.

    :param extra: Overrides/additions applied after the base env.
    :returns: Environment mapping for ``subprocess.Popen``.
    """
    env = {
        **os.environ,
        "PYTHONPATH": _PYTHONPATH,
        "NO_PROXY": "127.0.0.1,localhost",
        "no_proxy": "127.0.0.1,localhost",
        # Header auth + single-user keeps the spawned server out of login
        # mode; ambient auth/OIDC vars would otherwise 401 every call.
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        env.pop(name, None)
    # Strip ambient credentials/config that would alter server behaviour:
    # any Databricks or OIDC setting, any cookie/signing secret, and the
    # specific provider/tunnel vars below.
    for name in list(env):
        if (
            name.startswith(("DATABRICKS_", "OMNIGENT_OIDC_"))
            or name.endswith("_SECRET")
            or name
            in (
                "ANTHROPIC_API_KEY",
                "OMNIGENT_AUTH_ENABLED",
                "OMNIGENT_RUNNER_TUNNEL_TOKEN",
            )
        ):
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


def _create_claude_native_session(base_url: str) -> str:
    """Create a claude-native wrapper session exactly like ``omnigent claude``.

    Reuses the production spec materializer and stamps the same wrapper /
    terminal-first labels the CLI writes, so the created session is a real
    claude-native conversation -- the kind whose transcript the forwarder
    mirrors in production.

    :param base_url: Spawned server base URL.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CLAUDE_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.claude_native.main import _materialize_claude_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        yaml_text = _materialize_claude_agent_spec(Path(tmp)).read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname routes through the omnigent compat translator
        # (the wrapper spec has no ``spec_version``).
        info = tarfile.TarInfo("claude-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CLAUDE_NATIVE_WRAPPER_VALUE,
    }
    create = _http.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={
            "bundle": (
                "claude-native-ui.tar.gz",
                buf.getvalue(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _seed_conversation_transcript(bridge_dir: Path) -> Path:
    """Write a three-turn Claude JSONL transcript + a Stop hook.

    Each record uses the shape a live Claude 2.1.236 CLI writes: ``type=user``
    records carry ``message.role == "user"`` with plain-string content and a
    distinct ``uuid`` (the forwarder's idempotency key), ``isSidechain: false``,
    ``userType: "external"``; ``type=assistant`` records carry a text content
    block. The forwarder mirrors each user and assistant record as exactly one
    ``external_conversation_item``. A recorded ``Stop`` hook reports the
    transcript path so the loop resolves it on the first poll.

    :param bridge_dir: Native Claude bridge directory.
    :returns: The transcript path.
    """
    from omnigent.harnesses.claude_native.bridge import record_hook_event

    transcript_path = bridge_dir / "transcript.jsonl"

    def _user(uuid: str, text: str) -> dict[str, Any]:
        return {
            "type": "user",
            "isSidechain": False,
            "uuid": uuid,
            "message": {"role": "user", "content": text},
            "promptSource": "typed",
            "userType": "external",
        }

    def _assistant(uuid: str, text: str) -> dict[str, Any]:
        return {
            "type": "assistant",
            "isSidechain": False,
            "uuid": uuid,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": text}],
            },
        }

    lines = [
        _user("user-one-uuid", _USER_ONE),
        _assistant("assistant-one-uuid", _ASSISTANT_ONE),
        _user("user-two-uuid", _USER_TWO),
        _assistant("assistant-two-uuid", _ASSISTANT_TWO),
        _user("user-three-uuid", _USER_THREE),
        _assistant("assistant-three-uuid", _ASSISTANT_THREE),
    ]
    transcript_path.write_text(
        "\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8"
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "claude-session-flaky-post",
            "transcript_path": str(transcript_path),
        },
    )
    return transcript_path


def _count_marker(base_url: str, session_id: str, marker: str) -> int:
    """Count committed conversation items whose payload contains *marker*.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation to query.
    :param marker: Substring to match against each item's serialized data.
    :returns: Number of committed items carrying the marker.
    """
    resp = _http.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 1000, "order": "asc"},
        timeout=30.0,
    )
    resp.raise_for_status()
    return sum(1 for item in resp.json()["data"] if marker in json.dumps(item))


async def _drive_forwarder_through_a_flaky_user_post(
    base_url: str, session_id: str, bridge_dir: Path
) -> None:
    """Run the real forwarder loop through one injected flaky user-message POST.

    Injects the reported trigger -- a flaky forwarder->server POST on a remote
    deployment -- by failing the POST of the SECOND user message exactly once
    with an ambiguous transport error (``httpx.ReadTimeout`` bound to a request:
    the request was sent but no response was seen).
    ``post_may_have_been_delivered`` treats this as ambiguous: the forwarder
    cannot know whether the server committed the item, so it must hold the
    cursor and re-post it (the ``source_id`` key makes the retry idempotent).
    Every other POST goes through to the real implementation.

    :param base_url: Spawned server base URL.
    :param session_id: Conversation the forwarder mirrors into.
    :param bridge_dir: Seeded native Claude bridge directory.
    """
    import omnigent.harnesses.claude_native.forwarder as fwd

    real_post = fwd._post_external_conversation_item
    flaked_once = {"done": False}

    async def _post_with_one_flaky_user_message(
        client: Any, *, session_id: str, item: Any
    ) -> None:
        text = json.dumps(item.data)
        is_user_two = _USER_TWO in text and item.data.get("role") == "user"
        if is_user_two and not flaked_once["done"]:
            flaked_once["done"] = True
            # A request that was sent but whose response was never seen: the
            # forwarder cannot know if the server committed it, so it skips it
            # (ambiguous). Bind a request so ``post_may_have_been_delivered``
            # classifies it as ambiguous (not a safe-to-retry connect error).
            request = httpx.Request("POST", f"{base_url}/v1/sessions/{session_id}/events")
            raise httpx.ReadTimeout(
                "injected flaky forwarder->server POST (request sent, no response seen)",
                request=request,
            )
        await real_post(client, session_id=session_id, item=item)

    fwd._post_external_conversation_item = _post_with_one_flaky_user_message
    try:
        task = asyncio.create_task(
            fwd.forward_claude_transcript_to_session(
                base_url=base_url,
                headers={},
                session_id=session_id,
                bridge_dir=bridge_dir,
                agent_name="claude-native-ui",
                start_at_end=False,
                poll_interval_s=0.02,
            )
        )
        try:
            # Enough wall time for the loop to consume the whole batch: post
            # user1 + assistant1, flake once on user2, back off (~1s), re-post
            # it, then post assistant2 + user3 + assistant3. 10s is generous
            # on a loaded CI box.
            await asyncio.sleep(10.0)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
    finally:
        fwd._post_external_conversation_item = real_post


@pytest.mark.timeout(300)
def test_flaky_forwarder_post_does_not_lose_a_user_message(tmp_path: Path) -> None:
    """A single flaky forwarder POST must not silently drop a user message.

    Journey (the reporter's): on a claude-native session the user sends several
    messages; the terminal (live Claude TUI) shows them all, but the web view --
    which renders from the conversation store -- is missing some of them. Only
    the transcript forwarder writes user messages into that store, and a flaky
    forwarder->server POST makes it skip an item without retrying.

    Expected: every user message reaches the conversation store even across one
    flaky POST. Buggy behavior: the middle user message whose POST flaked once is
    skipped and never re-posted, so ``GET /v1/sessions/<id>/items`` is missing it
    while the seeded transcript (the terminal's source) still contains it -- this
    test FAILS with the missing marker.

    :param tmp_path: Per-test temp dir (server DB, artifacts, bridge dir).
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    db_path = tmp_path / "chat.db"
    database_uri = f"sqlite:///{db_path}"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    bridge_dir: Path | None = None

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

        session_id = _create_claude_native_session(base_url)
        # Root the bridge dir under the production claude-native bridge root
        # (prepare_bridge_dir is the same helper the runner uses at launch), so
        # the forwarder tails a genuinely-rooted bridge exactly as in production.
        from omnigent.harnesses.claude_native.bridge import prepare_bridge_dir

        bridge_dir = prepare_bridge_dir(session_id, workspace=workspace)
        transcript_path = _seed_conversation_transcript(bridge_dir)

        # Drive the real forwarder loop through one injected flaky user POST.
        asyncio.run(_drive_forwarder_through_a_flaky_user_post(base_url, session_id, bridge_dir))

        user_one = _count_marker(base_url, session_id, _USER_ONE)
        user_two = _count_marker(base_url, session_id, _USER_TWO)
        user_three = _count_marker(base_url, session_id, _USER_THREE)
        assistant_one = _count_marker(base_url, session_id, _ASSISTANT_ONE)
        assistant_three = _count_marker(base_url, session_id, _ASSISTANT_THREE)

        server_tail = (tmp_path / "server.log").read_text()[-2000:]

        # The terminal side is intact: the live transcript still contains the
        # message the user sent -- the loss is only in the web conversation store.
        assert _USER_TWO in transcript_path.read_text(encoding="utf-8"), (
            "seed invariant: the flaky-POST user message must remain in the "
            "transcript (the terminal's source)"
        )

        # Sanity: the forwarder delivered the rest of the conversation, so the
        # loss assertion below is not vacuous (the loop actually ran and posted).
        assert user_one >= 1 and user_three >= 1, (
            "forwarder never delivered the surrounding user messages; "
            f"user_one={user_one} user_three={user_three} "
            f"user_two={user_two} -- server log tail:\n{server_tail}"
        )
        assert assistant_one >= 1 and assistant_three >= 1, (
            "forwarder never delivered the assistant messages; "
            f"assistant_one={assistant_one} assistant_three={assistant_three} "
            f"-- server log tail:\n{server_tail}"
        )

        # The bug: the middle user message's POST flaked once, so the forwarder
        # skipped it (ambiguous failure) and advanced its cursor without
        # retrying. It is present in the terminal transcript but absent from the
        # web conversation store -- exactly the reported "some user messages lost
        # on web / terminal fine" desync.
        assert user_two >= 1, (
            "A single flaky forwarder->server POST silently dropped a user "
            f"message from the conversation store: '{_USER_TWO}' is in the "
            "transcript (terminal view) but committed "
            f"{user_two} times to /items (web view) -- expected at least 1. "
            "The forwarder treats an ambiguous POST failure as 'may already be "
            "committed' and skips the item without retrying; when the server "
            "had not committed it, the user message is lost from the web "
            f"conversation store. user_one={user_one} user_three={user_three} "
            f"assistant_one={assistant_one} assistant_three={assistant_three}. "
            f"server log tail:\n{server_tail}"
        )
    finally:
        _terminate(server_proc)
        server_log.close()
        if bridge_dir is not None:
            shutil.rmtree(bridge_dir, ignore_errors=True)
