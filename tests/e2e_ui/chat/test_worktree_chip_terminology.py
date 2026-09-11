"""E2E: the composer's git chip and its popover must speak one language.

The second chip in the gray bar above the composer
(``composer-git-branch`` on ``pages/ChatPage.tsx``) shows the session's
runner-reported branch. On a session whose runner has not reported a
branch the chip's empty-state label is phrased in *branch* terms
("No branch reported"), while the popover it opens is titled in
*worktree* terms ("Session worktree") and its body mixes both nouns
("has not reported a branch" / "keeps its workspace and worktree").
One control naming one concept with two different nouns leaves a user
unable to tell whether it is a branch indicator or a worktree selector.

Journey: open a session whose runner has not reported a branch (the
seeded hello_world session never reports one), read the chip in the
gray bar above the composer, click it, and read the popover's title and
body. Whichever noun the product picks, the chip's empty state, the
popover title, and the popover body have to agree.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

# The two competing nouns for this control. The copy must settle on one;
# the assertions below check agreement, not a specific choice, so the
# test stays green whichever term the product standardizes on.
_TERMS = ("branch", "worktree")


def _terms_in(text: str) -> set[str]:
    """Which of the two competing nouns *text* mentions (case-insensitive)."""
    lowered = text.lower()
    return {term for term in _TERMS if term in lowered}


def _open_git_chip_popover(page: Page, base_url: str, session_id: str) -> tuple[str, str, str]:
    """Open ``/c/<id>``, click the git chip, and return its copy.

    :returns: ``(chip_label, popover_title, popover_body)`` — the chip's
        rendered empty-state label, the popover's title line, and the
        popover's body paragraphs joined with newlines.
    """
    page.goto(f"{base_url}/c/{session_id}")

    chip = page.get_by_test_id("composer-git-branch")
    expect(chip).to_be_visible(timeout=30_000)
    chip_label = (chip.inner_text() or "").strip()
    assert chip_label, "the git chip rendered without any label text"

    chip.click()
    menu = page.locator('[data-slot="dropdown-menu-content"]')
    expect(menu).to_be_visible()

    title_el = menu.locator('[data-slot="dropdown-menu-label"]')
    expect(title_el).to_be_visible()
    title = (title_el.inner_text() or "").strip()
    assert title, "the git chip's popover rendered without a title"

    # Let the popover's open animation finish so its copy is fully painted
    # (and legible in journey recordings) before reading it.
    page.wait_for_timeout(1_500)

    body = "\n".join(t.strip() for t in menu.locator("p").all_inner_texts())
    assert body, "the git chip's popover rendered without body copy"
    return chip_label, title, body


def test_git_chip_empty_state_matches_popover_title(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The chip's empty-state label and its popover title use the same noun.

    Today the chip reads "No branch reported" (branch terms) while the
    popover is titled "Session worktree" (worktree terms): the two sets
    of nouns are non-empty and disjoint, so this fails until the copy
    agrees on one term.
    """
    base_url, session_id = seeded_session
    chip_label, title, _body = _open_git_chip_popover(page, base_url, session_id)

    chip_terms = _terms_in(chip_label)
    title_terms = _terms_in(title)
    assert not (chip_terms and title_terms and not (chip_terms & title_terms)), (
        f"the composer chip is labeled {chip_label!r} "
        f"({'/'.join(sorted(chip_terms))} terms) but clicking it opens a popover "
        f"titled {title!r} ({'/'.join(sorted(title_terms))} terms); the chip and "
        "its popover must use the same noun for the one concept they name"
    )


def test_git_chip_popover_body_does_not_mix_nouns(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The popover's empty-state body sticks to one of the two nouns.

    Today the body mixes both: "The runner has not reported a branch for
    this session." followed by "The current session keeps its workspace
    and worktree." — so a user reading the popover still cannot tell
    which concept the control is about. Fails until the empty-state body
    settles on the same single term as the rest of the control.
    """
    base_url, session_id = seeded_session
    _chip_label, title, body = _open_git_chip_popover(page, base_url, session_id)

    body_terms = _terms_in(body)
    assert body_terms != set(_TERMS), (
        f"the popover body mixes both 'branch' and 'worktree' terminology:\n"
        f"{body!r}\n"
        "the empty-state copy must use one noun, consistently with the chip "
        "label and popover title"
    )
    # The body must also elaborate the same concept the title names —
    # a body speaking only the *other* noun is the same contradiction.
    title_terms = _terms_in(title)
    assert not (body_terms and title_terms and not (body_terms & title_terms)), (
        f"the popover is titled {title!r} but its body {body!r} only uses the "
        "competing noun; title and body must describe the same concept"
    )
