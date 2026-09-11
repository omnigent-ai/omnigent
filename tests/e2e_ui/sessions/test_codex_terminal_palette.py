r"""E2E: a Light Omnigent terminal appearance must reach TUI palette probes.

Runner-created session terminals run in a detached, private tmux server; the
SPA's Terminal view attaches through a tmux control-mode client that reports
its size but never its default foreground/background colors. Full-screen TUIs
(Codex among them) probe the terminal palette with OSC 10/11 during startup
and cache the answer for their input/menu surfaces. In this setup the probe
is answered by tmux itself: nothing while the pane is unattached, and hard
black (``rgb:0000/0000/0000``) once the control-mode client is attached —
never the palette the user picked in Settings → Appearance. A fresh Codex
session therefore boots believing the terminal is dark: its ``/theme`` picker
defaults to a dark theme and its input/menu surfaces render dark on the
browser's light terminal canvas.

Journey (all through the real SPA against a live server + runner + codex CLI):

1. Settings → Appearance: pin the app theme AND the terminal theme to Light.
2. Open a fresh runner-bound Codex session; switch to the Terminal view and
   wait for the live TUI (the browser is now attached to the session's tmux).
3. Open Codex's ``/theme`` picker — the user-visible symptom: its current /
   default theme is a dark one despite the Light terminal (journey footage;
   the picker is closed again without changing anything).
4. Open a Shell tab in the same session — a runner-created terminal attached
   by this same Light-appearance browser — and run the exact palette probe a
   TUI performs at startup: emit ``OSC 11 ?`` to the tty and read the reply.
5. Assert the contract the symptom depends on: the probe must see a reply,
   and the reported default background must be light. Today tmux answers
   black (or nothing at all before a client attaches), so this fails and
   pins the bug. Any fix that makes a fresh Codex session under a Light
   appearance render light input/menu surfaces must make this probe see a
   light background.

Codex's model backend is the in-process mock LLM server — no real credentials
or model calls are needed; the palette is cached during TUI startup regardless.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _HEALTH_POLL_INTERVAL_S,
    _HEALTH_TIMEOUT_S,
    _REPO_ROOT,
    _TEST_AGENT_YAML,
    _codex_cli_supports_mocked_app_server,
    _create_native_codex_session,
    _find_free_port,
    _write_mock_codex_provider_config,
    open_right_rail,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _TERMINAL_VIEW,
    _XTERM_INPUT,
    _open_terminal_view,
    _wait_terminal_connected,
)
from tests.e2e_ui.sessions.test_terminal_theme import (
    _open_appearance,
    _pick_app_theme,
    _pick_terminal_theme,
)

_log = logging.getLogger(__name__)

# Codex is launched on bind; after the terminal WS connects, give the TUI a
# generous settle so the banner + composer are up before /theme is typed.
_CODEX_BOOT_SETTLE_MS = 12_000

# The probe reads the tty until 3s of silence; the file appears shortly after.
_PROBE_REPLY_TIMEOUT_S = 45.0

# An OSC 11 color report: ``ESC ] 11 ; rgb:RRRR/GGGG/BBBB`` with 1–4 hex
# digits per channel (xterm scales shorter forms).
_OSC11_REPLY_RE = re.compile(
    r"\x1b\]11;rgb:([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})/([0-9a-fA-F]{1,4})"
)

# The palette probe a TUI app effectively performs at startup, runnable as a
# user command in a shell pane: ask the terminal for its default foreground
# and background (OSC 10/11), read the reply from the tty until it goes
# quiet, persist the raw bytes, and print a human-readable verdict line.
_PROBE_SCRIPT = """\
#!/usr/bin/env bash
out="$1"
exec </dev/tty
printf '\\033]10;?\\033\\\\\\033]11;?\\033\\\\' >/dev/tty
reply=""
for _ in $(seq 1 512); do
  IFS= read -r -t 3 -n 1 ch || break
  reply+="$ch"
done
printf '%s' "$reply" >"$out"
if [[ "$reply" == *"]11;rgb:"* ]]; then
  echo "PALETTE PROBE: terminal answered: $(printf '%s' "$reply" | cat -v)"
else
  echo "PALETTE PROBE: no reply - TUI apps fall back to their dark default"
fi
"""


@pytest.fixture
def codex_palette_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, float]]:
    """Spawn a codex-native session against the mock Responses server.

    Owns its own server + runner (not the shared ``live_server``) so the
    runner's ``OMNIGENT_CONFIG_HOME`` can be a writable dir holding a mock
    openai/responses provider — the shared runner's config home is read-only
    and gateway-only, which parks Codex on its sign-in screen. The runner
    auto-launches Codex in the session terminal on bind, routed to the mock
    LLM, so a real Codex TUI boots (and caches its palette) with no real
    credentials or model traffic.

    :param built_spa: Ensures the SPA bundle is on disk before the server boots.
    :param mock_llm_server_url: Session-scoped mock LLM (Responses) base URL.
    :param tmp_path_factory: Pytest temp path factory.
    :returns: ``(base_url, session_id, created_at)`` — *created_at* is the
        epoch time just before the session (and so its terminal tmux server)
        was created, for scoping the evidence pane dumps to this journey.
    """
    codex_path = shutil.which("codex")
    if codex_path is None:
        pytest.skip("codex CLI is required for the codex terminal-palette e2e")
    if not _codex_cli_supports_mocked_app_server(codex_path):
        pytest.skip("codex CLI >= 0.139.0 is required for the codex terminal-palette e2e")

    from omnigent.runner.identity import token_bound_runner_id

    server_tmp = tmp_path_factory.mktemp("e2e_ui_codex_palette_server")
    config_home = server_tmp / "config-home"
    source_codex_home = server_tmp / "source-codex-home"
    home_dir = server_tmp / "home"
    state_dir = server_tmp / "codex-native-state"
    artifact_dir = server_tmp / "artifacts"
    for path in (source_codex_home, home_dir, state_dir, artifact_dir):
        path.mkdir(parents=True, exist_ok=True)

    _write_mock_codex_provider_config(
        config_home, f"{mock_llm_server_url}/v1", model=_CODEX_MOCK_MODEL
    )

    port = _find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    log_path = server_tmp / "server.log"
    runner_log_path = server_tmp / "runner.log"
    db_path = server_tmp / "test.db"
    agent_yaml_path = server_tmp / "hello_world.yaml"
    agent_yaml_path.write_text(_TEST_AGENT_YAML, encoding="utf-8")

    import secrets as _secrets

    binding_token = _secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(state_dir),
        "CODEX_HOME": str(source_codex_home),
        "HOME": str(home_dir),
        # The server's own hello_world agent isn't used by this journey, but
        # point it at the mock too so nothing reaches a real provider.
        "OPENAI_BASE_URL": f"{mock_llm_server_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    log_handle = open(log_path, "w")  # noqa: SIM115 — closed in finally
    runner_log_handle = open(runner_log_path, "w")  # noqa: SIM115
    proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "omnigent.cli",
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

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
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
                    if status_resp.status_code == 200 and status_resp.json().get("online") is True:
                        ready = True
                        break
                    last_error = (
                        f"runner status HTTP {status_resp.status_code}: {status_resp.text[:200]}"
                    )
                else:
                    last_error = f"health HTTP {resp.status_code}: {resp.text[:200]}"
            except (httpx.ConnectError, httpx.ReadError, httpx.TimeoutException) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(_HEALTH_POLL_INTERVAL_S)

        if not ready:
            raise RuntimeError(
                f"codex-palette e2e server did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s on {base_url} (last_error={last_error}).\n"
                f"Server log:\n{log_path.read_text()[-3000:] if log_path.exists() else ''}\n"
                f"Runner log:\n"
                f"{runner_log_path.read_text()[-3000:] if runner_log_path.exists() else ''}"
            )

        created_at = time.time()
        session_id = _create_native_codex_session(base_url, runner_id, model=_CODEX_MOCK_MODEL)
        yield (base_url, session_id, created_at)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for child in (runner_proc, proc):
            if child is not None and child.poll() is None:
                child.send_signal(signal.SIGTERM)
                try:
                    child.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=5)
        runner_log_handle.close()
        log_handle.close()


def _focus_xterm(page: Page, terminal_view) -> None:
    """Focus a terminal's hidden xterm.js input textarea for keyboard input.

    :param page: The Playwright page.
    :param terminal_view: The ``terminal-view`` locator to type into.
    """
    xterm_input = terminal_view.locator(_XTERM_INPUT)
    expect(xterm_input).to_be_attached(timeout=30_000)
    xterm_input.focus()


def _open_new_shell(page: Page) -> None:
    """Open a Shell tab via the workspace rail's "+" → Shell menu.

    :param page: The Playwright page, on a ``/c/{id}`` session route.
    """
    open_right_rail(page)
    rail = page.get_by_role("complementary", name="Workspace")
    rail.get_by_role("button", name="Open new").click()
    page.get_by_role("menuitem", name=re.compile("Shell")).click()


def _connected_rail_terminal(page: Page):
    """Wait for the freshly opened Shell tab's xterm to mount + connect.

    :param page: The Playwright page, after :func:`_open_new_shell`.
    :returns: The connected shell terminal-view locator (rail-scoped).
    """
    rail = page.get_by_role("complementary", name="Workspace")
    terminal_view = rail.get_by_test_id("terminal-view").last
    expect(terminal_view).to_be_visible(timeout=60_000)
    expect(terminal_view).to_have_attribute("data-state", "connected", timeout=60_000)
    return terminal_view


def _dump_terminal_panes(out_dir: Path, since: float, label: str) -> None:
    """Best-effort ``capture-pane -e`` dumps of runner terminals for evidence.

    Runner-created terminals each own a private tmux server under the system
    temp dir; dump the pane contents (with SGR escapes) of every server
    created after *since* so the rendered TUI palette is machine-checkable
    alongside the browser video. Never fails the test.

    :param out_dir: Directory to write ``pane-*.txt`` dumps into.
    :param since: Only dump instance dirs modified at/after this epoch time.
    :param label: Journey-step label baked into the dump filenames.
    """
    with contextlib.suppress(Exception):
        out_dir.mkdir(parents=True, exist_ok=True)
        import tempfile

        root = Path(tempfile.gettempdir())
        for index, entry in enumerate(sorted(root.glob("omnigent-terminal-*"))):
            sock = entry / "tmux.sock"
            if not sock.exists() or entry.stat().st_mtime < since:
                continue
            with contextlib.suppress(Exception):
                dump = subprocess.run(
                    ["tmux", "-S", str(sock), "capture-pane", "-e", "-p"],
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                if dump.returncode == 0:
                    (out_dir / f"pane-{label}-{index}-{entry.name}.txt").write_text(
                        dump.stdout, encoding="utf-8"
                    )


def _channel_fraction(hex_value: str) -> float:
    """Scale an xterm 1–4 hex-digit color channel to the 0.0–1.0 range.

    :param hex_value: The channel's hex digits from an ``rgb:`` reply.
    :returns: The channel intensity as a fraction of its maximum.
    """
    return int(hex_value, 16) / (16 ** len(hex_value) - 1)


def _relative_luminance(red: str, green: str, blue: str) -> float:
    """Approximate relative luminance of an OSC color reply (0=black, 1=white).

    :param red: Red channel hex digits.
    :param green: Green channel hex digits.
    :param blue: Blue channel hex digits.
    :returns: Luminance in the 0.0–1.0 range.
    """
    return (
        0.2126 * _channel_fraction(red)
        + 0.7152 * _channel_fraction(green)
        + 0.0722 * _channel_fraction(blue)
    )


@pytest.mark.timeout(600)
def test_light_terminal_appearance_reaches_tui_palette_probes(
    page: Page,
    codex_palette_session: tuple[str, str, float],
    tmp_path: Path,
) -> None:
    """A TUI palette probe under a Light terminal must see a light background.

    Drives the reported journey — Light appearance, fresh Codex session,
    Terminal view, ``/theme`` — then runs the startup palette probe (OSC 11)
    as a user command in a Shell tab of the same session and asserts the
    terminal reports a *light* default background. Today tmux answers black
    (``rgb:0000/0000/0000``) or nothing, which is exactly why Codex caches a
    dark input/menu palette on a Light terminal.

    :param page: Playwright page fixture.
    :param codex_palette_session: ``(base_url, session_id, created_at)`` for
        a runner-bound codex-native session backed by the mock LLM.
    :param tmp_path: Pytest per-test temp dir (shared with the local runner).
    :returns: None.
    """
    base_url, session_id, created_at = codex_palette_session
    evidence_dir = Path(os.environ.get("OMNIGENT_E2E_RECORD_DIR") or tmp_path) / "pane-dumps"

    # Step 1 — the user pins a Light appearance: app theme AND terminal theme.
    _open_appearance(page, base_url)
    _pick_app_theme(page, "light")
    _pick_terminal_theme(page, "light")

    # Step 2 — open the fresh Codex session's Terminal view (browser attaches).
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    page.wait_for_timeout(_CODEX_BOOT_SETTLE_MS)

    # Step 3 — the user-visible symptom: open Codex's /theme picker. On the
    # buggy build its current/default theme is a dark one despite the Light
    # terminal (this lands in the journey footage and the pane dumps); close
    # it again with Escape so nothing is changed.
    codex_terminal = page.locator(_TERMINAL_VIEW).last
    expect(codex_terminal).to_have_attribute("data-terminal-theme", "light")
    _focus_xterm(page, codex_terminal)
    page.keyboard.type("/theme", delay=30)
    page.wait_for_timeout(1_500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(4_000)
    _dump_terminal_panes(evidence_dir, since=created_at, label="theme-picker")
    page.keyboard.press("Escape")
    page.wait_for_timeout(500)

    # Step 4 — run the startup palette probe as a user command in a Shell tab
    # of the same session (a runner-created terminal attached by this same
    # Light-appearance browser).
    probe_path = tmp_path / "palette_probe.sh"
    probe_path.write_text(_PROBE_SCRIPT, encoding="utf-8")
    probe_path.chmod(0o755)
    reply_path = tmp_path / "palette_reply.bin"

    _open_new_shell(page)
    shell_terminal = _connected_rail_terminal(page)
    expect(shell_terminal).to_have_attribute("data-terminal-theme", "light")
    page.wait_for_timeout(3_000)  # let the shell prompt come up
    _focus_xterm(page, shell_terminal)
    page.keyboard.type(f"bash {probe_path} {reply_path}", delay=15)
    page.keyboard.press("Enter")

    deadline = time.monotonic() + _PROBE_REPLY_TIMEOUT_S
    while time.monotonic() < deadline and not reply_path.exists():
        page.wait_for_timeout(500)
    # Keep the probe's printed verdict on screen in the footage for a beat.
    page.wait_for_timeout(2_000)
    _dump_terminal_panes(evidence_dir, since=created_at, label="probe")

    assert reply_path.exists(), (
        "the palette probe never completed in the session's shell terminal "
        f"(no reply file after {_PROBE_REPLY_TIMEOUT_S:.0f}s)"
    )

    raw_reply = reply_path.read_bytes().decode("utf-8", errors="replace")
    printable = raw_reply.replace("\x1b", "\\x1b")
    _log.info("palette probe raw reply: %r", raw_reply)

    # Step 5 — the palette contract behind the symptom. A TUI's startup probe
    # must (a) get an answer and (b) be told the background the user actually
    # sees — light. On the buggy build tmux itself answers rgb:0000/0000/0000
    # (or nothing before a client attaches), so Codex caches a dark palette.
    first_reply = _OSC11_REPLY_RE.search(raw_reply)
    assert first_reply is not None, (
        "the terminal never reported a default background to the pane's "
        "OSC 11 palette probe — TUI apps started in this terminal (Codex) "
        "cache their dark fallback palette even though the Omnigent "
        f"terminal appearance is Light (raw probe bytes: {printable!r})"
    )

    luminance = _relative_luminance(*first_reply.groups())
    assert luminance > 0.5, (
        "the terminal reports a dark default background "
        f"(OSC 11 → rgb:{first_reply.group(1)}/{first_reply.group(2)}/"
        f"{first_reply.group(3)}, luminance {luminance:.3f}) to TUI palette "
        "probes while the Omnigent terminal appearance is Light — Codex "
        "caches this dark palette for its input/menu surfaces "
        f"(raw probe bytes: {printable!r})"
    )
