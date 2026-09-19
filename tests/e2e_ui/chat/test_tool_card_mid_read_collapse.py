"""E2E: a tool card the user expanded must not collapse mid-read.

Reported journey (tool-history cards collapse or move while the user is
reading):

1. send a prompt that makes the agent run several tool steps,
2. while the turn is running, click one of the visible tool-step rows to
   expand its parameters/output and start reading it,
3. the agent's next tool call lands -> the expanded card collapses and
   vanishes into the closed "Ran N ..." run fold while the user is
   mid-read (its row also disappears, shifting the remaining rows).

Mechanism (root-cause lead, not asserted here): ``BlockRenderer`` keeps only
the trailing ``STREAMING_TAIL`` (3) tools of the live run visible as
individual rows; when a 4th tool lands, the oldest row is re-parented into
``ToolGroupSummary``'s ``CollapsibleContent``, which mounts CLOSED — so the
card the user explicitly opened (uncontrolled ``defaultOpen={false}``
Collapsible state) is unmounted and its content disappears mid-read.

The mock LLM scripts four single-tool rounds plus a final reply. Rounds 4
and 5 are gated (``block: true``) so the test controls exactly when "the
next step lands", making the mid-read collapse deterministic instead of a
race against the model.

On a buggy build this test FAILS at the "step-1 row still visible"
assertion (the reproduction); after a fix the expanded card survives the
new tool call and the test passes.

Run::

    pytest tests/e2e_ui/chat/test_tool_card_mid_read_collapse.py --ui-skip-build
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm

_COMPOSER = "Send a message…"
_WORKING = '[data-testid="working-indicator"]'

# Seconds for the runner to finish a tool round and park the next LLM
# request on the mock's gate (includes first-turn harness boot).
_GATE_TIMEOUT_S = 90.0


def _wait_for_gate(mock_url: str, timeout_s: float = _GATE_TIMEOUT_S) -> None:
    """Poll until an LLM request is parked on the mock's blocking gate.

    :param mock_url: Mock LLM server base URL.
    :param timeout_s: Max seconds to wait for the gate to go pending.
    :raises AssertionError: If no request blocks within *timeout_s*.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"]:
            return
        time.sleep(0.2)
    raise AssertionError(f"mock LLM gate never went pending within {timeout_s:.0f}s")


def _release_gate(mock_url: str) -> None:
    """Release the oldest gated LLM request so the turn's next step lands.

    :param mock_url: Mock LLM server base URL.
    """
    resp = httpx.post(f"{mock_url}/gate/release", timeout=5.0)
    resp.raise_for_status()
    assert resp.json()["released"] is True


def _shell_step(step: int, *, block: bool = False) -> dict[str, Any]:
    """One scripted LLM round: a single ``sys_os_shell`` echo tool call.

    :param step: Step number baked into the call id and command, so each
        rendered tool row carries a unique ``echo midread-step-N`` title.
    :param block: Park this round on the mock's gate until released.
    :returns: A ``configure_mock_llm`` response config.
    """
    cfg: dict[str, Any] = {
        "tool_calls": [
            {
                "call_id": f"call_midread_step{step}",
                "name": "sys_os_shell",
                "arguments": json.dumps({"command": f"echo midread-step-{step}"}),
            }
        ]
    }
    if block:
        cfg["block"] = True
    return cfg


def test_expanded_tool_card_survives_next_tool_call(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A tool card the user expanded stays visible when the next step lands."""
    base_url, session_id = seeded_session
    token = f"midread-{uuid.uuid4().hex[:8]}"

    # Content-route this test's turn to its own queue via the unique token
    # in the prompt. Rounds 1-3 flow freely (they become the live run's
    # visible 3-row tail); round 4 and the final reply are gated so the
    # test controls when new activity lands under the reading user.
    configure_mock_llm(
        mock_llm_server_url,
        [
            _shell_step(1),
            _shell_step(2),
            _shell_step(3),
            _shell_step(4, block=True),
            {"text": "All four preparation steps are done.", "block": True},
        ],
        key="midread-steps",
        match=token,
    )

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"Run the four preparation steps one at a time. {token}")
    page.get_by_role("button", name="Send", exact=True).click()

    # Steps 1-3 complete and stay visible as individual rows (the live
    # run's streaming tail keeps the 3 most recent tools unfolded).
    for step in (1, 2, 3):
        expect(page.get_by_text(f"midread-step-{step}").first).to_be_visible(timeout=90_000)

    # Round 4 is now parked on the gate: the turn is live but quiet, with
    # three settled rows on screen — the moment a user starts reading.
    _wait_for_gate(mock_llm_server_url)

    # The user clicks the FIRST step's row to read its parameters.
    step1_card = page.locator('[data-slot="collapsible"]').filter(has_text="midread-step-1").first
    step1_card.locator('[data-slot="collapsible-trigger"]').first.click()
    expect(step1_card.get_by_text("Parameters", exact=True)).to_be_visible()

    # Reading time — the expanded card is open under the user's eyes.
    page.wait_for_timeout(1_500)

    # The agent's next step lands while the user is still reading.
    _release_gate(mock_llm_server_url)
    expect(page.get_by_text("midread-step-4").first).to_be_visible(timeout=90_000)

    # REGRESSION GUARD (the reported bug): the card the user explicitly
    # expanded must not collapse or move away when the new step lands.
    # On a buggy build step-1's row is re-parented into the closed
    # "Ran 1 shell command" fold — its row and open parameters panel
    # vanish mid-read, and these assertions fail.
    expect(page.get_by_text("midread-step-1").first).to_be_visible()
    expect(
        page.locator('[data-slot="collapsible"]')
        .filter(has_text="midread-step-1")
        .first.get_by_text("Parameters", exact=True)
    ).to_be_visible()

    # Cleanup: let the gated final reply land so the turn settles.
    _wait_for_gate(mock_llm_server_url)
    _release_gate(mock_llm_server_url)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=90_000)
