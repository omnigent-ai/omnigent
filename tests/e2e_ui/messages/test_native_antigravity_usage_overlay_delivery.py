"""E2E (UI): a Chat-view prompt must survive an open agy ``/usage`` overlay.

With an Antigravity (``agy``) session, opening **Terminal View**,
running the ``/usage`` slash command, leaving its full-screen overlay on
screen, then switching back to **Chat** and sending a prompt loses the prompt.
The antigravity-native delivery path
(:func:`omnigent.antigravity_native_bridge.inject_user_message_via_tui`) keys
readiness on agy's statusline markers (``? for shortcuts`` idle /
``esc to cancel`` active); a full-screen overlay panel renders neither (agy >=
1.0.17 removed those hints from overlay panels — see the changelog embedded in
the agy binary), so the readiness gate burns its whole budget, the bracketed
paste is swallowed by the overlay, and the turn dies with ``Could not deliver
the turn to the agy TUI: agy did not render the pasted message in its input
box before submit``. Unlike claude-native (whose bridge reclaims an occupied
composer with a verified Escape — ``_restore_occupied_input`` in
``omnigent/claude_native_bridge.py``, guarded by
``test_native_claude_composer_delivers_into_an_occupied_tui``), the agy bridge
never restores prompt readiness, so the user's message is lost.

This test drives the reported user journey end-to-end in the real product:
a dedicated Omnigent server + runner, the real antigravity-native wrapper
session (runner auto-creates the agy terminal in a runner-owned tmux pane),
the real SPA in a real browser — Terminal view typing goes through the
embedded xterm, the Chat send goes through the web composer, and delivery
runs the real executor + tmux bracketed-paste bridge.

**The one substitution: the ``agy`` binary itself.** agy is OAuth-only (no
API-key mode; sign-in is an interactive Google browser flow), so CI cannot
bring the real binary to its prompt. The runner therefore launches a scripted
stand-in TUI (``_AGY_STUB_SOURCE``) resolved via ``PATH``, faithful to the two
observable agy states the bridge parses: the separator-framed composer with
the ``? for shortcuts`` footer, and a footer-less full-screen ``/usage``
overlay that swallows input until a bare Escape / ``q`` dismisses it. On a
machine with a signed-in agy the same journey reproduces against the real
binary. Mirrors the ``mocked_native_codex_session`` precedent (dedicated
server + shimmed harness binary) in ``tests/e2e_ui/conftest.py``.

Expected (post-fix) behavior, per the report: Omnigent restores prompt
readiness (e.g. a verified Escape) before injecting, so the prompt reaches
agy — the pane echoes it and no delivery error is recorded. Before the fix
this test fails: the transcript gains the delivery ExecutorError and the pane
still shows the overlay.
"""

from __future__ import annotations

import contextlib
import io
import json
import logging
import os
import shutil
import signal
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
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _find_free_port,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ensure_chat_view,
    _select_view_mode,
    _send,
)

_log = logging.getLogger(__name__)

_TERMINAL_VIEW = '[data-testid="terminal-view"]'
# xterm.js routes all keystrokes through a hidden helper <textarea>; focusing
# it and typing is how a user (and Playwright) drives the embedded TUI.
_XTERM_INPUT = ".xterm-helper-textarea"

# Dedicated server + runner boot (SPA is prebuilt via ``built_spa``).
_BOOT_TIMEOUT_S = 90.0
_BOOT_POLL_S = 0.5
# Auto-created agy terminal: tmux advert + stub TUI first paint + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
_PANE_READY_TIMEOUT_S = 90.0
# Opening the /usage overlay re-sends its keystrokes when a burst got split.
_OVERLAY_ATTEMPTS = 3
_OVERLAY_ATTEMPT_TIMEOUT_S = 20.0
# The un-fixed delivery burns its full 30s readiness budget + 5s paste-commit
# before erroring; a healthy delivery lands in a few seconds. 90s bounds both
# without racing a slow CI box.
_DELIVERY_TIMEOUT_S = 90.0

# Markers the stand-in TUI renders (mirroring real agy chrome).
_IDLE_FOOTER = "? for shortcuts"
_OVERLAY_MARKER = "Usage Statistics"
# The executor error the un-fixed bridge records on the turn (see
# ``AntigravityNativeExecutor._deliver``).
_DELIVERY_ERROR_NEEDLE = "Could not deliver the turn to the agy TUI"

# Scripted stand-in for the agy TUI. Renders the two observable agy states the
# delivery bridge parses; see the module docstring for the fidelity notes.
_AGY_STUB_SOURCE = '''#!/usr/bin/env python3
"""Scripted stand-in for the agy TUI (usage-overlay delivery reproduction).

Two states, mirroring agy 1.1.x chrome:

* Idle prompt: separator-framed composer (``> draft`` between long ``-``
  rules) with the ``? for shortcuts`` footer. Typed/pasted input lands in the
  draft (bracketed paste enabled); Enter submits it into the transcript;
  Enter on ``/usage`` opens the overlay.
* /usage overlay: full-screen panel with NEITHER statusline marker (agy >=
  1.0.17 dropped the hints from overlay panels). Swallows all input except a
  bare Escape or ``q``, which dismisses it back to the prompt.
"""
import os
import select
import sys
import tty

SEP = "\\u2500" * 44
IDLE_FOOTER = "  ? for shortcuts"
OVERLAY_LINES = [
    "Usage Statistics",
    "",
    "Plan: Google AI Pro",
    "",
    "Session usage",
    "  gemini-2.5-pro       12% of session quota",
    "  gemini-2.5-flash      3% of session quota",
    "",
    "Quota resets in 4h 12m",
    "",
    "press esc or q to close",
]


def draw(state, transcript, draft):
    out = ["\\x1b[2J\\x1b[H"]
    if state == "overlay":
        out.extend(line + "\\r\\n" for line in OVERLAY_LINES)
    else:
        out.extend(line + "\\r\\n" for line in transcript[-12:])
        out.append(SEP + "\\r\\n")
        out.append("> " + draft + "\\r\\n")
        out.append(SEP + "\\r\\n")
        out.append(IDLE_FOOTER + "\\r\\n")
    sys.stdout.write("".join(out))
    sys.stdout.flush()


def read_byte(timeout=None):
    fd = sys.stdin.fileno()
    if timeout is not None:
        ready, _, _ = select.select([fd], [], [], timeout)
        if not ready:
            return None
    data = os.read(fd, 1)
    return data if data else None


def read_csi():
    """Read one CSI sequence body after ESC-[ (final byte included)."""
    body = b""
    while True:
        byte = read_byte(0.25)
        if byte is None:
            return body
        body += byte
        if 0x40 <= byte[0] <= 0x7E:
            return body


def main():
    tty.setraw(sys.stdin.fileno())
    # Enable bracketed paste so ``tmux paste-buffer -p`` wraps the payload in
    # ESC[200~ / ESC[201~ markers, exactly as it does for the real agy.
    sys.stdout.write("\\x1b[?2004h")
    state = "prompt"
    transcript = ["Welcome to the Antigravity CLI."]
    draft = ""
    paste = None  # collecting bracketed-paste bytes when not None
    draw(state, transcript, draft)
    while True:
        byte = read_byte()
        if byte is None:
            return
        ch = byte[0]
        if ch == 0x1B:
            nxt = read_byte(0.05)
            if nxt == b"[":
                body = read_csi()
                if body == b"200~":
                    paste = b""
                elif body == b"201~":
                    if paste is not None and state == "prompt":
                        draft += paste.decode("utf-8", "replace").replace("\\r", " ")
                    paste = None  # an overlay swallows the whole paste
                # other CSI sequences (arrows, ...) are swallowed in any state
            elif state == "overlay":
                # Bare Escape dismisses the overlay (like real agy); on the
                # bare prompt it is a no-op.
                state = "prompt"
            draw(state, transcript, draft)
            continue
        if paste is not None:
            paste += byte
            continue
        if state == "overlay":
            if byte in (b"q", b"Q"):
                state = "prompt"
                draw(state, transcript, draft)
            continue  # the overlay swallows everything else
        if ch == 0x0D:  # Enter submits the draft
            line = draft.strip()
            draft = ""
            if line == "/usage":
                state = "overlay"
            elif line:
                transcript.append("> " + line)
        elif ch == 0x01:  # C-a (home) - no cursor model, no-op
            pass
        elif ch == 0x0B:  # C-k (kill to end) - the bridge clears via C-a + C-k
            draft = ""
        elif ch == 0x7F:  # backspace
            draft = draft[:-1]
        elif ch == 0x03:  # C-c
            return
        elif ch >= 0x20:
            draft += byte.decode("utf-8", "replace")
        draw(state, transcript, draft)


if __name__ == "__main__":
    main()
'''


def _write_agy_stub(shim_dir: Path) -> None:
    """Write the stand-in ``agy`` executable into *shim_dir*.

    :param shim_dir: Directory prepended to the runner's ``PATH`` so
        ``agy_binary_path()`` (``shutil.which("agy")``) resolves the stub.
    """
    shim_dir.mkdir(parents=True, exist_ok=True)
    stub = shim_dir / "agy"
    stub.write_text(_AGY_STUB_SOURCE, encoding="utf-8")
    stub.chmod(0o755)


def _antigravity_bundle() -> bytes:
    """Build the exact terminal-first bundle ``omnigent antigravity`` ships.

    Reuses :func:`omnigent.antigravity_native._materialize_antigravity_agent_spec`
    so the fixture never drifts from production. The spec carries no
    ``spec_version``, so a non-``config.yaml`` arcname routes it through the
    omnigent compat translator (mirrors ``_create_native_cursor_session``).

    :returns: Gzipped tarball bytes for the session-create upload.
    """
    from omnigent.antigravity_native import _materialize_antigravity_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_antigravity_agent_spec(Path(tmp))
        yaml_text = spec_path.read_text()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("antigravity-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def _create_antigravity_session(base_url: str, runner_id: str, workspace: Path) -> str:
    """Create + runner-bind an antigravity-native session (web-UI launch path).

    Binding triggers the runner's ``_auto_create_antigravity_terminal``, which
    launches ``agy`` (here: the PATH-shimmed stub) in a runner-owned tmux pane.

    :param base_url: Dedicated server base URL.
    :param runner_id: The token-bound runner id to bind.
    :param workspace: Session workspace directory (must exist runner-side).
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: ANTIGRAVITY_NATIVE_WRAPPER_VALUE,
    }
    metadata = {"labels": labels, "workspace": str(workspace)}
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps(metadata)},
        files={
            "bundle": (
                "antigravity-native-ui.tar.gz",
                _antigravity_bundle(),
                "application/gzip",
            )
        },
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    # Bind with a generous timeout (conftest's ``_bind_session_runner`` allows
    # 10s): binding an antigravity session runs the runner's terminal
    # auto-create, whose best-effort RPC cold-start polls port discovery for
    # ~20s when agy (here: the stub) exposes no connect-RPC port.
    patch = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=90.0,
    )
    patch.raise_for_status()
    return session_id


def _bridge_tmux_info(session_id: str) -> dict[str, str] | None:
    """Read the session's advertised tmux socket/target, if any.

    :param session_id: The session/conversation id (also the bridge id — this
        fixture stamps no explicit bridge-id label).
    :returns: ``{"socket_path": ..., "tmux_target": ...}`` or ``None``.
    """
    from omnigent.antigravity_native_bridge import bridge_dir_for_bridge_id

    advert = bridge_dir_for_bridge_id(session_id) / "tmux.json"
    if not advert.exists():
        return None
    return json.loads(advert.read_text(encoding="utf-8"))


def _pane_text(session_id: str) -> str:
    """Capture the agy terminal's tmux pane — the TUI's own screen.

    Read straight from tmux rather than from the SPA's xterm: the browser
    renders the terminal on a canvas (glyphs are not in the DOM), and the
    pane is anyway the surface the delivery bridge itself reads.

    :param session_id: The session/conversation id.
    :returns: The pane's visible text, or ``""`` before the terminal is
        advertised (or if the capture fails).
    """
    info = _bridge_tmux_info(session_id)
    if info is None:
        return ""
    proc = subprocess.run(
        ["tmux", "-S", info["socket_path"], "capture-pane", "-t", info["tmux_target"], "-p"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _delivery_error_item(base_url: str, session_id: str) -> str:
    """Return the transcript's TUI-delivery error text, or ``""``.

    :param base_url: Dedicated server base URL.
    :param session_id: The session/conversation id.
    :returns: The serialized item carrying the delivery ExecutorError, if any.
    """
    try:
        resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    except httpx.HTTPError:
        return ""
    if resp.status_code != 200:
        return ""
    for item in resp.json().get("items", []):
        text = json.dumps(item)
        if _DELIVERY_ERROR_NEEDLE in text:
            return text
    return ""


@pytest.fixture
def antigravity_overlay_session(
    built_spa: None,
    tmp_path_factory: pytest.TempPathFactory,
    request: pytest.FixtureRequest,
) -> Iterator[tuple[str, str]]:
    """A runner-bound antigravity-native session driving the stand-in agy TUI.

    Spawns a dedicated server + runner (mirroring ``mocked_native_codex_session``:
    the runner env must carry the PATH shim before it resolves the ``agy``
    binary, so the shared ``live_server`` runner cannot be reused), creates the
    production antigravity wrapper session, and waits until the auto-created
    tmux terminal shows the stub's idle composer.

    :returns: ``(base_url, session_id)``.
    """
    if request.config.getoption("--ui-base-url"):
        pytest.skip("antigravity overlay e2e requires an isolated spawned server")
    if shutil.which("tmux") is None:
        pytest.skip("antigravity overlay e2e needs `tmux` on PATH (runner-owned TUI pane)")

    server_tmp = tmp_path_factory.mktemp("e2e_ui_antigravity_overlay")
    shim_dir = server_tmp / "shim-bin"
    _write_agy_stub(shim_dir)
    workspace = server_tmp / "workspace"
    workspace.mkdir()
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")
    db_path = server_tmp / "test.db"
    artifact_dir = server_tmp / "artifacts"
    artifact_dir.mkdir()
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"

    import secrets as _secrets

    from omnigent.runner.identity import token_bound_runner_id

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        # The runner resolves the agy binary via ``shutil.which`` from ITS env;
        # the shim must win over any real (unauthenticated) agy on PATH.
        "PATH": f"{shim_dir}{os.pathsep}{os.environ.get('PATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    log_handle = open(log_path, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
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
                    status = httpx.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json()["online"] is True:
                        ready = True
                        break
                    last_error = f"runner status HTTP {status.status_code}: {status.text[:200]}"
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_BOOT_POLL_S)
        if not ready:
            raise RuntimeError(
                f"antigravity overlay e2e server did not become healthy within "
                f"{_BOOT_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
                f"Server log at {log_path}:\n"
                f"{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
                f"Runner log at {runner_log_path}:\n"
                f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
            )

        session_id = _create_antigravity_session(base_url, runner_id, workspace)

        # Wait for the runner's auto-created agy terminal: tmux advertised AND
        # the stub's idle composer painted (the bridge polls the same footer).
        pane_deadline = time.monotonic() + _PANE_READY_TIMEOUT_S
        pane = ""
        while time.monotonic() < pane_deadline:
            pane = _pane_text(session_id)
            if _IDLE_FOOTER in pane:
                break
            time.sleep(1.0)
        else:
            raise RuntimeError(
                f"stand-in agy TUI never reached its idle prompt within "
                f"{_PANE_READY_TIMEOUT_S:.0f}s; pane was:\n{pane}\n"
                f"Runner log tail:\n"
                f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
            )

        yield (base_url, session_id)
    finally:
        info = _bridge_tmux_info(session_id) if session_id else None
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for child, grace in ((runner_proc, 5), (proc, 10)):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=grace)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        # The runner-owned tmux server (and the stub inside it) outlives the
        # runner process; kill it so nothing leaks across tests.
        if info is not None:
            subprocess.run(
                ["tmux", "-S", info["socket_path"], "kill-server"],
                check=False,
                capture_output=True,
                timeout=10,
            )
        runner_log_handle.close()
        log_handle.close()


def _open_terminal_view(page: Page) -> None:
    """Switch the terminal-first session to its Terminal (TUI) view."""
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "Terminal")


def _wait_terminal_connected(page: Page) -> None:
    """Wait until the embedded xterm has attached to the live agy pane."""
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )


def _open_usage_overlay(page: Page, session_id: str) -> None:
    """Run ``/usage`` in the embedded TUI and block until the overlay renders.

    Without pane proof the journey would pass vacuously: keystrokes that never
    reached the TUI leave an ordinary composer, which of course accepts the
    later message. Keystrokes are re-sent while the overlay is verifiably
    absent (mirrors ``_occupy_tui_surface`` in the claude parity suite).

    :param page: The Playwright page, on the connected Terminal view.
    :param session_id: The session/conversation id (for tmux pane reads).
    :raises AssertionError: If the overlay never renders.
    """
    xterm_input = page.locator(_TERMINAL_VIEW).last.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    pane = ""
    for attempt in range(1, _OVERLAY_ATTEMPTS + 1):
        xterm_input.focus()
        page.keyboard.type("/usage", delay=30)
        page.keyboard.press("Enter")
        deadline = time.monotonic() + _OVERLAY_ATTEMPT_TIMEOUT_S
        while time.monotonic() < deadline:
            pane = _pane_text(session_id)
            if _OVERLAY_MARKER in pane:
                return
            page.wait_for_timeout(500)
        _log.info("/usage overlay not rendered (attempt %d/%d)", attempt, _OVERLAY_ATTEMPTS)
    raise AssertionError(
        f"the /usage overlay never rendered in the agy pane, so this journey "
        f"would prove nothing. Pane was:\n{pane}"
    )


@pytest.mark.timeout(600)
def test_chat_prompt_delivers_while_usage_overlay_is_open(
    page: Page,
    antigravity_overlay_session: tuple[str, str],
) -> None:
    """A Chat-view prompt must survive an open /usage overlay.

    Journey (verbatim from the report): open the session → Terminal View →
    run ``/usage`` and leave its overlay on screen → back to Chat → send a
    prompt. The prompt must reach agy (the pane echoes it after the bridge
    restores prompt readiness) and the turn must not die with the TUI-delivery
    ExecutorError. Before the fix, the readiness gate times out against the
    footer-less overlay, the paste is swallowed, the transcript records
    ``Could not deliver the turn to the agy TUI: agy did not render the pasted
    message in its input box before submit`` — and the Terminal view still
    shows the captured ``/usage`` screen.
    """
    base_url, session_id = antigravity_overlay_session
    marker = f"OVERLAY_PROBE_{uuid.uuid4().hex[:8]}"
    prompt = f"Summarize the repo. {marker}"

    page.goto(f"{base_url}/c/{session_id}")

    # Steps 2-5: Terminal View → /usage → leave the overlay on screen.
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _open_usage_overlay(page, session_id)

    # Steps 6-7: back to Chat, send a new prompt.
    _ensure_chat_view(page)
    _send(page, prompt)

    # The prompt must reach the TUI. Poll the pane for the echoed marker;
    # bail early if the turn instead records the delivery ExecutorError.
    delivered = False
    error_item = ""
    deadline = time.monotonic() + _DELIVERY_TIMEOUT_S
    while time.monotonic() < deadline:
        if marker in _pane_text(session_id):
            delivered = True
            break
        error_item = _delivery_error_item(base_url, session_id)
        if error_item:
            break
        page.wait_for_timeout(1000)

    # Step 7 of the report: switch back to Terminal View — post-fix it shows
    # the prompt delivered into the TUI; pre-fix it is still stuck on /usage.
    _select_view_mode(page, "Terminal")
    page.wait_for_timeout(2500)
    final_pane = _pane_text(session_id)
    _log.info("final agy pane:\n%s", final_pane)

    assert not error_item, (
        f"the Chat-view prompt was lost to the open /usage overlay: the turn "
        f"recorded the TUI-delivery error instead of delivering the message.\n"
        f"transcript item: {error_item}\nfinal pane:\n{final_pane}"
    )
    assert delivered and marker in final_pane, (
        f"the Chat-view prompt never reached the agy TUI within "
        f"{_DELIVERY_TIMEOUT_S:.0f}s (no delivery error was recorded either "
        f"— the turn is hung).\nfinal pane:\n{final_pane}"
    )
