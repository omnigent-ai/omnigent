"""Hermetic browser coverage for the in-session model catalog."""

from __future__ import annotations

from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Request, expect

from tests.browser_ui.chat.session_contract import ChatSessionContract, model_option


def _open_models(page: Page) -> None:
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=20_000)
    gear.click()
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_test_id("composer-agent-config-menu")).to_be_visible()


def test_catalog_rows_render_for_every_native_picker(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """The real chat page consumes runner-owned rows for every picker family."""
    chat = chat_session_contract
    for harness in (
        "claude-native",
        "codex-native",
        "cursor-native",
        "kiro-native",
        "opencode-native",
        "pi-native",
        "devin-native",
        "acp",
    ):
        chat.set_catalog(
            harness=harness,
            models=[
                model_option("primary", display_name="Primary", is_default=True),
                model_option("alternate", display_name="Alternate"),
            ],
            selected_model="primary",
        )
        page.goto(chat.url)
        _open_models(page)

        rows = page.locator('[role="menuitemcheckbox"][data-model-id]')
        expect(rows).to_have_count(2)
        expect(rows.nth(0)).to_have_attribute("data-model-id", "primary")
        expect(rows.nth(0)).to_contain_text("Primary")
        expect(rows.nth(1)).to_have_attribute("data-model-id", "alternate")
        expect(rows.nth(1)).to_contain_text("Alternate")


def test_reported_model_highlights_only_an_exact_catalog_match(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Reported provider IDs select exact rows; unknown reports stay honest."""
    chat = chat_session_contract
    chat.set_catalog(
        harness="claude-native",
        models=[
            model_option("sonnet", model="system.ai.claude-sonnet-5", display_name="Sonnet 5"),
            model_option("opus", model="system.ai.claude-opus-4-10", display_name="Opus 4.10"),
        ],
        selected_model="system.ai.claude-sonnet-5",
    )
    page.goto(chat.url)
    _open_models(page)
    expect(page.locator('[data-model-id="sonnet"]')).to_have_attribute("aria-checked", "true")
    expect(page.locator('[data-model-id="opus"]')).to_have_attribute("aria-checked", "false")

    chat.set_catalog(
        harness="claude-native",
        models=chat.models,
        selected_model="claude-opus-4-8[1m]",
    )
    page.goto(chat.url)
    _open_models(page)
    current = page.locator('[data-model-id="claude-opus-4-8[1m]"]')
    expect(current).to_have_count(1)
    expect(current).to_contain_text("(current)")
    expect(current).to_have_attribute("aria-checked", "true")
    expect(page.locator('[data-model-id="opus"]')).to_have_attribute("aria-checked", "false")


def test_selecting_a_catalog_row_patches_its_exact_id(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """A real picker selection reaches the session PATCH contract unchanged."""
    chat = chat_session_contract
    chat.set_catalog(
        harness="kiro-native",
        models=[
            model_option("primary", display_name="Primary", is_default=True),
            model_option("provider/alternate", display_name="Alternate"),
        ],
        selected_model="primary",
    )
    page.goto(chat.url)
    _open_models(page)
    session_path = f"/v1/sessions/{chat.session_id}"
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == session_path
            and response.status == 200
        )
    ) as patch_info:
        page.locator('[data-model-id="provider/alternate"]').click()

    expect(page.get_by_test_id("composer-agent-model-summary")).to_have_text("Alternate")
    assert patch_info.value.request.post_data_json == {"model_override": "provider/alternate"}
    assert chat.session_patches == [{"model_override": "provider/alternate"}]


def test_picker_open_and_selection_do_not_refetch_catalog_or_session(
    page: Page,
    chat_session_contract: ChatSessionContract,
) -> None:
    """Opening and using preloaded rows does not trigger a click-time GET."""
    chat = chat_session_contract
    chat.set_catalog(
        harness="opencode-native",
        models=[
            model_option("primary", display_name="Primary", is_default=True),
            model_option("alternate", display_name="Alternate"),
        ],
        selected_model="primary",
    )
    page.goto(chat.url)
    chat.wait_for_stream()
    expect(page.get_by_test_id("composer-config-gear")).to_be_visible(timeout=20_000)
    page.wait_for_timeout(500)

    unexpected_gets: list[str] = []
    session_path = f"/v1/sessions/{chat.session_id}"

    def record_request(request: Request) -> None:
        path = urlparse(request.url).path
        if request.method == "GET" and (path == session_path or path.endswith("/model-options")):
            unexpected_gets.append(request.url)

    page.on("request", record_request)
    _open_models(page)
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == session_path
            and response.status == 200
        )
    ) as patch_info:
        page.locator('[data-model-id="alternate"]').click()
    expect(page.get_by_test_id("composer-agent-model-summary")).to_have_text("Alternate")
    page.wait_for_timeout(500)

    assert patch_info.value.request.post_data_json == {"model_override": "alternate"}
    assert chat.session_patches == [{"model_override": "alternate"}]
    assert unexpected_gets == []


@pytest.mark.parametrize("viewport_height", [600, 340], ids=["desktop", "short"])
def test_long_catalog_scrolls_to_and_selects_the_last_model(
    page: Page,
    chat_session_contract: ChatSessionContract,
    viewport_height: int,
) -> None:
    """Long model catalogs stay viewport-bounded, scrollable, and selectable."""
    chat = chat_session_contract
    models = [
        model_option(
            f"model-{index:02}",
            display_name=f"Model {index:02} with a descriptive catalog label",
            is_default=index == 0,
        )
        for index in range(32)
    ]
    last_model = models[-1]
    chat.set_catalog(
        harness="kiro-native",
        models=models,
        selected_model=str(models[0]["id"]),
    )
    viewport = {"width": 1280, "height": viewport_height}
    page.set_viewport_size(viewport)
    page.goto(chat.url)
    _open_models(page)

    menu = page.get_by_test_id("composer-agent-config-menu")
    last_row = page.get_by_test_id(f"composer-agent-model-{last_model['id']}")
    page.wait_for_function(
        """async () => {
          const menu = document.querySelector('[data-testid="composer-agent-config-menu"]');
          if (!menu) return false;
          if (menu.getAnimations({subtree: true}).some(
            animation => animation.playState === "running" || animation.playState === "pending"
          )) return false;

          const rect = () => {
            const {top, bottom, height} = menu.getBoundingClientRect();
            return {top, bottom, height};
          };
          const close = (a, b) =>
            Math.abs(a.top - b.top) < 0.25 &&
            Math.abs(a.bottom - b.bottom) < 0.25 &&
            Math.abs(a.height - b.height) < 0.25;
          const frame = () => new Promise(requestAnimationFrame);

          await frame();
          const first = rect();
          await frame();
          const second = rect();
          await frame();
          const third = rect();
          return close(first, second) && close(second, third);
        }"""
    )
    scroll_metrics = menu.evaluate(
        """element => {
          const rect = element.getBoundingClientRect();
          return {
            scrollHeight: element.scrollHeight,
            clientHeight: element.clientHeight,
            scrollTop: element.scrollTop,
            overflowY: getComputedStyle(element).overflowY,
            left: rect.left,
            top: rect.top,
            width: rect.width,
            height: rect.height,
          };
        }"""
    )
    assert scroll_metrics["overflowY"] in ("auto", "scroll")
    assert scroll_metrics["scrollHeight"] > scroll_metrics["clientHeight"]
    assert 0 < scroll_metrics["height"] <= 384
    assert scroll_metrics["top"] >= 0
    assert scroll_metrics["top"] + scroll_metrics["height"] <= viewport["height"]

    page.mouse.move(
        scroll_metrics["left"] + scroll_metrics["width"] / 2,
        scroll_metrics["top"] + scroll_metrics["height"] / 2,
    )
    page.mouse.wheel(0, 2_000)
    page.wait_for_function(
        """() => {
          const menu = document.querySelector('[data-testid="composer-agent-config-menu"]');
          return menu && menu.scrollTop > 0;
        }"""
    )
    menu_box = menu.bounding_box()
    last_box = last_row.bounding_box()
    assert menu_box is not None and last_box is not None
    assert last_box["y"] >= menu_box["y"]
    assert last_box["y"] + last_box["height"] <= menu_box["y"] + menu_box["height"]

    session_path = f"/v1/sessions/{chat.session_id}"
    with page.expect_response(
        lambda response: (
            response.request.method == "PATCH"
            and urlparse(response.url).path == session_path
            and response.status == 200
        )
    ) as patch_info:
        last_row.click()

    assert patch_info.value.request.post_data_json == {"model_override": last_model["id"]}
    assert chat.session_patches == [{"model_override": last_model["id"]}]
    expect(last_row).to_have_attribute("aria-checked", "true")
