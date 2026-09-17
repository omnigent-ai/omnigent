"""E2E regression: a transient attachment read must not permanently drop an
attachment from a claude-native turn.

The failure mode this guards: a user attaches a file in a ``claude-native``
session and sends a turn. The out-of-process runner does not
hold the server's file store, so it fetches the uploaded bytes back through the
session-scoped resource endpoints and inlines them before handing the message
to the Claude Code terminal (``resolve_file_id_block`` ->
``_resolve_forwarded_message_content``). That fetch is two GETs (metadata, then
content). On ANY ``httpx.HTTPError`` -- a ``ReadTimeout``, or a ``500`` surfaced
by ``raise_for_status()`` -- the resolver returns ``None`` with no retry, so the
runner keeps the raw ``file_id`` block. The native executor then renders that
unresolved block through ``attachment_reference_line`` -> the visible
``"[Attachment <name> could not be loaded]"`` marker delivered into the pane.
The attachment is permanently dropped even though the resource is readable again
on the very next read -- a transient blip, not data loss.

This drives the REAL data path end to end where the bug lives:

* a real ``omnigent server`` subprocess whose file store fails the FIRST read of
  each file id and succeeds every read after (the real route + exception handler
  produce the production ``500 {"error":{"code":"internal_error",...}}`` body),
* the real runner resolver ``_resolve_forwarded_message_content`` fetching the
  uploaded file back over real HTTP against that transient fault, and
* the real ``ClaudeNativeExecutor.run_turn`` rendering the resolved content and
  delivering it into a real ``tmux`` pane running a fake Claude Code TUI, via the
  production ``inject_user_message`` transport.

Desired behavior (asserted): the attachment is recoverable -- the second read
succeeds -- so a bounded retry inside the resolver must recover it and the pane
must show the ``"[Attached: <path>]"`` reference. On the buggy build the single
transient read drops the attachment and the pane shows
``"[Attachment protocol.md could not be loaded]"`` instead, so this test FAILS
with that marker in the pane.

The Claude CLI is replaced by a fake TUI, so no Claude login is needed; the bug
lives entirely upstream in the runner's resolution, not in the model.

Run::

    .venv/bin/python -m pytest \
        tests/e2e/test_native_attachment_transient_read_drop_e2e.py -v
"""

from __future__ import annotations

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
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from omnigent.harnesses.claude_native.bridge import (
    _BRIDGE_ROOT,
    _capture_pane,
    write_tmux_target,
)
from omnigent.inner.claude_native_executor import ClaudeNativeExecutor
from omnigent.inner.executor import TurnComplete
from omnigent.runner.app import _resolve_forwarded_message_content

_REPO_ROOT = Path(__file__).resolve().parents[2]

# Every HTTP call in this test targets 127.0.0.1; bypass any CI egress proxy.
_http = httpx.Client(trust_env=False)

# The runner/server imports resolve from sdks/ in a worktree, site-packages in
# an installed venv.
_PYTHONPATH = os.pathsep.join(
    [
        str(_REPO_ROOT),
        str(_REPO_ROOT / "sdks" / "python-client"),
        str(_REPO_ROOT / "sdks" / "ui"),
        os.environ.get("PYTHONPATH", ""),
    ]
)

# Bootstrap for the spawned server: make the file store raise on the FIRST read
# of each file id and succeed on every read after. The first read is the
# runner's metadata GET during resolution; the REAL route + app-level exception
# handler turn the raise into the production 500 body. Because the id is
# readable again immediately, this is a transient blip -- exactly the condition
# under which a bounded retry recovers but the buggy no-retry resolver drops.
_SERVER_BOOTSTRAP = """
import omnigent.stores.file_store.sqlalchemy_store as _fs

_orig_get = _fs.SqlAlchemyFileStore.get
_failed = set()

def _flaky_get(self, file_id, session_id=None):
    if file_id not in _failed:
        _failed.add(file_id)
        raise RuntimeError(f"simulated transient file read failure for file_id={file_id}")
    return _orig_get(self, file_id, session_id=session_id)

_fs.SqlAlchemyFileStore.get = _flaky_get

from omnigent.cli import main

main()
"""

# The fake Claude Code TUI. It renders the framed composer -- a box rule, the
# prompt row, a closing rule -- so the bridge's readiness gate and
# draft-visibility poll pass and the pasted draft is seen to land, then commits
# the draft into the transcript on the submit Enter. No stall, no model: this
# test cares only about WHAT text reaches the pane.
_FAKE_CLAUDE_TUI = """\
import os, sys, termios, tty

PROMPT_GLYPH = "\\u276f"
RULE = "\\u2500" * 30


def render(draft, transcript):
    sys.stdout.write("\\x1b[2J\\x1b[H")
    for line in transcript:
        sys.stdout.write(line + "\\r\\n")
    sys.stdout.write(RULE + "\\r\\n")
    sys.stdout.write(PROMPT_GLYPH + " " + draft + "\\r\\n")
    sys.stdout.write(RULE + "\\r\\n")
    sys.stdout.flush()


def main():
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    tty.setraw(fd)
    chars = []
    transcript = []
    render("", transcript)
    try:
        while True:
            data = os.read(fd, 1)
            if not data:
                break
            byte = data[0]
            if byte == 3:  # Ctrl-C tears the pane down on teardown
                break
            if byte == 13:  # Enter submits a non-empty draft
                if chars:
                    transcript.append("sent: " + "".join(chars))
                    chars = []
                    render("", transcript)
                continue
            if byte < 0x20:
                # Swallow control bytes (Escape, the ESC of bracketed-paste
                # markers); the visible message content is printable.
                continue
            chars.append(chr(byte))
            render("".join(chars), transcript)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


main()
"""

_FILENAME = "protocol.md"
_FILE_BYTES = b"# Zebra Protocol\nThe passphrase is MARSCRATE-7.\n"

_HEALTH_TIMEOUT_S = 120.0
_POLL_S = 1.0

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="claude-native terminals run inside tmux; tmux not installed",
)


def _find_free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _localhost_env(extra: dict[str, str]) -> dict[str, str]:
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
    if proc is None or proc.poll() is not None:
        return
    proc.send_signal(signal.SIGTERM)
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _wait_http_ok(url: str, deadline: float) -> None:
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
        files={"bundle": ("claude-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _upload_file(base_url: str, session_id: str, name: str, data: bytes) -> str:
    up = _http.post(
        f"{base_url}/v1/sessions/{session_id}/resources/files",
        files={"file": (name, data, "text/markdown")},
        timeout=30.0,
    )
    up.raise_for_status()
    return str(up.json()["id"])


@pytest.fixture
def fault_injected_server(tmp_path: Path) -> Iterator[str]:
    """A real ``omnigent server`` whose file store fails the first read of each
    file id (a transient blip) and succeeds every read after.

    Yields the base URL. The server is always terminated on teardown.
    """
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_log = (tmp_path / "server.log").open("w")
    proc: subprocess.Popen[bytes] | None = None
    try:
        proc = subprocess.Popen(
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
                f"sqlite:///{tmp_path / 'chat.db'}",
                "--artifact-location",
                str(tmp_path / "artifacts"),
            ],
            env=_localhost_env({}),
            stdout=server_log,
            stderr=subprocess.STDOUT,
        )
        try:
            _wait_http_ok(f"{base_url}/health", time.monotonic() + _HEALTH_TIMEOUT_S)
        except AssertionError as exc:
            raise AssertionError(
                f"{exc}\nserver log:\n{(tmp_path / 'server.log').read_text()[-3000:]}"
            ) from exc
        yield base_url
    finally:
        _terminate(proc)
        server_log.close()


@pytest.fixture
def claude_pane() -> Iterator[Path]:
    """A real ``tmux`` pane running the fake Claude TUI, advertised through the
    production ``write_tmux_target``.

    Yields the bridge dir. The tmux server is always killed on teardown.
    """
    work = Path(tempfile.mkdtemp(prefix="attach-transient-"))
    # Keep the socket path short: a long path overflows the AF_UNIX limit.
    socket_path = work / "t.sock"
    tui_path = work / "fake_claude_tui.py"
    tui_path.write_text(_FAKE_CLAUDE_TUI, encoding="utf-8")

    subprocess.run(
        [
            "tmux",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "claude",
            "-x",
            "80",
            "-y",
            "24",
            sys.executable,
            str(tui_path),
        ],
        check=True,
        timeout=30.0,
    )

    # The bridge validates its dir sits under the trusted claude-native root.
    bridge_dir = _BRIDGE_ROOT / f"attach-transient-{uuid.uuid4().hex}"
    write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="claude")

    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        if "❯" in _capture_pane(str(socket_path), "claude"):
            break
        time.sleep(0.1)

    try:
        yield bridge_dir
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"],
            check=False,
            timeout=30.0,
        )
        with contextlib.suppress(OSError):
            for child in work.iterdir():
                child.unlink()
            work.rmdir()
        shutil.rmtree(bridge_dir, ignore_errors=True)


async def test_transient_attachment_read_does_not_drop_native_attachment(
    fault_injected_server: str,
    claude_pane: Path,
) -> None:
    """A single transient read of an available attachment must not drop it from
    the claude-native turn.

    Journey (the reporter's): a user attaches a file in a claude-native session
    and sends a turn; the runner's fetch of the uploaded bytes hits a transient
    read blip; the attachment must still reach Claude because it is readable
    again immediately.

    Buggy behavior: the resolver returns ``None`` on the blip with no retry, the
    runner keeps the raw ``file_id`` block, and the pane shows
    ``"[Attachment protocol.md could not be loaded]"`` -- the attachment is
    permanently lost.
    """
    base_url = fault_injected_server
    bridge_dir = claude_pane

    session_id = _create_claude_native_session(base_url)
    file_id = _upload_file(base_url, session_id, _FILENAME, _FILE_BYTES)

    # Sanity: the transient signature is live -- the FIRST read 500s and the
    # SECOND succeeds. Use a THROWAWAY file so the primary attachment's
    # fail-once stays armed for the resolver under test.
    probe_id = _upload_file(base_url, session_id, "probe.md", b"probe")
    probe_base = f"{base_url}/v1/sessions/{session_id}/resources/files/{probe_id}"
    first = _http.get(probe_base, timeout=10.0)
    second = _http.get(probe_base, timeout=10.0)
    assert first.status_code == 500, f"transient failure signature not live: {first.status_code}"
    assert first.json()["error"]["code"] == "internal_error"
    assert second.status_code == 200, "resource must be readable again on the next read"

    # THE TURN: the runner resolves the forwarded attachment block over real
    # HTTP against the transient fault, then the native executor renders and
    # delivers the result into the Claude pane.
    block = {"type": "input_file", "file_id": file_id, "filename": _FILENAME}
    async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
        resolved = await _resolve_forwarded_message_content(
            [block], session_id=session_id, server_client=client
        )

    executor = ClaudeNativeExecutor(bridge_dir=bridge_dir)
    events = [
        event
        async for event in executor.run_turn(
            messages=[{"role": "user", "content": resolved}],
            tools=[],
            system_prompt="",
            config=None,
        )
    ]
    assert events and isinstance(events[-1], TurnComplete), events

    # Poll the pane for the delivered attachment text to settle.
    socket_path = _socket_of(bridge_dir)
    pane = ""
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        pane = _capture_pane(socket_path, "claude")
        if "[Attached:" in pane or "could not be loaded" in pane:
            break
        time.sleep(0.2)

    assert "could not be loaded" not in pane, (
        "a single transient read of an AVAILABLE attachment permanently dropped "
        "it from the claude-native turn: the pane shows the "
        "'could not be loaded' marker even though the resource was readable "
        f"again on the very next read. pane:\n{pane}"
    )
    assert "[Attached:" in pane, (
        "the attachment reference never reached the Claude pane after a "
        f"transient read blip. pane:\n{pane}"
    )


def _socket_of(bridge_dir: Path) -> str:
    """The tmux socket path advertised for *bridge_dir*."""
    info = json.loads((bridge_dir / "tmux.json").read_text())
    return str(info["socket_path"])
