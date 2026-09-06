"""Browser E2E coverage for the Archived view's rolling date presets."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from playwright.sync_api import Page, Request, expect


def _is_archive_list_request(request: Request) -> bool:
    parsed = urlparse(request.url)
    query = parse_qs(parsed.query)
    return parsed.path == "/v1/sessions" and query.get("archived_only") == ["true"]


def _query(request: Request) -> dict[str, list[str]]:
    return parse_qs(urlparse(request.url).query)


def test_archive_date_presets_and_manual_override(page: Page, live_server: str) -> None:
    """Default to <30d, let valid manual dates win, then restore <7d."""
    page.set_viewport_size({"width": 320, "height": 844})

    with page.expect_request(
        lambda request: (
            _is_archive_list_request(request)
            and "archived_after" in _query(request)
            and "archived_before" not in _query(request)
        )
    ) as default_request:
        page.goto(f"{live_server}/settings/archived")

    default_query = _query(default_request.value)
    assert "archived_after" in default_query
    assert "archived_before" not in default_query

    page.get_by_role("button", name="Filter archive by date").click()
    popover = page.get_by_test_id("archive-date-popover")
    expect(popover).to_be_visible()
    box = popover.bounding_box()
    assert box is not None
    assert box["width"] <= 320
    assert box["x"] >= 0
    assert box["x"] + box["width"] <= 320
    assert popover.evaluate("element => element.scrollWidth <= element.clientWidth")

    date_input = page.get_by_test_id("archive-date-input")
    last_7_days = page.get_by_test_id("archive-date-preset-lt7d")
    last_30_days = page.get_by_test_id("archive-date-preset-lt30d")
    last_year = page.get_by_test_id("archive-date-preset-lt365d")
    expect(date_input).to_have_value("")
    expect(last_7_days).to_have_text("<7d")
    expect(last_30_days).to_have_text("<30d")
    expect(last_year).to_have_text("<1y")
    expect(last_30_days).to_have_attribute("aria-pressed", "true")
    expect(page.get_by_role("button", name="Today")).to_be_visible()

    with page.expect_request(
        lambda request: (
            _is_archive_list_request(request)
            and "archived_after" in _query(request)
            and "archived_before" in _query(request)
        )
    ) as manual_request:
        date_input.fill("20260901-20260904")

    manual_query = _query(manual_request.value)
    assert "archived_after" in manual_query
    assert "archived_before" in manual_query
    for preset in (last_7_days, last_30_days, last_year):
        expect(preset).to_have_attribute("aria-pressed", "false")

    with page.expect_request(
        lambda request: (
            _is_archive_list_request(request)
            and "archived_after" in _query(request)
            and "archived_before" not in _query(request)
        )
    ):
        last_7_days.click()

    expect(date_input).to_have_value("")
    expect(last_7_days).to_have_attribute("aria-pressed", "true")
