"""E2E: composer pills render the full permission-mode and model names.

On a claude-native session the
composer's permission-mode pill clips "Bypass permissions" to "Bypass per…"
and the model pill clips "Fable 5.1 (1M context)" to "Fable 5.1 …" even when
the composer's bottom row has ample free horizontal space between the two
pills. The labels sit behind fixed max-width caps (``max-w-20`` on the
permission label span, ``md:max-w-40`` on the model trigger), so they
ellipsize regardless of the space actually available.

Each test first proves the row has more free space than the label needs
(the precondition that makes truncation a bug rather than a legitimate
overflow response), then asserts the label is not visually ellipsized.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry

_FABLE_MODEL = "system.ai.claude-fable-5-1"
_FABLE_DISPLAY_NAME = "Fable 5.1 (1M context)"
_PERMISSION_MODE_LABEL = "Bypass permissions"

_MODEL_OPTIONS = [
    {
        "id": "fable",
        "model": _FABLE_MODEL,
        "displayName": _FABLE_DISPLAY_NAME,
        "isDefault": True,
    },
]


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Pin the "normal desktop width" from the report.

    Wide enough that the pill row shows a large empty gap between the two
    pills — the free space the labels should be allowed to use. The video
    size matches so a ``--video on`` run films the pills readably (ignored
    when video recording is off).
    """
    return {
        **browser_context_args,
        "viewport": {"width": 1600, "height": 900},
        "record_video_size": {"width": 1600, "height": 900},
    }


@pytest.fixture(autouse=True)
def _finish_snapshot_routes(page: Page) -> Iterator[None]:
    """Drain snapshot response handlers before Playwright disposes the page."""
    yield
    page.unroute_all(behavior="wait")


def _patch_session_as_claude_native(page: Page, session_id: str) -> None:
    """Patch the browser's session snapshot into a claude-native response.

    The server fixture seeds a normal ``hello_world`` session so the page
    boots against the real app/server. This route patch changes only the
    ``GET /v1/sessions/{session_id}`` snapshot as the browser sees it: a
    claude-native session in ``bypassPermissions`` mode, bound to the
    long-named ``Fable 5.1 (1M context)`` catalog model at xhigh effort —
    the exact composer labels from the bug report.

    :param page: Playwright page, before navigation.
    :param session_id: Session id whose snapshot to patch.
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
            "omnigent.claude_native.permission_mode": "bypassPermissions",
        }
        payload["harness"] = "claude"
        payload["llm_model"] = _FABLE_MODEL
        payload["model_options"] = _MODEL_OPTIONS
        payload["reasoning_effort"] = "xhigh"
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)


def _horizontal_overflow(label: Locator) -> int:
    """Pixels of the label's content hidden by CSS truncation (0 = full)."""
    return label.evaluate("el => el.scrollWidth - el.clientWidth")


def _action_row_free_space(page: Page) -> float:
    """Visible empty pixels between the row's leading and trailing pills.

    The leading group is ``flex-1``, so the row's unused width is the gap
    between the leading group's last rendered control and the trailing
    group — the "wide gap between the two pills" from the report.
    """
    return page.get_by_test_id("composer-action-row").evaluate(
        """
        (row) => {
          const [leading, trailing] = row.children;
          const leadingChildren = [...leading.children];
          const leadingEdge = leadingChildren.length
            ? Math.max(
                ...leadingChildren.map((el) => el.getBoundingClientRect().right),
              )
            : leading.getBoundingClientRect().left;
          return trailing.getBoundingClientRect().left - leadingEdge;
        }
        """
    )


def test_permission_mode_pill_shows_full_label_when_space_is_free(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The permission pill renders "Bypass permissions" unclipped given room.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to
        claude-native in ``bypassPermissions`` mode.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    chip = page.get_by_test_id("composer-permission-chip")
    expect(chip).to_be_visible(timeout=15_000)
    label = chip.locator("span")
    # The full mode name is in the DOM either way; truncation is visual.
    expect(label).to_have_text(_PERMISSION_MODE_LABEL)

    overflow = _horizontal_overflow(label)
    free_space = _action_row_free_space(page)
    # Precondition, not the bug: the row must genuinely have room for the
    # full label, or an ellipsis would be the correct overflow behavior.
    assert free_space > overflow + 48, (
        f"composer row too narrow to judge truncation ({free_space:.0f}px free)"
    )
    assert overflow <= 1, (
        f"permission-mode pill is ellipsized: {_PERMISSION_MODE_LABEL!r} is "
        f"clipped by {overflow}px while the composer row still has "
        f"{free_space:.0f}px of free space"
    )


def test_model_pill_shows_full_model_name_when_space_is_free(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The model pill renders "Fable 5.1 (1M context)" unclipped given room.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real
        server-backed session; the browser snapshot is patched to
        claude-native bound to the long-named Fable catalog model.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id)

    page.goto(f"{base_url}/c/{session_id}")

    model_label = page.get_by_test_id("composer-agent-model-value")
    # The catalog resolves the bound model to its display name; the full
    # name is in the DOM either way — truncation is visual.
    expect(model_label).to_have_text(_FABLE_DISPLAY_NAME, timeout=15_000)

    overflow = _horizontal_overflow(model_label)
    free_space = _action_row_free_space(page)
    # Precondition, not the bug: the row must genuinely have room for the
    # full label, or an ellipsis would be the correct overflow behavior.
    assert free_space > overflow + 48, (
        f"composer row too narrow to judge truncation ({free_space:.0f}px free)"
    )
    assert overflow <= 1, (
        f"model pill is ellipsized: {_FABLE_DISPLAY_NAME!r} is clipped by "
        f"{overflow}px while the composer row still has {free_space:.0f}px "
        f"of free space"
    )
