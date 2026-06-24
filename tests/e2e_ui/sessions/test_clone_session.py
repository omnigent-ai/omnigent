"""Browser e2e for the Clone session flow (ForkSessionDialog).

Drives the real chain the unit layer can't: per-message "Fork from
here" action → Radix dialog → ``POST /v1/sessions/{id}/fork`` → close +
navigate into the clone → the copied transcript renders from the fork's
snapshot. (The desktop header has no Clone button — the per-message
action is the desktop entry point; mobile keeps a three-dot menu entry.)

The seeded session is runner-bound with no workspace, so the dialog takes
the non-coding path (plain "Clone", no host/directory picker, no runner
launch). The host-bound "Clone & start" variant fires its runner launch
in the background and needs a connected ``omnigent host`` daemon this
harness doesn't spawn; its pieces are covered by the dialog unit tests
(background launch + error handoff to ``useForkLaunchStore``) and the
``ResumeWithDirectoryDialog`` ``initialError`` test.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

# Unique marker so the copied-transcript assertion can't match
# UI chrome or another test's message.
_MARKER = "kumquat-clone-marker"

# Marker for the cross-family fork test, distinct from _MARKER so the two
# tests' transcripts can't satisfy each other's assertions.
_XFAM_MARKER = "loquat-xfam-marker"


def test_clone_session_copies_transcript_and_navigates(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Clone a session from a message's Fork action and land in a fork with history.

    Failure modes this catches that the mocked dialog tests can't:

    - The dialog submits but the fork request 4xxs (client/server wire
      shape drift on ``SessionForkRequest`` — e.g. ``extra="forbid"``
      rejecting a new field).
    - The fork succeeds but navigation doesn't happen or lands on the
      SOURCE session (the dialog's close+navigate ordering broke).
    - The fork navigates but renders an empty chat (the server-side
      transcript deep-copy or the fork snapshot hydration broke).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    # Seed the transcript with a uniquely-marked user turn and wait for
    # the assistant reply so the fork has BOTH roles to copy.
    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    # Open the fork dialog from the assistant bubble's "Fork from here"
    # action (the desktop entry point; the action bar is dimmed until
    # hover but stays clickable). Forking from the LAST response is a
    # full clone, so this covers the same copy-everything path the old
    # header button drove. Non-coding source → the submit button reads
    # "Clone" (no host/directory section).
    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone")
    submit.click()

    # ONE call → dialog closes and the URL moves to a DIFFERENT /c/<id>.
    # A URL still on the source id means navigation never fired (or
    # landed back on the source); a visible dialog means the fork call
    # failed and surfaced an inline error instead.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})conv_[0-9a-f]+"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # The clone's transcript carries the source's marked user turn —
    # rendered from the fork's own snapshot (the clone has no runner, so
    # nothing here can come from a live stream). An empty chat means the
    # deep-copy or fork hydration regressed.
    copied_user = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_MARKER
    )
    expect(copied_user.first).to_be_visible(timeout=30_000)


# Forking needs a real assistant bubble to anchor "Fork from here", so
# this sends a turn and waits on the reply. The in-process harness
# occasionally produces no assistant output on the first turn (the
# runner goes idle after dispatch — a nondeterministic harness
# scheduling stall, not a real-LLM artifact since this drives the mock
# LLM). Rerun on failure rather than widen the already-generous 60s
# wait, which a stalled turn would never satisfy.
@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_clone_dialog_offers_cross_family_native_target_and_forks(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The fork dialog offers a CROSS-FAMILY native target and forks into it.

    The seeded source runs ``openai-agents`` (openai family); the packaged
    ``claude-native-ui`` built-in is an anthropic NATIVE harness. The picker
    used to hide cross-family native targets (``forkSwitchPreservesHistory``
    returned false), so this guards the new rule end-to-end against the real
    agent catalog: the server must report a classifiable harness for the
    built-in AND the dialog must offer it. It then submits the switch and
    asserts the fork is created with the carry-history label stamped and the
    source-session directive absent — the server-side gates that route the
    runner to the rebuild path.

    Failure modes this catches that the dialog unit tests can't:

    - The catalog stops reporting a harness for the native built-ins
      (``harness: null`` → the option vanishes from the picker).
    - The fork request with a cross-family ``agent_id`` 4xxs (wire drift).
    - The route/store label gating regresses (wrong labels on the fork).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    # Resolve the packaged claude-native-ui built-in from the live catalog.
    agents_resp = httpx.get(f"{base_url}/v1/agents", params={"limit": 100}, timeout=30.0)
    agents_resp.raise_for_status()
    claude_native = next(
        (a for a in agents_resp.json()["data"] if a["name"] == "claude-native-ui"), None
    )
    assert claude_native is not None, (
        "claude-native-ui built-in not registered on the test server — it is "
        "seeded unconditionally at startup, so its absence is a server bug"
    )

    page.goto(f"{base_url}/c/{session_id}")

    # One marked turn so the fork has content and an assistant bubble to
    # anchor the "Fork from here" action.
    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_XFAM_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # Open the agent picker: the cross-family native target must be offered
    # (the regression: it used to be filtered out as non-history-preserving).
    page.get_by_test_id("fork-session-agent-select").click()
    option = page.get_by_test_id(f"fork-session-agent-option-{claude_native['id']}")
    expect(option).to_be_visible()
    option.click()
    page.get_by_test_id("fork-session-submit").click()

    # The fork succeeds and navigates to a NEW session id.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})conv_[0-9a-f]+"),
        timeout=30_000,
    )
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # Server-side gating made observable: the fork must carry history into
    # the native target (label stamped) WITHOUT the source-session directive
    # (the SDK source has no native session; presence would mean the store
    # stamped a wrong-format resume pointer), and present as the TARGET
    # harness (terminal-first claude wrapper), not the source's chat mode.
    snap = httpx.get(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)
    snap.raise_for_status()
    labels: dict[str, str] = snap.json().get("labels") or {}
    assert labels.get("omnigent.fork.carry_history") == "1", (
        f"cross-family native fork must stamp carry-history, got {labels!r}"
    )
    assert "omnigent.fork.source_external_session_id" not in labels, (
        f"fork of an SDK source must not stamp a source native session id, got {labels!r}"
    )
    assert labels.get("omnigent.wrapper") == "claude-code-native-ui", (
        f"fork must present as the TARGET (claude-native) harness, got {labels!r}"
    )
