"""Multiple repositories, stable selection, and durable link/unlink UI flow."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.github.test_github_tab import _INFO


def _expect_pr_tooltip(page: Page, label: str, trigger: Locator) -> Locator:
    expect(trigger).to_have_attribute("aria-describedby", re.compile(r"\S+"))
    tooltip_id = trigger.get_attribute("aria-describedby")
    assert tooltip_id
    content = page.locator(f'[role="tooltip"][id="{tooltip_id}"]:not([data-state="closed"])')
    expect(content).to_be_visible()
    # A card's tooltip follows the label with the PR's status line.
    expect(content).to_have_accessible_name(re.compile(f"^{re.escape(label)}"))
    expect(content).to_have_attribute("data-slot", "tooltip-content")
    expect(content).to_have_class(re.compile(r"\bbg-neutral-900\b"))
    expect(content).to_have_class(re.compile(r"\btext-white\b"))
    expect(content).to_have_css("color", "rgb(255, 255, 255)")
    content.evaluate("""element => Promise.all(
        element.getAnimations().map(animation => animation.finished.catch(() => {}))
    )""")
    background = content.evaluate("""element => {
        const canvas = document.createElement("canvas");
        canvas.width = canvas.height = 1;
        const context = canvas.getContext("2d");
        context.fillStyle = getComputedStyle(element).backgroundColor;
        context.fillRect(0, 0, 1, 1);
        return [...context.getImageData(0, 0, 1, 1).data];
    }""")
    assert max(background[:3]) < 64 and background[3] == 255
    box, anchor, viewport = content.bounding_box(), trigger.bounding_box(), page.viewport_size
    assert box and anchor and viewport
    assert box["x"] >= -1 and box["x"] + box["width"] <= viewport["width"] + 1
    assert box["y"] >= -1 and box["y"] + box["height"] <= viewport["height"] + 1
    horizontal_gap = max(
        box["x"] - anchor["x"] - anchor["width"], anchor["x"] - box["x"] - box["width"], 0
    )
    vertical_gap = max(
        box["y"] - anchor["y"] - anchor["height"], anchor["y"] - box["y"] - box["height"], 0
    )
    assert max(horizontal_gap, vertical_gap) <= 24
    return content


def test_session_pr_selection_and_unlink(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    one = "https://github.com/example/one/pull/42"
    two = "https://github.com/example/two/pull/42"
    one_label = "example/one #42 — First repository"
    two_label = "example/two #42 — Second repository"
    urls = [one, two]
    requested: list[tuple[str, str]] = []
    pending_info: list[Route] = []
    hold_second_pr = True

    def info(url: str | None) -> dict:
        return {
            **_INFO,
            "repo": {"name_with_owner": "example/one" if url == one else "example/two"},
            "tracking_available": True,
            "selected_pr_url": url,
            "prs": [
                {
                    "url": value,
                    "host": "github.com",
                    "repository": value.split("github.com/")[1].split("/pull/")[0],
                    "number": 42,
                    "title": "  First repository  " if value == one else "Second repository",
                    "relationship": "created",
                }
                for value in urls
            ],
            "pr": {
                **_INFO["pr"],
                "number": 42,
                "url": url,
                "title": "First repository" if url == one else "Second repository",
                "head_sha": "head",
                "base_sha": "base",
            }
            if url
            else None,
        }

    def respond(route: Route) -> None:
        parsed = urlsplit(route.request.url)
        url = parse_qs(parsed.query).get("pr_url", [urls[0] if urls else None])[0]
        if parsed.path.endswith("/prs"):
            body = route.request.post_data_json
            if body["action"] == "remove":
                urls.remove(body["url"])
                url = urls[0] if urls else None
            else:
                urls.append(body["url"])
                url = body["url"]
            route.fulfill(json=info(url))
        elif parsed.path.endswith("/changes"):
            requested.append(("changes", url))
            filename = "one.py" if url == one else "two.py"
            route.fulfill(
                json={
                    "object": "list",
                    "data": [
                        {
                            "path": filename,
                            "name": filename,
                            "status": "modified",
                            "lines_added": 1,
                            "lines_removed": 1,
                        }
                    ],
                    "has_more": False,
                }
            )
        elif parsed.path.endswith("/diff"):
            requested.append(("diff", url))
            filename = "one.py" if url == one else "two.py"
            route.fulfill(
                json={
                    "object": "session.github.pr_diff",
                    "patch": (
                        f"diff --git a/{filename} b/{filename}\n--- a/{filename}\n"
                        f"+++ b/{filename}\n@@ -1 +1 @@\n-old\n+new\n"
                    ),
                }
            )
        else:
            assert url is None or url in urls, "Removed PR was selected again"
            if hold_second_pr and url == two:
                pending_info.append(route)
            else:
                route.fulfill(json=info(url))

    page.route(re.compile(r"/resources/github(?:/|\?|$)"), respond)
    page.goto(f"{base_url}/c/{session_id}")
    indicator = page.get_by_test_id("composer-pr-link")
    expect(indicator).to_have_accessible_name("2 PRs", timeout=30_000)
    indicator.click()
    rail = page.get_by_role("complementary", name="Workspace")
    expect(rail.get_by_role("tab", name="Pull Requests")).to_have_attribute(
        "aria-selected", "true"
    )
    strip = rail.get_by_role("group", name="Session pull requests")
    first_card = strip.get_by_role("button", name=one_label, exact=True)
    second_card = strip.get_by_role("button", name=two_label, exact=True)
    unlink_first = strip.get_by_role("button", name=f"Unlink {one_label}", exact=True)
    unlink_second = strip.get_by_role("button", name=f"Unlink {two_label}", exact=True)
    first_title = rail.get_by_role("link", name="First repository #42", exact=True)
    second_title = rail.get_by_role("link", name="Second repository #42", exact=True)
    expect(first_card).to_have_attribute("aria-current", "true")
    expect(second_card).not_to_have_attribute("aria-current", re.compile(r".*"))
    expect(first_card).not_to_have_attribute("title", re.compile(r".*"))
    # The header keeps Refresh and Link; each card carries its own unlink button.
    expect(rail.get_by_role("button", name="Unlink PR", exact=True)).to_have_count(0)
    original_strip = strip.element_handle()
    assert original_strip
    link_action = rail.get_by_role("button", name="Link a PR", exact=True)
    expect(link_action).to_have_text("")
    link_action.hover()
    expect(page.get_by_role("tooltip", name="Link a PR", exact=True)).to_be_visible()
    link_action.click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_be_visible()
    rail.get_by_role("textbox", name="Pull request URL").focus()
    page.screenshot(path=str(tmp_path / "session-pr-link-input.png"), animations="disabled")
    rail.get_by_role("button", name="Cancel", exact=True).click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)
    expect(first_card).to_have_attribute("aria-current", "true")
    link_action.click()
    rail.get_by_role("textbox", name="Pull request URL").press("Escape")
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)

    # Hovering a card reveals its own unlink button, not the other card's.
    first_card.hover()
    tooltip = _expect_pr_tooltip(page, one_label, first_card)
    expect(tooltip).to_contain_text("Open · 1 failed · 3 passed")
    expect(unlink_first).to_have_css("opacity", "1")
    expect(unlink_second).to_have_css("opacity", "0")
    expect(unlink_first).to_have_text("")
    # Glide like a pointer: Radix opens the next tooltip only on a move after the
    # pointer leaves the card tooltip's hover grace area, so a one-step jump never does.
    unlink_box = unlink_first.bounding_box()
    assert unlink_box
    x = unlink_box["x"] + unlink_box["width"] / 2
    y = unlink_box["y"] + unlink_box["height"] / 2
    page.mouse.move(x, y, steps=10)
    page.mouse.move(x + 1, y)
    expect(page.get_by_role("tooltip", name="Unlink PR", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("tooltip")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "session-pr-strip.png"), animations="disabled")

    # Tabbing to a card reveals its tooltip and unlink button; Enter selects it.
    page.mouse.move(0, 0)
    first_card.focus()
    page.keyboard.press("Tab")
    expect(unlink_first).to_be_focused()
    page.keyboard.press("Tab")
    expect(second_card).to_be_focused()
    _expect_pr_tooltip(page, two_label, second_card)
    expect(unlink_second).to_have_css("opacity", "1")
    expect(unlink_first).to_have_css("opacity", "0")
    page.keyboard.press("Enter")
    expect(second_card).to_have_attribute("aria-current", "true")
    expect(first_card).not_to_have_attribute("aria-current", re.compile(r".*"))
    expect(rail.get_by_text("Loading pull requests…", exact=True)).to_be_visible()
    expect(first_title).to_have_count(0)
    assert original_strip.evaluate("element => element.isConnected")
    expect(link_action).to_be_enabled()
    expect(unlink_second).to_be_enabled()
    page.screenshot(path=str(tmp_path / "session-pr-switch-loading.png"), animations="disabled")
    assert pending_info
    hold_second_pr = False
    for route in pending_info:
        route.fulfill(json=info(two))
    expect(second_title).to_be_visible()
    expect(second_card).to_have_attribute("aria-current", "true")

    first_card.click()
    expect(first_card).to_have_attribute("aria-current", "true")
    expect(first_title).to_be_visible()
    # A clicked card keeps focus, but its unlink button hides once the pointer leaves.
    page.mouse.move(0, 0)
    expect(first_card).to_be_focused()
    expect(unlink_first).to_have_css("opacity", "0")
    second_card.click()
    expect(second_card).to_have_attribute("aria-current", "true")
    expect(second_title).to_be_visible()

    rail.get_by_role("tablist", name="Pull request").get_by_role(
        "tab", name="Changes", exact=True
    ).click()
    expect(rail.get_by_text("two.py", exact=True).first).to_be_visible()
    assert ("changes", two) in requested
    assert ("diff", two) in requested
    # Unlinking the other PR keeps the one in view.
    unlink_first.click()
    expect(first_card).to_have_count(0)
    expect(second_card).to_have_attribute("aria-current", "true")
    expect(second_title).to_be_visible()
    expect(indicator).to_have_accessible_name("#42")
    unlink_second.click()
    expect(strip).to_have_count(0)
    expect(indicator).to_have_count(0)
    description = rail.get_by_text(
        "Pull requests created in this session appear here. You can also link an existing PR."
    )
    expect(description).to_be_visible()
    link = rail.get_by_role("button", name="Link a PR", exact=True)
    expect(link).to_be_visible()
    description_box = description.bounding_box()
    link_box = link.bounding_box()
    assert description_box and link_box
    assert link_box["y"] >= description_box["y"] + description_box["height"]
    page.screenshot(path=str(tmp_path / "session-pr-empty-state.png"), animations="disabled")
    link.click()
    rail.get_by_role("textbox", name="Pull request URL").fill(two)
    page.screenshot(path=str(tmp_path / "session-pr-empty-link-input.png"), animations="disabled")
    rail.get_by_role("button", name="Cancel", exact=True).click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)
    expect(indicator).to_have_count(0)
    link.click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_value(two)
    rail.get_by_role("button", name="Link", exact=True).click()
    expect(second_card).to_have_attribute("aria-current", "true")
    expect(second_card).not_to_have_attribute("title", re.compile(r".*"))
    expect(indicator).to_have_accessible_name("#42")
    expect(second_title).to_be_visible()


@pytest.mark.parametrize("viewport_width", [1280, 390], ids=["desktop", "mobile"])
def test_session_pr_card_long_title(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path, viewport_width: int
) -> None:
    base_url, session_id = seeded_session
    url = "https://github.com/example/one/pull/42"
    title = (
        "Keep pull requests recognizable across multiple repositories while preserving "
        "the full descriptive title for reviewers using narrow workspace panels"
    )
    label = f"example/one #42 — {title}"
    info = {
        **_INFO,
        "tracking_available": True,
        "selected_pr_url": url,
        "prs": [
            {
                "url": url,
                "host": "github.com",
                "repository": "example/one",
                "number": 42,
                "title": title,
                "relationship": "created",
            },
            {
                "url": "https://github.com/example/one/pull/43",
                "host": "github.com",
                "repository": "example/one",
                "number": 43,
                "title": None,
                "relationship": "attached",
            },
        ],
        "pr": {**_INFO["pr"], "url": url, "number": 42, "title": title},
    }
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda route: route.fulfill(json=info))
    page.set_viewport_size({"width": viewport_width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if viewport_width < 768
        else page.get_by_role("complementary", name="Workspace")
    )
    strip = panel.get_by_role("group", name="Session pull requests")
    titled = strip.get_by_role("button", name=label, exact=True)
    untitled = strip.get_by_role("button", name="example/one #43", exact=True)
    expect(titled).to_have_attribute("aria-current", "true")
    expect(untitled).to_be_visible()
    page.screenshot(path=tmp_path / "session-pr-long-title.png", animations="disabled")
    # A long title stays on one truncated line, so the cards keep one height.
    title_text = titled.get_by_text(title, exact=True)
    expect(title_text).to_have_css("white-space", "nowrap")
    expect(title_text).to_have_css("text-overflow", "ellipsis")
    assert title_text.evaluate("element => element.scrollWidth > element.clientWidth")
    assert title_text.evaluate("element => element.scrollHeight <= element.clientHeight")
    titled_box, untitled_box = titled.bounding_box(), untitled.bounding_box()
    assert titled_box and untitled_box
    assert titled_box["height"] == pytest.approx(untitled_box["height"], abs=0.5)
    for card in (titled, untitled):
        expect(card).not_to_have_attribute("title", re.compile(r".*"))
        assert card.evaluate("element => element.scrollWidth <= element.clientWidth")
    strip_box = strip.bounding_box()
    assert strip_box and strip_box["x"] >= 0
    assert strip_box["x"] + strip_box["width"] <= viewport_width
    expect(panel.get_by_role("button", name="Link a PR", exact=True)).to_be_in_viewport()
    # Desktop reaches the tooltip by hover; touch-width layouts by focus.
    if viewport_width >= 768:
        titled.hover()
    else:
        titled.focus()
    tooltip = _expect_pr_tooltip(page, label, titled)
    assert tooltip.evaluate("element => element.scrollWidth <= element.clientWidth")
    page.screenshot(path=tmp_path / "session-pr-card-tooltip.png", animations="disabled")
    untitled.click()
    expect(untitled).to_have_attribute("aria-current", "true")


def test_session_pr_account_fallback(
    page: Page, seeded_session: tuple[str, str], tmp_path: Path
) -> None:
    base_url, session_id = seeded_session
    url = "https://github.com/example/one/pull/42"
    info = {
        **_INFO,
        "repo": None,
        "pr": None,
        "tracking_available": True,
        "selected_pr_url": url,
        "prs": [
            {
                "url": url,
                "host": "github.com",
                "repository": "example/one",
                "number": 42,
                "relationship": "created",
            }
        ],
        "accounts": [
            {
                "login": login,
                "active": login == "personal",
                "state": "success",
                "host": "github.com",
            }
            for login in ("personal", "work")
        ],
        "selected_account": "personal",
    }
    page.route(re.compile(r"/resources/github(?:\?|$)"), lambda route: route.fulfill(json=info))
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    rail = page.get_by_role("complementary", name="Workspace")
    expect(
        rail.get_by_role("group", name="Session pull requests").locator('[aria-current="true"]')
    ).to_have_accessible_name("example/one #42")
    expect(rail.get_by_text("Can’t reach the upstream repo", exact=True)).to_be_visible()
    account = rail.get_by_role("combobox", name="GitHub account")
    alternative = rail.get_by_text("or", exact=True)
    link = rail.get_by_role("link", name="Open the PR on GitHub", exact=True)
    expect(account).to_have_text("personal (active)")
    expect(link).to_have_attribute("href", url)
    expect(link).to_have_attribute("target", "_blank")
    account_box, alternative_box, link_box = (
        element.bounding_box() for element in (account, alternative, link)
    )
    assert account_box and alternative_box and link_box
    assert alternative_box["y"] >= account_box["y"] + account_box["height"]
    assert link_box["y"] >= alternative_box["y"] + alternative_box["height"]
    page.screenshot(path=str(tmp_path / "session-pr-account-fallback.png"), animations="disabled")
