"""E2E regression: concurrent Kiro ACP permission requests surface serially.

When the Kiro TUI emits several
ACP ``session/request_permission`` requests before the first one resolves,
``supervise_kiro_permission_mirror`` mirrors only the first request into a web
approval card. The later requests are skipped while ``pending`` is non-empty,
but the recorder offset is committed past them, so they can never be surfaced
on a later poll: after the user approves the one visible card, Omnigent
reports zero pending elicitations while the Kiro terminal stays blocked on the
next native ``requires approval`` prompt.

User journey covered (all through the real product path — web SPA → server →
runner → kiro-native bridge → tmux TUI → ACP recorder → permission mirror):

1. start a Kiro-native session through Omnigent;
2. send a task that makes Kiro request permission for three independent shell
   operations together (three distinct request ids in one recorder batch);
3. one approval card appears in chat — approve it;
4. EXPECTED: the next unresolved request surfaces as a new approval card
   (one active card at a time, served serially).
   ACTUAL (bug): no further card ever appears and the session reports no
   pending elicitations while the Kiro TUI remains blocked on the next
   ``requires approval`` prompt.

The real ``kiro-cli`` authenticates against Kiro's own backend and cannot run
in CI, so the fixture points ``OMNIGENT_KIRO_PATH`` (the bridge's supported
binary override) at a minimal fake TUI that speaks the same contracts the
bridge drives: the ``────`` input separator + "ask a question or describe a
task" ready marker, bracketed paste, the native approval picker markers, and
the ``KIRO_ACP_RECORD_PATH`` ACP recorder. Everything Omnigent-side — terminal
autocreate, tmux injection, the ACP recorder tailing, the permission mirror,
the native-permission hook, the approval card, and verdict delivery back into
the pane — is the real production path.
"""

from __future__ import annotations

import json
import re
import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _bind_session_runner,
    _find_free_port,
)

from .test_message_render_parity import _ensure_chat_view, _select_view_mode, _send

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="kiro-native permission mirror e2e needs `tmux` on PATH",
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_BOOT_TIMEOUT_S = 90.0
_BOOT_POLL_INTERVAL_S = 0.5
_FIRST_CARD_TIMEOUT_MS = 120_000
# Generous versus the mirror's 0.4s poll: after the first verdict lands, a
# fixed mirror promotes the next queued request within a poll or two.
_SECOND_CARD_TIMEOUT_MS = 45_000

# The three permission requests the fake TUI raises for the first task, in
# the order it queues them (ids match the shim's ``perm-req-<turn>-<n>``).
_REQUEST_IDS = ("perm-req-1-1", "perm-req-1-2", "perm-req-1-3")

# A minimal fake ``kiro-cli`` TUI. It emulates only the surfaces the
# kiro-native bridge actually drives (see module docstring); the reported
# trigger — several independent shell operations requesting permission
# together — is reproduced by appending three ACP request records in a single
# write, exactly the shape the reporter observed in the recorder.
_FAKE_KIRO_SOURCE = r'''#!/usr/bin/env python3
"""Fake kiro-cli TUI for the concurrent-permission mirror regression test."""
import json
import os
import sys

RECORD_PATH = os.environ.get("KIRO_ACP_RECORD_PATH", "")
SEP = "─" * 44
READY_MARKER = "ask a question or describe a task"
OPTIONS = (
    "Yes, single permission",
    "Trust, always allow in this session",
    "No (Tab to edit)",
)
PASTE_START = b"\x1b[200~"
PASTE_END = b"\x1b[201~"
COMMANDS = ("alpha", "beta", "gamma")


def append_records(messages):
    if not RECORD_PATH:
        return
    payload = "".join(json.dumps({"msg": message}) + "\n" for message in messages)
    with open(RECORD_PATH, "a", encoding="utf-8") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())


class FakeKiro:
    def __init__(self):
        self.transcript = []
        self.queue = []
        self.active = None
        self.focus = 0
        self.draft = ""
        self.turn = 0

    def render(self):
        lines = list(self.transcript[-6:])
        if self.active is not None:
            lines.append("")
            lines.append(self.active["title"])
            lines.append(" requires approval")
            for index, option in enumerate(OPTIONS):
                prefix = "❯ " if index == self.focus else "  "
                lines.append(prefix + option)
        lines.append(SEP)
        lines.append("> " + READY_MARKER)
        for draft_line in self.draft.splitlines():
            if draft_line.strip():
                lines.append(draft_line)
        sys.stdout.write("\x1b[2J\x1b[H" + "\r\n".join(lines) + "\r\n")
        sys.stdout.flush()

    def submit(self):
        text = self.draft.strip()
        self.draft = ""
        if not text:
            self.render()
            return
        self.turn += 1
        self.transcript.append("> " + text[:64])
        requests = []
        for index, name in enumerate(COMMANDS, 1):
            requests.append(
                {
                    "id": "perm-req-%d-%d" % (self.turn, index),
                    "title": "Running: touch /tmp/setup-step-%s" % name,
                    "allow": "allow-%d-%d" % (self.turn, index),
                    "reject": "reject-%d-%d" % (self.turn, index),
                }
            )
        # The reported trigger: several independent operations request
        # permission together — three distinct request ids land in the ACP
        # recorder in one batch, before the first is resolved.
        append_records(
            [
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "method": "session/request_permission",
                    "params": {
                        "sessionId": "fake-kiro-session",
                        "toolCall": {
                            "toolCallId": "tc-" + request["id"],
                            "title": request["title"],
                        },
                        "options": [
                            {"optionId": request["allow"], "kind": "allow_once"},
                            {"optionId": request["reject"], "kind": "reject_once"},
                        ],
                    },
                }
                for request in requests
            ]
        )
        self.queue.extend(requests)
        self.advance()

    def advance(self):
        self.active = self.queue.pop(0) if self.queue else None
        self.focus = 0
        self.render()

    def resolve_active(self, accepted):
        request = self.active
        option = request["allow"] if accepted else request["reject"]
        append_records(
            [
                {
                    "jsonrpc": "2.0",
                    "id": request["id"],
                    "result": {
                        "outcome": {"outcome": "selected", "optionId": option}
                    },
                }
            ]
        )
        marker = "✓" if accepted else "✗"
        self.transcript.append(marker + " " + request["title"])
        self.advance()

    def on_enter(self):
        if self.active is not None:
            if self.focus == 0:
                self.resolve_active(True)
            elif self.focus == len(OPTIONS) - 1:
                self.resolve_active(False)
            return
        self.submit()

    def run(self):
        import tty

        tty.setraw(0)
        sys.stdout.write("\x1b[?2004h")  # request bracketed paste
        self.render()
        buf = b""
        in_paste = False
        paste_buf = b""
        while True:
            chunk = os.read(0, 4096)
            if not chunk:
                return
            buf += chunk
            while buf:
                if in_paste:
                    end = buf.find(PASTE_END)
                    if end == -1:
                        keep = len(PASTE_END) - 1
                        if len(buf) > keep:
                            paste_buf += buf[:-keep]
                            buf = buf[-keep:]
                        break
                    paste_buf += buf[:end]
                    buf = buf[end + len(PASTE_END) :]
                    in_paste = False
                    self.draft += paste_buf.decode("utf-8", "replace").replace(
                        "\r", "\n"
                    )
                    paste_buf = b""
                    self.render()
                    continue
                if len(buf) < len(PASTE_START) and PASTE_START.startswith(buf):
                    break  # incomplete paste marker: wait for more bytes
                if buf.startswith(PASTE_START):
                    in_paste = True
                    buf = buf[len(PASTE_START) :]
                    continue
                byte = buf[0]
                if byte == 0x1B:
                    if len(buf) == 1:
                        buf = b""  # bare Escape: ignore
                        break
                    if buf[1:2] == b"[":
                        end = 2
                        while end < len(buf) and not (0x40 <= buf[end] <= 0x7E):
                            end += 1
                        if end >= len(buf):
                            break  # incomplete CSI: wait for more bytes
                        seq = buf[: end + 1]
                        buf = buf[end + 1 :]
                        if self.active is not None and seq == b"\x1b[B":
                            self.focus = min(self.focus + 1, len(OPTIONS) - 1)
                            self.render()
                        elif self.active is not None and seq == b"\x1b[A":
                            self.focus = max(self.focus - 1, 0)
                            self.render()
                        continue
                    buf = buf[1:]
                    continue
                buf = buf[1:]
                if byte in (0x0D, 0x0A):
                    self.on_enter()
                elif byte == 0x0B:  # C-k — the bridge's pre-paste line kill
                    self.draft = ""
                    self.render()
                elif byte in (0x01, 0x09):  # C-a / Tab: ignore
                    pass
                elif byte >= 0x20:
                    self.draft += chr(byte)
                    self.render()


def main():
    if "--list-models" in sys.argv:
        print(
            json.dumps(
                {
                    "models": [
                        {"model_id": "fake-model", "model_name": "Fake Model"}
                    ],
                    "default_model": "fake-model",
                }
            )
        )
        return
    FakeKiro().run()


if __name__ == "__main__":
    main()
'''


def _wait_for(
    predicate: Callable[[], bool], *, timeout_s: float = 30.0, interval_s: float = 0.5
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _recorder_messages(record_file: Path) -> list[dict]:
    """Parse the Kiro ACP recorder JSONL into its ``msg`` payloads."""
    try:
        raw = record_file.read_text(encoding="utf-8")
    except OSError:
        return []
    messages: list[dict] = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            continue
        message = record.get("msg") if isinstance(record, dict) else None
        if isinstance(message, dict):
            messages.append(message)
    return messages


def _recorder_request_ids(record_file: Path) -> set[str]:
    return {
        str(message["id"])
        for message in _recorder_messages(record_file)
        if message.get("method") == "session/request_permission" and message.get("id")
    }


def _recorder_response_ids(record_file: Path) -> set[str]:
    return {
        str(message["id"])
        for message in _recorder_messages(record_file)
        if isinstance(message.get("result"), dict) and message.get("id")
    }


def _kiro_pane_text(bridge_dir: Path) -> str:
    """Capture the live Kiro tmux pane (what the user's terminal shows)."""
    from omnigent.kiro_native_bridge import read_tmux_info

    info = read_tmux_info(bridge_dir)
    if info is None:
        return ""
    try:
        proc = subprocess.run(
            [
                "tmux",
                "-S",
                info["socket_path"],
                "capture-pane",
                "-p",
                "-t",
                info["tmux_target"],
            ],
            check=False,
            capture_output=True,
            text=True,
            timeout=10.0,
        )
    except (subprocess.TimeoutExpired, OSError):
        return ""
    return proc.stdout if proc.returncode == 0 else ""


def _create_kiro_native_session(base_url: str, runner_id: str, workspace: Path) -> str:
    """Register the real ``kiro-native`` wrapper agent and bind its session.

    Mirrors ``tests.e2e_ui.conftest._create_native_kiro_session`` but pins the
    session workspace to a scratch directory so the kiro workspace MCP config
    is not written into the repo checkout.
    """
    import io
    import tarfile
    import tempfile

    from omnigent._wrapper_labels import (
        KIRO_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.kiro_native import _materialize_kiro_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_kiro_agent_spec(Path(tmp), model=None)
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("kiro-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    metadata = {
        "labels": {
            UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
            WRAPPER_LABEL_KEY: KIRO_NATIVE_WRAPPER_VALUE,
        },
        "workspace": str(workspace),
    }
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={"bundle": ("kiro-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    _bind_session_runner(base_url, session_id, runner_id)
    return session_id


@pytest.fixture
def kiro_shim_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound kiro-native session backed by the fake ``kiro-cli`` TUI.

    Spawns a dedicated server + runner (mirroring ``mocked_native_codex_session``)
    because ``OMNIGENT_KIRO_PATH`` must be present in the *runner's* environment
    before its kiro terminal autocreate resolves the binary; a session-scoped
    shared ``live_server`` cannot carry the shim override.

    :returns: ``(base_url, session_id, bridge_dir)``.
    """
    import os

    if request.config.getoption("--ui-base-url"):
        pytest.skip("kiro permission-mirror e2e requires an isolated spawned server")

    from omnigent.kiro_native_bridge import bridge_dir_for_session_id
    from omnigent.runner.identity import token_bound_runner_id

    server_tmp = tmp_path_factory.mktemp("e2e_ui_kiro_shim_server")
    shim_path = server_tmp / "kiro-cli"
    shim_path.write_text(_FAKE_KIRO_SOURCE, encoding="utf-8")
    shim_path.chmod(0o755)

    workspace = server_tmp / "workspace"
    artifact_dir = server_tmp / "artifacts"
    for path in (workspace, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")
    db_path = server_tmp / "test.db"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_KIRO_PATH": str(shim_path),
        # The fake TUI renders kiro's ``❯`` / ``─`` markers; make sure tmux and
        # the pane run under a UTF-8 locale so pane captures match them.
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    log_handle = open(log_path, "w")  # noqa: SIM115
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-c",
                "import omnigent.server.presence as _p; _p._LEAVE_GRACE_S = 1.0; "
                + "from omnigent.cli import main; main()",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{db_path}",
                "--artifact-location",
                str(artifact_dir),
                "--agent",
                str(agent_yaml_path),
            ],
            env=server_env,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
        )
        runner_proc = subprocess.Popen(
            [sys.executable, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_log_handle,
            stderr=subprocess.STDOUT,
        )

        deadline = time.monotonic() + _BOOT_TIMEOUT_S
        ready = False
        last_error = "not polled yet"
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                last_error = f"server exited early with code {proc.returncode}"
                break
            if runner_proc.poll() is not None:
                last_error = f"runner exited early with code {runner_proc.returncode}"
                break
            try:
                resp = httpx.get(f"{base_url}/health", timeout=2)
                if resp.status_code == 200:
                    status_resp = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status_resp.status_code == 200 and status_resp.json()["online"] is True:
                        ready = True
                        break
                    last_error = (
                        f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
                    )
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_BOOT_POLL_INTERVAL_S)

        if not ready:
            raise RuntimeError(
                f"kiro shim e2e server did not become healthy within "
                f"{_BOOT_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
                f"Server log at {log_path}:\n"
                f"{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
                f"Runner log at {runner_log_path}:\n"
                f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
            )

        session_id = _create_kiro_native_session(base_url, runner_id, workspace)
        bridge_dir = bridge_dir_for_session_id(session_id)
        yield (base_url, session_id, bridge_dir)
    finally:
        if session_id is not None:
            try:
                from omnigent.kiro_native_bridge import read_tmux_info

                info = read_tmux_info(bridge_dir_for_session_id(session_id))
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
                if info is not None:
                    subprocess.run(
                        ["tmux", "-S", info["socket_path"], "kill-server"],
                        check=False,
                        capture_output=True,
                        timeout=10.0,
                    )
            except Exception:
                pass
        for child in (runner_proc, proc):
            if child is None:
                continue
            child.terminate()
            try:
                child.wait(timeout=10)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait(timeout=10)
        log_handle.close()
        runner_log_handle.close()


@pytest.mark.timeout(600)
def test_concurrent_kiro_permissions_surface_serially(
    kiro_shim_session: tuple[str, str, Path],
    page: Page,
) -> None:
    """Three concurrent Kiro permission requests must all reach the user.

    Red on the current build: only the first request is mirrored; after it is
    approved the two remaining requests are permanently lost (the recorder
    offset advanced past them), so the second approval card never appears while
    the Kiro TUI stays blocked on its next ``requires approval`` prompt.
    """
    from omnigent.kiro_native_bridge import acp_record_path
    from omnigent.kiro_native_permissions import kiro_permission_elicitation_id

    base_url, session_id, bridge_dir = kiro_shim_session
    record_file = acp_record_path(bridge_dir)

    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run the three setup commands that each need shell approval.")

    # Kiro raised three concurrent permission requests in one recorder batch…
    _wait_for(
        lambda: set(_REQUEST_IDS) <= _recorder_request_ids(record_file),
        timeout_s=120.0,
    )

    # …and the first surfaces as a pending approval card in chat.
    first_card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(first_card).to_be_visible(timeout=_FIRST_CARD_TIMEOUT_MS)
    expect(first_card.get_by_text("Kiro", exact=False).first).to_be_visible()
    first_elicitation_id = kiro_permission_elicitation_id(session_id, _REQUEST_IDS[0])
    assert first_elicitation_id in json.dumps(_pending_elicitations(base_url, session_id)), (
        "the first parked elicitation is not the first ACP permission request"
    )

    # Approve it from the web UI; the verdict reaches the native TUI, which
    # records the ACP response for request #1 and immediately blocks on the
    # next queued prompt.
    first_card.get_by_role("button", name="Approve").click()
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first).to_be_visible(
        timeout=30_000
    )
    _wait_for(lambda: _REQUEST_IDS[0] in _recorder_response_ids(record_file), timeout_s=60.0)
    _wait_for(
        lambda: (
            "requires approval" in _kiro_pane_text(bridge_dir)
            and "setup-step-beta" in _kiro_pane_text(bridge_dir)
        ),
        timeout_s=30.0,
    )

    # Show the user-visible blocked state: the session terminal still sits on
    # Kiro's native "requires approval" prompt for the next operation.
    _select_view_mode(page, "Terminal")
    page.wait_for_timeout(3_000)
    _select_view_mode(page, "Chat")

    # EXPECTED (the regression guard): the remaining unresolved requests are
    # surfaced serially — a new pending approval card appears for the next
    # queued request. ACTUAL on the buggy build: the mirror skipped requests
    # #2/#3 while #1 was pending and committed the recorder offset past them,
    # so no card ever appears and the session reports no pending elicitations
    # while the Kiro TUI remains blocked.
    second_card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(second_card).to_be_visible(timeout=_SECOND_CARD_TIMEOUT_MS)
    expect(second_card.get_by_text(re.compile(r"setup-step-(beta|gamma)")).first).to_be_visible()
    followup_elicitation_ids = {
        kiro_permission_elicitation_id(session_id, request_id) for request_id in _REQUEST_IDS[1:]
    }
    pending_now = json.dumps(_pending_elicitations(base_url, session_id))
    assert any(elicitation_id in pending_now for elicitation_id in followup_elicitation_ids), (
        "server has no pending elicitation for the queued Kiro permission requests"
    )
