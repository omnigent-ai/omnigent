"""Real Claude Code keeps a person's unsent input-box draft while Omnigent delivers input.

The server, a runner subprocess, Claude Code in tmux with its hooks, and the
person's own terminal all run for real. That terminal is a tmux client attached
to the Claude pane through a pseudo-terminal, the way a same-machine
``omnigent claude`` attaches; one journey types through the web terminal's
attach WebSocket instead. In each journey the person leaves unsent text in
Claude's input box while Omnigent delivers input into the same pane: a web chat
message, another session's ``sys_session_message``, a message during a running
turn, or a web settings change. The text must still be in the box afterwards,
exactly as left, and the delivered input must reach Claude without it.

Claude's replies do not matter here. When the machine's Claude Code managed
settings pin a gateway credential, that gateway answers; otherwise a scripted
local gateway does. The peer session's model is always scripted. Assertions read
the input box from the pane and the prompts from Claude's own transcript, never
reply text.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import pty
import secrets
import shutil
import struct
import subprocess
import sys
import termios
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from fcntl import ioctl
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest
import websockets
import yaml

from omnigent.harnesses.claude_native.bridge import (
    BRIDGE_ID_LABEL_KEY,
    bridge_dir_for_bridge_id,
    read_transcript_path,
)
from omnigent.onboarding.ambient import claude_managed_gateway
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN, token_bound_runner_id
from tests._helpers.session import bundle_files, post_session_bundle, send_user_text
from tests.e2e.conftest import configure_mock_llm, set_fallback_mock_llm
from tests.server.integration.mock_llm_server import (
    anthropic_sse_text_response,
    anthropic_sse_tool_call_response,
)

pytestmark = [
    pytest.mark.skipif(
        sys.platform != "linux" or not shutil.which("claude") or not shutil.which("tmux"),
        reason="requires Linux, the real Claude CLI, and tmux",
    ),
    pytest.mark.timeout(300),
]

_REPO = Path(__file__).resolve().parents[2]
# Set to a directory to keep each person's terminal output as an asciicast.
_RECORD_DIR_ENV = "OMNIGENT_E2E_RECORD_DIR"
_PERSON_COLUMNS = 120
_PERSON_ROWS = 36
_BUSY_CALL_ID = "toolu_draft_busy"
# Claude Code's default chat:stash binding and its persistent footer marker.
_CTRL_S = b"\x13"
_STASH_MARKER = "› stashed"
_HELD_NOTICE = "unsent draft"


class _ScriptedGateway:
    """Answer Claude's turns when no managed gateway does; one prompt may run a command."""

    def __init__(self) -> None:
        self.busy_marker: str | None = None
        self.busy_command: str | None = None

    def response(self, body: dict[str, Any]) -> str:
        model = body.get("model", "claude-sonnet-4-6")
        if not body.get("tools"):
            return anthropic_sse_text_response("Local test", model=model)
        messages = body.get("messages", [])
        answered = any(
            block.get("type") == "tool_result" and block.get("tool_use_id") == _BUSY_CALL_ID
            for message in messages
            if isinstance(message.get("content"), list)
            for block in message["content"]
        )
        if (
            self.busy_marker
            and self.busy_command
            and not answered
            and self.busy_marker in json.dumps(messages)
        ):
            return anthropic_sse_tool_call_response(
                [
                    {
                        "call_id": _BUSY_CALL_ID,
                        "name": "Bash",
                        "arguments": json.dumps(
                            {"command": self.busy_command, "description": "Wait for the test"}
                        ),
                    }
                ],
                model=model,
            )
        return anthropic_sse_text_response("ACK", model=model)


@pytest.fixture
def gateway() -> Iterator[tuple[_ScriptedGateway, str]]:
    scripted = _ScriptedGateway()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            if self.path.split("?", 1)[0] == "/v1/messages":
                if body.get("stream"):
                    payload = scripted.response(body).encode()
                    content_type = "text/event-stream"
                else:
                    payload = json.dumps(
                        {
                            "id": "msg_auxiliary",
                            "type": "message",
                            "role": "assistant",
                            "model": body.get("model"),
                            "content": [{"type": "text", "text": "Local test"}],
                            "stop_reason": "end_turn",
                            "stop_sequence": None,
                            "usage": {"input_tokens": 10, "output_tokens": 2},
                        }
                    ).encode()
                    content_type = "application/json"
            else:
                payload, content_type = b'{"input_tokens":10}', "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield scripted, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _wait_for(check: Callable[[], Any], description: str, timeout: float = 60) -> Any:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        time.sleep(0.25)
    raise AssertionError(f"Timed out after {timeout:.0f}s waiting for {description}")


def _is_rule(line: str) -> bool:
    text = line.strip()
    return len(text) >= 20 and set(text) == {"─"}


def _composer_text(pane: str) -> str | None:
    """Return the text in Claude's framed input box, or ``None`` when no box is on screen.

    A shell-mode box keeps its ``!`` glyph; a chat box drops its ``❯`` glyph.
    """
    lines = pane.replace("\xa0", " ").splitlines()
    rules = [index for index, line in enumerate(lines) if _is_rule(line)]
    if len(rules) < 2:
        return None
    rows = lines[rules[-2] + 1 : rules[-1]]
    if not rows or rows[0].strip()[:1] not in ("❯", "!"):
        return None
    first = rows[0].strip()
    text = [first[1:].strip() if first.startswith("❯") else first]
    text.extend(row[2:].rstrip() if row.startswith("  ") else row.rstrip() for row in rows[1:])
    return "\n".join(text).strip()


def _transcript_prompts(path: Path | None) -> list[str]:
    """Prompts Claude recorded, oldest first, including those queued during a turn."""
    if path is None or not path.exists():
        return []
    prompts: list[str] = []
    for line in path.read_text(errors="replace").splitlines():
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(entry, dict):
            continue
        attachment = entry.get("attachment")
        if isinstance(attachment, dict) and attachment.get("type") == "queued_command":
            if isinstance(attachment.get("prompt"), str):
                prompts.append(attachment["prompt"])
            continue
        message = entry.get("message")
        if entry.get("type") != "user" or not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            prompts.append(content)
        elif isinstance(content, list):
            text = "".join(
                block.get("text", "")
                for block in content
                if isinstance(block, dict) and block.get("type") == "text"
            )
            if text:
                prompts.append(text)
    return prompts


@dataclass
class _ClaudeSession:
    client: httpx.Client
    base_url: str
    session_id: str
    runner_id: str
    bridge_dir: Path
    socket_path: str
    target: str
    workspace: Path
    timeline: list[tuple[float, str]] = field(default_factory=list)

    def tmux(self, *args: str) -> str:
        return subprocess.run(
            ["tmux", "-S", self.socket_path, *args],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout

    def pane(self) -> str:
        return self.tmux("capture-pane", "-p", "-t", self.target)

    def composer(self) -> str | None:
        return _composer_text(self.pane())

    def prompts(self) -> list[str]:
        return _transcript_prompts(read_transcript_path(self.bridge_dir))

    def mark(self, event: str) -> None:
        self.timeline.append((time.time(), event))

    def wait_for_prompt(self, marker: str, timeout: float = 90) -> str:
        try:
            return _wait_for(
                lambda: next((prompt for prompt in self.prompts() if marker in prompt), None),
                f"Claude's transcript to record a prompt containing {marker!r}",
                timeout,
            )
        except AssertionError as exc:
            raise AssertionError(f"{exc}\npane:\n{self.pane()}") from None

    def wait_for_composer(self, expected: str, timeout: float = 30) -> None:
        try:
            _wait_for(lambda: self.composer() == expected, "the input box text", timeout)
        except AssertionError:
            raise AssertionError(
                "Claude's input box no longer holds the person's draft.\n"
                f"expected: {expected!r}\nshown:    {self.composer()!r}\n"
                f"pane:\n{self.pane()}"
            ) from None


class _PersonTerminal:
    """The person's own terminal: a tmux client attached to Claude's pane through a pty."""

    def __init__(self, claude: _ClaudeSession, recording: Path | None) -> None:
        self._claude = claude
        self._recording = recording
        self._started = time.time()
        self._events: list[tuple[float, str]] = []
        self.fd, follower = pty.openpty()
        ioctl(
            follower, termios.TIOCSWINSZ, struct.pack("HHHH", _PERSON_ROWS, _PERSON_COLUMNS, 0, 0)
        )
        env = {key: value for key, value in os.environ.items() if not key.startswith("TMUX")}
        env["TERM"] = "xterm-256color"
        self._process = subprocess.Popen(
            ["tmux", "-S", claude.socket_path, "attach-session", "-t", claude.target],
            stdin=follower,
            stdout=follower,
            stderr=follower,
            env=env,
            start_new_session=True,
        )
        os.close(follower)
        self._reader = threading.Thread(target=self._drain, daemon=True)
        self._reader.start()
        _wait_for(
            lambda: claude.tmux("list-clients", "-F", "#{client_control_mode}").split().count("0"),
            "the person's terminal to attach",
            20,
        )

    def _drain(self) -> None:
        while True:
            try:
                data = os.read(self.fd, 65536)
            except OSError:
                return
            if not data:
                return
            self._events.append((time.time() - self._started, data.decode(errors="replace")))

    def type(self, text: str, *, delay: float = 0.04) -> None:
        for character in text:
            os.write(self.fd, character.encode())
            time.sleep(delay)

    def press(self, keys: bytes) -> None:
        os.write(self.fd, keys)

    def paste(self, text: str) -> None:
        """Paste the way a terminal does: one bracketed burst."""
        os.write(self.fd, b"\x1b[200~" + text.encode() + b"\x1b[201~")

    def close(self) -> None:
        self._process.terminate()
        try:
            self._process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            self._process.kill()
            self._process.wait(timeout=5)
        with contextlib.suppress(OSError):
            os.close(self.fd)
        self._reader.join(timeout=5)
        _write_recording(self._recording, self._started, self._events, self._claude.timeline)


class _WebTerminal:
    """The web terminal: the attach WebSocket the browser's terminal view uses."""

    def __init__(self, claude: _ClaudeSession, recording: Path | None) -> None:
        self._claude = claude
        self._recording = recording
        self._started = time.time()
        self._events: list[tuple[float, str]] = []
        terminals = claude.client.get(f"/v1/sessions/{claude.session_id}/resources").json()
        terminal_id = next(
            str(resource["id"])
            for resource in terminals.get("data", [])
            if resource.get("type") == "terminal"
        )
        self._url = (
            claude.base_url.replace("http://", "ws://", 1)
            + f"/v1/sessions/{claude.session_id}/resources/terminals/{terminal_id}/attach"
        )
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._loop.run_forever, daemon=True)
        self._thread.start()
        self._socket = self._run(self._connect())

    def _run(self, coroutine: Any) -> Any:
        return asyncio.run_coroutine_threadsafe(coroutine, self._loop).result(timeout=60)

    async def _connect(self) -> Any:
        socket = await websockets.connect(
            self._url,
            max_size=None,
            open_timeout=20,
            additional_headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        )
        await socket.send(
            json.dumps({"type": "resize", "cols": _PERSON_COLUMNS, "rows": _PERSON_ROWS})
        )
        self._receiver = asyncio.ensure_future(self._receive(socket))
        return socket

    async def _receive(self, socket: Any) -> None:
        try:
            async for frame in socket:
                if isinstance(frame, bytes):
                    self._events.append(
                        (time.time() - self._started, frame.decode(errors="replace"))
                    )
        except websockets.WebSocketException:
            return

    def type(self, text: str, *, delay: float = 0.04) -> None:
        for character in text:
            self._run(self._socket.send(character.encode()))
            time.sleep(delay)

    def close(self) -> None:
        try:
            self._run(self._socket.close())
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
        _write_recording(self._recording, self._started, self._events, self._claude.timeline)


class _SessionStream:
    """The session's live event stream, as the web UI reads it."""

    def __init__(self, claude: _ClaudeSession) -> None:
        self._claude = claude
        self._events: list[dict[str, Any]] = []
        self._connected = threading.Event()
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._read, daemon=True)

    def __enter__(self) -> _SessionStream:
        self._thread.start()
        assert self._connected.wait(timeout=30), "the session stream never connected"
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        self._thread.join(timeout=5)

    def _read(self) -> None:
        with httpx.Client(
            base_url=self._claude.base_url,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=httpx.Timeout(10.0, read=2.0),
            trust_env=False,
        ) as client:
            while not self._stop.is_set():
                try:
                    with client.stream(
                        "GET", f"/v1/sessions/{self._claude.session_id}/stream"
                    ) as response:
                        if response.status_code == 200:
                            self._connected.set()
                        for line in response.iter_lines():
                            if self._stop.is_set():
                                return
                            if line.startswith("data:"):
                                with contextlib.suppress(ValueError):
                                    self._events.append(json.loads(line[len("data:") :]))
                except httpx.HTTPError:
                    continue

    def text(self) -> str:
        """Text the session streamed to the web UI so far."""
        return "".join(
            str(event.get("delta", ""))
            for event in list(self._events)
            if isinstance(event, dict) and event.get("type") == "response.output_text.delta"
        )


def _write_recording(
    recording: Path | None,
    started: float,
    events: list[tuple[float, str]],
    timeline: list[tuple[float, str]],
) -> None:
    if recording is None:
        return
    recording.parent.mkdir(parents=True, exist_ok=True)
    header = {
        "version": 2,
        "width": _PERSON_COLUMNS,
        "height": _PERSON_ROWS,
        "timestamp": int(started),
    }
    with recording.open("w") as cast:
        cast.write(json.dumps(header) + "\n")
        for offset, data in events:
            cast.write(json.dumps([round(offset, 4), "o", data]) + "\n")
    steps = [{"at": round(at - started, 3), "event": event} for at, event in timeline]
    recording.with_suffix(".timeline.json").write_text(json.dumps(steps, indent=2))


@dataclass
class _PeerSession:
    """Another session of the same person whose scripted model sends ``sys_session_message``."""

    client: httpx.Client
    session_id: str
    mock_llm_url: str

    def send_to(self, target_session_id: str, message: str) -> None:
        trigger = f"PEER_TRIGGER_{secrets.token_hex(4)}"
        configure_mock_llm(
            self.mock_llm_url,
            [
                {
                    "tool_calls": [
                        {
                            "name": "sys_session_message",
                            "arguments": json.dumps(
                                {"session_id": target_session_id, "message": message}
                            ),
                            "call_id": f"call_{trigger.lower()}",
                        }
                    ]
                },
                {"text": "PEER_SENT"},
            ],
            match=trigger,
            required_tools=["sys_session_message"],
        )
        send_user_text(self.client, session_id=self.session_id, text=trigger)


def _claude_env(
    tmp_path: Path, server_url: str, gateway_url: str, workspace: Path, token: str
) -> dict[str, str]:
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    (config_dir / "settings.json").write_text(json.dumps({"permissions": {"allow": ["Bash"]}}))
    env = {
        key: os.environ[key]
        for key in ("PATH", "HOME", "USER", "LANG", "TMPDIR")
        if key in os.environ
    }
    env.update(
        PYTHONPATH=os.pathsep.join(
            str(path) for path in (_REPO, _REPO / "sdks/python-client", _REPO / "sdks/ui")
        ),
        OMNIGENT_CONFIG_HOME=str(tmp_path / "omnigent-config"),
        OMNIGENT_DATA_DIR=str(tmp_path / "omnigent-data"),
        OMNIGENT_PROCESS_LOG_FILE=str(tmp_path / "runner.log"),
        OMNIGENT_AUTH_PROVIDER="header",
        OMNIGENT_LOCAL_SINGLE_USER="1",
        OMNIGENT_DISABLE_CATALOG_LOOKUP="1",
        OMNIGENT_RUNNER_ID=token_bound_runner_id(token),
        OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN=token,
        OMNIGENT_RUNNER_PARENT_PID=str(os.getpid()),
        OMNIGENT_RUNNER_WORKSPACE=str(workspace),
        RUNNER_SERVER_URL=server_url,
        CLAUDE_CONFIG_DIR=str(config_dir),
        CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="1",
        CLAUDE_CODE_DISABLE_CLAUDE_MDS="1",
        DISABLE_AUTOUPDATER="1",
        DISABLE_TELEMETRY="1",
        DISABLE_ERROR_REPORTING="1",
        NO_PROXY="127.0.0.1,localhost",
        TERM="xterm-256color",
    )
    _, managed_credential = claude_managed_gateway()
    if not managed_credential:
        env.update(
            ANTHROPIC_AUTH_TOKEN="local-draft-test",
            ANTHROPIC_BASE_URL=gateway_url,
            CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST="1",
        )
    return env


@pytest.fixture
def claude(
    tmp_path: Path,
    resume_test_server: str,
    gateway: tuple[_ScriptedGateway, str],
) -> Iterator[_ClaudeSession]:
    """A runner-bound claude-native session whose Claude Code input box is ready."""
    _, gateway_url = gateway
    workspace = tmp_path / "workspace"
    runner_cwd = tmp_path / "runner-cwd"
    workspace.mkdir()
    runner_cwd.mkdir()
    token = secrets.token_urlsafe(32)
    env = _claude_env(tmp_path, resume_test_server, gateway_url, workspace, token)
    runner_id = env["OMNIGENT_RUNNER_ID"]
    print(f"Draft preservation logs: {tmp_path}", flush=True)
    session: _ClaudeSession | None = None
    with (
        (tmp_path / "runner.out").open("w") as log,
        httpx.Client(
            base_url=resume_test_server,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
            timeout=90,
            trust_env=False,
        ) as client,
    ):
        runner = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            cwd=runner_cwd,
            env=env,
            stdout=log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_for(
                lambda: client.get(f"/v1/runners/{runner_id}/status").json().get("online"),
                "the runner tunnel",
            )
            spec = b"""name: draft-preservation
prompt: Reply briefly.
executor:
  harness: claude-native
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""
            response = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files({"draft-preservation.yaml": spec}),
                metadata={
                    "workspace": str(workspace),
                    "labels": {"omnigent.wrapper": "claude-code-native-ui"},
                    # Auto mode asks the model whether a tool call may run, which a
                    # scripted gateway cannot answer; the allow rule decides instead.
                    "terminal_launch_args": ["--permission-mode", "default"],
                },
            )
            response.raise_for_status()
            session_id = response.json()["session_id"]
            client.patch(
                f"/v1/sessions/{session_id}", json={"runner_id": runner_id}
            ).raise_for_status()
            client.post(
                f"/v1/sessions/{session_id}/resources/terminals",
                json={"terminal": "claude", "session_key": "main", "ensure_native_terminal": True},
            ).raise_for_status()
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            bridge_dir = bridge_dir_for_bridge_id(
                snapshot.get("labels", {}).get(BRIDGE_ID_LABEL_KEY) or session_id
            )
            _wait_for(lambda: (bridge_dir / "tmux.json").exists(), "Claude's tmux target")
            advert = json.loads((bridge_dir / "tmux.json").read_text())
            session = _ClaudeSession(
                client=client,
                base_url=resume_test_server,
                session_id=session_id,
                runner_id=runner_id,
                bridge_dir=bridge_dir,
                socket_path=advert["socket_path"],
                target=advert["tmux_target"],
                workspace=workspace,
            )
            _wait_for(lambda: session.composer() == "", "Claude's empty input box", 90)
            yield session
        finally:
            if session is not None:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    (tmp_path / "pane.txt").write_text(
                        session.tmux("capture-pane", "-p", "-S", "-200", "-t", session.target)
                    )
            runner.terminate()
            try:
                runner.wait(timeout=15)
            except subprocess.TimeoutExpired:
                runner.kill()
                runner.wait(timeout=5)
            if session is not None:
                subprocess.run(
                    ["tmux", "-S", session.socket_path, "kill-server"],
                    check=False,
                    capture_output=True,
                    timeout=5,
                )


def _recording_path(request: pytest.FixtureRequest) -> Path | None:
    record_dir = os.environ.get(_RECORD_DIR_ENV)
    return Path(record_dir) / f"{request.node.name}.cast" if record_dir else None


@pytest.fixture
def person(claude: _ClaudeSession, request: pytest.FixtureRequest) -> Iterator[_PersonTerminal]:
    terminal = _PersonTerminal(claude, _recording_path(request))
    try:
        yield terminal
    finally:
        terminal.close()


@pytest.fixture
def peer(claude: _ClaudeSession, isolated_mock_llm_server_url: str) -> _PeerSession:
    """Another session of the same person, on the same runner, driven by a scripted model."""
    mock = isolated_mock_llm_server_url
    set_fallback_mock_llm(mock, key="_policy_llm_", text='{"action":"allow","reason":""}')
    spec = {
        "name": "peer",
        "prompt": "Coordinate session work.",
        "spawn": True,
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o",
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock}/v1"},
        },
        "skills_filter": "none",
    }
    response = post_session_bundle(
        claude.client.post,
        "/v1/sessions",
        bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
        metadata={"title": "Peer", "workspace": str(claude.workspace)},
    )
    response.raise_for_status()
    session_id = response.json()["session_id"]
    claude.client.patch(
        f"/v1/sessions/{session_id}", json={"runner_id": claude.runner_id}
    ).raise_for_status()
    return _PeerSession(claude.client, session_id, mock)


def _type_in_background(terminal: _PersonTerminal | _WebTerminal, text: str) -> threading.Thread:
    typing = threading.Thread(target=terminal.type, args=(text,), kwargs={"delay": 0.1})
    typing.start()
    return typing


def test_peer_message_while_typing_keeps_every_keystroke(
    claude: _ClaudeSession, person: _PersonTerminal, peer: _PeerSession
) -> None:
    """Another session messages this one while the person is still typing in the terminal."""
    token = secrets.token_hex(4)
    text = f"can you double-check the retry logic in the uploader before we merge {token}"
    claude.mark("The person starts typing in Claude's input box")
    typing = _type_in_background(person, text)
    _wait_for(lambda: len(claude.composer() or "") >= 12, "the person's typing to show")
    marker = f"PEER_MESSAGE_{token}"
    claude.mark("Another session sends this one a message")
    peer.send_to(claude.session_id, f"{marker} Reply with only ACK.")
    typing.join()
    claude.mark("The person stops typing")
    delivered = claude.wait_for_prompt(marker)
    claude.mark("Claude received the other session's message")
    claude.wait_for_composer(text)
    claude.mark("Every keystroke is still in the input box")
    assert token not in delivered.replace(marker, ""), delivered
    assert not any(text in prompt for prompt in claude.prompts())


def test_web_message_keeps_the_terminal_draft(
    claude: _ClaudeSession, person: _PersonTerminal
) -> None:
    """A web chat message arrives while the person has an unsent draft in the terminal."""
    token = secrets.token_hex(4)
    draft = f"half-written question about the parser {token}"
    claude.mark("The person types a draft in Claude's input box")
    person.type(draft)
    claude.wait_for_composer(draft)
    marker = f"WEB_MESSAGE_{token}"
    claude.mark("A chat message is sent from the web UI")
    send_user_text(
        claude.client, session_id=claude.session_id, text=f"{marker} Reply with only ACK."
    )
    claude.wait_for_prompt(marker)
    claude.mark("Claude received the web message")
    claude.wait_for_composer(draft)
    claude.mark("The draft is still in the input box")
    assert not any(draft in prompt for prompt in claude.prompts())


def test_message_during_a_turn_keeps_the_follow_up_draft(
    tmp_path: Path,
    claude: _ClaudeSession,
    person: _PersonTerminal,
    peer: _PeerSession,
    gateway: tuple[_ScriptedGateway, str],
) -> None:
    """A message steers a running turn while the person drafts a follow-up in the terminal."""
    scripted, _ = gateway
    token = secrets.token_hex(4)
    pipe = tmp_path / f"busy-{token}.fifo"
    os.mkfifo(pipe)
    command = f"cat {pipe}"
    busy = f"BUSY_{token}"
    scripted.busy_marker, scripted.busy_command = busy, command
    writer: int | None = None

    def command_started() -> bool:
        nonlocal writer
        try:
            writer = os.open(pipe, os.O_WRONLY | os.O_NONBLOCK)
        except OSError:
            return False
        return True

    try:
        claude.mark("A web message starts a turn that runs a long command")
        send_user_text(
            claude.client,
            session_id=claude.session_id,
            text=(
                f"{busy} Run exactly this shell command with the Bash tool, then reply "
                f"with only DONE: {command}"
            ),
        )
        # Opening the pipe for writing succeeds only once the command reads it.
        _wait_for(command_started, "Claude to start running the command", 120)
        draft = f"follow-up once that finishes {token}"
        claude.mark("While Claude works, the person types a follow-up")
        person.type(draft)
        claude.wait_for_composer(draft)
        marker = f"STEER_MESSAGE_{token}"
        claude.mark("Another session messages this one mid-turn")
        peer.send_to(claude.session_id, f"{marker} Reply with only ACK.")
        _wait_for(
            lambda: marker in claude.pane().replace(claude.composer() or "", ""),
            "Claude to show the queued message",
        )
        claude.mark("Claude queued the other session's message")
        claude.wait_for_composer(draft)
        claude.mark("The follow-up draft is still in the input box")
    finally:
        if writer is None:
            command_started()
        if writer is not None:
            os.write(writer, f"RELEASED_{token}\n".encode())
            os.close(writer)
    claude.wait_for_prompt(marker)
    claude.mark("Claude took the queued message after the command finished")
    claude.wait_for_composer(draft)
    assert not any(draft in prompt for prompt in claude.prompts())


def test_web_effort_change_keeps_the_terminal_draft(
    claude: _ClaudeSession, person: _PersonTerminal
) -> None:
    """The web UI changes Claude's effort while the person has an unsent draft."""
    token = secrets.token_hex(4)
    draft = f"draft written before the effort change {token}"
    claude.mark("The person types a draft in Claude's input box")
    person.type(draft)
    claude.wait_for_composer(draft)
    claude.mark("The web UI switches Claude's effort to high")
    claude.client.patch(
        f"/v1/sessions/{claude.session_id}", json={"reasoning_effort": "high"}
    ).raise_for_status()
    claude.wait_for_prompt("<command-name>/effort</command-name>")
    claude.mark("Claude ran /effort high")
    claude.wait_for_composer(draft)
    claude.mark("The draft is still in the input box")


def test_shell_mode_draft_survives_a_message(
    claude: _ClaudeSession, person: _PersonTerminal
) -> None:
    """A message arrives while the person has a shell command typed in ``!`` mode."""
    token = secrets.token_hex(4)
    command = f"echo SHELL_DRAFT_{token}"
    claude.mark("The person switches Claude's input box to shell mode")
    person.type("!")
    _wait_for(lambda: (claude.composer() or "").startswith("!"), "shell mode")
    claude.mark("The person types a shell command without running it")
    person.type(command)
    expected = f"! {command}"
    claude.wait_for_composer(expected)
    marker = f"WEB_MESSAGE_{token}"
    claude.mark("A chat message is sent from the web UI")
    send_user_text(
        claude.client, session_id=claude.session_id, text=f"{marker} Reply with only ACK."
    )
    claude.wait_for_prompt(marker)
    claude.mark("Claude received the web message")
    claude.wait_for_composer(expected)
    claude.mark("The shell command is still typed, still in shell mode")
    person.press(b"\r")
    _wait_for(
        lambda: f"SHELL_DRAFT_{token}" in claude.pane().replace(command, ""),
        "the restored shell command's output",
    )
    claude.mark("The person runs the restored command")


def test_multiline_draft_with_a_paste_survives_a_message(
    claude: _ClaudeSession, person: _PersonTerminal, peer: _PeerSession
) -> None:
    """A message arrives while the draft holds several lines and a collapsed paste."""
    token = secrets.token_hex(4)
    pasted = "\n".join(f"Traceback line {index} {token}" for index in range(1, 6))
    claude.mark("The person writes a multi-line draft and pastes a log")
    person.type(f"Why does this fail? {token}")
    person.press(b"\n")
    person.paste(pasted)
    person.type(" Look at line 3 first.")
    before = _wait_for(
        lambda: (lambda text: text if text and "[Pasted text" in text else None)(
            claude.composer()
        ),
        "the collapsed paste in the input box",
    )
    marker = f"PEER_MESSAGE_{token}"
    claude.mark("Another session sends this one a message")
    peer.send_to(claude.session_id, f"{marker} Reply with only ACK.")
    claude.wait_for_prompt(marker)
    claude.mark("Claude received the other session's message")
    claude.wait_for_composer(before)
    claude.mark("The draft, paste placeholder included, is back")
    person.press(b"\r")
    sent = claude.wait_for_prompt(f"Why does this fail? {token}")
    claude.mark("The person sends the restored draft")
    assert pasted in sent, sent
    assert "Look at line 3 first." in sent, sent


def test_message_waits_while_the_stash_holds_another_draft(
    claude: _ClaudeSession, person: _PersonTerminal
) -> None:
    """With Claude's one stash slot taken, a message waits instead of erasing the new draft."""
    token = secrets.token_hex(4)
    stashed = f"older idea kept in the stash {token}"
    draft = f"newer draft in the box {token}"
    claude.mark("The person stashes one draft with Ctrl+S")
    person.type(stashed)
    claude.wait_for_composer(stashed)
    person.press(_CTRL_S)
    _wait_for(
        lambda: claude.composer() == "" and _STASH_MARKER in claude.pane(),
        "Claude to stash the first draft",
    )
    claude.mark("The person types a second draft")
    person.type(draft)
    claude.wait_for_composer(draft)
    marker = f"WEB_MESSAGE_{token}"
    claude.mark("A chat message is sent from the web UI")
    with _SessionStream(claude) as stream:
        send_user_text(
            claude.client, session_id=claude.session_id, text=f"{marker} Reply with only ACK."
        )
        _wait_for(
            lambda: (
                _HELD_NOTICE in stream.text().lower()
                or any(marker in prompt for prompt in claude.prompts())
            ),
            "the message to be delivered or held",
        )
    claude.wait_for_composer(draft)
    assert _STASH_MARKER in claude.pane(), claude.pane()
    assert not any(marker in prompt for prompt in claude.prompts())
    claude.mark("The message waits; both drafts are intact")
    person.press(b"\r")
    claude.mark("The person sends the second draft")
    claude.wait_for_prompt(marker, timeout=120)
    claude.mark("Then the waiting message is delivered")
    prompts = claude.prompts()
    sent_at = next(index for index, prompt in enumerate(prompts) if draft in prompt)
    delivered_at = next(index for index, prompt in enumerate(prompts) if marker in prompt)
    assert sent_at < delivered_at, prompts
    claude.wait_for_composer(stashed)
    claude.mark("The stashed draft is back in the input box")


def test_draft_longer_than_the_input_box_survives_a_message(
    claude: _ClaudeSession, person: _PersonTerminal
) -> None:
    """A message arrives while the draft has more lines than Claude's input box shows."""
    token = secrets.token_hex(4)
    lines = [f"step {index} of the migration plan {token}" for index in range(1, 46)]
    claude.mark("The person writes a draft longer than the input box shows")
    for index, line in enumerate(lines):
        person.type(line, delay=0.005)
        if index < len(lines) - 1:
            person.press(b"\n")
    _wait_for(
        lambda: lines[-1] in claude.pane() and lines[0] not in claude.pane(),
        "the input box to show only the end of the long draft",
    )
    marker = f"WEB_MESSAGE_{token}"
    claude.mark("A chat message is sent from the web UI")
    send_user_text(
        claude.client, session_id=claude.session_id, text=f"{marker} Reply with only ACK."
    )
    claude.wait_for_prompt(marker)
    claude.mark("Claude received the web message")
    try:
        _wait_for(lambda: lines[-1] in claude.pane(), "the long draft to be back", 30)
    except AssertionError:
        raise AssertionError(
            f"Claude's input box lost the long draft.\npane:\n{claude.pane()}"
        ) from None
    claude.mark("The long draft is back")
    person.press(b"\r")
    sent = claude.wait_for_prompt(lines[0])
    claude.mark("The person sends the restored draft")
    assert sent.strip() == "\n".join(lines), sent


def test_peer_message_while_typing_in_the_web_terminal_keeps_every_keystroke(
    claude: _ClaudeSession, peer: _PeerSession, request: pytest.FixtureRequest
) -> None:
    """Another session messages this one while the person types in the web terminal."""
    web = _WebTerminal(claude, _recording_path(request))
    try:
        token = secrets.token_hex(4)
        text = f"please also rerun the flaky integration suite twice {token}"
        claude.mark("The person starts typing in the web terminal")
        typing = _type_in_background(web, text)
        _wait_for(lambda: len(claude.composer() or "") >= 12, "the person's typing to show")
        marker = f"PEER_MESSAGE_{token}"
        claude.mark("Another session sends this one a message")
        peer.send_to(claude.session_id, f"{marker} Reply with only ACK.")
        typing.join()
        claude.mark("The person stops typing")
        delivered = claude.wait_for_prompt(marker)
        claude.mark("Claude received the other session's message")
        claude.wait_for_composer(text)
        claude.mark("Every keystroke is still in the input box")
        assert token not in delivered.replace(marker, ""), delivered
    finally:
        web.close()
