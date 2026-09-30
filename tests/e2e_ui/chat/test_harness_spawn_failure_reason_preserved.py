"""A harness-spawn failure aborts the turn AND surfaces its spawn reason.

When the runner cannot spawn the harness subprocess for a turn,
``HarnessProcessManager.get_client`` raises ``HarnessSpawnError``; the runner's
turn-dispatch path (``omnigent/runner/app.py`` ``_stream_message_to_harness`` ->
``_run_turn_bg_setup_and_stream``) turns that into a ``503`` body
``{"error": "harness_spawn_failed", "detail": ...}``, and
``_publish_turn_status(..., "failed", error={"code": "runner_error", ...})``
surfaces the aborted turn to the web SPA as a failed turn.

The guarded behavior is twofold:

1. The turn must not hang or vanish — the SPA shows the failed-turn error pill
   with the ``runner_error`` headline.
2. The pill's expanded detail must carry the *actual spawn-failure reason*
   (``HarnessSpawnError`` messages are curated to be client-safe), not only the
   opaque "see the runner log" pointer. Redacting the reason leaves users and
   telemetry with no attributable cause for the aborted turn.

To drive the spawn failure deterministically under the mock-LLM e2e harness,
the session is bound to the ``open-responses`` harness. ``open-responses`` is a
valid harness *name* (it passes spec validation) but is **not** registered in
``omnigent.runtime.harnesses._HARNESS_MODULES``, so the runner's
``_resolve_module_path`` raises ``HarnessSpawnError`` inside ``get_client`` at
turn time — the same mock-incompatibility documented in
``tests/e2e/omnigent/test_repl_overview_terminal_visibility.py``.

The failed-turn signal reaches the SPA over the ``session.status`` SSE event,
not as a persisted ``/items`` error record, so the assertions read the rendered
pill.

Run::

    pytest tests/e2e_ui/chat/test_harness_spawn_failure_reason_preserved.py --ui-skip-build
"""

from __future__ import annotations

import io
import json
import tarfile
import uuid
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# A minimal single-model agent whose harness fails to spawn on the runner.
# spec_version 1 + ``executor.config.harness`` routes through the strict parser
# (arcname ``config.yaml``); ``open-responses`` is accepted as a valid harness
# name at parse time but is unregistered in ``_HARNESS_MODULES`` so the runner
# cannot spawn it -- the deterministic ``harness_spawn_failed`` trigger.
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
# The client-safe spawn cause (from ``HarnessSpawnError``) that the expanded
# pill must preserve instead of redacting it to a log pointer alone.
_SPAWN_FAILURE_REASON = f"unknown harness '{_SPAWN_FAIL_HARNESS}'"


def _bundle(name: str) -> bytes:
    """Gzip-tar the inline agent YAML under ``config.yaml`` for multipart upload."""
    yaml_text = _AGENT_YAML.format(name=name)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        info = tarfile.TarInfo("config.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@pytest.fixture
def spawn_fail_session(live_server: str, runner_id: str) -> Iterator[tuple[str, str]]:
    """A runner-bound session whose harness fails to spawn at turn time.

    :param live_server: Spawned server fixture; its runner is reused.
    :param runner_id: The token-bound runner id to bind the session to.
    :returns: ``(base_url, session_id)``.
    """
    name = f"spawn_fail_{uuid.uuid4().hex[:8]}"
    create = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", _bundle(name), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    session_id = str(create.json()["session_id"])
    httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    ).raise_for_status()
    try:
        yield (live_server, session_id)
    finally:
        with httpx.Client(timeout=10.0) as client:
            client.delete(f"{live_server}/v1/sessions/{session_id}")


@pytest.mark.timeout(180)
def test_spawn_failure_aborts_turn_and_preserves_reason(
    page: Page,
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
    expect(error_pill).to_contain_text(_SPAWN_FAILURE_REASON, timeout=10_000)
