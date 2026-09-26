"""E2E: tool-call Output panels soft-wrap long lines instead of clipping.

The ToolCard expanded panel renders tool output through ``CodeBlock``
(``web/src/components/ai-elements/code-block.tsx``). When its ``<pre>``
keeps ``white-space: pre`` inside an ``overflow-auto`` scroller, a long
output line never wraps: it clips at the panel edge and can only be read
by scrolling horizontally. Chat markdown code blocks soft-wrap by default
(``.chat-code-wrap`` + "Toggle word wrap"); the tool Output panel must
wrap the same way.

The journey drives the real user path: a deterministic agent turn (mock
LLM) runs one ``sys_os_shell`` command that prints a single very long
line, then the test expands the settled turn's Worked fold, the tool-run
summary, and the tool row, and asserts on what the user sees in the
Output panel:

  - the long output line must FIT the panel (``scrollWidth`` no wider
    than ``clientWidth``), i.e. wrap instead of forcing a horizontal
    scrollbar.

On the buggy build the line overflows horizontally by hundreds of
pixels, so the final assertion fails — that failure is the reproduction.
After a wrap fix lands the same journey passes, making this the
regression guard.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# The command itself stays short (so the Parameters panel is not the
# thing that overflows); the LONG single line is produced at runtime.
_PHRASE = "long tool output line "
_COMMAND = f"python3 -c \"print('{_PHRASE}' * 40 + 'x' * 300)\""

# The phrase appears once inside the command string (Parameters panel),
# but only the tool OUTPUT contains it repeated back-to-back — use the
# doubled phrase to key every locator to the Output panel's <pre>.
_DOUBLED = (_PHRASE * 2).strip()

_REPLY = "Long command finished."

# JS helpers: find the Output panel's <pre> (the one carrying the doubled
# phrase) and measure its horizontal overflow. ``scrollWidth`` vs
# ``clientWidth`` ties the assertion to what the user sees — whether the
# content fits the column or needs horizontal scrolling.
_FIND_PRE = (
    "() => {"
    f"  const doubled = {json.dumps(_DOUBLED)};"
    "  const pres = Array.from(document.querySelectorAll('[data-language] pre'));"
    "  return pres.find((p) => (p.textContent || '').includes(doubled)) ?? null;"
    "}"
)
_PRE_PRESENT = f"() => !!({_FIND_PRE})()"
_FITS = f"() => {{ const p = ({_FIND_PRE})(); return !!p && p.scrollWidth - p.clientWidth <= 1; }}"
_OVERFLOW_PX = (
    f"() => {{ const p = ({_FIND_PRE})(); return p ? p.scrollWidth - p.clientWidth : null; }}"
)
# For the recording: sweep the nearest horizontally-scrollable ancestor to
# the far right and back, so the clip shows the clipped line being
# scrolled. A no-op once the output wraps (nothing scrollable).
_DEMO_SCROLL = (
    "(left) => {"
    f"  const p = ({_FIND_PRE})();"
    "  if (!p) return;"
    "  let el = p;"
    "  while (el && el.scrollWidth - el.clientWidth <= 1) el = el.parentElement;"
    "  if (el) el.scrollTo({ left, behavior: 'smooth' });"
    "}"
)

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic test assistant. When asked to run the probe
  command you call sys_os_shell exactly once and then reply with one
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


@pytest.fixture
def long_output_session(
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> Iterator[tuple[str, str]]:
    """A runner-bound session whose turn runs one long-output shell command.

    The mock queue is keyed by a per-fixture unique model name so the
    scripted tool call cannot leak into other tests' queues: one
    ``sys_os_shell`` call printing a ~1300-char single line, then a text
    fallback for the wrap-up LLM call.

    :param live_server: Spawned server fixture.
    :param runner_id: Token-bound id of the spawned runner.
    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :returns: ``(base_url, session_id)``.
    """
    ws = Path(tempfile.mkdtemp(prefix="omnigent-e2e-long-output-"))
    name = f"long_output_probe_{uuid.uuid4().hex[:8]}"
    model = f"long-output-probe-{uuid.uuid4().hex[:8]}"

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_long_output",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": _COMMAND}),
                    }
                ]
            },
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, _REPLY)

    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=str(ws))
    session_id = _create_bundled_session(live_server, runner_id, yaml_text)

    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(ws, ignore_errors=True)


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _expand_tool_output(page: Page) -> None:
    """Expand Worked fold → tool-run summary → tool row → Output panel."""
    # The Worked row only forms once the wrap-up text lands and the turn
    # settles, so waiting for it covers the turn.
    worked = page.get_by_test_id("turn-worked-fold")
    expect(worked).to_be_visible(timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    worked.locator('[data-slot="collapsible-trigger"]').first.click()

    # The settled run collapses into its semantic one-liner; expand it.
    fold = page.get_by_text("Ran 1 shell command", exact=True)
    expect(fold).to_be_visible(timeout=15_000)
    fold.click()

    # The per-tool trigger row shows the raw command; expand its panel.
    row = page.locator('[data-slot="collapsible-trigger"]', has_text="python3 -c").first
    expect(row).to_be_visible(timeout=15_000)
    row.click()

    # The expanded panel carries the Output code block with the long line.
    expect(page.get_by_text("Output", exact=True)).to_be_visible(timeout=15_000)
    page.wait_for_function(_PRE_PRESENT, timeout=15_000)


@pytest.mark.timeout(300)
def test_tool_output_long_line_wraps_instead_of_clipping(
    page: Page,
    long_output_session: tuple[str, str],
) -> None:
    """A long tool-output line must fit the panel, not scroll horizontally."""
    base_url, session_id = long_output_session
    page.goto(f"{base_url}/c/{session_id}")

    _send(page, "Run the long-output probe command.")
    expect(page.locator(_ASSISTANT, has_text=_REPLY).first).to_be_visible(timeout=90_000)

    _expand_tool_output(page)

    # Regression guard: the long output line fits the panel width — no
    # horizontal clipping/scrolling needed to read it.
    try:
        page.wait_for_function(_FITS, timeout=8_000)
    except PlaywrightTimeoutError:
        overflow = page.evaluate(_OVERFLOW_PX)
        # Show the failure the user experiences (for the journey recording):
        # sweep the clipped panel to the far right and back.
        page.evaluate(_DEMO_SCROLL, 10_000)
        page.wait_for_timeout(1_200)
        page.evaluate(_DEMO_SCROLL, 0)
        page.wait_for_timeout(800)
        pytest.fail(
            "tool Output panel clips its content: the long output line "
            f"overflows the panel horizontally by {overflow}px "
            "(scrollWidth > clientWidth), so it can only be read by "
            "scrolling sideways"
        )
