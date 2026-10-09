"""Browser regressions for composer popovers below the session header."""

from __future__ import annotations

from urllib.parse import urlparse

import httpx
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.chat.test_model_flows_contract import _install_stream_controller

_SKILLS = [
    {"name": f"repro-skill-{i:02d}", "description": f"Reproduction skill number {i:02d}"}
    for i in range(40)
]
_HELP_NEEDLE = "/repro-skill-00"
_BG_TASKS = [
    {
        "id": f"bg-task-{i}",
        "type": "shell",
        "status": "running",
        "description": f"Background job {i}: watch the deploy pipeline and tail its logs",
        "command": (
            f"while true; do gh run watch 100{i} --exit-status && "
            f"kubectl logs -f deploy/service-{i} --all-containers --since=1h | "
            "tee -a /var/log/service.log; sleep 30; done"
        ),
    }
    for i in range(12)
]


def _patch_session_host(page: Page, session_id: str, *, native: bool = False) -> None:
    """Give the browser's session snapshot a host + workspace so skills load."""
    session_path = f"/v1/sessions/{session_id}"

    def snapshot(route: Route) -> None:
        if urlparse(route.request.url).path != session_path or route.request.method != "GET":
            route.fallback()
            return
        response = route.fetch()
        body = response.json()
        body.update(host_id="header-band-host", workspace="/workspace")
        if native:
            body.update(harness="codex-native")
            body["labels"] = {**body.get("labels", {}), "omnigent.wrapper": "codex-native-ui"}
        route.fulfill(response=response, json=body)

    page.route(f"**{session_path}*", snapshot)


def _serve_skills(page: Page, session_id: str) -> None:
    page.route(
        f"**/v1/skills?session_id={session_id}",
        lambda route: route.fulfill(json={"skills": _SKILLS}),
    )


def _serve_files(page: Page, session_id: str) -> None:
    entries = [
        {
            "id": f"file-{i}",
            "name": f"file-{i:02d}.txt",
            "path": f"file-{i:02d}.txt",
            "type": "file",
            "bytes": 1,
            "modified_at": None,
        }
        for i in range(40)
    ]
    page.route(
        f"**/v1/sessions/{session_id}/resources/environments/default/filesystem*",
        lambda route: route.fulfill(json={"object": "list", "data": entries, "has_more": False}),
    )


def _header_and_card(page: Page, needle: str) -> dict:
    """Header rect, composer-card rect, and the needle-holding feedback rect."""
    return page.evaluate(
        """
        (needle) => {
          const rect = (el) => {
            const b = el.getBoundingClientRect();
            return { top: b.top, bottom: b.bottom, left: b.left, right: b.right };
          };
          const header = document.querySelector('.chat-header');
          const card = document.querySelector('[data-composer-card]');
          const feedback = card
            ? [...card.querySelectorAll('.whitespace-pre-wrap')].find((el) =>
                el.textContent.includes(needle),
              )
            : null;
          return {
            header: header ? rect(header) : null,
            headerZ: header ? getComputedStyle(header).zIndex : null,
            card: card ? rect(card) : null,
            feedback: feedback ? rect(feedback) : null,
          };
        }
        """,
        needle,
    )


def _header_and_menu(page: Page, item_testid: str) -> dict:
    """Header rect and the floating suggestion-menu container rect."""
    return page.evaluate(
        """
        (itemTestId) => {
          const rect = (el) => {
            const b = el.getBoundingClientRect();
            return { top: b.top, bottom: b.bottom, left: b.left, right: b.right };
          };
          const header = document.querySelector('.chat-header');
          let menu = document.querySelector(`[data-testid="${itemTestId}"]`);
          while (
            menu &&
            !(typeof menu.className === 'string' && menu.className.includes('bottom-full'))
          ) {
            menu = menu.parentElement;
          }
          return {
            header: header ? rect(header) : null,
            headerZ: header ? getComputedStyle(header).zIndex : null,
            menu: menu ? rect(menu) : null,
            menuZ: menu ? getComputedStyle(menu).zIndex : null,
          };
        }
        """,
        item_testid,
    )


def test_help_command_output_stays_below_header(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The /help listing must not grow the card up into the header band."""
    base_url, session_id = seeded_session
    _patch_session_host(page, session_id)
    _serve_skills(page, session_id)
    _install_stream_controller(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 720})
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    # The skills catalog must be resolved so /help lists them (grows the card).
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-repro-skill-00")).to_be_visible(timeout=15_000)

    composer.fill("/help")
    page.keyboard.press("Enter")

    feedback = page.locator("[data-composer-card] .whitespace-pre-wrap").filter(
        has_text=_HELP_NEEDLE
    )
    expect(feedback).to_be_visible(timeout=15_000)

    geom = _header_and_card(page, _HELP_NEEDLE)
    assert geom["header"] is not None, "ChatHeader overlay not found"
    assert geom["card"] is not None, "composer card not found"
    assert geom["feedback"] is not None, "/help output did not render inside the composer card"
    # The header overlay is the fixed z-30 band it must paint on top of.
    assert geom["headerZ"] == "30", geom["headerZ"]

    assert geom["card"]["top"] >= geom["header"]["bottom"] - 1, (
        f"composer card top {geom['card']['top']} crosses under header bottom "
        f"{geom['header']['bottom']} — header paints over the /help output"
    )
    assert geom["feedback"]["top"] >= geom["header"]["bottom"] - 1, (
        f"/help output top {geom['feedback']['top']} crosses under header bottom "
        f"{geom['header']['bottom']} — header paints over the /help output"
    )
    panel = page.get_by_test_id("composer-command-output")
    overflow = panel.evaluate(
        "(el) => [el.clientHeight, el.scrollHeight, getComputedStyle(el).overflowY]"
    )
    assert overflow[1] > overflow[0] and overflow[2] == "auto", overflow


def test_slash_menu_stays_below_header_on_short_window(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The slash-command menu must be viewport-capped below the header."""
    base_url, session_id = seeded_session
    _patch_session_host(page, session_id)
    _serve_skills(page, session_id)
    _install_stream_controller(page, session_id)

    # A short window: an uncapped max-h-80 menu reaches into the header.
    page.set_viewport_size({"width": 1280, "height": 430})
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("/")
    expect(page.get_by_test_id("slash-menu-item-help")).to_be_visible(timeout=15_000)
    expect(page.get_by_test_id("slash-menu-item-repro-skill-00")).to_be_visible(timeout=15_000)

    geom = _header_and_menu(page, "slash-menu-item-help")
    assert geom["header"] is not None, "ChatHeader overlay not found"
    assert geom["menu"] is not None, "slash-command menu container not found"
    # The menu sits in the z-20 popover band, strictly beneath the z-30 header.
    assert int(geom["menuZ"]) < int(geom["headerZ"]), (geom["menuZ"], geom["headerZ"])

    assert geom["menu"]["top"] >= geom["header"]["bottom"] - 1, (
        f"slash menu top {geom['menu']['top']} crosses under header bottom "
        f"{geom['header']['bottom']} on a short window"
    )
    overflow = page.get_by_test_id("slash-menu-item-help").evaluate(
        "(el) => [el.parentElement.clientHeight, el.parentElement.scrollHeight]"
    )
    assert overflow[1] > overflow[0], overflow


def test_mention_menu_stays_below_header_on_short_window(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    base_url, session_id = seeded_session
    _patch_session_host(page, session_id, native=True)
    _serve_files(page, session_id)
    _install_stream_controller(page, session_id)

    page.set_viewport_size({"width": 1280, "height": 430})
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("@")
    item = page.get_by_test_id("file-mention-item-0")
    expect(item).to_be_visible(timeout=15_000)

    geom = _header_and_menu(page, "file-mention-item-0")
    assert geom["header"] is not None
    assert geom["menu"] is not None
    assert int(geom["menuZ"]) < int(geom["headerZ"]), (geom["menuZ"], geom["headerZ"])
    assert geom["menu"]["top"] >= geom["header"]["bottom"] - 1, geom
    overflow = item.evaluate(
        "(el) => { const list = el.closest('[role=listbox]'); "
        "return [list.clientHeight, list.scrollHeight, getComputedStyle(list).overflowY]; }"
    )
    assert overflow[1] > overflow[0] and overflow[2] == "auto", overflow


def _header_and_popover(page: Page, label: str) -> dict:
    """Header rect and the Radix popover panel (portaled out of the card) rect."""
    return page.evaluate(
        """
        (label) => {
          const rect = (el) => {
            const b = el.getBoundingClientRect();
            return { top: b.top, bottom: b.bottom, left: b.left, right: b.right };
          };
          const header = document.querySelector('.chat-header');
          const panel = document.querySelector(`[aria-label="${label}"]`);
          return {
            header: header ? rect(header) : null,
            headerZ: header ? getComputedStyle(header).zIndex : null,
            panel: panel ? rect(panel) : null,
          };
        }
        """,
        label,
    )


def test_background_task_card_stays_below_header_on_short_window(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The collision-aware background-task popover must open below the header band."""
    base_url, session_id = seeded_session
    httpx.post(
        f"{base_url}/v1/sessions/{session_id}/events",
        json={
            "type": "external_session_status",
            "data": {
                "status": "idle",
                "background_task_count": len(_BG_TASKS),
                "background_tasks": _BG_TASKS,
            },
        },
        timeout=10.0,
    ).raise_for_status()

    # A short window pushes the top-side popover up toward the header.
    page.set_viewport_size({"width": 1280, "height": 430})
    page.goto(f"{base_url}/c/{session_id}")

    expect(page.get_by_label("Message the agent")).to_be_visible(timeout=30_000)
    pill = page.get_by_test_id("background-task-pill")
    expect(pill).to_be_visible(timeout=15_000)
    pill.click()

    label = f"{len(_BG_TASKS)} background tasks"
    expect(page.locator(f'[aria-label="{label}"]')).to_be_visible(timeout=15_000)

    geom = _header_and_popover(page, label)
    assert geom["header"] is not None, "ChatHeader overlay not found"
    assert geom["panel"] is not None, "background-task popover not found"
    assert geom["panel"]["top"] >= geom["header"]["bottom"] - 1, (
        f"background-task card top {geom['panel']['top']} crosses under header bottom "
        f"{geom['header']['bottom']} (z-index {geom['headerZ']})"
    )
