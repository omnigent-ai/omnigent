"""Multiple repositories, stable selection, and durable link/unlink UI flow."""

from __future__ import annotations

import re
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Locator, Page, Route, expect

from tests.e2e_ui.github.test_github_tab import _INFO


def _row_tooltip_trigger(row: Locator) -> Locator:
    """The tooltip anchors on the row's wrapper so the button keeps its own description."""
    return row.locator("xpath=..")


def _expect_pr_tooltip(page: Page, label: str, trigger: Locator) -> Locator:
    expect(trigger).to_have_attribute("aria-describedby", re.compile(r"\S+"))
    tooltip_id = trigger.get_attribute("aria-describedby")
    assert tooltip_id
    content = page.locator(f'[role="tooltip"][id="{tooltip_id}"]:not([data-state="closed"])')
    expect(content).to_be_visible()
    expect(content).to_have_accessible_name(label)
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
    pr_list = rail.get_by_role("list", name="Session pull requests")
    first_row = pr_list.get_by_role("button", name=one_label, exact=True)
    second_row = pr_list.get_by_role("button", name=two_label, exact=True)
    # The second PR's lookup is held, so the first load reveals at its deadline.
    expect(first_row).to_have_attribute("aria-current", "true", timeout=15_000)
    expect(first_row).not_to_have_attribute("title", re.compile(r".*"))
    original_list = pr_list.element_handle()
    assert original_list
    link_action = rail.get_by_role("button", name="Link a PR", exact=True)
    unlink_action = rail.get_by_role("button", name="Unlink PR", exact=True)
    expect(link_action).to_have_text("")
    expect(unlink_action).to_have_text("")
    link_action.hover()
    expect(page.get_by_role("tooltip", name="Link a PR", exact=True)).to_be_visible()
    link_action.click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_be_visible()
    rail.get_by_role("textbox", name="Pull request URL").focus()
    page.screenshot(path=str(tmp_path / "session-pr-link-input.png"), animations="disabled")
    rail.get_by_role("button", name="Cancel", exact=True).click()
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)
    expect(first_row).to_have_attribute("aria-current", "true")
    link_action.click()
    rail.get_by_role("textbox", name="Pull request URL").press("Escape")
    expect(rail.get_by_role("textbox", name="Pull request URL")).to_have_count(0)
    first_row.hover()
    _expect_pr_tooltip(page, one_label, _row_tooltip_trigger(first_row))
    unlink_action.hover()
    expect(page.get_by_role("tooltip", name="Unlink PR", exact=True)).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.get_by_role("tooltip")).to_have_count(0)
    page.screenshot(path=str(tmp_path / "session-pr-count.png"), animations="disabled")
    # Rows are plain buttons: Tab reaches the next PR and Enter selects it.
    first_row.focus()
    page.keyboard.press("Tab")
    expect(second_row).to_be_focused()
    page.keyboard.press("Enter")
    expect(second_row).to_have_attribute("aria-current", "true")
    expect(first_row).not_to_have_attribute("aria-current", "true")
    expect(second_row).not_to_have_attribute("title", re.compile(r".*"))
    expect(rail.get_by_text("Loading pull requests…", exact=True)).to_be_visible()
    expect(rail.get_by_text("First repository", exact=True)).to_have_count(0)
    assert original_list.evaluate("element => element.isConnected")
    expect(rail.get_by_role("button", name="Link a PR", exact=True)).to_be_enabled()
    expect(unlink_action).to_be_enabled()
    page.screenshot(path=str(tmp_path / "session-pr-switch-loading.png"), animations="disabled")
    assert pending_info
    hold_second_pr = False
    for route in pending_info:
        route.fulfill(json=info(two))
    expect(rail.get_by_text("Second repository", exact=True)).to_be_visible()
    expect(second_row).to_have_attribute("aria-current", "true")

    second_row.hover()
    _expect_pr_tooltip(page, two_label, _row_tooltip_trigger(second_row))
    first_row.hover()
    _expect_pr_tooltip(page, one_label, _row_tooltip_trigger(first_row))
    first_row.click()
    expect(first_row).to_have_attribute("aria-current", "true")
    expect(rail.get_by_text("First repository", exact=True)).to_be_visible()
    second_row.click()
    expect(second_row).to_have_attribute("aria-current", "true")
    expect(rail.get_by_text("Second repository", exact=True)).to_be_visible()

    rail.get_by_role("tablist", name="Pull request").get_by_role(
        "tab", name="Changes", exact=True
    ).click()
    expect(rail.get_by_text("two.py", exact=True).first).to_be_visible()
    assert ("changes", two) in requested
    assert ("diff", two) in requested
    unlink_action.click()
    confirm = page.get_by_role("dialog", name="Unlink this pull request?")
    expect(confirm).to_contain_text("Second repository")
    expect(confirm.get_by_role("link", name="example/two #42")).to_have_attribute("href", two)
    page.screenshot(path=str(tmp_path / "session-pr-unlink-confirm.png"), animations="disabled")
    confirm.get_by_role("button", name="Unlink", exact=True).click()
    expect(confirm).to_have_count(0)
    expect(first_row).to_have_attribute("aria-current", "true")
    expect(second_row).to_have_count(0)
    expect(indicator).to_have_accessible_name("#42")
    unlink_action.click()
    page.get_by_role("dialog", name=re.compile(r"^Unlink this ")).get_by_role(
        "button", name="Unlink", exact=True
    ).click()
    expect(pr_list).to_have_count(0)
    expect(indicator).to_have_count(0)
    expect(unlink_action).to_have_count(0)
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
    expect(second_row).to_have_attribute("aria-current", "true")
    expect(second_row).not_to_have_attribute("title", re.compile(r".*"))
    expect(indicator).to_have_accessible_name("#42")
    expect(rail.get_by_text("Second repository", exact=True)).to_be_visible()


@pytest.mark.parametrize("viewport_width", [1280, 390], ids=["desktop", "mobile"])
def test_session_pr_list_long_title(
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

    def fulfill(route: Route) -> None:
        # Each row loads its own PR; #43's details stay unavailable so its row keeps no title.
        query = parse_qs(urlsplit(route.request.url).query)
        if query.get("pr_url", [url])[0] == url:
            route.fulfill(json=info)
        else:
            route.fulfill(json={**info, "pr": None})

    page.route(re.compile(r"/resources/github(?:\?|$)"), fulfill)
    page.set_viewport_size({"width": viewport_width, "height": 900})
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("composer-pr-link").click()
    panel = (
        page.get_by_test_id("github-panel-drawer")
        if viewport_width < 768
        else page.get_by_role("complementary", name="Workspace")
    )
    pr_list = panel.get_by_role("list", name="Session pull requests")
    titled_row = pr_list.get_by_role("button", name=label, exact=True)
    untitled_row = pr_list.get_by_role("button", name="example/one #43", exact=True)
    expect(titled_row).to_have_attribute("aria-current", "true")
    expect(untitled_row).to_be_visible()
    expect(panel.get_by_role("button", name="Link a PR", exact=True)).to_be_in_viewport()
    expect(panel.get_by_role("button", name="Unlink PR", exact=True)).to_be_in_viewport()
    page.screenshot(path=tmp_path / "session-pr-long-title.png", animations="disabled")
    titled_box, untitled_box = titled_row.bounding_box(), untitled_row.bounding_box()
    assert titled_box and untitled_box
    assert titled_box["height"] == pytest.approx(untitled_box["height"], abs=0.5)
    for row in (titled_row, untitled_row):
        expect(row).not_to_have_attribute("title", re.compile(r".*"))
        row_text = row.locator(".truncate")
        expect(row_text).to_have_css("white-space", "nowrap")
        expect(row_text).to_have_css("text-overflow", "ellipsis")
        assert row_text.evaluate("element => element.scrollHeight <= element.clientHeight")
        assert row.evaluate("element => element.scrollWidth <= element.clientWidth")
        box = row.bounding_box()
        # Full-width rows share the rail content's 1px offset past the rail border.
        assert box and box["x"] >= 0 and box["x"] + box["width"] <= viewport_width + 1
    assert titled_row.locator(".truncate").evaluate(
        "element => element.scrollWidth > element.clientWidth"
    )
    if viewport_width >= 768:
        titled_row.hover()
        tooltip = _expect_pr_tooltip(page, label, _row_tooltip_trigger(titled_row))
        assert tooltip.evaluate("element => element.scrollWidth <= element.clientWidth")
        page.screenshot(path=tmp_path / "session-pr-row-tooltip.png", animations="disabled")
    untitled_row.click()
    expect(untitled_row).to_have_attribute("aria-current", "true")


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
        rail.get_by_role("list", name="Session pull requests").locator(
            'button[aria-current="true"]'
        )
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
