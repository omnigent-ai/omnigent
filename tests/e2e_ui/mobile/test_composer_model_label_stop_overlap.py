"""E2E: composer model/effort text must stay clear of the Stop button.

On a phone-sized viewport (iOS web), the composer's read-only
``<Model> <Effort>`` label — e.g. ``databricks-claude-fable-5 xHigh`` on a
Databricks-backed claude-native session — extends beneath the Stop
(Interrupt) button while a turn is running, so the composer controls are
unreadable: the model text runs under the destructive square button instead
of truncating beside it.

User journey covered (the reporter's, on iOS web):

1. open a session bound to a long Databricks model id at xhigh effort on a
   phone-sized viewport,
2. send a message — the agent starts working, so the composer's Send button
   becomes the destructive Stop (Interrupt) square,
3. the model/effort text in the composer action row extends beneath the
   Stop button instead of truncating.

Harness notes:

- The session is the standard ``seeded_session`` (a real server-backed
  ``hello_world`` session); the browser's ``GET /v1/sessions/{id}`` snapshot
  is patched into a claude-native session on a Databricks workspace model at
  ``xhigh`` effort — the exact shape the reporter's session has — the same
  route-patch approach as ``chat/test_claude_model_picker.py``. The catalog
  rows carry a ``databricks`` ``source`` so the label renders inside the
  ``composer-model-source`` tooltip wrapper, exactly as on a real Databricks
  workspace session.
- The turn is held open with the mock LLM's ``block`` gate (released in the
  ``finally``), so the Stop button is genuinely showing while the geometry
  is measured — the same running-turn state the reporter saw.
- The viewport matches Playwright's "iPhone 13" profile (390x664), so a
  recorder run with ``--device "iPhone 13"`` films pixel-exact.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urlparse

import httpx
from playwright.sync_api import FloatRect, Locator, Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry, reset_mock_llm

# iPhone 13-class portrait viewport (matches Playwright's "iPhone 13" device
# profile, so a recorder run with ``--device "iPhone 13"`` films pixel-exact).
_IPHONE_VIEWPORT = {"width": 390, "height": 664}

# The reporter's session shape: a Databricks-served Claude model (shown raw —
# the id is not in the alias catalog) at xhigh effort. The composer label
# reads ``databricks-claude-fable-5-extended-thinking xHigh``. Long enough
# (~282px at text-sm) that the label MUST truncate to fit a phone-width
# action row — on the buggy build it doesn't truncate at all and extends
# beneath the Stop button instead.
_MODEL_ID = "databricks-claude-fable-5-extended-thinking"
_EFFORT = "xhigh"

# Unique sentinel so the mock LLM's blocking gate fires only for this test's
# turn (content-based routing), never for background LLM traffic.
_SENTINEL = "sentinel-model-label-overlap keep this turn running"

# Databricks workspace catalog rows, as a real claude-native launch reports
# them (aliases + a non-secret ``source``). The bound model id is
# intentionally NOT in the catalog, so the label shows it verbatim — the
# pre-catalog/raw-id read-out the reporter's screenshot shows.
_DATABRICKS_SOURCE = {
    "kind": "databricks",
    "label": "Workspace",
    "name": "production-west",
    "host": "ws.example.com",
}
_MODEL_OPTIONS = [
    {
        "id": "opus",
        "model": "system.ai.claude-opus-4-10",
        "displayName": "Opus 4.10",
        "isDefault": False,
        "source": _DATABRICKS_SOURCE,
    },
    {
        "id": "sonnet",
        "model": "system.ai.claude-sonnet-5",
        "displayName": "Sonnet 5",
        "isDefault": True,
        "source": _DATABRICKS_SOURCE,
    },
]


def _patch_session_as_databricks_claude_native(page: Page, session_id: str) -> None:
    """Shape the browser's session snapshot like the reporter's session.

    Patches only ``GET /v1/sessions/{session_id}`` as seen by the browser:
    claude-native wrapper labels, a Databricks model id the catalog doesn't
    list (rendered raw by the composer label), catalog rows carrying a
    ``databricks`` source, and ``xhigh`` reasoning effort. Everything else —
    the session, the runner, the turn — is the real spawned server.

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
        payload["reasoning_effort"] = _EFFORT
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def _box(locator: Locator) -> FloatRect:
    """Return the element's bounding box, failing loudly when it has none.

    :param locator: A locator resolved to exactly one visible element.
    :returns: The element's bounding box.
    """
    box = locator.bounding_box()
    assert box is not None, f"element {locator} has no bounding box"
    return box


def _intersection(a: FloatRect, b: FloatRect) -> tuple[float, float]:
    """Return the (horizontal, vertical) overlap in px between two boxes.

    :param a: First bounding box.
    :param b: Second bounding box.
    :returns: ``(x_overlap, y_overlap)``; both positive iff the boxes intersect.
    """
    x_overlap = min(a["x"] + a["width"], b["x"] + b["width"]) - max(a["x"], b["x"])
    y_overlap = min(a["y"] + a["height"], b["y"] + b["height"]) - max(a["y"], b["y"])
    return (x_overlap, y_overlap)


def _release_gates(mock_url: str) -> None:
    """Release any turn still parked on the mock LLM's blocking gate.

    :param mock_url: Mock LLM server base URL.
    :returns: None.
    """
    deadline = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        pending = httpx.get(f"{mock_url}/gate/pending", timeout=5.0, trust_env=False)
        pending.raise_for_status()
        if not pending.json().get("pending"):
            return
        httpx.post(f"{mock_url}/gate/release", timeout=5.0, trust_env=False).raise_for_status()
        time.sleep(0.2)


def test_composer_model_label_stays_clear_of_stop_button_on_mobile(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """While a turn runs on a phone viewport, the model/effort text must not
    run under the Stop (Interrupt) button.

    Sends a message whose turn the mock LLM holds open, waits for the
    composer's destructive Stop square, and asserts the composer's
    model/effort label neither intersects the Stop button's bounding box
    nor pushes it off-screen. A ≤1px touch is tolerated (border rounding /
    antialiasing); anything more is the reported overlap.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound session.
    :param mock_llm_server_url: Session-scoped mock LLM server URL.
    :returns: None.
    """
    base_url, session_id = seeded_session

    page.set_viewport_size(_IPHONE_VIEWPORT)
    _patch_session_as_databricks_claude_native(page, session_id)

    # Hold this test's turn open so the Stop button is genuinely showing
    # while the geometry is measured. Content-matched to the sentinel on a
    # dedicated queue key; several blocked responses are queued because
    # background LLM traffic that embeds the user message (e.g. title
    # generation) can match the sentinel too and would otherwise consume
    # the only gate, letting the agent turn fall through to the instant
    # ``gpt-4o-mini`` fallback and finish before the geometry is measured.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "done", "block": True}] * 4,
        key="model-label-overlap-gate",
        match=_SENTINEL,
    )

    try:
        page.goto(f"{base_url}/c/{session_id}")

        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=15_000)

        # The reporter's label must actually be on screen, or the geometry
        # assertions below would vacuously pass.
        label = page.get_by_test_id("composer-model-effort-label")
        expect(label).to_be_visible(timeout=15_000)
        expect(label).to_contain_text(_MODEL_ID)

        composer.fill(_SENTINEL)
        page.get_by_role("button", name="Send", exact=True).click()

        # The turn is running (gated open on the mock LLM) and the draft is
        # cleared, so the Send button is now the destructive Stop square.
        stop = page.get_by_role("button", name="Interrupt")
        expect(stop).to_be_visible(timeout=60_000)

        # Hold the running state briefly so the failure is observable in a
        # recording before the geometry assertions run.
        page.wait_for_timeout(1_500)

        label_box = _box(label)
        stop_box = _box(stop)

        # The Stop button itself must be usable: fully on-screen. A row that
        # refuses to shrink pushes it past the right edge instead.
        viewport_right = _IPHONE_VIEWPORT["width"]
        assert stop_box["x"] + stop_box["width"] <= viewport_right + 1.0, (
            f"the Stop button is pushed off-screen: button at "
            f"(x={stop_box['x']:.0f}, w={stop_box['width']:.0f}) vs viewport "
            f"width {viewport_right}"
        )

        # The reported failure: the model/effort text extends beneath the
        # Stop button instead of truncating beside it.
        x_overlap, y_overlap = _intersection(label_box, stop_box)
        assert not (x_overlap > 1.0 and y_overlap > 1.0), (
            f"the composer model/effort label overlaps the Stop button by "
            f"{x_overlap:.0f}x{y_overlap:.0f}px: label at "
            f"(x={label_box['x']:.0f}, y={label_box['y']:.0f}, "
            f"w={label_box['width']:.0f}, h={label_box['height']:.0f}), "
            f"Stop button at (x={stop_box['x']:.0f}, y={stop_box['y']:.0f}, "
            f"w={stop_box['width']:.0f}, h={stop_box['height']:.0f})"
        )
    finally:
        # Drop the snapshot route before teardown so an in-flight fetch
        # doesn't error against the closing context, then let the gated
        # turn finish so the shared server tears down clean.
        page.unroute_all(behavior="ignoreErrors")
        _release_gates(mock_llm_server_url)
        reset_mock_llm(mock_llm_server_url)
