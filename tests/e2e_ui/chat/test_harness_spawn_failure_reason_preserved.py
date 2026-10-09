"""A runner spawn failure reaches the chat error pill with its client-safe cause.

The failed-turn signal reaches the SPA over the ``session.status`` SSE event,
not as a persisted ``/items`` record, so the assertions read the rendered pill.

Run::

    pytest tests/e2e_ui/chat/test_harness_spawn_failure_reason_preserved.py --ui-skip-build
"""

from __future__ import annotations

import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _create_bundled_session

# ``open-responses`` passes spec validation (spec_version 1, strict parser via
# the ``config.yaml`` arcname) but is unregistered in ``_HARNESS_MODULES``, so the
# runner cannot spawn it: the deterministic ``harness_spawn_failed`` trigger.
_SPAWN_FAIL_HARNESS = "open-responses"
_AGENT_YAML = f"""\
spec_version: 1
name: {{name}}
prompt: |
  You are a deterministic test assistant.

executor:
  model: gpt-4o-mini
  config:
    harness: {_SPAWN_FAIL_HARNESS}
"""

# The runner_error code -> friendly headline the SPA renders (mirrors
# ``FAILURE_CODE_DESCRIPTIONS`` in web/src/components/blocks/StatusBlocks.tsx).
_RUNNER_ERROR_HEADLINE = "Something went wrong setting up the turn on the host."
# The structured reason the runner attaches to the failed turn.
_SPAWN_FAILED_CODE = "harness_spawn_failed"
# The client-safe spawn cause the expanded pill must preserve instead of
# redacting it to a log pointer alone; the harness name is asserted separately.
_SPAWN_FAILURE_CAUSE = "unknown harness"


@pytest.fixture
def spawn_fail_session(live_server: str, runner_id: str) -> Iterator[tuple[str, str]]:
    """A runner-bound session whose harness fails to spawn at turn time.

    :param live_server: Spawned server fixture; its runner is reused.
    :param runner_id: The token-bound runner id to bind the session to.
    :returns: ``(base_url, session_id)``.
    """
    name = f"spawn_fail_{uuid.uuid4().hex[:8]}"
    session_id = _create_bundled_session(live_server, runner_id, _AGENT_YAML.format(name=name))
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


@pytest.mark.timeout(180)
def test_spawn_failure_aborts_turn_and_preserves_reason(
    request: pytest.FixtureRequest,
    spawn_fail_session: tuple[str, str],
) -> None:
    """Send a turn whose harness cannot spawn; the pill names the spawn cause.

    The turn aborts with a ``runner_error`` failure pill carrying the
    ``harness_spawn_failed`` code, and the expanded detail preserves the
    client-safe spawn reason (here: the unknown-harness cause). A regression
    that swallows the reason, hangs the turn, or surfaces no failure at all
    fails these assertions.
    """
    base_url, session_id = spawn_fail_session

    # Open the page only after the session exists so a recording starts at the journey.
    page: Page = request.getfixturevalue("page")
    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("Reply with the single token OK.")
    page.get_by_role("button", name="Send", exact=True).click()

    # User-visible outcome: the turn fails to start and the error pill appears
    # with the runner_error headline (the turn was aborted on the host).
    error_pill = page.get_by_test_id("error-pill").first
    expect(error_pill).to_be_visible(timeout=60_000)
    expect(error_pill).to_have_attribute("data-level", "error")
    expect(error_pill).to_contain_text(_RUNNER_ERROR_HEADLINE)

    # Expand the pill so the raw structured reason renders. The turn must be
    # attributed to the spawn failure AND carry the actual client-safe cause,
    # not only the generic "see the runner log" pointer.
    error_pill.click()
    expect(error_pill).to_contain_text(_SPAWN_FAILED_CODE, timeout=10_000)
    expect(error_pill).to_contain_text(_SPAWN_FAILURE_CAUSE, timeout=10_000)
    expect(error_pill).to_contain_text(_SPAWN_FAIL_HARNESS, timeout=10_000)
