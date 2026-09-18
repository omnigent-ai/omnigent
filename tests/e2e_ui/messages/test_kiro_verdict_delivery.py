"""E2E regression: a Kiro web approval must be confirmed against the tmux pane.

Two failure modes of the same kiro-native verdict-delivery seam, both ending in
the same user-visible divergence — Omnigent shows the approval resolved with no
pending elicitation while the native Kiro pane stays blocked on the same
``requires approval`` prompt:

1. post-delivery: ``send_kiro_permission_verdict`` types one ``Enter``, sleeps,
   and returns unconditionally, so an Enter that Kiro drops under load is still
   reported as a delivered verdict (no ACP response is ever recorded);
2. pre-delivery: ``_kiro_active_permission_tool_line`` reduces the rendered
   tool block to one physical line, so a command title that wraps at 80 columns
   (or a ``╰ working_dir=…`` metadata row) fails the fail-closed title check
   and the verdict is never typed at all.

User journey covered (all through the real product path — web SPA → server →
runner → kiro-native bridge → tmux TUI → ACP recorder → permission mirror):

1. start a Kiro-native session through Omnigent;
2. send a task that makes Kiro request one shell approval;
3. the approval card appears in chat — approve it; the card resolves and the
   session reports no pending elicitation;
4. EXPECTED: the verdict reaches the Kiro TUI — the ACP response is recorded
   and the native approval prompt leaves the pane.
   ACTUAL (bug): the verdict never lands; the Kiro terminal stays blocked on
   ``requires approval`` while the web UI claims everything is resolved.

The real ``kiro-cli`` authenticates against Kiro's own backend and cannot run
in CI, so ``OMNIGENT_KIRO_PATH`` points at a fake TUI speaking the contracts
the bridge drives (same seam as ``test_kiro_concurrent_permissions``), with the
reported fault injected per test: ``drop-first-enter`` swallows the first
Enter on the approval picker; ``wrapped-title`` renders the real wrapped
80-column tool block with a working-dir row and separator.
"""

from __future__ import annotations

import secrets
import shutil
import subprocess
import sys
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
)

from .test_kiro_concurrent_permissions import (
    _create_kiro_native_session,
    _kiro_pane_text,
    _pending_elicitations,
    _recorder_request_ids,
    _recorder_response_ids,
)
from .test_message_render_parity import _ensure_chat_view, _select_view_mode, _send

pytestmark = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="kiro-native verdict-delivery e2e needs `tmux` on PATH",
)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_BOOT_TIMEOUT_S = 90.0
_BOOT_POLL_INTERVAL_S = 0.5
_FIRST_CARD_TIMEOUT_MS = 120_000
# Generous versus a fixed bridge: confirming/retrying a verdict takes a few
# seconds; the buggy build never records the response at all.
_DELIVERY_CONFIRM_TIMEOUT_S = 45.0
# The single permission request the fake TUI raises for the first task.
_REQUEST_ID = "perm-req-1"

# A minimal fake ``kiro-cli`` TUI (see module docstring). ``__FAKE_MODE__`` is
# substituted per test: ``drop-first-enter`` ignores the first Enter while the
# approval picker is active (the reported under-load keystroke drop);
# ``wrapped-title`` renders the approval block the way the reporter captured it
# live — the command title wrapped at 80 columns, a ``╰ working_dir=…`` row,
# and a horizontal separator before ``shell requires approval``.
_FAKE_KIRO_TEMPLATE = r'''#!/usr/bin/env python3
"""Fake kiro-cli TUI for the verdict-delivery regression tests."""
import json
import os
import sys

MODE = "__FAKE_MODE__"
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
SHORT_TITLE = "Running: touch /tmp/kiro-e2e-step"
# The 80-column wrap of the long command title, exactly as rendered.
WRAP_FIRST = "Running: cd /tmp/omnigent-e2e-worktrees/fix-kiro-native-verdict-"
WRAP_REST = "1eea && git status --porcelain=v1 --untracked-files=all"
WORKING_DIR_ROW = (
    "╰ working_dir=/tmp/omnigent-e2e-worktrees/fix-kiro-native-verdict-1eea"
)


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
        self.active = None
        self.focus = 0
        self.draft = ""
        self.turn = 0

    def render(self):
        lines = list(self.transcript[-6:])
        if self.active is not None:
            lines.append("")
            if MODE == "wrapped-title":
                lines.append(WRAP_FIRST)
                lines.append(WRAP_REST)
                lines.append(WORKING_DIR_ROW)
                lines.append(SEP)
                lines.append(" shell requires approval")
            else:
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
        title = WRAP_FIRST + WRAP_REST if MODE == "wrapped-title" else SHORT_TITLE
        request = {
            "id": "perm-req-%d" % self.turn,
            "title": title,
            "allow": "allow-%d" % self.turn,
            "reject": "reject-%d" % self.turn,
        }
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
            ]
        )
        self.active = request
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
        self.transcript.append(marker + " " + request["title"][:64])
        self.active = None
        self.focus = 0
        self.render()

    def on_enter(self):
        if self.active is not None:
            if self.focus == 0:
                if MODE == "drop-first-enter" and not self.active.get("dropped"):
                    # The reported under-load behavior: the first Enter on the
                    # approval picker is consumed without any effect.
                    self.active["dropped"] = True
                    return
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


def _fake_kiro_source(mode: str) -> str:
    return _FAKE_KIRO_TEMPLATE.replace("__FAKE_MODE__", mode)


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
    message: str = "condition not met within timeout",
) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError(message)


@contextmanager
def _kiro_stack(server_tmp: Path, shim_source: str) -> Iterator[tuple[str, str, Path]]:
    """Spawn a dedicated server + runner whose kiro binary is the fake TUI.

    A dedicated stack (mirroring ``kiro_shim_session``) because
    ``OMNIGENT_KIRO_PATH`` must be present in the *runner's* environment before
    its kiro terminal autocreate resolves the binary.

    :returns: ``(base_url, session_id, bridge_dir)``.
    """
    import os

    from omnigent.harnesses.kiro_native.bridge import (
        bridge_dir_for_session_id,
        read_tmux_info,
    )
    from omnigent.runner.identity import token_bound_runner_id

    shim_path = server_tmp / "kiro-cli"
    shim_path.write_text(shim_source, encoding="utf-8")
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
                    status_resp = httpx.get(
                        f"{base_url}/v1/runners/{runner_id}/status", timeout=2
                    )
                    if status_resp.status_code == 200 and status_resp.json()["online"] is True:
                        ready = True
                        break
                    last_error = (
                        f"runner status HTTP {status_resp.status_code}: "
                        f"{status_resp.text[:200]}"
                    )
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_BOOT_POLL_INTERVAL_S)

        if not ready:
            raise RuntimeError(
                f"kiro fault e2e server did not become healthy within "
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


@pytest.fixture
def kiro_fault_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str, Path]]:
    """A runner-bound kiro-native session backed by the fault-injecting fake TUI.

    Parametrize indirectly with the fake's mode (``drop-first-enter`` or
    ``wrapped-title``).
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("kiro verdict-delivery e2e requires an isolated spawned server")
    mode = request.param
    server_tmp = tmp_path_factory.mktemp(f"e2e_ui_kiro_{mode.replace('-', '_')}")
    with _kiro_stack(server_tmp, _fake_kiro_source(mode)) as stack:
        yield stack


def _approve_from_web(page: Page, base_url: str, session_id: str, record_file: Path) -> None:
    """Drive the journey up to the divergence: web shows everything resolved."""
    page.goto(f"{base_url}/c/{session_id}")
    _ensure_chat_view(page)
    _send(page, "Run the setup command that needs shell approval.")

    _wait_for(
        lambda: _REQUEST_ID in _recorder_request_ids(record_file),
        timeout_s=120.0,
        message="Kiro never raised the ACP permission request",
    )
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_FIRST_CARD_TIMEOUT_MS)
    expect(card.get_by_text("Kiro", exact=False).first).to_be_visible()

    card.get_by_role("button", name="Approve").click()
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first).to_be_visible(
        timeout=30_000
    )
    _wait_for(
        lambda: not _pending_elicitations(base_url, session_id),
        message="the approved elicitation stayed pending on the server",
    )


def _assert_verdict_reached_kiro(record_file: Path, bridge_dir: Path) -> None:
    """The regression guard: the accepted verdict must actually land in Kiro.

    Red on the buggy build: no ACP response is ever recorded and the pane stays
    on ``requires approval`` while the web UI already reports the approval
    resolved with zero pending elicitations.
    """
    deadline = time.monotonic() + _DELIVERY_CONFIRM_TIMEOUT_S
    while time.monotonic() < deadline:
        if _REQUEST_ID in _recorder_response_ids(record_file):
            _wait_for(
                lambda: "requires approval" not in _kiro_pane_text(bridge_dir),
                timeout_s=15.0,
                message="Kiro recorded the verdict but the approval prompt is still on the pane",
            )
            return
        time.sleep(0.5)
    raise AssertionError(
        "web reported the approval resolved (card responded, zero pending "
        f"elicitations) but the verdict never reached Kiro: no ACP response for "
        f"{_REQUEST_ID!r} within {_DELIVERY_CONFIRM_TIMEOUT_S:.0f}s; "
        "the Kiro pane still shows:\n" + _kiro_pane_text(bridge_dir)
    )


@pytest.mark.timeout(600)
@pytest.mark.parametrize("kiro_fault_session", ["drop-first-enter"], indirect=True)
def test_kiro_web_approval_confirms_tmux_verdict_delivery(
    kiro_fault_session: tuple[str, str, Path],
    page: Page,
) -> None:
    """An Enter that Kiro drops must be retried, not reported as delivered."""
    from omnigent.harnesses.kiro_native.bridge import acp_record_path

    base_url, session_id, bridge_dir = kiro_fault_session
    record_file = acp_record_path(bridge_dir)

    _approve_from_web(page, base_url, session_id, record_file)

    # Keep the terminal view on screen: it shows the pane the user sees while
    # web already claims all is resolved.
    _select_view_mode(page, "Terminal")
    page.wait_for_timeout(2_000)

    _assert_verdict_reached_kiro(record_file, bridge_dir)


@pytest.mark.timeout(600)
@pytest.mark.parametrize("kiro_fault_session", ["wrapped-title"], indirect=True)
def test_kiro_wrapped_approval_title_still_gets_the_verdict(
    kiro_fault_session: tuple[str, str, Path],
    page: Page,
) -> None:
    """A wrapped 80-column tool title must not abort verdict delivery."""
    from omnigent.harnesses.kiro_native.bridge import acp_record_path

    base_url, session_id, bridge_dir = kiro_fault_session
    record_file = acp_record_path(bridge_dir)

    _approve_from_web(page, base_url, session_id, record_file)

    _select_view_mode(page, "Terminal")
    page.wait_for_timeout(2_000)

    _assert_verdict_reached_kiro(record_file, bridge_dir)
