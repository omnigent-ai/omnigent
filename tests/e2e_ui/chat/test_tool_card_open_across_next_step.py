"""E2E: a tool card the user expanded mid-turn survives the next tool step.

While a turn is live the transcript shows the trailing tool rows standalone
and folds older rows into a "Ran N shell commands" summary. A row the user
has opened to read must stay open and in place when the next step lands.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_WORKING = '[data-testid="working-indicator"]'
_TRIGGER = '[data-slot="collapsible-trigger"]'
_CONTENT = '[data-slot="collapsible-content"]'
_FOLD_LABEL = "Ran 1 shell command"
_REPLY = "All four probe steps finished."
_STEPS = 4
_MAX_ROW_SHIFT_PX = 2

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to run the probe
  steps you call sys_os_shell once per step and then reply with one
  short sentence.

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

_SCROLL_TOP = (
    "(el) => {"
    "  for (let p = el.parentElement; p; p = p.parentElement) {"
    "    const o = getComputedStyle(p).overflowY;"
    "    if (o === 'auto' || o === 'scroll') return p.scrollTop;"
    "  }"
    "  return null;"
    "}"
)


def _step_command(step: int) -> str:
    return f"echo probe-step-{step}"


def _step_call(step: int) -> dict[str, object]:
    return {
        "tool_calls": [
            {
                "call_id": f"call_probe_step_{step}",
                "name": "sys_os_shell",
                "arguments": json.dumps({"command": _step_command(step)}),
            }
        ]
    }


def _gate_pending(mock_url: str) -> bool:
    resp = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
    resp.raise_for_status()
    return bool(resp.json()["pending"])


def _wait_gate_pending(mock_url: str, timeout_s: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if _gate_pending(mock_url):
            return
        time.sleep(0.2)
    raise AssertionError("mock LLM never reached the gated response")


def _release_gate(mock_url: str) -> None:
    resp = httpx.post(f"{mock_url}/gate/release", timeout=5.0, trust_env=False)
    resp.raise_for_status()
    assert resp.json()["released"], "no gated mock response to release"


def _release_remaining_gates(mock_url: str, quiet_s: float = 3.0, timeout_s: float = 30.0) -> None:
    """Let the turn finish: release gates until none appear for ``quiet_s``."""
    deadline = time.monotonic() + timeout_s
    quiet_since = time.monotonic()
    while time.monotonic() < deadline:
        if _gate_pending(mock_url):
            httpx.post(f"{mock_url}/gate/release", timeout=5.0, trust_env=False).raise_for_status()
            quiet_since = time.monotonic()
        elif time.monotonic() - quiet_since >= quiet_s:
            return
        time.sleep(0.25)
    raise AssertionError("mock LLM gate still pending after the release deadline")


@pytest.fixture
def four_step_session(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str, str]]:
    """A runner-bound session whose turn runs four shell steps.

    The fourth step and the wrap-up reply are gated on the mock so the test
    decides when the next step lands and keeps the turn live afterwards.

    :param live_server: Spawned (or prepared) server fixture.
    :param runner_id: Token-bound id of its runner.
    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :returns: ``(base_url, session_id, mock_url)``.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-tool-fold-"))
    suffix = uuid.uuid4().hex[:8]
    model = f"tool-fold-probe-{suffix}"

    responses: list[dict[str, object]] = [_step_call(step) for step in range(1, _STEPS)]
    responses.append({**_step_call(_STEPS), "block": True})
    responses.append({"text": _REPLY, "block": True})
    configure_mock_llm(mock_llm_server_url, responses, key=model, required_tools=["sys_os_shell"])
    set_fallback_mock_llm(mock_llm_server_url, model, _REPLY)

    yaml_text = _AGENT_YAML.format(name=f"tool_fold_probe_{suffix}", model=model, cwd=str(ws))
    session_id = _create_bundled_session(live_server, runner_id, yaml_text)
    try:
        yield (live_server, session_id, mock_llm_server_url)
    finally:
        try:
            _release_remaining_gates(mock_llm_server_url)
        finally:
            try:
                httpx.delete(
                    f"{live_server}/v1/sessions/{session_id}", timeout=10.0, trust_env=False
                )
            finally:
                shutil.rmtree(ws, ignore_errors=True)


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _step_row(page: Page, step: int) -> Locator:
    return page.locator(_TRIGGER, has_text=_step_command(step))


def _step_parameters(page: Page, step: int) -> Locator:
    panels = page.locator(_CONTENT).filter(has_text="Parameters")
    return panels.filter(has_text=_step_command(step))


def _fold(page: Page) -> Locator:
    return page.locator(_TRIGGER, has_text=_FOLD_LABEL)


def _row_tops(page: Page, steps: range) -> dict[int, float | None]:
    tops: dict[int, float | None] = {}
    for step in steps:
        box = _step_row(page, step).bounding_box()
        tops[step] = box["y"] if box else None
    return tops


@pytest.mark.timeout(300)
def test_expanded_tool_card_survives_next_step(
    request: pytest.FixtureRequest,
    four_step_session: tuple[str, str, str],
) -> None:
    """An expanded step card stays open and in place when the next step lands."""
    base_url, session_id, mock_url = four_step_session
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, "Run the four probe steps.")
    for step in range(1, _STEPS):
        expect(_step_row(page, step)).to_be_visible(timeout=90_000)
    _wait_gate_pending(mock_url)
    expect(page.locator(_WORKING)).to_be_visible(timeout=15_000)

    first_row = _step_row(page, 1)
    first_row.click()
    parameters = _step_parameters(page, 1)
    expect(parameters).to_be_visible(timeout=15_000)
    expect(_fold(page)).to_have_count(0)
    later_rows = range(2, _STEPS)
    tops_before = _row_tops(page, later_rows)
    scroll_before = _step_row(page, 2).evaluate(_SCROLL_TOP)
    page.wait_for_timeout(2_000)

    _release_gate(mock_url)
    expect(_step_row(page, _STEPS)).to_be_visible(timeout=60_000)
    page.wait_for_timeout(1_500)
    tops_after = _row_tops(page, later_rows)
    scroll_after = _step_row(page, 2).evaluate(_SCROLL_TOP)
    fold = _fold(page)
    fold_state = fold.first.get_attribute("data-state") if fold.count() else None
    row_visible = first_row.is_visible()
    parameters_visible = parameters.is_visible()

    problems: list[str] = []
    if not row_visible:
        problems.append(f"the `{_step_command(1)}` row is gone")
    if not parameters_visible:
        problems.append("its Parameters panel is gone")
    if fold_state is not None:
        problems.append(f"a {fold_state} '{_FOLD_LABEL}' fold stands in its place")
    shifted = {
        step: round(after - before)
        for step in later_rows
        if (before := tops_before[step]) is not None
        and (after := tops_after[step]) is not None
        and abs(after - before) > _MAX_ROW_SHIFT_PX
    }
    if shifted:
        problems.append(
            f"later rows moved by {shifted} px (scrollTop {scroll_before} -> {scroll_after})"
        )
    if problems:
        pytest.fail(
            "after step 4 landed while the user was reading step 1: " + "; ".join(problems)
        )
