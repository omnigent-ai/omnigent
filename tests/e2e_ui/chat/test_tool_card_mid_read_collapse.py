"""E2E: a tool card the user expanded mid-turn survives the next tool call.

The live run of tool steps keeps only its most recent rows un-folded; older
rows fold into a "Ran N shell commands" summary. A card the user has opened
to read must not be swept into that closed fold when the next step lands.

The journey drives the real user path against a scripted mock LLM: the agent
runs four ``sys_os_shell`` steps, with the fourth model call (and the wrap-up
call) held on the mock gate so the turn stays live while the user expands the
step-1 row and reads its Parameters panel. Releasing the gate lands step 4.

Run::

    pytest tests/e2e_ui/chat/test_tool_card_mid_read_collapse.py --ui-skip-build
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER_LABEL = "Message the agent"
_PROMPT = "Run the four probe steps."
_REPLY = "All four probe steps finished."
_STEPS = 4
_RUN_FOLD_LABEL = "Ran 1 shell command"

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to run the probe
  steps you call sys_os_shell once per step, in order, then reply with
  one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none
"""


def _step_command(step: int) -> str:
    return f"echo probe-step-{step}"


def _tool_call(step: int) -> dict[str, object]:
    return {
        "tool_calls": [
            {
                "call_id": f"call_step_{step}",
                "name": "sys_os_shell",
                "arguments": json.dumps({"command": _step_command(step)}),
            }
        ]
    }


@pytest.fixture
def gated_four_step_session(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str]]:
    """A runner-bound session whose turn runs four shell steps with gates.

    The mock queue is keyed by a per-fixture unique model name: three
    immediate ``sys_os_shell`` calls, a fourth held on the gate, then the
    wrap-up text held on a second gate so the turn is still live after
    step 4 lands.

    :returns: ``(base_url, session_id, mock_llm_url)``.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-mid-read-"))
    name = f"mid_read_probe_{uuid.uuid4().hex[:8]}"
    model = f"mid-read-probe-{uuid.uuid4().hex[:8]}"

    responses: list[dict[str, object]] = [_tool_call(step) for step in range(1, _STEPS)]
    responses.append({"block": True, **_tool_call(_STEPS)})
    responses.append({"block": True, "text": _REPLY})
    configure_mock_llm(mock_llm_server_url, responses, key=model)
    set_fallback_mock_llm(mock_llm_server_url, model, _REPLY)

    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=str(ws))
    session_id = _create_bundled_session(live_server, runner_id, yaml_text)
    try:
        yield (live_server, session_id, mock_llm_server_url)
    finally:
        # Never leave the shared runner blocked on a gate.
        for _ in range(3):
            if not _gate_pending(mock_llm_server_url):
                break
            _release_gate(mock_llm_server_url)
            time.sleep(0.5)
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _gate_pending(mock_url: str) -> bool:
    return bool(httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"])


def _release_gate(mock_url: str) -> None:
    response = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    response.raise_for_status()
    assert response.json()["released"] is True


def _wait_for(page: Page, predicate: Callable[[], bool], *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _step_row(page: Page, step: int) -> Locator:
    """The clickable trigger row of the step's tool card."""
    return page.locator('[data-slot="collapsible-trigger"]', has_text=_step_command(step))


def _step_card(page: Page, step: int) -> Locator:
    """The step's tool-card collapsible (innermost match wrapping its row)."""
    return page.locator('[data-slot="collapsible"]', has=_step_row(page, step)).last


def _row_top(row: Locator) -> float | None:
    box = row.bounding_box() if row.count() > 0 else None
    return None if box is None else box["y"]


@pytest.mark.timeout(300)
def test_expanded_tool_card_survives_next_tool_call(
    page: Page,
    gated_four_step_session: tuple[str, str, str],
) -> None:
    """A user-opened step card stays open and in place when the next step lands."""
    base_url, session_id, mock_url = gated_four_step_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(_PROMPT)
    page.get_by_role("button", name="Send", exact=True).click()

    # Steps 1-3 land as individual rows while the fourth model call waits.
    for step in range(1, _STEPS):
        expect(_step_row(page, step)).to_be_visible(timeout=90_000)
    _wait_for(page, lambda: _gate_pending(mock_url), timeout_s=60.0)

    # The user opens step 1 and starts reading its parameters.
    _step_row(page, 1).click()
    panel = _step_card(page, 1).locator('[data-slot="collapsible-content"]')
    expect(panel).to_have_attribute("data-state", "open")
    expect(panel.get_by_text("Parameters", exact=True)).to_be_visible()
    tops_before = {step: _row_top(_step_row(page, step)) for step in range(1, _STEPS)}
    page.wait_for_timeout(1_500)

    # Step 4 lands while the card is open; the wrap-up call is still gated,
    # so the turn stays live for the observation.
    _release_gate(mock_url)
    expect(_step_row(page, _STEPS)).to_be_visible(timeout=60_000)
    _wait_for(page, lambda: _gate_pending(mock_url), timeout_s=60.0)
    page.wait_for_timeout(1_000)

    step1_row = _step_row(page, 1)
    step1_present = step1_row.count() > 0 and step1_row.first.is_visible()
    panel_open = (
        step1_present and panel.count() > 0 and panel.get_attribute("data-state") == "open"
    )
    fold = page.get_by_text(_RUN_FOLD_LABEL, exact=True)
    fold_shown = fold.count() > 0 and fold.first.is_visible()
    shifts = {
        step: None
        if tops_before[step] is None or (top := _row_top(_step_row(page, step))) is None
        else round(top - tops_before[step])
        for step in (2, 3)
    }

    if not panel_open:
        pytest.fail(
            "the tool card the user expanded folded away when the next tool call "
            f"landed: step-1 row still visible={step1_present}, "
            f"closed '{_RUN_FOLD_LABEL}' fold shown={fold_shown}, "
            f"rows 2/3 moved vertically by {shifts} px"
        )
    expect(panel.get_by_text("Parameters", exact=True)).to_be_visible()
    assert not fold_shown, "the open step-1 card must not be replaced by a run fold"
    assert all(shift == 0 for shift in shifts.values()), (
        f"rows the user was reading moved by {shifts} px when step 4 landed"
    )

    # Let the turn finish.
    _release_gate(mock_url)
    expect(page.get_by_text(_REPLY, exact=True)).to_be_visible(timeout=60_000)
