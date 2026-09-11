"""E2E: hovering the composer's model/effort pill must show ONE tooltip.

Hovering the composer's model / effort pill (e.g. ``Fable 5.1 xHigh ⌄``)
shows two overlapping tooltip surfaces instead of one, on both a freshly
created session's composer and an existing session's composer:

- behind: the pill's own config summary (``composer-config-gear-tooltip``:
  ``Harness: … / Model: … / Effort: … / Connection: …``), partially clipped;
- in front: the model-source wrapper's surface
  (``composer-model-source-tooltip``: ``Model configuration /
  Connection: …``), anchored to the same pill.

Both open after the same hover delay because ``SessionHarnessPicker``'s pill
tooltip is nested inside ``ComposerModelSource``'s tooltip trigger — two
tooltip surfaces for one trigger. Expected: a single tooltip whose content
carries the union (harness, model, effort, connection); never two surfaces.

User journey covered:

1. open a session in the web UI (a just-created one, or one with a
   completed turn),
2. hover the model / effort pill in the composer,
3. two stacked tooltips appear anchored to the pill, the lower one clipped.

Harness notes:

- The session is the standard ``seeded_session`` (a real server-backed
  ``hello_world`` session); the browser's ``GET /v1/sessions/{id}`` snapshot
  is patched into a claude-native session whose catalog rows carry a
  ``databricks`` ``source`` — the exact shape a Databricks-workspace
  claude-native session reports, and the state that makes the model-source
  wrapper render its tooltip. Same route-patch approach as
  ``mobile/test_composer_model_label_stop_overlap.py``. Everything else —
  the session, the runner, the turn — is the real spawned server.
- The "existing session" variant first completes a real turn against the
  mock LLM, so the composer hovered is one of a session with history.

Red while the bug lives: two tooltip surfaces open on one hover. Green after
a fix: exactly one surface, still carrying harness + model + connection.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry, reset_mock_llm

_COMPOSER_LABEL = "Message the agent"

# Unique sentinel so the mock LLM's scripted reply fires only for this test's
# turn (content-based routing), never for background LLM traffic.
_SENTINEL = "sentinel-pill-double-tooltip please answer briefly"
_REPLY = "Acknowledged, turn complete."

# Databricks workspace catalog rows, as a real claude-native launch reports
# them: aliases plus a non-secret ``source``. The ``source`` is what makes
# the composer render the model label inside the model-source tooltip
# wrapper — the reported state (``Connection: Databricks · oss``).
_DATABRICKS_SOURCE = {
    "kind": "databricks",
    "label": "Workspace",
    "name": "oss",
    "host": "oss.databricks.com",
}
_MODEL_ID = "system.ai.claude-fable-5-1"
_MODEL_OPTIONS = [
    {
        "id": "fable",
        "model": _MODEL_ID,
        "displayName": "Fable 5.1",
        "isDefault": True,
        "source": _DATABRICKS_SOURCE,
    },
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": False,
        "source": _DATABRICKS_SOURCE,
    },
]

# The union of configuration the single surviving tooltip must carry — the
# harness, a model row, and the connection the two stacked surfaces split
# between them while the bug lives. The model row's value is asserted
# per-test: a completed live turn updates the reported model, so only the
# no-turn variant can pin the exact catalog name.
_UNION_NEEDLES = ("Harness: Claude", "Model:", "Connection: Databricks · oss")


def _patch_session_as_databricks_claude_native(page: Page, session_id: str) -> None:
    """Shape the browser's session snapshot like the reporter's session.

    Patches only ``GET /v1/sessions/{session_id}`` as seen by the browser:
    claude-native wrapper labels, a Databricks model catalog whose rows carry
    a ``databricks`` source, and ``xhigh`` reasoning effort. Everything else
    goes to the real spawned server.

    :param page: Playwright page, before navigation.
    :param session_id: Session id to patch, e.g. ``"conv_abc123"``.
    :returns: None.
    """

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {
            **payload.get("labels", {}),
            "omnigent.wrapper": "claude-code-native-ui",
        }
        payload["harness"] = "claude"
        payload["llm_model"] = _MODEL_ID
        payload["model_options"] = _MODEL_OPTIONS
        payload["reasoning_effort"] = "xhigh"
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def _hover_pill_and_collect_tooltips(page: Page) -> list[dict[str, Any]]:
    """Hover the composer's model/effort pill and return the open tooltips.

    Moves the pointer away first (so the hover-delay timers start from a
    clean state), hovers the pill, waits for the first tooltip surface to
    open, then dwells long enough for any second surface sharing the same
    hover delay to open too before sampling the DOM.

    :param page: Playwright page showing a session's composer.
    :returns: One dict per open tooltip surface: ``testid``, ``text``,
        ``rect`` (viewport-relative bounding box).
    """
    pill = page.get_by_test_id("composer-config-gear")
    pill.wait_for(state="visible", timeout=30_000)
    page.mouse.move(5, 5)
    page.wait_for_timeout(400)
    pill.hover()
    page.wait_for_selector('[data-slot="tooltip-content"]', state="visible", timeout=10_000)
    # Both surfaces share the same hover delay; dwell so a second surface
    # (the bug) has opened before sampling — and so a recording of the run
    # visibly shows the stacked state.
    page.wait_for_timeout(2_000)
    return page.evaluate(
        """() => [...document.querySelectorAll('[data-slot="tooltip-content"]')]
            .filter((el) => {
                const rect = el.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 &&
                    getComputedStyle(el).visibility !== 'hidden';
            })
            .map((el) => {
                const rect = el.getBoundingClientRect();
                return {
                    testid: el.getAttribute('data-testid'),
                    text: el.textContent,
                    rect: {x: rect.x, y: rect.y, width: rect.width, height: rect.height},
                };
            })"""
    )


def _assert_single_merged_tooltip(
    tooltips: list[dict[str, Any]],
    *,
    extra_needles: tuple[str, ...] = (),
) -> None:
    """One tooltip surface, carrying the union of the configuration rows.

    :param tooltips: Open tooltip surfaces from
        :func:`_hover_pill_and_collect_tooltips`.
    :param extra_needles: Additional substrings the single surface must
        carry (e.g. the exact model name when no turn has updated it).
    :returns: None.
    """
    summary = ", ".join(f"{tip['testid']}: {tip['text']!r} @ {tip['rect']}" for tip in tooltips)
    assert len(tooltips) == 1, (
        f"hovering the composer model/effort pill opened {len(tooltips)} tooltip "
        f"surfaces; expected exactly one merged tooltip. Open surfaces: {summary}"
    )
    text = tooltips[0]["text"]
    for needle in (*_UNION_NEEDLES, *extra_needles):
        assert needle in text, (
            f"the pill's single tooltip must carry the merged configuration; "
            f"missing {needle!r} in {text!r}"
        )


def test_new_session_composer_pill_shows_one_tooltip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A just-created session's composer pill opens exactly one tooltip.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_databricks_claude_native(page, session_id)
    try:
        page.goto(f"{base_url}/c/{session_id}")
        tooltips = _hover_pill_and_collect_tooltips(page)
        _assert_single_merged_tooltip(tooltips, extra_needles=("Model: Fable 5.1",))
    finally:
        page.unroute_all(behavior="ignoreErrors")


def test_existing_session_composer_pill_shows_one_tooltip(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """A session with a completed turn opens exactly one pill tooltip.

    Completes one real turn against the mock LLM first, so the composer
    hovered belongs to an existing session with history.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_databricks_claude_native(page, session_id)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": _REPLY}],
        key="pill-double-tooltip-turn",
        match=_SENTINEL,
    )
    try:
        page.goto(f"{base_url}/c/{session_id}")
        composer = page.get_by_label(_COMPOSER_LABEL)
        expect(composer).to_be_visible(timeout=30_000)
        composer.fill(_SENTINEL)
        page.get_by_role("button", name="Send", exact=True).click()
        # The turn is complete once the scripted reply is rendered and the
        # composer's action button is Send again (working → idle).
        expect(page.get_by_text(_REPLY)).to_be_visible(timeout=90_000)
        expect(page.get_by_role("button", name="Send", exact=True)).to_be_visible(timeout=30_000)
        tooltips = _hover_pill_and_collect_tooltips(page)
        _assert_single_merged_tooltip(tooltips)
    finally:
        page.unroute_all(behavior="ignoreErrors")
        reset_mock_llm(mock_llm_server_url)
