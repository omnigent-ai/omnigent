"""UI journey: a Cursor TUI ``/clear`` must not strand the web session.

``cursor-agent``'s TUI has ``/clear`` (aliases ``/new``, ``/new-chat``,
``/newchat``): it starts a brand-new chat, creating a fresh
``~/.cursor/chats/<md5(cwd)>/<chat-id>/store.db`` while the old store stays on
disk. Rotation-capable native harnesses (claude-native, codex-native,
antigravity-native) move Omnigent ownership onto a fresh conversation when the
vendor TUI starts a new session; cursor-native must not leave the web session
pinned to the cleared-away chat. When it does, three user-visible failures
follow:

1. The mirror stays on the pre-``/clear`` chat, so nothing from the new chat
   ever reaches the web transcript.
2. Web composer messages are injected into the TUI's *new* chat, whose replies
   are never mirrored — from the browser the session looks dead.
3. ``external_session_id`` (the cold-resume ``--resume <chatId>`` target) is
   patched exactly once, so a later resume reattaches the chat the user
   cleared away from.

The journey this file drives, exactly as a user would:

1. Start a cursor-native session and exchange one composer turn (the chat
   store exists and its reply is mirrored into the web transcript).
2. In the Terminal view, run ``/clear`` in the cursor-agent pane.
3. Send another message from the web composer.
4. The TUI accepts and answers it (its own chat store carries the reply), and
   that reply must reach the transcript the browser is showing — either the
   same session (re-discovered store) or a rotated session the SPA redirects
   to (the claude-native behavior). The session the user ends up on must have
   ``external_session_id`` pointing at the *new* chat so a cold resume lands
   on it.

CI has no Cursor account, so the vendor binary is a scripted fake: a tiny
line-oriented "TUI" that renders the idle markers the injection path settles
on, answers every prompt by writing cursor-shaped user/assistant blobs into
the same ``~/.cursor/chats`` store layout the real CLI uses, and starts a new
chat on ``/clear``. Everything else — server, runner, tmux pane, executor
injection, store discovery, forwarder mirror, SPA — is the real stack: the
runner is respawned with an isolated ``$HOME`` and the fake on ``PATH``
(``OMNIGENT_CURSOR_PATH``), mirroring how the cursor-native list-models e2e
stubs the binary.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _bind_session_runner,
    _ensure_runner_online,
    _REPO_ROOT,
    _server_state,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_cursor_render_parity import (
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="cursor-native drives a runner-owned tmux pane; needs `tmux` on PATH",
)

# The first mirrored reply proves the whole cold path (runner respawn + TUI
# auto-launch + injection + store discovery + mirror), so it gets the most
# headroom. The post-/clear reply rides an already-warm mirror loop (~0.7s
# poll cadence), so 90s is far beyond any healthy latency — when this expires
# the reply is not merely slow, it is never coming.
_FIRST_MIRROR_TIMEOUT_MS = 180_000
_POST_CLEAR_MIRROR_TIMEOUT_MS = 90_000
# Filesystem-side waits (the fake's state file / chat store writes).
_FS_TIMEOUT_S = 60.0
_RUNNER_SWAP_TIMEOUT_S = 120.0
_POLL_S = 0.5

# A scripted stand-in for the ``cursor-agent`` TUI (CI provisions no Cursor
# account). Line-oriented: the pty's canonical mode echoes input (so tmux
# paste needles render) and delivers one line per submit. It prints the idle
# markers the injection path settles on ("Plan, search, build" /
# "Add a follow-up"), persists every turn into the same
# ``~/.cursor/chats/<md5(cwd)>/<chat-id>/store.db`` blobs layout the real CLI
# writes (user turns wrapped in ``<user_query>``), and on ``/clear`` (and its
# aliases) starts a new chat exactly like the vendor TUI: new chat dir + new
# store while the old store stays on disk. A ``fake-cursor-state.json`` under
# ``$HOME/.cursor`` exposes the current chat id so the test can synchronize.
_FAKE_CURSOR_AGENT = r'''#!/usr/bin/env python3
"""Scripted cursor-agent TUI stand-in for e2e tests (no Cursor account)."""
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from pathlib import Path

IDLE_BANNER = "Plan, search, build"
IDLE_FOLLOW = "Add a follow-up"
CLEAR_COMMANDS = ("/clear", "/new", "/new-chat", "/newchat")
# Strip terminal key/paste escape sequences (bracketed paste markers, End key,
# cursor keys) that canonical-mode line reads deliver as raw bytes.
ESCAPES = re.compile("\x1b\\[[0-9;?]*[A-Za-z~]|\x1bO.|\x1b.")


def main() -> None:
    argv = sys.argv[1:]
    resume_id = None
    for i, arg in enumerate(argv):
        if arg == "--resume" and i + 1 < len(argv):
            resume_id = argv[i + 1]
        elif arg.startswith("--resume="):
            resume_id = arg.split("=", 1)[1]

    workspace = os.path.realpath(os.getcwd())
    chats_root = (
        Path.home() / ".cursor" / "chats" / hashlib.md5(workspace.encode()).hexdigest()
    )
    state_path = Path.home() / ".cursor" / "fake-cursor-state.json"

    chat_id = resume_id  # None until the first message (cursor creates lazily)
    turn = 0

    def write_state() -> None:
        # Atomic write (temp + rename) so a concurrent reader never sees a
        # truncated/empty file mid-write.
        state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = state_path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"chat_id": chat_id, "turns": turn}))
        os.replace(tmp, state_path)

    def new_chat() -> None:
        nonlocal chat_id
        chat_id = str(uuid.uuid4())
        chat_dir = chats_root / chat_id
        chat_dir.mkdir(parents=True, exist_ok=True)
        (chat_dir / "meta.json").write_text(
            json.dumps({"createdAtMs": int(time.time() * 1000)})
        )
        con = sqlite3.connect(chat_dir / "store.db")
        con.execute("CREATE TABLE IF NOT EXISTS blobs(id TEXT PRIMARY KEY, data BLOB)")
        con.commit()
        con.close()
        write_state()

    def append_blob(role: str, text: str) -> None:
        con = sqlite3.connect(chats_root / chat_id / "store.db")
        con.execute("CREATE TABLE IF NOT EXISTS blobs(id TEXT PRIMARY KEY, data BLOB)")
        payload = {"role": role, "content": [{"type": "text", "text": text}]}
        con.execute(
            "INSERT INTO blobs(id, data) VALUES(?, ?)",
            (hashlib.sha256(uuid.uuid4().bytes).hexdigest(), json.dumps(payload).encode()),
        )
        con.commit()
        con.close()

    if resume_id:
        print(f"Resumed chat {resume_id}", flush=True)
    write_state()
    print(IDLE_BANNER, flush=True)

    for raw in sys.stdin:
        text = ESCAPES.sub("", raw)
        text = "".join(ch for ch in text if ch >= " " or ch in "\n\t").strip()
        if not text:
            continue
        if text in CLEAR_COMMANDS:
            new_chat()
            print("Started a new chat", flush=True)
            print(IDLE_BANNER, flush=True)
            continue
        if chat_id is None:
            new_chat()
        turn += 1
        append_blob("user", f"<user_query>\n{text}\n</user_query>")
        reply = f"fake-cursor reply {turn}: {text}"
        append_blob("assistant", reply)
        write_state()
        print(reply, flush=True)
        print(IDLE_FOLLOW, flush=True)


main()
'''


def _wait_until(
    predicate: Callable[[], bool], *, timeout_s: float, message: str
) -> None:
    """Poll *predicate* until true or raise ``AssertionError`` with *message*."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(_POLL_S)
    raise AssertionError(message)


def _terminate_shared_runner(base_url: str, runner_id: str) -> None:
    """SIGTERM every ``omnigent.runner._entry`` process and wait until offline.

    The suite's shared runner is a sibling subprocess of the spawned server;
    the replacement runner must reuse its token-bound id, so the old process
    has to be fully gone (tunnel dropped, status offline) before the swap.
    """
    result = subprocess.run(
        ["pgrep", "-f", "omnigent.runner._entry"], capture_output=True, text=True
    )
    pids = (
        [int(line) for line in result.stdout.split()] if result.returncode == 0 else []
    )
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    def _offline() -> bool:
        try:
            resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
        except httpx.HTTPError:
            return False
        return not (resp.status_code == 200 and resp.json().get("online") is True)

    _wait_until(
        _offline,
        timeout_s=_RUNNER_SWAP_TIMEOUT_S,
        message="shared runner never dropped offline; cannot swap in the fake-cursor runner",
    )


def _spawn_fake_cursor_runner(
    base_url: str, home: Path, fake_bin_dir: Path, log_path: Path
) -> subprocess.Popen[bytes]:
    """Respawn the suite runner with the fake ``cursor-agent`` and a private HOME.

    Reuses the shared runner's token-bound id/binding token (the server only
    accepts that tunnel). ``HOME`` isolation matters twice over: the runner-side
    forwarder resolves ``~/.cursor/chats`` from its own environment, and the
    tmux pane it spawns inherits it — so the fake TUI and the forwarder agree
    on the same private chats root the test can inspect.
    """
    runner_id = str(_server_state["runner_id"])
    binding_token = str(_server_state["binding_token"])
    mock_url = str(_server_state.get("mock_llm_url", ""))
    fake_binary = fake_bin_dir / "cursor-agent"
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
        "HOME": str(home),
        "OMNIGENT_CURSOR_PATH": str(fake_binary),
        "PATH": f"{fake_bin_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        **(
            {"OPENAI_BASE_URL": f"{mock_url}/v1", "OPENAI_API_KEY": "mock-key"}
            if mock_url
            else {}
        ),
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — child holds its own dup
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()

    def _online() -> bool:
        if proc.poll() is not None:
            raise AssertionError(
                f"fake-cursor runner exited early (code {proc.returncode}); "
                f"log:\n{log_path.read_text()[-3000:]}"
            )
        try:
            resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
        except httpx.HTTPError:
            return False
        return resp.status_code == 200 and resp.json().get("online") is True

    try:
        _wait_until(
            _online,
            timeout_s=_RUNNER_SWAP_TIMEOUT_S,
            message="fake-cursor runner never registered with the server",
        )
    except AssertionError:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()
                proc.wait(timeout=5)
        raise
    return proc


def _create_cursor_native_session(
    base_url: str, runner_id: str, workspace: Path
) -> str:
    """Register the ``cursor-native`` wrapper agent and bind its session.

    Mirrors the suite's ``_create_native_cursor_session`` fixture helper —
    reuses the exact terminal-first spec ``omnigent cursor`` ships and stamps
    the same wrapper / terminal-first labels — but pins the launch cwd to a
    per-test workspace so the ``md5(cwd)`` chat-store key is private to this
    session.
    """
    import io
    import tarfile
    import tempfile

    from omnigent._wrapper_labels import (
        CURSOR_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.harnesses.cursor_native.main import _materialize_cursor_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_cursor_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → the omnigent compat translator (the spec
        # carries no spec_version), matching the suite's cursor fixture.
        info = tarfile.TarInfo("cursor-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: CURSOR_NATIVE_WRAPPER_VALUE,
        },
        "workspace": str(workspace),
        # ``-f`` trusts the dir + auto-approves tools so the unattended pane
        # never blocks (the fake never prompts, but the flag matches the
        # production launch the ``omnigent cursor`` CLI performs).
        "terminal_launch_args": ["-f"],
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("cursor-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def fake_cursor_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path, Path]]:
    """A cursor-native session whose ``cursor-agent`` is the scripted fake.

    Swaps the suite's shared runner for one spawned with a private ``HOME``
    and the fake TUI on ``PATH``, then creates and binds a cursor-native
    session against it. Teardown deletes the session and the swapped runner;
    a later test that needs the shared runner respawns it on demand via
    ``_ensure_runner_online`` (the established post-runner-kill pattern).

    :returns: ``(base_url, session_id, home, workspace)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    stack_tmp = tmp_path_factory.mktemp("fake_cursor_stack")
    home = stack_tmp / "home"
    home.mkdir()
    fake_bin_dir = stack_tmp / "bin"
    fake_bin_dir.mkdir()
    fake_binary = fake_bin_dir / "cursor-agent"
    fake_binary.write_text(_FAKE_CURSOR_AGENT, encoding="utf-8")
    fake_binary.chmod(0o755)
    workspace = Path(os.path.realpath(stack_tmp / "workspace"))
    workspace.mkdir()

    _terminate_shared_runner(live_server, runner_id)
    if respawned is not None:
        respawned.wait(timeout=15)
    runner_proc = _spawn_fake_cursor_runner(
        live_server, home, fake_bin_dir, stack_tmp / "runner.log"
    )
    session_id = _create_cursor_native_session(live_server, runner_id, workspace)
    try:
        yield (live_server, session_id, home, workspace)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        runner_proc.terminate()
        try:
            runner_proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            runner_proc.kill()
            runner_proc.wait(timeout=5)


def _chats_root(home: Path, workspace: Path) -> Path:
    """Return the cursor chats dir for *workspace* under the pane's HOME.

    cursor keys each workspace's chat dir on ``md5(realpath(cwd))``; the fake
    TUI and the runner-side forwarder both resolve it from the same private
    HOME the fixture installs, so the test inspects the exact tree they use.
    """
    return home / ".cursor" / "chats" / hashlib.md5(str(workspace).encode()).hexdigest()


def _list_chat_ids(home: Path, workspace: Path) -> set[str]:
    """Return every chat id (dir name) that has a ``store.db`` on disk."""
    root = _chats_root(home, workspace)
    if not root.is_dir():
        return set()
    return {d.name for d in root.iterdir() if (d / "store.db").is_file()}


def _store_has_assistant_reply(
    home: Path, workspace: Path, chat_id: str, token: str
) -> bool:
    """True when *chat_id*'s store carries an assistant blob containing *token*."""
    store = _chats_root(home, workspace) / chat_id / "store.db"
    if not store.is_file():
        return False
    try:
        con = sqlite3.connect(f"file:{store}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        return False
    try:
        rows = con.execute("SELECT data FROM blobs").fetchall()
    except sqlite3.Error:
        return False
    finally:
        con.close()
    for (data,) in rows:
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        if '"assistant"' in text and token in text:
            return True
    return False


def _external_session_id(base_url: str, session_id: str) -> str | None:
    """Return the session's persisted cold-resume chat id, if any."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    if resp.status_code != 200:
        return None
    value = resp.json().get("external_session_id")
    return value if isinstance(value, str) and value else None


def _poll_external_session_id(base_url: str, session_id: str, *, timeout_s: float) -> str:
    """Poll until the forwarder patches ``external_session_id``; return it."""
    result: dict[str, str] = {}

    def _got() -> bool:
        value = _external_session_id(base_url, session_id)
        if value:
            result["id"] = value
            return True
        return False

    _wait_until(
        _got,
        timeout_s=timeout_s,
        message="the forwarder never patched external_session_id after the first turn",
    )
    return result["id"]


def _current_session_id(page: Page) -> str:
    """Extract the session id from the SPA's ``/c/<id>`` URL."""
    match = re.search(r"/c/([^/?#]+)", page.url)
    assert match, f"not on a session page: {page.url}"
    return match.group(1)


@pytest.mark.timeout(900)
def test_tui_clear_does_not_strand_web_session(
    page: Page,
    fake_cursor_session: tuple[str, str, Path, Path],
) -> None:
    """After a TUI ``/clear``, composer replies must still reach the browser.

    Fails on unfixed code at the post-``/clear`` reply wait: the TUI accepts
    and answers the composer message in its new chat (asserted against its own
    chat store first), but the mirror stays pinned to the cleared-away store,
    so no reply ever reaches the transcript the browser shows and the session
    looks dead. Passes once cursor-native handles the TUI's new-chat rotation
    the way the other rotation-capable native harnesses do — whether by
    rotating the Omnigent session (and redirecting the SPA, claude-native
    style) or by re-binding the mirror and resume target to the new chat.
    """
    base_url, session_id, home, workspace = fake_cursor_session
    page.goto(f"{base_url}/c/{session_id}")

    # The Terminal view proves the (fake) cursor-agent booted in the session
    # terminal — the runner's cursor-native auto-launch worked — before we
    # send anything.
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    nonce = uuid.uuid4().hex[:8]
    token_before = f"tok-before-{nonce}"
    token_after = f"tok-after-{nonce}"

    # --- Journey step 1: one composer turn lands and is mirrored back. ---
    _ensure_chat_view(page)
    _send(page, f"first turn, please echo {token_before}")
    expect(page.locator(_ASSISTANT, has_text=token_before).first).to_be_visible(
        timeout=_FIRST_MIRROR_TIMEOUT_MS
    )
    # The forwarder discovered the store and patched the cold-resume target;
    # that patched id IS the first chat id (HOME-independent, the production
    # signal). The fake also creates the store dir on disk under the pane HOME.
    chat_before = _poll_external_session_id(base_url, session_id, timeout_s=_FS_TIMEOUT_S)
    assert _list_chat_ids(home, workspace) == {chat_before}, (
        f"expected exactly the first chat on disk; "
        f"found {_list_chat_ids(home, workspace)}, patched id {chat_before!r}"
    )

    # --- Journey step 2: the user starts a new chat from the TUI pane. ---
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _type_into_tui(page, "/clear")
    # cursor creates a NEW chat dir/store while the old one stays on disk.
    _wait_until(
        lambda: len(_list_chat_ids(home, workspace)) >= 2,
        timeout_s=_FS_TIMEOUT_S,
        message="the TUI never started a new chat store after /clear was typed into the pane",
    )
    new_ids = _list_chat_ids(home, workspace) - {chat_before}
    assert len(new_ids) == 1, f"expected exactly one new chat after /clear, got {new_ids}"
    chat_after = next(iter(new_ids))

    # --- Journey step 3: another composer message. ---
    _ensure_chat_view(page)
    _send(page, f"after clear, please echo {token_after}")
    # The TUI accepted and answered it in its NEW chat: the reply exists in
    # cursor's own store. Whatever fails from here on is the Omnigent mirror,
    # not the vendor side.
    _wait_until(
        lambda: _store_has_assistant_reply(home, workspace, chat_after, token_after),
        timeout_s=_FS_TIMEOUT_S,
        message="the injected composer message never reached the TUI's new chat store",
    )

    # --- The bug: that reply must reach the transcript the user is watching,
    # on this session or on a rotated session the SPA redirects to. ---
    expect(page.locator(_ASSISTANT, has_text=token_after).first).to_be_visible(
        timeout=_POST_CLEAR_MIRROR_TIMEOUT_MS
    )

    # --- And a cold resume must target the new chat, not the cleared one. ---
    final_session = _current_session_id(page)
    assert _external_session_id(base_url, final_session) == chat_after, (
        "the session the user ended on still resumes the cleared-away chat "
        f"(external_session_id={_external_session_id(base_url, final_session)!r}, "
        f"new chat={chat_after!r})"
    )
