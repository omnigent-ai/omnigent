"""Browser e2e: a codex→codex fork of a bypass-armed session keeps bypass.

The source session runs Codex with the DANGEROUS full-bypass
stance (the ``omnigent.codex_native.bypass_sandbox`` label, which the runner
turns into ``--dangerously-bypass-approvals-and-sandbox``). Forking it
codex→codex from the per-message "Fork from here" action seeds the dialog's
Approval picker on "Bypass approvals & sandbox" — danger banner and all — but
an UNTOUCHED picker emits no run-config field on submit, and the server always
drops the source's bypass label (instance-scoped), so the clone silently
launches with approvals back on. The dialog's displayed stance and the fork's
actual stance diverge: the user watched a danger banner promise a bypass fork
and got a default-approvals fork.

``test_codex_fork_of_bypass_source_arms_the_clone`` guards the promise
end-to-end: the seeded dialog shows bypass, and after Clone the fork itself is
bypass-armed — visible by reopening the fork dialog ON the clone (its Approval
picker seeds from the clone's own label) and tight via the fork's labels.

``test_explicit_bypass_pick_arms_the_clone`` guards the companion path:
explicitly picking "Bypass approvals & sandbox" in the fork dialog arms the
clone (there is a way to set bypass while forking a non-bypass source).

Neither test binds a runner: the fork flow under test is dialog + server
store behavior, an unbound source still renders its seeded transcript, and an
unbound clone renders the copied transcript from its own snapshot — so no live
Codex CLI (or its ~15s cold boot) is needed.
"""

from __future__ import annotations

import contextlib
import io
import json
import re
import tarfile
import tempfile
from pathlib import Path

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import seed_committed_items

# The conversation-label key the runner reads to arm
# --dangerously-bypass-approvals-and-sandbox (see
# omnigent.stores.conversation_store.CODEX_NATIVE_BYPASS_SANDBOX_LABEL_KEY).
_BYPASS_LABEL_KEY = "omnigent.codex_native.bypass_sandbox"

# The dialog's 4th (dangerous) Approval option, as displayed.
_BYPASS_OPTION_LABEL = "Bypass approvals & sandbox"

# Unique transcript markers so bubble assertions can't match UI chrome or
# another test's message.
_MARKER_SEEDED = "quince-bypass-seeded-marker"
_MARKER_EXPLICIT = "medlar-bypass-explicit-marker"


def _create_codex_wrapper_session(base_url: str, *, bypass: bool) -> str:
    """Create a top-level ``codex-native-ui`` wrapper session, optionally armed.

    Mirrors ``_create_native_codex_session`` in conftest (same production spec
    via ``_materialize_codex_agent_spec``, same wrapper / terminal-first
    labels) but skips the runner bind — no Codex CLI launch is needed for the
    fork-dialog journey — and optionally stamps the DANGEROUS bypass label the
    way the new-session dialog's opt-in does at create time.

    :param base_url: Spawned server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param bypass: Stamp ``omnigent.codex_native.bypass_sandbox = "1"``.
    :returns: The new session/conversation id.
    """
    from omnigent._wrapper_labels import (
        CODEX_NATIVE_WRAPPER_VALUE,
        UI_MODE_LABEL_KEY,
        UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY,
    )
    from omnigent.codex_native import _materialize_codex_agent_spec

    with tempfile.TemporaryDirectory() as tmp:
        spec_path = _materialize_codex_agent_spec(Path(tmp), model=None)
        yaml_text = spec_path.read_text()

    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        data = yaml_text.encode()
        # Non-config.yaml arcname → omnigent compat translator (the spec has
        # no spec_version), matching the native_codex_session fixture.
        info = tarfile.TarInfo("codex-native-ui.yaml")
        info.size = len(data)
        tar.addfile(info, io.BytesIO(data))

    labels = {
        UI_MODE_LABEL_KEY: UI_MODE_TERMINAL_VALUE,
        WRAPPER_LABEL_KEY: CODEX_NATIVE_WRAPPER_VALUE,
    }
    if bypass:
        labels[_BYPASS_LABEL_KEY] = "1"
    create = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({"labels": labels})},
        files={"bundle": ("codex-native-ui.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create.raise_for_status()
    return str(create.json()["session_id"])


def _seed_turn(session_id: str, marker: str) -> None:
    """Write one committed user+assistant Codex exchange straight into the store.

    The per-message "Fork from here" action anchors on a committed assistant
    bubble; seeding skips the runner and the LLM entirely (a real Codex turn
    would need a live CLI this harness doesn't spawn for this journey).

    :param session_id: Session to append to.
    :param marker: Unique token embedded in both messages.
    """
    from omnigent.entities import MessageData, NewConversationItem

    seed_committed_items(
        session_id,
        [
            NewConversationItem(
                type="message",
                response_id="resp_seeded",
                data=MessageData(
                    role="user",
                    content=[{"type": "input_text", "text": f"Do the thing. Marker: {marker}"}],
                ),
            ),
            NewConversationItem(
                type="message",
                response_id="resp_seeded",
                data=MessageData(
                    role="assistant",
                    content=[{"type": "output_text", "text": f"Done. Marker: {marker}"}],
                    agent="codex-native-ui",
                ),
            ),
        ],
    )


def _ensure_chat_view(page: Page) -> None:
    """Switch a terminal-first (native wrapper) session to its chat view.

    Native wrapper sessions default to the terminal view; the chat view
    renders the committed transcript as ``message-bubble``s. When the toggle
    isn't rendered (terminal unavailable on an unbound session), the page is
    already on chat.

    :param page: The Playwright page, on the session's surface.
    """
    toggle = page.get_by_test_id("view-mode-toggle")
    try:
        expect(toggle).to_be_visible(timeout=10_000)
    except AssertionError:
        return
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def _open_fork_dialog(page: Page, marker: str) -> None:
    """Open the fork dialog from the marked assistant bubble's fork action.

    :param page: The Playwright page, on the session's chat surface.
    :param marker: Transcript marker identifying the assistant bubble.
    """
    assistant = (
        page.locator('[data-testid="message-bubble"][data-role="assistant"]')
        .filter(has_text=marker)
        .first
    )
    expect(assistant).to_be_visible(timeout=30_000)
    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible(timeout=10_000)


def _submit_and_wait_for_fork(page: Page, source_session_id: str) -> str:
    """Click Clone and wait for navigation into the new fork.

    :param page: The Playwright page, with the fork dialog open.
    :param source_session_id: The source id the URL must move away from.
    :returns: The fork's session id, parsed from the URL.
    """
    page.get_by_test_id("fork-session-submit").click()
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(source_session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    return page.url.rsplit("/c/", 1)[1].split("?", 1)[0]


def _session_labels(base_url: str, session_id: str) -> dict[str, str]:
    """Fetch a session's labels from its snapshot.

    :param base_url: Spawned server base URL.
    :param session_id: Session to read.
    :returns: The session's labels dict.
    """
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("labels", {}) or {}


def _delete_session(base_url: str, session_id: str) -> None:
    """Best-effort session cleanup so back-to-back tests stay independent.

    :param base_url: Spawned server base URL.
    :param session_id: Session to delete.
    """
    with contextlib.suppress(httpx.HTTPError):
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)


def test_codex_fork_of_bypass_source_arms_the_clone(
    page: Page,
    live_server: str,
) -> None:
    """A codex→codex fork whose dialog shows bypass must arm bypass on the clone.

    The reported journey: fork a bypass-armed Codex session
    codex→codex and the clone reverts to asking for approvals. On the current
    build the fork dialog even SEEDS "Bypass approvals & sandbox" (with the
    danger banner) for such a source — but submitting without touching the
    picker emits nothing and the server drops the source's bypass label, so
    the displayed stance and the fork's actual stance diverge.

    Asserts the correct behavior end-to-end, so it FAILS on the buggy build:

    - the same-agent fork dialog seeds the bypass option + danger banner
      (guards the seeding contract this test's fix relies on);
    - after Clone, reopening the fork dialog ON the clone shows the bypass
      option seeded from the clone's OWN label (the user-visible symptom:
      today it reads "Default");
    - the clone's labels carry ``omnigent.codex_native.bypass_sandbox = "1"``
      (the exact bit the runner arms the Codex full-bypass launch from).

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Spawned server base URL.
    """
    fork_id: str | None = None
    session_id = _create_codex_wrapper_session(live_server, bypass=True)
    try:
        _seed_turn(session_id, _MARKER_SEEDED)
        page.goto(f"{live_server}/c/{session_id}")
        _ensure_chat_view(page)

        # The same-agent fork dialog seeds the DANGEROUS bypass option from
        # the source's label, danger banner included — this is the dialog's
        # on-screen promise of what the clone will run.
        _open_fork_dialog(page, _MARKER_SEEDED)
        approval = page.get_by_test_id("fork-session-config-approval")
        expect(approval).to_be_visible(timeout=15_000)
        expect(approval).to_contain_text(_BYPASS_OPTION_LABEL)
        expect(page.get_by_test_id("fork-session-codex-bypass-banner")).to_be_visible()

        # Submit with the picker UNTOUCHED — the seeded value is the promise.
        fork_id = _submit_and_wait_for_fork(page, session_id)

        # User-visible check: the fork dialog reopened ON the clone seeds its
        # Approval picker from the clone's own label. A bypass-armed clone
        # shows "Bypass approvals & sandbox"; today's un-armed clone shows
        # "Default" — the reported "forked session reverts to asking for
        # approvals", surfaced in the UI.
        _ensure_chat_view(page)
        _open_fork_dialog(page, _MARKER_SEEDED)
        fork_approval = page.get_by_test_id("fork-session-config-approval")
        expect(fork_approval).to_be_visible(timeout=15_000)
        expect(fork_approval).to_contain_text(_BYPASS_OPTION_LABEL)

        # Tight check: the label the runner turns into
        # --dangerously-bypass-approvals-and-sandbox must be on the fork.
        labels = _session_labels(live_server, fork_id)
        assert labels.get(_BYPASS_LABEL_KEY) == "1", (
            "codex→codex fork of a bypass-armed source is not bypass-armed: "
            f"the fork dialog displayed {_BYPASS_OPTION_LABEL!r} (danger "
            "banner and all) but the clone's labels are missing "
            f"{_BYPASS_LABEL_KEY}=1 — the clone launches with approvals "
            f"back on. Fork labels: {labels!r}"
        )
    finally:
        if fork_id is not None:
            _delete_session(live_server, fork_id)
        _delete_session(live_server, session_id)


def test_explicit_bypass_pick_arms_the_clone(
    page: Page,
    live_server: str,
) -> None:
    """Explicitly picking bypass in the fork dialog arms the clone.

    Guards the explicit opt-in path: there is a way to set "bypass
    permissions" while forking. From a NON-bypass codex source,
    pick "Bypass approvals & sandbox" in the fork dialog's Approval select
    (danger banner appears), Clone, and the fork carries the bypass label —
    visible again by reopening the fork dialog on the clone.

    :param page: Playwright page fixture (fresh context per test).
    :param live_server: Spawned server base URL.
    """
    fork_id: str | None = None
    session_id = _create_codex_wrapper_session(live_server, bypass=False)
    try:
        _seed_turn(session_id, _MARKER_EXPLICIT)
        page.goto(f"{live_server}/c/{session_id}")
        _ensure_chat_view(page)

        _open_fork_dialog(page, _MARKER_EXPLICIT)
        approval = page.get_by_test_id("fork-session-config-approval")
        expect(approval).to_be_visible(timeout=15_000)
        # Non-bypass source seeds the plain default — no banner yet.
        expect(approval).not_to_contain_text(_BYPASS_OPTION_LABEL)
        expect(page.get_by_test_id("fork-session-codex-bypass-banner")).not_to_be_visible()

        # The explicit, deliberate opt-in: pick the 4th (dangerous) option.
        approval.click()
        page.get_by_role("option", name=_BYPASS_OPTION_LABEL).click()
        expect(page.get_by_test_id("fork-session-codex-bypass-banner")).to_be_visible()

        fork_id = _submit_and_wait_for_fork(page, session_id)

        # The clone is bypass-armed: label present, and the reopened fork
        # dialog on the clone seeds the bypass option from it.
        labels = _session_labels(live_server, fork_id)
        assert labels.get(_BYPASS_LABEL_KEY) == "1", (
            "explicit bypass pick in the fork dialog did not arm the clone: "
            f"missing {_BYPASS_LABEL_KEY}=1 in fork labels: {labels!r}"
        )
        _ensure_chat_view(page)
        _open_fork_dialog(page, _MARKER_EXPLICIT)
        fork_approval = page.get_by_test_id("fork-session-config-approval")
        expect(fork_approval).to_be_visible(timeout=15_000)
        expect(fork_approval).to_contain_text(_BYPASS_OPTION_LABEL)
    finally:
        if fork_id is not None:
            _delete_session(live_server, fork_id)
        _delete_session(live_server, session_id)
