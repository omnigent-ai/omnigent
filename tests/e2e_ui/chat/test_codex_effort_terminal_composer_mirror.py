r"""UI journey (nightly): a Codex terminal effort change reaches the composer.

When a user changes the model reasoning effort inside the embedded
Codex terminal (native mode), the chat session composer's effort control keeps
showing the OLD value. This test drives the reporter's journey on the live SPA
against a real ``codex`` CLI (mock LLM backend):

1. Open a native Codex chat session; note the reasoning effort the composer's
   configuration gear (``composer-config-effort``) shows.
2. Switch to the embedded Codex Terminal view (the live TUI over a WebSocket)
   and change the model reasoning effort there via the in-TUI ``/model`` picker.
3. Start a turn so the forwarder observes the change, then return to Chat view
   and inspect the composer's effort control.
4. The composer must now show the effort the terminal is on. While the bug is
   live it stays at the OLD effort (stale), so the final assertion times out.

Why it stays stale (the plumbing this journey exercises): an in-TUI ``/model``
writes ``model`` and ``model_reasoning_effort`` to the session's ``config.toml``
and emits NO ``thread/settings`` notification. The codex-native forwarder learns
config-only changes by re-reading ``config.toml`` at each ``turn/started`` --
which it does for the model and developer instructions, but not for the
reasoning effort, and it never calls the effort mirror on that path. So the
terminal effort change never becomes a ``session.reasoning_effort`` event and
the composer never updates. (The forwarder-level reproduction that fails on
``main`` today is ``tests/test_codex_effort_terminal_composer_mirror.py``;
this is its user-visible surface guard.)

Robustness: rather than hard-coding which effort the ``/model`` picker lands on,
the test reads the terminal's OWN source of truth after the picker action -- the
session ``config.toml``'s ``model_reasoning_effort`` -- and asserts the composer
matches THAT. The bug is exactly a divergence between the composer and the
terminal's real (config.toml) effort, so this contract holds regardless of the
picker's exact geometry. If the ``/model`` picker keystrokes below stop
changing the effort on a future codex build, the test fails early at
``_wait_for_config_effort_change`` (a driving problem, clearly distinguished
from the mirror bug under test) so the keystrokes can be retuned without
weakening the assertion.

Marked ``nightly``: it boots a real codex CLI and drives its TUI, exactly like
``tests/e2e_ui/messages/test_native_codex_render_parity.py``. codex-native turns
cannot complete in the repro sandbox, so this journey is validated in the
nightly e2e_ui pass; the executable reproduction is the forwarder-level test.
"""

from __future__ import annotations

import logging
import time
import uuid

import pytest
from playwright.sync_api import Page, expect

from omnigent.harnesses.codex_native.bridge import (
    bridge_dir_for_bridge_id,
    codex_home_for_bridge_dir,
)
from tests.e2e_ui.conftest import (
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)

# Reuse the native-codex render-parity driving helpers: both suites boot the
# same real codex CLI and drive the same embedded TUI / view toggle.
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
    _send,
    _turn_prompt,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _TERMINAL_READY_TIMEOUT_MS,
    _open_terminal_view,
    _type_into_tui,
    _wait_terminal_connected,
)

_log = logging.getLogger(__name__)

_EFFORT_GEAR = '[data-testid="composer-config-effort"]'
_CONFIG_GEAR = '[data-testid="composer-config-gear"]'
_CONFIG_MODAL = '[data-testid="composer-config-modal"]'


def _read_config_effort(session_id: str) -> str | None:
    """Read ``model_reasoning_effort`` from the session's Codex ``config.toml``.

    This is the terminal's own source of truth for the active reasoning effort
    -- what an in-TUI ``/model`` writes. In the e2e_ui harness the runner is
    in-process on this machine, so the file is directly readable.

    :param session_id: The session id (also the codex-native bridge id).
    :returns: The effort string (e.g. ``"low"``), or ``None`` when the key or
        file is not present yet.
    """
    config_path = codex_home_for_bridge_dir(bridge_dir_for_bridge_id(session_id)) / "config.toml"
    if not config_path.exists():
        return None
    for line in config_path.read_text(errors="replace").splitlines():
        stripped = line.strip()
        if stripped.startswith("model_reasoning_effort"):
            _, _, rhs = stripped.partition("=")
            return rhs.strip().strip('"').strip("'") or None
    return None


def _wait_for_config_effort_change(
    session_id: str, baseline: str | None, *, timeout_s: float = 30.0
) -> str:
    """Wait until ``config.toml``'s effort differs from *baseline* and return it.

    Confirms the in-TUI ``/model`` action actually changed the terminal's
    reasoning effort before the composer is inspected. A timeout here means the
    picker keystrokes did not land a change (a TUI-driving problem), NOT the
    composer-mirror bug under test.

    :param session_id: The session id (codex-native bridge id).
    :param baseline: The effort observed before the ``/model`` change.
    :param timeout_s: Max seconds to wait for the file to reflect the change.
    :returns: The new effort string the terminal is now on.
    :raises AssertionError: When the effort never changes within the budget.
    """
    deadline = time.monotonic() + timeout_s
    latest = baseline
    while time.monotonic() < deadline:
        latest = _read_config_effort(session_id)
        if latest and latest != baseline:
            return latest
        time.sleep(0.5)
    raise AssertionError(
        "in-TUI /model did not change model_reasoning_effort in config.toml "
        f"(baseline={baseline!r}, still {latest!r}); the picker keystrokes need "
        "retuning for this codex build -- this is a TUI-driving issue, not the "
        "composer-mirror bug under test"
    )


def _change_effort_in_tui(page: Page) -> None:
    """Change the reasoning effort in the embedded Codex TUI via ``/model``.

    Drives codex's ``/model`` popup (the same command the routing suite drives):
    open it, keep the current model, and pick a different reasoning effort. The
    caller confirms the change landed by reading ``config.toml`` (see
    :func:`_wait_for_config_effort_change`), so this only needs to move the
    effort selection off its current value.

    :param page: The Playwright page, on the connected Terminal view.
    """
    # Open the model/effort picker.
    _type_into_tui(page, "/model")
    # Give codex's picker a beat to render before navigating it.
    page.wait_for_timeout(1_500)
    # Keep the current model (Enter), which advances codex's picker to its
    # reasoning-effort step; then move off the current effort and confirm.
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)
    page.keyboard.press("ArrowDown")
    page.wait_for_timeout(300)
    page.keyboard.press("Enter")
    page.wait_for_timeout(500)


@pytest.mark.nightly
@pytest.mark.timeout(360)
def test_codex_terminal_effort_change_reaches_composer(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A ``/model`` effort change in the terminal must update the composer gear.

    Reproduces the stale-composer bug on the live SPA: after changing the reasoning effort in
    the embedded Codex terminal, the chat composer's effort control must show
    the terminal's new effort. On ``main`` the forwarder never mirrors a
    config-only terminal effort change, so the composer stays stale and the
    final assertion times out.

    :param page: Playwright page fixture.
    :param native_codex_mock_session: ``(base_url, session_id)`` for a live
        native Codex session (mock LLM backend).
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    """
    base_url, session_id = native_codex_mock_session
    _log.info("native-codex mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _log.info("Codex TUI attached (terminal-view connected)")

    # --- Baseline: the composer's effort control on the launch effort. -------
    _ensure_chat_view(page)
    gear = page.locator(_CONFIG_GEAR)
    expect(gear).to_be_visible(timeout=_TERMINAL_READY_TIMEOUT_MS)
    gear.click()
    expect(page.locator(_CONFIG_MODAL)).to_be_visible(timeout=15_000)
    effort_control = page.locator(_EFFORT_GEAR)
    # The effort row is catalog-gated: a launch model the codex catalog does
    # not list (this fixture pins a mock-provider model) renders no effort
    # control until the in-TUI ``/model`` lands the session on a catalog model.
    # The baseline is whatever the composer shows now -- possibly nothing.
    baseline_composer_effort = (
        (effort_control.inner_text() or "").strip() if effort_control.is_visible() else ""
    )
    baseline_config_effort = _read_config_effort(session_id)
    _log.info(
        "baseline effort: composer=%r config.toml=%r",
        baseline_composer_effort,
        baseline_config_effort,
    )
    # Close the config modal before driving the terminal.
    page.keyboard.press("Escape")

    # --- Change the reasoning effort inside the embedded Codex terminal. -----
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _change_effort_in_tui(page)
    new_config_effort = _wait_for_config_effort_change(session_id, baseline_config_effort)
    _log.info("terminal effort changed in config.toml: %r", new_config_effort)

    # --- Start a turn so the forwarder observes the config change. -----------
    nonce = uuid.uuid4().hex[:8]
    user_marker = f"usr-effort-{nonce}"
    assistant_token = f"ast-effort-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=user_marker,
        match=user_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    _ensure_chat_view(page)
    _send(page, _turn_prompt(1, user_marker, assistant_token))
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    # --- The composer's effort control must now match the terminal. ----------
    gear = page.locator(_CONFIG_GEAR)
    expect(gear).to_be_visible(timeout=30_000)
    gear.click()
    expect(page.locator(_CONFIG_MODAL)).to_be_visible(timeout=15_000)
    effort_control = page.locator(_EFFORT_GEAR)
    expect(effort_control).to_be_visible(timeout=15_000)

    # The composer must reflect the effort the user set in the
    # terminal. While the bug is live it stays on the launch effort, so this
    # times out; after the fix it shows the terminal's new effort.
    expect(effort_control).to_contain_text(new_config_effort, timeout=30_000)
    assert new_config_effort != baseline_composer_effort, (
        "test setup: the terminal effort change must move off the composer's "
        f"baseline effort ({baseline_composer_effort!r}) to prove the mirror"
    )


def _run_mock_turn(page: Page, mock_llm_server_url: str, turn: int) -> None:
    """Complete one mock-LLM chat turn from the Chat view.

    Each turn drives the forwarder's ``turn/started`` (the config re-read
    point under test) and completes against the mock backend so the next
    composer inspection sees a settled session.

    :param page: The Playwright page.
    :param mock_llm_server_url: Session-scoped mock LLM server base URL.
    :param turn: Turn ordinal (unique prompt markers per turn).
    """
    nonce = uuid.uuid4().hex[:8]
    user_marker = f"usr-effort-{turn}-{nonce}"
    assistant_token = f"ast-effort-{turn}-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=user_marker,
        match=user_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")
    _ensure_chat_view(page)
    _send(page, _turn_prompt(turn, user_marker, assistant_token))
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)


def _wait_for_config_effort(session_id: str, expected: str, *, timeout_s: float = 30.0) -> None:
    """Wait until ``config.toml``'s effort equals *expected*.

    A timeout means the executor's ``write_codex_config_effort`` mirror never
    landed the composer-picked effort in the terminal's source of truth — the
    exact gap that lets a forwarder reconnect revert the pick.

    :param session_id: The session id (codex-native bridge id).
    :param expected: The composer-picked effort the file must adopt.
    :param timeout_s: Max seconds to wait.
    :raises AssertionError: When the file never adopts *expected*.
    """
    deadline = time.monotonic() + timeout_s
    latest: str | None = None
    while time.monotonic() < deadline:
        latest = _read_config_effort(session_id)
        if latest == expected:
            return
        time.sleep(0.5)
    raise AssertionError(
        f"config.toml never adopted the composer-picked effort {expected!r} "
        f"(still {latest!r}): the executor did not mirror the "
        "thread/settings/update effort into the terminal's config, so a "
        "forwarder reconnect would revert the composer's pick"
    )


def _open_config_modal(page: Page) -> None:
    """Open the composer configuration gear modal (idempotent per call).

    :param page: The Playwright page, on the Chat view.
    """
    gear = page.locator(_CONFIG_GEAR)
    expect(gear).to_be_visible(timeout=30_000)
    gear.click()
    expect(page.locator(_CONFIG_MODAL)).to_be_visible(timeout=15_000)


@pytest.mark.nightly
@pytest.mark.timeout(480)
def test_composer_effort_pick_survives_terminal_turns(
    page: Page,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A composer-gear effort pick applies and is not reverted by later turns.

    The counterpart journey to
    :func:`test_codex_terminal_effort_change_reaches_composer`, in the other
    direction: the user picks a reasoning effort in the chat composer's
    configuration gear, and subsequent terminal turns must not roll it back.

    Why this can regress: a composer pick is applied to the live thread via
    ``thread/settings/update`` — which does NOT touch the session's
    ``config.toml``, the file the forwarder's effort mirror treats as source of
    truth. The executor therefore mirrors the applied effort into
    ``config.toml`` (``write_codex_config_effort``), exactly as it does for a
    web model pick. Without that write, ``config.toml`` keeps the stale
    pre-pick effort and a fresh forwarder state (thread resume / reconnect)
    re-reads and re-posts it, silently reverting the composer while the live
    thread still runs the picked effort.

    Journey:

    1. Change the effort in the embedded terminal first (same driving as the
       mirror journey) and run a turn — this lands the session on a known
       terminal effort and makes the composer's effort row render.
    2. Pick a DIFFERENT effort in the composer gear and Save; the gear must
       show the pick (the "works" half of the requirement).
    3. Run a turn so the executor applies the pick; the terminal's
       ``config.toml`` must adopt it (fails while the mirror write is absent).
    4. Run one more turn (another ``turn/started`` config re-read) and verify
       the composer still shows the pick — not the pre-pick effort.
    """
    base_url, session_id = native_codex_mock_session
    _log.info("native-codex mock session ready: base_url=%s session_id=%s", base_url, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)

    # --- 1. Land the session on a terminal-set effort (renders the row). -----
    baseline_config_effort = _read_config_effort(session_id)
    _change_effort_in_tui(page)
    terminal_effort = _wait_for_config_effort_change(session_id, baseline_config_effort)
    _log.info("terminal effort landed in config.toml: %r", terminal_effort)
    _run_mock_turn(page, mock_llm_server_url, 1)

    # The composer mirrors the terminal effort (the already-guarded direction).
    _open_config_modal(page)
    effort_control = page.locator(_EFFORT_GEAR)
    expect(effort_control).to_be_visible(timeout=15_000)
    expect(effort_control).to_contain_text(terminal_effort, timeout=30_000)

    # --- 2. Pick a DIFFERENT effort in the composer gear and Save. -----------
    effort_control.click()
    options = page.locator('[role="option"][data-effort-level]')
    expect(options.first).to_be_visible(timeout=15_000)
    picked = ""
    for i in range(options.count()):
        level = options.nth(i).get_attribute("data-effort-level") or ""
        # Skip substring-related rungs ("high" ⊂ "xhigh") so the final
        # contain_text assertions cannot false-positive on the old value.
        if (
            level
            and level != terminal_effort
            and level not in terminal_effort
            and terminal_effort not in level
        ):
            picked = level
            break
    assert picked, (
        f"no selectable composer effort differs from the terminal's {terminal_effort!r}; "
        "cannot exercise a composer-initiated change"
    )
    _log.info("picking composer effort: %r (was %r)", picked, terminal_effort)
    page.locator(f'[role="option"][data-effort-level="{picked}"]').click()
    page.get_by_test_id("composer-config-save").click()
    expect(page.locator(_CONFIG_MODAL)).to_be_hidden(timeout=15_000)

    # The pick works: reopening the gear shows the composer-picked effort.
    _open_config_modal(page)
    expect(page.locator(_EFFORT_GEAR)).to_contain_text(picked, timeout=30_000)
    page.keyboard.press("Escape")
    expect(page.locator(_CONFIG_MODAL)).to_be_hidden(timeout=15_000)

    # --- 3. A turn applies the pick; config.toml must adopt it. --------------
    _run_mock_turn(page, mock_llm_server_url, 2)
    _wait_for_config_effort(session_id, picked)
    _log.info("composer-picked effort mirrored into config.toml: %r", picked)

    # --- 4. Another terminal turn must not revert the composer's pick. -------
    _run_mock_turn(page, mock_llm_server_url, 3)
    _open_config_modal(page)
    effort_control = page.locator(_EFFORT_GEAR)
    expect(effort_control).to_be_visible(timeout=15_000)
    expect(effort_control).to_contain_text(picked, timeout=30_000)
    # And the terminal's own source of truth still agrees with the composer.
    assert _read_config_effort(session_id) == picked
