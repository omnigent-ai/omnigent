"""E2E: every claude-native permission mode the composer offers must apply.

Reported journey: a user opens a Claude Code (claude-native) session, opens
the composer's permission-mode picker, and selects "Auto". The switch never
takes effect -- the composer surfaces a "Could not switch to auto mode" error
and the mode stays where it was. The picker offers "Auto" as a switchable
mode, but a running Claude Code session's shift+tab cycle only reaches
``default`` / ``acceptEdits`` / ``plan``; ``auto`` is not in the cycle, so the
runner exhausts its presses and the switch fails.

What this test drives
---------------------
The reported journey against a live, terminal-first claude-native session: open
the session, open the composer permission picker, and switch into *every* mode
the picker offers. The user-facing invariant the bug violates is simple -- a
mode the picker offers must actually apply when selected: the composer must not
show a switch error and the chip must update to the chosen mode.

* Buggy build: the picker offers "Auto" but selecting it errors out and the
  chip never becomes "Auto" -- this test FAILS on that mode.
* Fixed build: either "Auto" is no longer offered as switchable, or it actually
  applies -- every offered mode switches cleanly and this test PASSES.

The real ``claude`` CLI boots against a mock anthropic provider written into the
rig's isolated ``OMNIGENT_CONFIG_HOME`` (no live credentials), so the runner
drives Claude Code's genuine shift+tab cycle -- the same path the bug fires on.
The rig mirrors ``test_claude_native_slow_ready_first_prompt``: a dedicated
server + runner pair with their own ``HOME`` / ``OMNIGENT_CONFIG_HOME``.
"""

from __future__ import annotations

import contextlib
import importlib.util
import logging
import os
import secrets
import shutil
import signal
import socket
import subprocess
import sys
import sysconfig
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import _create_native_claude_session
from tests.e2e_ui.messages.test_message_render_parity import (
    _ensure_chat_view,
    _select_view_mode,
)

_log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).resolve().parents[3]


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, object]) -> dict[str, object]:
    """Film the sync ``page`` journey when a record dir is requested.

    The conftest's ``OMNIGENT_E2E_RECORD_DIR`` auto-injection only patches the
    async API, so the sync ``page`` fixture this test drives needs the record
    dir threaded in explicitly to capture the reproduction.
    """
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if record_dir:
        return {**browser_context_args, "record_video_dir": record_dir}
    return browser_context_args


# Boot budget for the spawned server + runner pair.
_HEALTH_TIMEOUT_S = 60.0
# claude-native auto-launch of the terminal + WS attach.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# A permission switch round-trips through the runner's shift+tab cycle; give
# the whole retry-until-ready gate room while the Claude TUI finishes booting.
_READY_TIMEOUT_S = 120.0
_SWITCH_TIMEOUT_MS = 30_000
# The composer sets the chosen mode optimistically, then rolls it back if the
# runner rejects the switch. Wait at least this long for that round-trip to
# resolve before judging the outcome, so a transient optimistic value can't be
# read as success.
_SWITCH_RESOLVE_MS = 12_000

_TERMINAL_VIEW = '[data-testid="terminal-view"]'

# Model baked into the rig's mock anthropic provider config (matches
# conftest._CLAUDE_MOCK_MODEL).
_CLAUDE_MOCK_MODEL = "claude-sonnet-4-20250514"

# Human labels the composer chip renders per mode value. Mirrors
# ``CLAUDE_NATIVE_PERMISSION_MODES`` in web/src/lib/claudePermissionMode.ts.
_MODE_LABELS = {
    "default": "Manual",
    "auto": "Auto",
    "acceptEdits": "Accept edits",
    "plan": "Plan",
}
# Canonical switch order; reachable control modes first so the reported
# failure (auto) surfaces only after the working modes are demonstrated.
_MODE_ORDER = ["plan", "acceptEdits", "default", "auto"]


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


# Proxy-blind client: CI forces an egress proxy via HTTP(S)_PROXY env vars
# that must not intercept loopback requests to the spawned server.
_client = httpx.Client(trust_env=False)

for _var in ("NO_PROXY", "no_proxy"):
    os.environ[_var] = ",".join(filter(None, [os.environ.get(_var, ""), "127.0.0.1,localhost"]))


def _no_proxy_env() -> dict[str, str]:
    env = os.environ.copy()
    for var in ("NO_PROXY", "no_proxy"):
        existing = env.get(var, "")
        env[var] = ",".join(filter(None, [existing, "127.0.0.1,localhost"]))
    return env


def _rig_python(work: Path) -> str:
    """Build an interpreter whose isolated mode can import this checkout.

    The claude-native bridge invokes its hook scripts as
    ``<runner python> -I -m omnigent...``; ``-I`` drops ``PYTHONPATH``, so on
    the CI worktree layout (checkout importable only via ``PYTHONPATH``) every
    hook dies with ``ModuleNotFoundError``. A dedicated venv whose
    ``site-packages`` carries a ``.pth`` naming the checkout (plus the parent
    environment's site-packages for dependencies) survives ``-I``.
    """
    venv_dir = work / "rig-venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(venv_dir)],
        check=True,
        capture_output=True,
    )
    site_packages = next((venv_dir / "lib").glob("python*/site-packages"))
    parent_purelib = sysconfig.get_paths()["purelib"]
    roots = [str(_REPO_ROOT)]
    for pkg in ("omnigent_client", "omnigent_ui_sdk"):
        spec = importlib.util.find_spec(pkg)
        if spec is not None and spec.origin:
            root = str(Path(spec.origin).resolve().parents[1])
            if root not in roots:
                roots.append(root)
    (site_packages / "omnigent_rig.pth").write_text("\n".join([*roots, parent_purelib]) + "\n")
    return str(venv_dir / "bin" / "python")


@pytest.fixture
def claude_permission_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """A live claude-native session whose real Claude Code CLI can be switched.

    Spawns a dedicated server + runner with an isolated ``HOME`` /
    ``OMNIGENT_CONFIG_HOME`` carrying a mock anthropic provider (so the real
    ``claude`` boots against the mock LLM without live credentials), then
    creates the same claude-native wrapper session ``omnigent claude`` ships.

    :returns: ``(base_url, session_id)``.
    """
    if shutil.which("tmux") is None:
        pytest.skip("tmux is required for the claude-native terminal rig")
    if shutil.which("claude") is None:
        pytest.skip("claude CLI is required for the claude-native permission rig")

    work = tmp_path_factory.mktemp("claude_permission")
    config_home = work / "config-home"
    home_dir = work / "home"
    artifacts = work / "artifacts"
    for path in (config_home, home_dir, artifacts):
        path.mkdir(parents=True, exist_ok=True)
    rig_python = _rig_python(work)

    (config_home / "config.yaml").write_text(
        "providers:\n"
        "  mock-claude:\n"
        "    kind: key\n"
        "    default: [anthropic]\n"
        "    anthropic:\n"
        f'      base_url: "{mock_llm_server_url}"\n'
        '      api_key: "mock-key"\n'
        "      models:\n"
        f"        default: {_CLAUDE_MOCK_MODEL}\n"
    )

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    binding_token = secrets.token_urlsafe(32)

    from omnigent.runner.identity import token_bound_runner_id

    runner_id = token_bound_runner_id(binding_token)

    shared_env = {
        **_no_proxy_env(),
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "HOME": str(home_dir),
        # Force the mock provider even if the CI env carries a real LLM_API_KEY:
        # the rig must drive Claude Code's own cycle, not a live gateway.
        "LLM_API_KEY": "",
    }
    server_env = {**shared_env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": binding_token}
    runner_env = {
        **shared_env,
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": base_url,
    }

    server_log = work / "server.log"
    runner_log = work / "runner.log"
    server_handle = server_log.open("w")
    runner_handle = runner_log.open("w")
    server_proc: subprocess.Popen[bytes] | None = None
    runner_proc: subprocess.Popen[bytes] | None = None
    session_id: str | None = None
    try:
        server_proc = subprocess.Popen(
            [
                rig_python,
                "-m",
                "omnigent.cli",
                "server",
                "--host",
                "127.0.0.1",
                "--port",
                str(port),
                "--database-uri",
                f"sqlite:///{work}/test.db",
                "--artifact-location",
                str(artifacts),
            ],
            env=server_env,
            stdout=server_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )
        runner_proc = subprocess.Popen(
            [rig_python, "-m", "omnigent.runner._entry"],
            env=runner_env,
            stdout=runner_handle,
            stderr=subprocess.STDOUT,
            cwd=str(_REPO_ROOT),
        )

        deadline = time.monotonic() + _HEALTH_TIMEOUT_S
        online = False
        while time.monotonic() < deadline:
            if server_proc.poll() is not None or runner_proc.poll() is not None:
                break
            with contextlib.suppress(httpx.HTTPError):
                if _client.get(f"{base_url}/health", timeout=2).status_code == 200:
                    status = _client.get(f"{base_url}/v1/runners/{runner_id}/status", timeout=2)
                    if status.status_code == 200 and status.json().get("online"):
                        online = True
                        break
            time.sleep(0.5)
        if not online:
            raise RuntimeError(
                "claude permission rig did not come online within "
                f"{_HEALTH_TIMEOUT_S:.0f}s.\nServer log:\n{server_log.read_text()[-3000:]}\n"
                f"Runner log:\n{runner_log.read_text()[-3000:]}"
            )

        session_id = _create_native_claude_session(base_url, runner_id)
        yield (base_url, session_id)
    finally:
        if session_id is not None:
            with contextlib.suppress(httpx.HTTPError):
                _client.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        for proc in (runner_proc, server_proc):
            if proc is not None and proc.poll() is None:
                proc.send_signal(signal.SIGTERM)
        for proc in (runner_proc, server_proc):
            if proc is not None:
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
        server_handle.close()
        runner_handle.close()


def _chip(page: Page) -> Locator:
    return page.get_by_test_id("composer-permission-chip")


def _switch_error(page: Page) -> Locator:
    """The composer's inline error the failed switch surfaces to the user."""
    return page.get_by_text("Could not switch to")


_OPTION_SEL = '[data-testid^="composer-permission-option-"]'


def _menu(page: Page) -> Locator:
    return page.get_by_test_id("composer-permission-menu")


def _open_menu(page: Page) -> Locator:
    """Open the permission dropdown, retrying until its options have rendered.

    The Radix trigger toggles the menu, so a click landing during the previous
    open/close animation is swallowed and leaves it shut. Always click to open
    (never trust a menu already on screen -- it may be the prior selection's
    menu mid-close) and gate on the first option being *visible*, so callers
    never read a menu that is still mounting or already closing.
    """
    chip = _chip(page)
    expect(chip).to_be_visible(timeout=_SWITCH_TIMEOUT_MS)
    menu = _menu(page)
    first_option = menu.locator(_OPTION_SEL).first
    for _ in range(8):
        expect(chip).to_be_enabled(timeout=_SWITCH_TIMEOUT_MS)
        chip.click()
        try:
            expect(first_option).to_be_visible(timeout=3_000)
            return menu
        except AssertionError:
            page.wait_for_timeout(500)
    expect(first_option).to_be_visible(timeout=5_000)
    return menu


def _close_menu(page: Page) -> None:
    first_option = _menu(page).locator(_OPTION_SEL).first
    if first_option.count() and first_option.is_visible():
        page.keyboard.press("Escape")
        expect(first_option).to_be_hidden(timeout=5_000)


def _offered_modes(page: Page) -> list[str]:
    """Mode values the picker currently offers (from the option testids)."""
    for _ in range(5):
        menu = _open_menu(page)
        testids = menu.locator(_OPTION_SEL).evaluate_all(
            "els => els.map(e => e.getAttribute('data-testid'))"
        )
        _close_menu(page)
        if testids:
            return [tid.rsplit("-", 1)[-1] for tid in testids]
        page.wait_for_timeout(500)
    return []


def _select_mode(page: Page, value: str) -> None:
    """Open the picker and click *value*, retrying the whole open+click.

    The composer re-renders on live state (model info, a settling switch),
    which re-mounts the dropdown's items; a click racing that re-mount detaches
    the item. Reopening and clicking again lands once the render settles.
    """
    option_id = f"composer-permission-option-{value}"
    for attempt in range(5):
        menu = _open_menu(page)
        try:
            menu.get_by_test_id(option_id).click(timeout=5_000)
            return
        except PlaywrightTimeoutError:
            if attempt == 4:
                raise
            _close_menu(page)
            page.wait_for_timeout(750)


def _settled_chip_label(page: Page) -> str | None:
    return _chip(page).get_attribute("aria-label")


def _banner_text(page: Page) -> str:
    banner = _switch_error(page)
    if banner.count() and banner.first.is_visible():
        return banner.first.inner_text()
    return "<none>"


def _wait_terminal_ready(page: Page) -> None:
    """Drive a control-mode switch until it *settles* applied, gating on boot.

    Switches to ``plan`` -- a mode Claude Code can always reach -- and checks
    the chip once the optimistic-then-maybe-rollback round-trip has resolved.
    Retries until the chip stays on Plan, absorbing the terminal's boot latency
    without a blind sleep; once a control switch settles, the runner's shift+tab
    machinery is live and a later failure can be trusted to be mode-specific.
    """
    deadline = time.monotonic() + _READY_TIMEOUT_S
    while True:
        _select_mode(page, "plan")
        page.wait_for_timeout(_SWITCH_RESOLVE_MS)
        if _settled_chip_label(page) == "Permission mode: Plan":
            return
        if time.monotonic() >= deadline:
            raise AssertionError(
                "Claude terminal never became switch-ready: 'plan' would not "
                f"apply (chip {_settled_chip_label(page)!r}, "
                f"composer error {_banner_text(page)!r})"
            )
        page.wait_for_timeout(1_000)


@pytest.mark.timeout(400)
def test_claude_native_permission_modes_all_apply(
    page: Page,
    claude_permission_session: tuple[str, str],
) -> None:
    """Every permission mode the composer offers must apply when selected.

    Journey (the reported one): open a claude-native session, open the composer
    permission picker, and switch into each offered mode. While the bug is live
    the picker offers "Auto", but selecting it errors ("Could not switch to auto
    mode") and the chip never becomes "Auto" -- so this test fails on that mode.
    The user-facing contract is that a mode the picker offers actually applies.
    """
    base_url, session_id = claude_permission_session

    page.goto(f"{base_url}/c/{session_id}")

    # Terminal-first session: wait for the runner to launch and attach the
    # Claude Code terminal before touching the permission control.
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _select_view_mode(page, "Terminal")
    terminal = page.locator(_TERMINAL_VIEW).last
    expect(terminal).to_have_attribute(
        "data-state", "connected", timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    _ensure_chat_view(page)

    _wait_terminal_ready(page)

    offered = _offered_modes(page)
    _log.info("composer offers switchable permission modes: %s", offered)
    assert offered, "composer permission picker offered no modes"

    for value in [m for m in _MODE_ORDER if m in offered]:
        expected = f"Permission mode: {_MODE_LABELS.get(value, value)}"
        _select_mode(page, value)
        # The composer applies the mode optimistically and rolls it back if the
        # runner rejects the switch, so judge the *settled* state: wait out the
        # round-trip, then require the chip to stay on the chosen mode.
        page.wait_for_timeout(_SWITCH_RESOLVE_MS)
        try:
            expect(_chip(page)).to_have_attribute("aria-label", expected, timeout=3_000)
        except AssertionError:
            pytest.fail(
                f"Permission mode {value!r} is offered in the composer but did "
                f"not apply: chip settled on {_settled_chip_label(page)!r} "
                f"(expected {expected!r}); composer error: {_banner_text(page)!r}"
            )
