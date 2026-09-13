r"""E2E: an MCP elicitation that asks for fields renders a form you can answer.

``elicitation/create`` lets an MCP server declare what it needs back in
``requestedSchema``. The approval card could collect one shape of that — a lone
``answer`` enum, rendered as option buttons — and everything else fell through
to Approve / Reject, so a server asking for a branch name and a release channel
showed two buttons and no way to say either.

An ``mcp_elicitation`` event is posted the way the runner's inline callback
posts one, the SPA renders the card, and the test drives the controls. Unlike
``test_ask_user_question.py`` this needs no background thread: the event
endpoint returns the elicitation id immediately rather than parking, which is
how the runner's own callback uses it.

The binary counterpart is ``test_approval_card.py``, which drives a real
consent prompt, and the keyboard half of the story is ``test_approve_hotkey.py``:
a prompt asking for fields must not be acceptable from the keyboard, since a
keystroke would send the server none of what it asked for.
"""

from __future__ import annotations

import logging
import time

import httpx
import pytest
from playwright.sync_api import Page, expect

_log = logging.getLogger(__name__)

_APPROVAL_CARD = '[data-testid="approval-card"]'
_FORM = '[data-testid="elicitation-schema-form"]'
_FIELD = '[data-testid="elicit-field-{}"]'
_SUBMIT = '[data-testid="elicitation-schema-submit"]'

_RENDER_TIMEOUT_MS = 15_000
# Loading the conversation is slower than rendering a card in it, and slower
# again when the whole directory runs at once, so it gets its own budget.
_LOAD_TIMEOUT_MS = 60_000
# A wrong accept has to travel keydown -> handler -> POST -> server state, so
# too short a window here reads as "nothing happened" on a loaded shard.
_NON_EVENT_MS = 5_000
# The composer's placeholder changes while a prompt is pending; its accessible
# name does not, which is what makes it usable as a "transcript is up" signal.
_COMPOSER = "Message the agent"

# What a server sends when it needs values rather than consent: a required
# free-form string, a required enum, and a boolean carrying a default.
_SCHEMA = {
    "type": "object",
    "properties": {
        "branch": {"type": "string", "title": "Release branch"},
        "channel": {"type": "string", "enum": ["beta", "stable"]},
        "notify": {"type": "boolean", "title": "Notify the channel", "default": True},
    },
    "required": ["branch", "channel"],
}


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=30.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _parked(base_url: str, session_id: str, elicitation_id: str) -> bool:
    """Whether the server still holds *elicitation_id* unanswered.

    Named rather than counted, so a renamed snapshot key cannot make "drained"
    pass by returning nothing.
    """
    return any(
        e.get("elicitation_id") == elicitation_id or e.get("id") == elicitation_id
        for e in _pending_elicitations(base_url, session_id)
    )


def _wait_for_parked(base_url: str, session_id: str, elicitation_id: str) -> None:
    """Block until the prompt is in the snapshot the SPA is about to read."""
    _wait_for(lambda: _parked(base_url, session_id, elicitation_id))


def _wait_for(predicate, *, timeout_s: float = 30.0, interval_s: float = 0.5) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


def _open_session(page: Page, base_url: str, session_id: str) -> None:
    """Open the session and wait until the transcript has actually loaded."""
    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_role("textbox", name=_COMPOSER)).to_be_visible(timeout=_LOAD_TIMEOUT_MS)


def _raise_elicitation(base_url: str, session_id: str, schema: dict) -> str:
    """Post an ``mcp_elicitation`` event and return its elicitation id.

    Mirrors ``RunnerMcpManager._build_elicitation_callback``: it POSTs the
    message and schema, and the server answers with the id it parked under.

    :param base_url: Server base URL.
    :param session_id: Session to raise the prompt on.
    :param schema: The MCP ``requestedSchema``.
    :returns: The server-minted elicitation id.
    """
    resp = httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "mcp_elicitation",
            "data": {"message": "Which release should I cut?", "requestedSchema": schema},
        },
        timeout=30.0,
    )
    resp.raise_for_status()
    elicitation_id = resp.json().get("elicitation_id")
    assert isinstance(elicitation_id, str) and elicitation_id, resp.text
    return elicitation_id


@pytest.mark.timeout(180)
def test_a_schema_that_names_fields_renders_a_form_and_submits(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """One control per property, gated on the required ones, then answered."""
    base_url, session_id = seeded_session
    _log.info("seeded session ready: base_url=%s session_id=%s", base_url, session_id)

    eid = _raise_elicitation(base_url, session_id, _SCHEMA)
    _wait_for_parked(base_url, session_id, eid)
    _open_session(page, base_url, session_id)

    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has=page.locator(_FORM))
        .first
    )
    expect(card).to_be_visible(timeout=_RENDER_TIMEOUT_MS)
    form = card.locator(_FORM)

    # One control per declared property, of the type the schema asked for, and
    # labelled by ``title`` where the server sent one.
    expect(form.locator(_FIELD.format("branch"))).to_contain_text("Release branch")
    branch = form.locator(f'{_FIELD.format("branch")} input[type="text"]')
    channel = form.locator(f"{_FIELD.format('channel')} select")
    notify = form.locator(f'{_FIELD.format("notify")} input[type="checkbox"]')
    expect(branch).to_be_visible()
    expect(channel).to_be_visible()
    expect(notify).to_be_visible()
    # ``default: true`` prefills, so the person is not asked to restate it.
    expect(notify).to_be_checked()

    # The whole point of the gate: the server declared two required fields, so
    # an accept cannot be sent before they are answered.
    submit = form.locator(_SUBMIT)
    expect(submit).to_be_disabled()

    # Every required property, not just one of them: answering `branch` alone
    # still leaves the server short of what it asked for.
    branch.fill("release/2.4")
    expect(submit).to_be_disabled()
    channel.select_option("stable")
    expect(submit).to_be_enabled()

    submit.click()

    responded = page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first
    expect(responded).to_be_visible(timeout=_RENDER_TIMEOUT_MS)
    _wait_for(lambda: not _parked(base_url, session_id, eid))


@pytest.mark.timeout(180)
def test_a_consent_prompt_keeps_its_buttons(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A schema naming no fields is a yes/no question, and stays one.

    A policy ASK carries no properties. Rendering an empty form for it would
    replace a working control with an unusable one. This one passes against the
    pre-change UI as well — it guards the path this change must not touch,
    rather than proving the change.
    """
    base_url, session_id = seeded_session

    eid = _raise_elicitation(base_url, session_id, {"type": "object"})
    _wait_for_parked(base_url, session_id, eid)
    _open_session(page, base_url, session_id)

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_RENDER_TIMEOUT_MS)
    expect(card.locator(_FORM)).to_have_count(0)
    expect(card.get_by_role("button", name="Approve")).to_be_visible()
    reject = card.get_by_role("button", name="Reject")
    expect(reject).to_be_visible()

    # Answer it rather than leaving it parked for whatever runs next.
    reject.click()
    _wait_for(lambda: not _parked(base_url, session_id, eid))


@pytest.mark.timeout(180)
def test_the_keyboard_cannot_accept_a_prompt_that_asks_for_fields(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Cmd/Ctrl+Enter must not walk around the disabled Submit.

    The hotkey accepts a pending prompt with no content. That is right for a
    yes/no gate and wrong here: the server asked for values, and accepting from
    the keyboard would send it none of them.

    No wait proves a keystroke did nothing, so the same test goes on to answer
    the form. Reaching ``responded`` that way is the control: the page was live
    and taking input all along, so the earlier stillness was the guard working
    rather than a keystroke that never landed.
    """
    base_url, session_id = seeded_session

    eid = _raise_elicitation(base_url, session_id, _SCHEMA)
    _wait_for_parked(base_url, session_id, eid)
    _open_session(page, base_url, session_id)

    card = (
        page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
        .filter(has=page.locator(_FORM))
        .first
    )
    expect(card).to_be_visible(timeout=_RENDER_TIMEOUT_MS)
    form = card.locator(_FORM)

    page.keyboard.press("Control+Enter")
    page.wait_for_timeout(_NON_EVENT_MS)

    # Still pending, on the card and on the server, and still refusing a submit
    # it has no values for.
    expect(card).to_be_visible()
    expect(form.locator(_SUBMIT)).to_be_disabled()
    assert _parked(base_url, session_id, eid), "the keystroke accepted it"

    form.locator(f'{_FIELD.format("branch")} input[type="text"]').fill("release/2.4")
    form.locator(f"{_FIELD.format('channel')} select").select_option("stable")
    form.locator(_SUBMIT).click()
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first).to_be_visible(
        timeout=_RENDER_TIMEOUT_MS
    )
    _wait_for(lambda: not _parked(base_url, session_id, eid))
