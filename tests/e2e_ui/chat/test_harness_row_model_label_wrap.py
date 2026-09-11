"""E2E: the composer picker's harness row keeps the model name on one line.

In an existing claude-native session, clicking the composer's model/effort
pill opens a popover whose ``Harnesses`` section shows one row per harness:
``<harness> | <model> | Edit``. When the bound model's display name is longer
than the row's model column allows (e.g. ``Fable 5.1 (1M context)``), the
name wraps onto two lines mid-label (``Fable 5.1 (1M`` / ``context)``),
pushing the row taller than its neighbours. The name must render on a single
line — fitting or ellipsized — and the row's height must stay consistent
with the picker's other rows.

The journey: open an existing session bound to a model with a long display
name → click the model/effort pill in the composer → the Harnesses row shows
the model name on one line, at the same row height as its neighbours.

Which label lengths overflow the column shifts with the platform's font
metrics (the reporter's macOS renders ``Fable 5.1 (1M context)`` a few px
wider than this suite's Linux font stack does), so the test drives the
journey with two real Claude catalog labels: the reported one, and the
longer ``opusplan`` alias label that overflows the column on any font stack.

Harness notes: the session is the standard ``seeded_session`` (a real
server-backed ``hello_world`` session); the browser's
``GET /v1/sessions/{id}`` snapshot is patched into a claude-native session
whose catalog carries the long display name and whose bound model resolves
to it — the same route-patch approach as
``chat/test_claude_model_picker_backticks.py``.
"""

from __future__ import annotations

import json
from urllib.parse import urlparse

import pytest
from playwright.sync_api import FloatRect, Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

# Real claude-native catalog rows, each long enough that the harness row's
# fixed-width model column may not fit it. The first is the reporter's exact
# model; the second is Claude Code's ``opusplan`` slot label, which is wider
# than the column under every font stack — the deterministic regression
# guard for the wrap.
_LONG_LABEL_CASES = [
    pytest.param("claude-fable-5-1[1m]", "Fable 5.1 (1M context)", id="fable-1m"),
    pytest.param("claude-sonnet-5", "Opus in plan mode, else Sonnet", id="opusplan"),
]


def _catalog_for(llm_model: str, display_name: str) -> list[dict[str, object]]:
    """Claude-native catalog rows binding ``llm_model`` to ``display_name``.

    :param llm_model: The session's bound model id.
    :param display_name: The catalog display name the composer must render.
    :returns: Catalog rows as a claude-native launch reports them.
    """
    return [
        {
            "id": "sonnet-default",
            "model": "claude-sonnet-5-standard",
            "displayName": "Sonnet 5",
            "isDefault": True,
        },
        {
            "id": "bound",
            "model": llm_model,
            "displayName": display_name,
            "isDefault": False,
        },
    ]


def _patch_session_as_claude_native(
    page: Page,
    session_id: str,
    llm_model: str,
    display_name: str,
) -> None:
    """Reshape the browser's session snapshot into the reporter's session.

    Patches only ``GET /v1/sessions/{session_id}`` as seen by the browser:
    claude-native wrapper labels, a catalog carrying the long display name,
    and a bound model that resolves to it. Everything else — the session,
    the server, the SPA — is the real spawned stack.

    :param page: Playwright page, before navigation.
    :param session_id: The seeded session's id.
    :param llm_model: Bound model id reported for the session.
    :param display_name: Catalog display name for the bound model.
    :returns: None.
    """
    model_options = _catalog_for(llm_model, display_name)

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
        payload["llm_model"] = llm_model
        payload["model_options"] = model_options
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


def _rendered_line_count(label: Locator) -> dict[str, float]:
    """Measure how many line boxes the label's text occupies.

    :param label: The model-name span inside the harness row.
    :returns: ``{"height", "lineHeight", "lines"}`` measured in the page.
    """
    return label.evaluate(
        """
        (el) => {
          const style = getComputedStyle(el);
          const lineHeight =
            parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.2;
          const height = el.getBoundingClientRect().height;
          return { height, lineHeight, lines: Math.round(height / lineHeight) };
        }
        """
    )


@pytest.mark.parametrize(("llm_model", "display_name"), _LONG_LABEL_CASES)
def test_harness_row_model_name_stays_on_one_line(
    page: Page,
    seeded_session: tuple[str, str],
    llm_model: str,
    display_name: str,
) -> None:
    """The harness row renders a long model name on a single line.

    Opens the composer's model/effort popover on a claude-native session
    bound to a long-named model and asserts the harness row's model name
    occupies exactly one line box (fitting or ellipsized — never wrapped
    mid-label) and that the row's height matches the popover's single-line
    neighbours.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` of a runner-bound
        session; the browser snapshot is patched claude-native.
    :param llm_model: Bound model id reported for the session.
    :param display_name: Catalog display name the harness row must render.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id, llm_model, display_name)

    try:
        page.goto(f"{base_url}/c/{session_id}")

        gear = page.get_by_test_id("composer-config-gear")
        expect(gear).to_be_visible(timeout=15_000)
        gear.click()

        menu = page.get_by_test_id("composer-agent-menu")
        expect(menu).to_be_visible(timeout=10_000)

        # The long label must actually be in the harness row, or the
        # geometry assertions below would vacuously pass.
        row = page.get_by_test_id("composer-agent-edit")
        expect(row).to_be_visible(timeout=10_000)
        expect(row).to_contain_text(display_name)
        label = row.get_by_text(display_name)
        expect(label).to_be_visible()

        # Let the popover's open animation settle (and hold the state long
        # enough to be observable in a recording) before measuring.
        page.wait_for_timeout(1_500)

        # The reported failure: the model name wraps onto two lines within
        # the row instead of rendering on one.
        metrics = _rendered_line_count(label)
        assert metrics["lines"] == 1, (
            f"the harness row's model name {display_name!r} wraps onto "
            f"{metrics['lines']:.0f} lines (label height "
            f"{metrics['height']:.1f}px at line-height "
            f"{metrics['lineHeight']:.1f}px); it must render on a single "
            f"line, fitting or ellipsized"
        )

        # The wrapped label's knock-on effect: the harness row grows taller
        # than the popover's single-line rows. Compare against the
        # "Advanced settings…" item, which always renders one line.
        neighbour = page.get_by_test_id("composer-advanced-settings")
        expect(neighbour).to_be_visible()
        row_box = _box(row)
        neighbour_box = _box(neighbour)
        assert row_box["height"] <= neighbour_box["height"] + metrics["lineHeight"] / 2, (
            f"the harness row is {row_box['height']:.1f}px tall while its "
            f"single-line neighbour is {neighbour_box['height']:.1f}px — the "
            f"row height must stay consistent with the picker's other rows"
        )
    finally:
        # Drop the snapshot route before teardown so an in-flight fetch
        # doesn't error against the closing context.
        page.unroute_all(behavior="ignoreErrors")
