"""Missing-dependency banner renders for an executor missing an optional dep.

Covers issue #548 (PR #570): when an inner executor's optional harness
dependency is missing, it raises a message shaped
``<Executor> requires the '<pkg>' package. Install it with: <cmd>``.
``ErrorBanner`` (``ap-web/src/components/blocks/StatusBlocks.tsx``) detects
this via ``parseMissingDependency`` and renders ``MissingDependencyBanner``
(copyable install command via ``CliCommandBlock`` + the raw executor error
collapsed behind a ``<details>`` block).

Trigger: a test agent on the ``cursor`` harness. ``cursor-sdk`` is an opt-in
extra (``omnigent[cursor]``) that the e2e-ui install does NOT pull in — both
local setup (``uv sync --extra all --extra dev``) and CI
(``uv sync --extra all --extra dev`` in ``.github/workflows/e2e-ui.yml``)
omit it, so the package is genuinely absent without uninstalling anything.
On the first turn ``CursorExecutor._ensure_session`` tries
``from cursor_sdk import ...``, raises ``ImportError("CursorExecutor
requires the 'cursor-sdk' package. Install it with: uv pip install
cursor-sdk")``, the cursor executor wraps it as an ``ExecutorError`` and
the adapter re-raises — the wrapped message still contains the
``requires the 'cursor-sdk' package. Install it with: …`` substring, so
``parseMissingDependency`` matches and the friendly banner renders. The
error fires before any LLM call, so no gateway response is needed for the
assertion; the banner appears as soon as the harness subprocess reports the
failed turn.

Like the rest of ``tests/e2e_ui``, this boots the live ``omnigent server``
+ runner (see ``conftest.py``); the suite is gated to the LLM workflow.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator

import httpx
import pytest
from playwright.sync_api import Page, expect

# Private helpers from the parent conftest — same import pattern the sibling
# ``tests/e2e_ui/agents/conftest.py`` uses for ``_ensure_runner_online`` /
# ``_server_state`` / ``_create_bundled_session``.
from tests.e2e_ui.conftest import (
    _create_bundled_session,
    _ensure_runner_online,
    _server_state,
)

_COMPOSER_LABEL = "Message the agent"
# CliCommandBlock test-ids (testIdPrefix="missing-dep-install"):
#   missing-dep-install-command — the <code> holding the install command
#   missing-dep-install-copy     — the copy-to-clipboard button
_INSTALL_COMMAND = '[data-testid="missing-dep-install-command"]'
_INSTALL_COPY = '[data-testid="missing-dep-install-copy"]'
_USER_BUBBLE = '[data-testid="message-bubble"][data-role="user"]'

# The cursor harness maps any databricks-* model to cursor's ``auto`` select,
# so databricks-gpt-5-4 is accepted by the spec validator but never honored
# — the missing-dep error fires before the model is used. spec_version: 1 +
# executor.config.harness routes through the strict parser (arcname
# config.yaml in ``_create_bundled_session``).
_MISSING_DEP_AGENT_YAML = """\
spec_version: 1
name: missing_dep_probe
prompt: |
  You are a terse probe assistant. When the user sends any message, reply
  with exactly one short sentence.

executor:
  model: databricks-gpt-5-4
  config:
    harness: cursor
"""


@pytest.fixture
def missing_dep_session(
    live_server: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str]]:
    """Create a runner-bound session on the ``cursor`` harness.

    ``cursor-sdk`` is absent from the e2e-ui install (opt-in ``cursor``
    extra, not in ``--extra all --extra dev``), so the first turn raises the
    missing-dependency error. Same runner-respawn + bind contract as the
    other conftest session fixtures.

    :param live_server: Spawned server fixture; its runner is reused.
    :param tmp_path_factory: Pytest temp path factory (for a respawn log).
    :returns: ``(base_url, session_id)``.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])
    session_id = _create_bundled_session(live_server, runner_id, _MISSING_DEP_AGENT_YAML)
    try:
        yield (live_server, session_id)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except subprocess.TimeoutExpired:  # parity with conftest fixtures
                respawned.kill()
                respawned.wait(timeout=5)


# Deterministic: the cursor-sdk ImportError fires in _ensure_session on the
# first turn, before any LLM call — so this isn't LLM-nondeterministic. Use a
# plain `flaky` rerun (covers banner-appearance timing races) rather than
# `llm_flaky`, which would pointlessly rotate models per attempt. #548
@pytest.mark.flaky(reruns=2, reruns_delay=1)
def test_missing_dependency_banner_renders(
    page: Page,
    missing_dep_session: tuple[str, str],
) -> None:
    """Send a turn → cursor-sdk missing → MissingDependencyBanner renders.

    Asserts the banner's friendly remediation surfaces: the "Missing
    dependency" title, the package name, the copyable install command (via
    ``CliCommandBlock`` test-ids), and the raw executor error collapsed
    behind a ``<details>``/``<summary>`` "Raw error" block that expands on
    click.

    :param page: Playwright page fixture.
    :param missing_dep_session: ``(base_url, session_id)`` on the cursor
        harness, whose ``cursor-sdk`` dep is absent in the e2e-ui install.
    """
    base_url, session_id = missing_dep_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label(_COMPOSER_LABEL)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("hello")
    page.get_by_role("button", name="Send", exact=True).click()

    # The turn fails fast: the harness subprocess raises the missing-dep
    # ImportError on the first run_turn before any LLM call. 60s covers the
    # harness subprocess boot + the failed-turn round trip.
    expect(page.get_by_text("Missing dependency")).to_be_visible(timeout=60_000)

    # The package name surfaces in the friendly summary line. ``exact=True``
    # targets only the package-name <code> ("cursor-sdk"); the install-command
    # <code> and the collapsed raw-error span carry longer strings, so they
    # don't match, keeping this assertion precise and visibility-safe.
    expect(page.get_by_text("cursor-sdk", exact=True)).to_be_visible()

    # The install command renders in a copyable CliCommandBlock.
    install_cmd = page.locator(_INSTALL_COMMAND)
    expect(install_cmd).to_be_visible()
    expect(install_cmd).to_have_text("uv pip install cursor-sdk")
    expect(page.locator(_INSTALL_COPY)).to_be_visible()

    # The raw executor error is collapsed behind a <details> block whose
    # <summary> reads "Raw error". The raw span lives in the DOM collapsed, so
    # the collapse is asserted via the <details> ``open`` attribute (absent
    # when closed) rather than a text-count check. Scoped to the "Raw error"
    # summary so an unrelated <details> elsewhere in the UI can't match.
    details = page.locator("details").filter(has=page.get_by_text("Raw error", exact=True))
    expect(details).to_be_visible()
    expect(details).not_to_have_attribute("open")

    # Expanding the details surfaces the raw error. The phrase
    # "CursorExecutor requires the" only appears in the raw-error span (the
    # install command is just the command; the package-name code is just
    # "cursor-sdk"), so it's a precise probe for the expanded raw text.
    page.get_by_text("Raw error", exact=True).click()
    expect(details).to_have_attribute("open", "")
    raw_error_phrase = page.get_by_text("CursorExecutor requires the", exact=False)
    expect(raw_error_phrase.first).to_be_visible(timeout=15_000)

    # The failed turn still consumed the user's prompt (an optimistic user
    # bubble rendered on send); the banner replaces the assistant reply.
    expect(page.locator(_USER_BUBBLE)).to_have_count(1)
