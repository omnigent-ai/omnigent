"""Journeys through the tabs sidebar's Diffs review that need a real browser's layout.

Each changes files in a real host's repository and reads the review as a person would:
what's in view after a citation or a visit, and how its headers and find bar fit. Unit
tests cover the bookkeeping behind each; these check the rendered result.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import Locator, Page, expect

from tests.e2e_ui.conftest import open_right_rail
from tests.e2e_ui.files._diffs_review import (
    WIDE,
    choose_in_tree,
    commit_then_edit,
    file_header,
    follow_link,
    open_diffs,
    open_find,
    workspace_tab,
)
from tests.e2e_ui.files._files_page import set_rail_width
from tests.e2e_ui.sessions.test_workspace_sidebar_v2 import (
    git_session,  # noqa: F401 — shared real-host fixture
)

# Four modules, each changed at one line of 80, so each file's diff fills about a screen.
_MODULES = {
    f"src/mod{index}.py": (
        "".join(f"old {index} {line}\n" for line in range(80)),
        "".join(
            f"new {index} {line}\n" if line == 40 else f"old {index} {line}\n"
            for line in range(80)
        ),
    )
    for index in range(4)
}
# Three hundred lines with one change near the end and a marked line far from it.
_LONG_BEFORE = "".join(f"line {number}\n" for number in range(1, 301)).replace(
    "line 150\n", "line 150 marker\n"
)
_LONG = {
    "server/long.py": (_LONG_BEFORE, _LONG_BEFORE.replace("line 295\n", "line 295 changed\n"))
}


def _repository(tmp_path: Path) -> Path:
    return tmp_path / "repository"


def _in_view(target: Locator, scroller: Locator) -> bool:
    """Whether ``target`` is drawn inside the review's scrolled area."""
    if not target.count():
        return False
    box, area = target.first.bounding_box(), scroller.bounding_box()
    return (
        box is not None
        and area is not None
        and area["y"] <= box["y"]
        and box["y"] + box["height"] <= area["y"] + area["height"]
    )


def _expect_in_review(review: Locator, text: str) -> None:
    expect(review.get_by_text(text, exact=True)).to_be_in_viewport(timeout=30_000)


# Resolves once the element has held its place for four frames; the review saves the reader's
# place two frames after the last scroll.
_HOLDS_STILL = """async (element) => {
  const frame = () => new Promise(requestAnimationFrame);
  let last = element.getBoundingClientRect().top;
  for (let still = 0, frames = 0; still < 4; frames++) {
    if (frames > 600) throw new Error("the review kept scrolling");
    await frame();
    const top = element.getBoundingClientRect().top;
    still = top === last ? still + 1 : 0;
    last = top;
  }
}"""


def _wait_until_still(target: Locator) -> None:
    target.first.evaluate(_HOLDS_STILL)


# Selects ``word`` in the first highlighted diff line that has it, inside the diff's shadow root.
_SELECT_IN_DIFF = """word => {
  for (const host of document.querySelectorAll("diffs-container")) {
    for (const row of host.shadowRoot.querySelectorAll("[data-line]")) {
      if (!row.querySelector("span[style*='--diffs-token']")) continue;
      const walker = document.createTreeWalker(row, NodeFilter.SHOW_TEXT);
      for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const at = node.textContent.indexOf(word);
        if (at < 0) continue;
        document.getSelection().setBaseAndExtent(node, at, node, at + word.length);
        return true;
      }
    }
  }
  return false;
}"""


def test_find_starts_from_selected_text_and_its_field_fits_the_query(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """⌘F with text selected searches for it; the field shows a 20-character query whole,
    and as the rail narrows its count gives way, then moves under the query."""
    base_url, session_id = git_session
    commit_then_edit(
        _repository(tmp_path),
        {
            "server/handler.py": (
                "def handler():\n    return 'old'\n",
                "def handler():\n    return 'needle'\n",
            )
        },
    )
    _sidebar, review = open_diffs(page, base_url, session_id, WIDE)
    expect(review.get_by_text("return 'needle'", exact=False).first).to_be_visible(timeout=30_000)
    review.get_by_test_id("diff-review-file-count").click()
    page.wait_for_function(_SELECT_IN_DIFF, arg="needle", timeout=15_000)
    page.keyboard.press("ControlOrMeta+f")
    query = page.get_by_test_id("diff-review-find-input")
    expect(query).to_have_value("needle")
    expect(page.get_by_test_id("diff-review-find-status")).to_have_text(re.compile(r"^1 of 1$"))
    # The seeded query is selected, so typing replaces it.
    page.keyboard.type("replacement")
    expect(query).to_have_value("replacement")
    # Reopened with another selection, find starts from that instead.
    query.press("Escape")
    expect(query).to_be_hidden()
    review.get_by_test_id("diff-review-file-count").click()
    page.wait_for_function(_SELECT_IN_DIFF, arg="handler", timeout=15_000)
    page.keyboard.press("ControlOrMeta+f")
    expect(query).to_have_value("handler")

    query.fill("provider_display_nam")
    expect(page.get_by_test_id("diff-review-find-status")).to_have_text("No results")
    fits = "input => input.scrollWidth <= input.clientWidth"
    assert query.evaluate(fits), "the query field cuts off a 20-character query"
    bar = page.get_by_role("search", name="Find in changes")
    field = page.get_by_test_id("diff-review-find-field")
    layouts = set()
    rail = page.get_by_role("complementary", name="Workspace")
    for width in (760, 640, 560, 480, 420, 360):
        set_rail_width(page, rail, width)
        expect(page.get_by_role("button", name="Close find")).to_be_in_viewport()
        pane, box, field_box = review.bounding_box(), bar.bounding_box(), field.bounding_box()
        assert pane is not None and box is not None and field_box is not None
        assert pane["x"] <= box["x"] + 0.5, (width, pane, box)
        assert box["x"] + box["width"] <= pane["x"] + pane["width"] + 0.5, (width, pane, box)
        one_row = box["height"] < field_box["height"] * 2
        layouts.add(one_row)
        # Sharing its row with the controls, the query still shows all 20 characters.
        if one_row:
            assert query.evaluate(fits), (width, box)
    assert layouts == {True, False}, "expected both the one-row and the two-row bar"


def test_a_cited_range_and_a_cited_column_come_into_view(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """A citation into a changed file centers the cited lines, and one to a far-right column
    of a long unwrapped line scrolls that column into view."""
    base_url, session_id = git_session
    long_line = "value = [" + ", ".join(f"item_{index}" for index in range(120)) + ", cited_end]\n"
    commit_then_edit(
        _repository(tmp_path),
        {**_LONG, "server/wide.py": ("first = 0\n" + long_line, "first = 1\n" + long_line)},
    )
    _sidebar, review = open_diffs(page, base_url, session_id)
    expect(review.get_by_text("line 295 changed")).to_be_attached(timeout=30_000)

    follow_link(page, f"/c/{session_id}?file=server/long.py&line=150&endLine=151&diff=1")
    _expect_in_review(review, "line 150 marker")
    _expect_in_review(review, "line 151")

    column = long_line.index("cited_end") + 1
    follow_link(page, f"/c/{session_id}?file=server/wide.py&line=2&column={column}&diff=1")
    page.wait_for_function(
        """() => {
            const review = document.querySelector('[data-testid="diff-review"]');
            const viewport = review.querySelector(".diff-review-scroll").getBoundingClientRect();
            for (const host of review.querySelectorAll(".diff-review-scroll *")) {
                const root = host.shadowRoot;
                if (!root) continue;
                const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
                for (let node = walker.nextNode(); node; node = walker.nextNode()) {
                    const index = node.data.indexOf("cited_end");
                    if (index < 0) continue;
                    const range = document.createRange();
                    range.setStart(node, index);
                    range.setEnd(node, index + "cited_end".length);
                    const bounds = range.getBoundingClientRect();
                    return bounds.left >= viewport.left && bounds.right <= viewport.right;
                }
            }
            return false;
        }""",
        timeout=15_000,
    )


def test_the_review_keeps_the_readers_place_and_a_line_it_expanded_across_a_visit(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """Scrolling down the review, visiting another tab and coming back shows the same lines;
    so does a place inside unchanged lines that find had to expand."""
    base_url, session_id = git_session
    files = {
        f"src/file{index:02}.py": (
            "".join(f"value_{index}_{line} = 0\n" for line in range(60)),
            "".join(f"value_{index}_{line} = 1\n" for line in range(60)),
        )
        for index in range(8)
    }
    commit_then_edit(_repository(tmp_path), {**files, **_LONG})
    sidebar, review = open_diffs(page, base_url, session_id)
    expect(review.get_by_test_id("diff-review-file-count")).to_have_text("9 files", timeout=30_000)
    target = review.get_by_text("value_5_10 = 1", exact=True)
    scroller = review.locator(".diff-review-scroll")
    scroller.hover()
    for _ in range(80):
        if _in_view(target, scroller):
            break
        page.mouse.wheel(0, 400)
        page.wait_for_timeout(50)
    expect(target).to_be_in_viewport()
    _wait_until_still(target)

    files_tab = workspace_tab(sidebar, "Files")
    if not files_tab.count():
        sidebar.get_by_role("button", name="New tab", exact=True).click()
        sidebar.get_by_role("option", name=re.compile(r"^Files", re.IGNORECASE)).click()
    else:
        files_tab.click()
    expect(review).to_be_hidden()
    workspace_tab(sidebar, "Diffs").click()
    _expect_in_review(review, "value_5_10 = 1")

    # A line find reveals inside collapsed unchanged lines is the place kept too.
    query = open_find(page, review)
    query.fill("150 marker")
    _expect_in_review(review, "line 150 marker")
    page.keyboard.press("Escape")
    _wait_until_still(review.get_by_text("line 150 marker", exact=True))
    workspace_tab(sidebar, "Files").click()
    expect(review).to_be_hidden()
    workspace_tab(sidebar, "Diffs").click()
    _expect_in_review(review, "line 150 marker")


def test_file_headers_copy_the_path_mark_it_viewed_and_expand_context(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """A file header copies its path, marks the file viewed across reloads, and its
    collapsed context opens in place."""
    base_url, session_id = git_session
    commit_then_edit(_repository(tmp_path), _MODULES)
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _sidebar, review = open_diffs(page, base_url, session_id)
    expect(file_header(review, "src/mod1.py")).to_be_attached(timeout=30_000)

    review.get_by_role("button", name="Copy path src/mod1.py").click()
    expect(page.get_by_text("Copied path")).to_be_visible()
    assert page.evaluate("navigator.clipboard.readText()").endswith("src/mod1.py")

    viewed = review.get_by_role("button", name="Viewed src/mod1.py")
    viewed.click()
    expect(viewed).to_have_attribute("aria-pressed", "true")
    expect(review.get_by_role("button", name="Expand src/mod1.py")).to_be_visible()
    # The mark is remembered for this session's changes.
    page.reload()
    open_right_rail(page)
    expect(review.get_by_role("button", name="Viewed src/mod1.py")).to_have_attribute(
        "aria-pressed", "true", timeout=30_000
    )
    expect(review.get_by_role("button", name="Expand src/mod1.py")).to_be_visible()

    # Collapsed context opens in place.
    expect(review.get_by_text("old 0 0", exact=True)).to_have_count(0)
    review.get_by_text(re.compile(r"^\d+ unmodified lines$")).first.click()
    expect(review.get_by_text("old 0 0", exact=True)).to_be_visible()


def test_the_review_follows_the_file_a_person_is_looking_at(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """The review's tree steps to a file, View File shows it in Files, and that file's Show
    changes comes back to it in the review."""
    base_url, session_id = git_session
    commit_then_edit(_repository(tmp_path), _MODULES)
    sidebar, review = open_diffs(page, base_url, session_id)
    expect(file_header(review, "src/mod0.py")).to_be_in_viewport(timeout=30_000)

    choose_in_tree(page, sidebar, "src/mod2.py")
    expect(file_header(review, "src/mod2.py")).to_be_in_viewport()
    expect(file_header(review, "src/mod0.py")).not_to_be_in_viewport()

    file_header(review, "src/mod2.py").click()
    page.get_by_role("menuitem", name="View File").click()
    files = sidebar.get_by_test_id("workspace-files-place").filter(visible=True)
    expect(files.get_by_text("old 2 0", exact=True)).to_be_visible(timeout=30_000)
    files.get_by_role("button", name="Show changes").click()
    expect(file_header(review, "src/mod2.py")).to_be_in_viewport(timeout=30_000)
    expect(file_header(review, "src/mod0.py")).not_to_be_in_viewport()


# The header's box and its title and ⋯ menu, read across the diff's shadow root.
_HEADER_GEOMETRY = """(menu) => {
    const header = menu.closest("[slot]").assignedSlot.closest("[data-diffs-header]");
    const suffix = header.querySelector("slot[name=header-filename-suffix]").assignedElements()[0];
    const title = suffix.querySelector("[title]");
    const box = (element) => element.getBoundingClientRect().toJSON();
    return {
        header: box(header),
        title: box(title),
        menu: box(menu),
        menuVisible: menu.checkVisibility(),
        truncated: title.scrollWidth > title.clientWidth,
        fontSize: parseFloat(getComputedStyle(title).fontSize),
    };
}"""
_LONG_PATH = "src/" + "a_deeply_nested_directory/" * 6 + "with_a_rather_long_module_name.py"
# Where the file name's last characters are drawn, against the title's box.
_NAME_END_SHOWN = """(menu) => {
    const header = menu.closest("[slot]").assignedSlot.closest("[data-diffs-header]");
    const suffix = header.querySelector("slot[name=header-filename-suffix]");
    const title = suffix.assignedElements()[0].querySelector("[title]");
    const end = "module_name.py";
    const walker = document.createTreeWalker(title, NodeFilter.SHOW_TEXT);
    for (let node = walker.nextNode(); node; node = walker.nextNode()) {
        const at = node.data.lastIndexOf(end);
        if (at < 0) continue;
        const range = document.createRange();
        range.setStart(node, at);
        range.setEnd(node, at + end.length);
        const name = range.getBoundingClientRect(), box = title.getBoundingClientRect();
        return {
            inside: name.width > 0 && name.left >= box.left - 0.5 && name.right <= box.right + 0.5,
            name: name.toJSON(),
            title: box.toJSON(),
        };
    }
    return { inside: false, text: title.textContent };
}"""


@pytest.mark.parametrize("color_scheme", ["light", "dark"])
def test_a_long_path_gives_up_its_folders_first(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
    color_scheme: str,
) -> None:
    """A long path is cut from its folders, so the file's name and the header's ⋯ menu stay
    in view, on a wide and a narrow rail."""
    base_url, session_id = git_session
    commit_then_edit(_repository(tmp_path), {_LONG_PATH: _MODULES["src/mod0.py"]})
    page.emulate_media(color_scheme=color_scheme)  # type: ignore[arg-type]
    sidebar, review = open_diffs(page, base_url, session_id, WIDE)
    menu = file_header(review, _LONG_PATH)
    expect(menu).to_be_visible(timeout=30_000)
    for width in (720, 420):
        set_rail_width(page, sidebar, width)
        geometry = menu.evaluate(_HEADER_GEOMETRY)
        header, title, menu_box = geometry["header"], geometry["title"], geometry["menu"]
        # Font-independent: the title is cut but keeps about three characters, and the menu
        # stays inside the header, whatever the platform's font metrics.
        assert geometry["truncated"], (width, geometry)
        assert title["width"] >= 3 * geometry["fontSize"], (width, geometry)
        assert header["left"] <= title["left"] and title["right"] <= menu_box["left"], geometry
        assert geometry["menuVisible"], (width, geometry)
        assert menu_box["right"] <= header["right"], (width, geometry)
        # The name, not the folders, is what stays readable: its last characters are drawn
        # inside the title's box, whichever end the cut is made at.
        shown = menu.evaluate(_NAME_END_SHOWN)
        assert shown["inside"], (width, shown)


_HEADER_OVERFLOW = """(header) => {
  const edge = header.getBoundingClientRect();
  const spilled = [...header.querySelectorAll("*")].filter((element) => {
    const box = element.getBoundingClientRect();
    if (box.width <= 1 || box.height <= 1) return false;
    return box.left < edge.left - 1 || box.right > edge.right + 1;
  });
  return {
    scrolls: header.scrollWidth > header.clientWidth + 1,
    spilled: spilled.map((element) => element.outerHTML.slice(0, 120)),
  };
}"""


def _expect_fits(container: Locator) -> None:
    fit = container.evaluate(_HEADER_OVERFLOW)
    assert fit == {"scrolls": False, "spilled": []}, fit


def _reach_commits_and_changes(page: Page, sidebar: Locator) -> str:
    """Show Commits, then Changes, by the header's tabs or its folded picker; returns which."""
    review = sidebar.get_by_test_id("changes-review")
    commits = review.get_by_test_id("changes-review-commits")
    commits_tab = review.get_by_role("tab", name="Commits")
    if commits_tab.is_visible():
        commits_tab.click()
        expect(commits).to_be_visible(timeout=30_000)
        review.get_by_role("tab", name="Changes").click()
        expect(commits).to_have_count(0)
        return "tabs"
    picker = review.get_by_role("combobox", name="Diffs section")
    expect(picker).to_be_visible()
    picker.click()
    page.get_by_role("option", name=re.compile(r"^Commits")).click()
    expect(commits).to_be_visible(timeout=30_000)
    picker.click()
    page.get_by_role("option", name="Changes", exact=True).click()
    expect(commits).to_have_count(0)
    return "picker"


def test_the_header_and_toolbar_fit_a_narrowing_rail(
    page: Page,
    git_session: tuple[str, str],  # noqa: F811 — pytest injects the imported fixture
    tmp_path: Path,
) -> None:
    """As the rail narrows, the review's header and toolbar stay inside it, Commits and
    Changes stay reachable (the tabs fold into a picker at the narrowest), and the toolbar's
    view options move into its menu."""
    base_url, session_id = git_session
    commit_then_edit(_repository(tmp_path), _MODULES)
    sidebar, review = open_diffs(page, base_url, session_id, WIDE)
    expect(review.get_by_test_id("diff-review-file-count")).to_have_text("4 files", timeout=30_000)
    header = sidebar.get_by_test_id("changes-review-header")
    toolbar = review.get_by_role("toolbar", name="Diff review")
    reached = []
    for width in (760, 520, 360):
        set_rail_width(page, sidebar, width)
        _expect_fits(header)
        _expect_fits(toolbar)
        reached.append(_reach_commits_and_changes(page, sidebar))
        _expect_fits(header)
    assert reached[0] == "tabs" and reached[-1] == "picker", reached

    # At the narrowest, the toolbar's view options are in its menu.
    toolbar.get_by_role("button", name="More view options").click()
    menu = page.get_by_role("menu")
    for label in ("Skip test files", "Hide whitespace changes", "Collapse all files"):
        expect(menu.get_by_role("menuitem", name=label)).to_be_visible()
    page.keyboard.press("Escape")
