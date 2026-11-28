"""Journeys through a Markdown file's rich diff in the tabs sidebar's Diffs review.

The rich diff lays out long documents in groups the browser skips while they're off screen.
These read what's on screen in a real browser, on a real host's repository, with the review
full screen.
"""

from __future__ import annotations

from pathlib import Path

from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.files._diffs_review import (
    choose_in_tree,
    commit_then_edit,
    open_diffs,
    open_find,
)
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import (
    git_session,  # noqa: F401 — shared real-host fixture
)

_WINDOW = {"width": 1280, "height": 800}

# Marks the element that scrolls the review's files, inside its scroll area.
_MARK_REVIEW_SCROLLER = """() => {
  const area = document.querySelector(".diff-review-scroll");
  const candidates = [area, ...area.querySelectorAll("*")];
  const scroller = candidates.find((element) => {
    const overflow = getComputedStyle(element).overflowY;
    return overflow === "auto" || overflow === "scroll";
  });
  scroller.setAttribute("data-perf-scroller", "");
}"""


def _open_review(
    page: Page,
    session: tuple[str, str],
    tmp_path: Path,
    files: dict[str, tuple[str, str]],
    reveal: str | None = None,
) -> Locator:
    """Diffs on ``files``' uncommitted edits, full screen, at ``reveal`` when given, with the
    changed-files tree hidden and its scroller marked."""
    base_url, session_id = session
    commit_then_edit(tmp_path / "repository", files)
    sidebar, review = open_diffs(page, base_url, session_id, _WINDOW)
    sidebar.get_by_role("button", name="Full screen", exact=True).click()
    expect(page.locator("[data-maximized]")).to_have_count(1)
    expect(review.get_by_test_id("diff-review-file-count")).to_have_text(
        f"{len(files)} file" + ("s" if len(files) > 1 else ""), timeout=60_000
    )
    if reveal is not None:
        choose_in_tree(page, sidebar, reveal)
    hide = sidebar.get_by_role("button", name="Hide changed files")
    if hide.count():
        hide.click()
    expect(sidebar.get_by_role("button", name="Show changed files")).to_be_visible()
    page.evaluate(_MARK_REVIEW_SCROLLER)
    return review


def _show_rich_diff(review: Locator, path: str) -> Locator:
    """Switch ``path`` to its rich diff and wait until it's rendered to the end."""
    review.get_by_role("button", name=f"Rich diff of {path}").click()
    preview = review.get_by_test_id("markdown-diff-preview")
    expect(preview.locator("[data-markdown-diff-complete]")).to_have_count(1, timeout=60_000)
    return preview


def _open_long_rich_diff(
    page: Page,
    session: tuple[str, str],
    tmp_path: Path,
    path: str,
    before: str,
    after: str,
) -> Locator:
    """A long document's rich diff, rendered to the end."""
    review = _open_review(page, session, tmp_path, {path: (before, after)})
    return _show_rich_diff(review, path)


# --- A file switched in place -------------------------------------------------------------

_MARKDOWN_BEFORE = "# Guide\n\nThe quick brown fox.\n\nThis paragraph stays.\n"
_MARKDOWN_AFTER = (
    "# Guide\n\nThe slow brown fox.\n\nThis paragraph stays.\n\nA new closing section.\n"
)
# Long code files around it, so the Markdown file sits mid-review with room above and below.
_AROUND_THE_GUIDE = "".join(f"line_{index} = {index}\n" for index in range(400))
_GUIDE_SWITCH = """[...document.querySelectorAll("button")].find(
    (button) => button.getAttribute("aria-label") === "Rich diff of docs/guide.md",
  )"""
# The guide's own file element, not its header, which sticks to the top while it scrolls past.
_GUIDE_TOP = f"""() => {{
  const scroller = document.querySelector("[data-perf-scroller]");
  const file = {_GUIDE_SWITCH}.closest("diffs-container");
  return file.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
}}"""
# Switches the guide twice in one task: the second click comes before the first switch can show,
# which no pointer can do.
_SWITCH_GUIDE_TWICE = f"""() => {{
  const toggle = {_GUIDE_SWITCH};
  toggle.click();
  toggle.click();
}}"""
# The review stops taking pointer events for a moment after each scroll, so a press then
# lands on the review behind the switch.
_GUIDE_TAKES_POINTER = f"() => getComputedStyle({_GUIDE_SWITCH}).pointerEvents !== 'none'"


def _wait_until_the_guide_takes_the_pointer(page: Page) -> None:
    page.evaluate(
        """() => new Promise((resolve) =>
          requestAnimationFrame(() => requestAnimationFrame(resolve)))"""
    )
    page.wait_for_function(_GUIDE_TAKES_POINTER, timeout=10_000)


def _watch_guide_top(page: Page) -> None:
    """Record the guide's top now and at every frame after."""
    page.evaluate(
        """(read) => {
          const top = new Function(`return (${read})`)();
          window.__guideTops = [top()];
          window.__guideWatching = true;
          const watch = () => {
            if (!window.__guideWatching) return;
            window.__guideTops.push(top());
            requestAnimationFrame(watch);
          };
          requestAnimationFrame(watch);
        }""",
        _GUIDE_TOP,
    )


def _press_guide_switch_watching_its_top(page: Page, review: Locator) -> None:
    """Press the guide's switch with the pointer, recording the guide's top from before it."""
    _wait_until_the_guide_takes_the_pointer(page)
    _watch_guide_top(page)
    review.get_by_role("button", name="Rich diff of docs/guide.md").click()


def _guide_tops_since_the_press(page: Page) -> list[float]:
    return page.evaluate("() => { window.__guideWatching = false; return window.__guideTops; }")


def _wait_for_guide_at(page: Page, top: float) -> None:
    page.wait_for_function(
        f"(top) => Math.abs(({_GUIDE_TOP})() - top) <= 2", arg=top, timeout=15_000
    )


def test_switching_a_file_to_its_rich_diff_keeps_it_in_place(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """Switching a mid-review Markdown file keeps it where it was, also when switched twice
    before the first switch shows."""
    review = _open_review(
        page,
        git_session,
        tmp_path,
        {
            "app/above.py": ("", _AROUND_THE_GUIDE),
            "docs/guide.md": (_MARKDOWN_BEFORE, _MARKDOWN_AFTER),
            "tools/below.py": ("", _AROUND_THE_GUIDE),
        },
        reveal="docs/guide.md",
    )
    toggle = review.get_by_role("button", name="Rich diff of docs/guide.md")
    expect(toggle).to_have_attribute("aria-pressed", "false", timeout=30_000)
    # The guide's file element 300 px down the view.
    page.evaluate(
        f"""(top) => {{
          const scroller = document.querySelector("[data-perf-scroller]");
          scroller.scrollTop += ({_GUIDE_TOP})() - top;
        }}""",
        300,
    )
    page.wait_for_function(f"() => Math.abs(({_GUIDE_TOP})() - 300) <= 2", timeout=10_000)
    preview = review.get_by_test_id("markdown-diff-preview")
    top = page.evaluate(_GUIDE_TOP)

    # From before the press until the rich diff shows, the guide never moves; nor going back.
    _press_guide_switch_watching_its_top(page, review)
    expect(preview.locator("[data-markdown-diff-complete]")).to_have_count(1, timeout=30_000)
    expect(toggle).to_have_attribute("aria-pressed", "true")
    _wait_for_guide_at(page, top)
    seen = _guide_tops_since_the_press(page)
    assert all(abs(each - top) <= 2 for each in seen), (top, seen)

    _press_guide_switch_watching_its_top(page, review)
    expect(preview).to_have_count(0, timeout=15_000)
    expect(toggle).to_have_attribute("aria-pressed", "false")
    _wait_for_guide_at(page, top)
    seen = _guide_tops_since_the_press(page)
    assert all(abs(each - top) <= 2 for each in seen), (top, seen)

    # On a slow machine a second click comes before the first switch shows: two clicks still
    # end on the source diff, and the guide never moves on the way.
    cdp = page.context.new_cdp_session(page)
    cdp.send("Emulation.setCPUThrottlingRate", {"rate": 6})
    try:
        _watch_guide_top(page)
        page.evaluate(_SWITCH_GUIDE_TWICE)
        # custom-lint: disable=sleep-then-assert -- lets any late switch show before the checks
        page.wait_for_timeout(1_500)
        expect(preview).to_have_count(0)
        expect(toggle).to_have_attribute("aria-pressed", "false")
        _wait_for_guide_at(page, top)
        seen = _guide_tops_since_the_press(page)
        assert all(abs(each - top) <= 2 for each in seen), (top, seen)
    finally:
        cdp.send("Emulation.setCPUThrottlingRate", {"rate": 1})


# --- Find ------------------------------------------------------------------------------------

_CURRENT_MATCH = """() => {
  const highlight = CSS.highlights.get("diff-review-find-current");
  const range = highlight && [...highlight][0];
  if (!range) return null;
  const bounds = range.getBoundingClientRect();
  const view = document.querySelector("[data-perf-scroller]").getBoundingClientRect();
  const element = range.startContainer.parentElement;
  return {
    section: element
      ?.closest("[data-markdown-diff-section]")
      ?.getAttribute("data-markdown-diff-section"),
    visible: bounds.height > 0 && bounds.top >= view.top && bounds.bottom <= view.bottom,
  };
}"""


def _wait_for_match_shown_in(page: Page, section: str | None) -> None:
    """Wait until find's current match is drawn inside the review's scroller, in ``section``
    when given."""
    page.wait_for_function(
        f"""(section) => {{
          const match = ({_CURRENT_MATCH})();
          return !!match && match.visible && (section === null || match.section === section);
        }}""",
        arg=section,
        timeout=15_000,
    )


def test_find_shows_a_late_match_in_one_tall_paragraph(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """Find brings a match near the end of one tall paragraph into view, not just its top."""
    tall = " ".join(f"word{index}" for index in range(6_000)) + " latesttarget."
    preview = _open_long_rich_diff(
        page,
        git_session,
        tmp_path,
        "docs/tall.md",
        "# Notes\n\nShort.\n",
        f"# Notes\n\nShort.\n\n{tall}\n",
    )
    query = open_find(page, page.get_by_test_id("diff-review"))
    query.fill("latesttarget")
    expect(page.get_by_test_id("diff-review-find-status")).to_have_text("1 of 1", timeout=15_000)
    paragraph = preview.get_by_role("group", name="Added")
    assert paragraph.bounding_box()["height"] > 2 * _WINDOW["height"]  # type: ignore[index]
    _wait_for_match_shown_in(page, None)


# Many short paragraphs, so the rich diff spans many groups; a few carry markers. The edited
# first paragraph is two sections, removed and added, so paragraph N is section N + 2.
_MARKERS = {1_700: "uniquetarget", 300: "pairmark", 1_500: "pairmark"}
_MANY_PARAGRAPHS = "".join(
    f"Paragraph {index} says hello there {_MARKERS.get(index, '')}.\n\n" for index in range(2_000)
)


def _open_many_groups(page: Page, session: tuple[str, str], tmp_path: Path) -> Locator:
    """The many-paragraph document's rich diff, whose groups skip layout while off screen."""
    after = _MANY_PARAGRAPHS.replace("Paragraph 0 says", "Paragraph 0 now says")
    preview = _open_long_rich_diff(
        page, session, tmp_path, "docs/many.md", _MANY_PARAGRAPHS, after
    )
    shape = page.evaluate(
        """() => {
          const groups = document.querySelectorAll(".markdown-diff-group");
          const section = document.querySelector("[data-markdown-diff-section]");
          return {
            groups: groups.length,
            group: groups.length > 0 ? getComputedStyle(groups[0]).contentVisibility : null,
            section: getComputedStyle(section).contentVisibility,
          };
        }"""
    )
    assert shape["groups"] > 10, shape
    assert shape["group"] == "auto" and shape["section"] == "visible", shape
    return preview


def test_find_shows_matches_in_groups_never_laid_out(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """Find shows a match in a far group the reader never scrolled to, then steps between
    matches in groups far apart."""
    preview = _open_many_groups(page, git_session, tmp_path)
    target = preview.locator('[data-markdown-diff-section="1702"]')
    assert target.evaluate(
        "(section) => !section.checkVisibility({ contentVisibilityAuto: true })"
    )

    query = open_find(page, page.get_by_test_id("diff-review"))
    status = page.get_by_test_id("diff-review-find-status")
    query.fill("uniquetarget")
    expect(status).to_have_text("1 of 1", timeout=15_000)
    _wait_for_match_shown_in(page, "1702")

    query.fill("pairmark")
    expect(status).to_have_text("1 of 2", timeout=15_000)
    for key, count, section in [
        ("Enter", "2 of 2", "1502"),
        ("Shift+Enter", "1 of 2", "302"),
        ("Shift+Enter", "2 of 2", "1502"),
    ]:
        query.press(key)
        expect(status).to_have_text(count)
        _wait_for_match_shown_in(page, section)


def test_the_end_of_the_scroll_shows_the_real_last_section(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """Wheeling to the end shows the document's last section, not an estimated height."""
    _open_many_groups(page, git_session, tmp_path)
    page.locator("[data-perf-scroller]").hover()
    at_end = """() => {
      const scroller = document.querySelector("[data-perf-scroller]");
      return scroller.scrollTop + scroller.clientHeight >= scroller.scrollHeight - 1;
    }"""
    for _ in range(400):
        if page.evaluate(at_end):
            break
        page.mouse.wheel(0, 5_000)
        page.wait_for_timeout(30)
    page.wait_for_function(at_end, timeout=10_000)
    last = page.evaluate(
        """() => {
          const scroller = document.querySelector("[data-perf-scroller]");
          const sections = document.querySelectorAll("[data-markdown-diff-section]");
          const section = sections[sections.length - 1];
          const view = scroller.getBoundingClientRect();
          const bounds = section.getBoundingClientRect();
          return {
            index: section.getAttribute("data-markdown-diff-section"),
            text: section.textContent.trim(),
            inView:
              bounds.height > 0 && bounds.top >= view.top && bounds.bottom <= view.bottom + 1,
          };
        }"""
    )
    # The edited first paragraph is two sections, removed and added: 2,001 in all.
    assert last["index"] == "2001" and last["text"].startswith("Paragraph 1999 says"), last
    assert last["inView"], last
