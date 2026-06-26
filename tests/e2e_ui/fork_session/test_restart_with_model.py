"""Browser e2e: the codex-only "Restart with model…" affordance.

Codex applies its model at launch, not mid-turn, so "restart on a different
model" is a fork that carries history: the dialog drives the SAME
``POST /v1/sessions/{id}/fork`` path the Clone dialog uses, with an explicit
``model_override``. Two things only a browser can prove live here:

1. The trigger is gated on the codex (GPT) harness family — visible for a
   codex-native session, absent for a non-codex (openai-agents) one.
2. The dialog's client-side validation gates submit (empty / unchanged /
   flag-shaped model id all disabled), and on submit it forks with the chosen
   ``model_override`` and navigates into the clone.

The e2e harness only runs the seeded ``hello_world`` (openai-agents) agent —
there is no codex CLI. So, like ``test_codex_model_metadata.py``, this patches
only the browser's view of ``GET /v1/sessions/{id}/agent`` to report a codex
harness. The fork POST is left to hit the real server (openai-agents is
multi-model, so the server's family check passes), and the test asserts both
the request body and the resulting navigation.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

_COMPOSER = "Ask the agent anything…"


def _patch_agent_as_codex(page: Page, session_id: str) -> None:
    """Make the browser see *session_id*'s agent as codex-native.

    Fetches the real ``GET /v1/sessions/{id}/agent`` response and overrides
    only its ``harness`` so the UI's codex-family gate fires. Everything else
    (name, mcp servers, policies) is preserved, so the agent-info popover
    renders normally. Server-side behavior is untouched — this patch is
    browser-scoped.

    :param page: Playwright page, before navigation.
    :param session_id: Source session id whose agent to recast, e.g.
        ``"conv_abc123"``.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if request.method != "GET" or urlparse(request.url).path != (
            f"/v1/sessions/{session_id}/agent"
        ):
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        # The canonical codex-native spelling the family gate accepts.
        payload["harness"] = "codex-native"
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(f"**/v1/sessions/{session_id}/agent", _handle)


def _capture_fork_requests(page: Page, session_id: str) -> list[dict]:
    """Record fork POST bodies for *session_id*, forwarding them to the server.

    :param page: Playwright page, before navigation.
    :param session_id: Source session id whose ``/fork`` POSTs to capture.
    :returns: A list, appended to with each fork request body.
    """
    bodies: list[dict] = []

    def _handle(route: Route) -> None:
        request = route.request
        if request.method == "POST":
            bodies.append(json.loads(request.post_data or "{}"))
        route.continue_()

    page.route(f"**/v1/sessions/{session_id}/fork", _handle)
    return bodies


def _open_agent_info(page: Page) -> None:
    """Open the desktop agent-info popover from a known-closed state.

    :param page: Playwright page on a ``/c/<id>`` route.
    """
    page.keyboard.press("Escape")
    trigger = page.get_by_test_id("agent-info-trigger")
    expect(trigger).to_be_visible(timeout=30_000)
    trigger.click()
    # The "Policies" section label proves the popover content mounted.
    expect(page.get_by_text("Policies", exact=True)).to_be_visible(timeout=15_000)


def test_restart_with_model_forks_codex_session(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Codex session → trigger shows, dialog validates, submit forks + navigates.

    Failure modes this catches:

    - The codex-only trigger never renders (gate broken) or renders for the
      wrong harness.
    - The dialog lets an unchanged / flag-shaped model id through the submit
      gate (the shared charset guard regressed client-side).
    - Submit fails to send ``model_override`` on the fork POST, or doesn't
      navigate into the clone.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to codex-native.
    """
    base_url, session_id = seeded_session
    _patch_agent_as_codex(page, session_id)
    fork_bodies = _capture_fork_requests(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    _open_agent_info(page)

    # (1) The codex-only trigger is present for this codex-native session.
    trigger = page.get_by_test_id("restart-with-model-trigger")
    expect(trigger).to_be_visible(timeout=15_000)
    trigger.click()

    dialog = page.get_by_test_id("restart-model-dialog")
    expect(dialog).to_be_visible(timeout=15_000)
    model_input = page.get_by_test_id("restart-model-input")
    submit = page.get_by_test_id("restart-model-submit")

    # (2) Validation gating — the seeded session has no model override, so the
    # field starts empty and submit is disabled.
    expect(submit).to_be_disabled()
    # A flag-shaped value fails the shared charset guard → still disabled.
    model_input.fill("--evil")
    expect(submit).to_be_disabled()
    # A valid, different codex model id enables submit.
    model_input.fill("databricks-gpt-5-4-mini")
    expect(submit).to_be_enabled()

    # (3) Submit forks with the chosen model and navigates into the clone.
    submit.click()
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})conv_[0-9a-f]+"),
        timeout=30_000,
    )
    assert fork_bodies, "the dialog never issued a fork POST"
    assert fork_bodies[-1].get("model_override") == "databricks-gpt-5-4-mini", (
        f"fork must carry the chosen model_override; got {fork_bodies[-1]!r}"
    )


def test_restart_with_model_hidden_for_non_codex(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A non-codex (openai-agents) session never shows the restart trigger.

    The seeded ``hello_world`` agent runs the openai-agents harness, which
    applies its model per-turn — there is no launch-time restart. The trigger
    must stay hidden so the affordance is offered only where it is honest.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        openai-agents session (left unpatched).
    """
    base_url, session_id = seeded_session

    page.goto(f"{base_url}/c/{session_id}")
    expect(page.get_by_placeholder(_COMPOSER)).to_be_visible(timeout=30_000)

    _open_agent_info(page)

    # The popover mounted (Policies asserted in _open_agent_info), but the
    # codex-only trigger must be absent for this harness.
    expect(page.get_by_test_id("restart-with-model-trigger")).to_have_count(0)
