"""E2E: substring matching in the slash-command suggestions menu.

Covers the user-facing behavior added by the substring-matching change:
the ``/`` menu matches a query as a case-insensitive **substring** of a
command's name, not just a prefix. Before the change the menu
prefix-matched the full (namespaced) name, so typing the leaf a user
remembers — e.g. ``/using-superpowers`` for
``/superpowers:using-superpowers`` — surfaced nothing. The mechanism is
identical for any mid-name substring, which is what these tests exercise
end-to-end in a real browser (a colon-namespaced skill can't be bundled
from a spec — ``SkillSpec.name`` is ``[a-z0-9-]+`` — but ``/review``
matching ``code-review`` is the same leaf/mid-string substring path).

Two surfaces own separate copies of the filter, so both are driven:

- **In-session composer** (``ChatPage`` ``menuMatches`` + the shared
  ``SlashCommandMenu`` render filter): a built-in is matched by a
  mid-name fragment. Asserting the matched row is *highlighted*
  (``data-active``) proves the render filter AND ``menuMatches`` agree —
  the highlight is driven by ``menuMatches`` (keyboard index), the row by
  the render filter, so a divergence would mis-place the highlight.
- **New-chat landing composer** (``NewChatDialog`` ``slashMenuMatches``):
  a bundled skill is matched by a mid-name fragment, then Tab completes
  it to its full canonical name — covering keyboard completion too.

Selectors mirror the component: rows are
``data-testid="slash-menu-item-<name-sans-slash>"`` and the highlighted
row carries ``data-active="true"`` (see ``SlashCommandMenu.tsx``).
"""

from __future__ import annotations

import json

from playwright.sync_api import Page, Route, expect


def test_in_session_composer_matches_builtin_by_substring(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The in-session ``/`` menu surfaces a built-in matched mid-name.

    ``/ontext`` is a substring of ``/context`` but a prefix of no command,
    so under the old prefix filter it matched nothing. The row must appear
    AND be the highlighted (auto-selected first) match — the highlight is
    driven by ``menuMatches`` (the keyboard-nav filter) while the row is
    rendered by ``SlashCommandMenu``'s own filter, so a passing assertion
    proves both filters substring-match in lockstep.

    :param page: Playwright page (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    # Mid-name fragment of "/context": prefix of nothing, so this only
    # surfaces a row under substring matching.
    composer.fill("/ontext")

    context_row = page.get_by_test_id("slash-menu-item-context")
    expect(context_row).to_be_visible()
    # The first (only) match is auto-highlighted; the highlight comes from
    # menuMatches, so this pins the keyboard-nav filter to the rendered row.
    expect(context_row).to_have_attribute("data-active", "true")


def test_landing_composer_matches_skill_and_tab_completes(
    page: Page,
    live_server: str,
) -> None:
    """The new-chat landing ``/`` menu matches a skill mid-name, and Tab completes it.

    Stubs ``GET /v1/agents`` with a single non-native agent bundling a
    ``code-review`` skill (the landing menu lists the selected agent's
    skills only, and suppresses entirely for native-terminal agents). The
    session list is stubbed empty so no agent discovered by the
    ``kind=any`` scan sorts ahead and steals auto-selection.

    ``/review`` is a substring of ``code-review`` but a prefix of no
    command — it surfaces the row only under substring matching. Tab then
    completes the highlighted match to its full canonical name with a
    trailing space (skills fill rather than execute), covering keyboard
    completion on this surface.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    """
    agents_body = {
        "data": [
            {
                "id": "ag_helper_e2e",
                "name": "helper",
                "display_name": "Helper",
                "description": "A helper agent",
                # Non-native brain harness: keeps the slash menu enabled
                # (native-terminal agents suppress it) and off the
                # permission-mode / native-wrapper paths.
                "harness": "claude-sdk",
                "skills": [{"name": "code-review", "description": "Review a pull request"}],
            }
        ]
    }
    empty_list = {"object": "list", "data": [], "has_more": False}

    def _fulfill(route: Route, body: dict[str, object]) -> None:
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route("**/v1/agents", lambda r: _fulfill(r, agents_body))
    # Neutralize the sidebar list + kind=any agent-discovery scan so only
    # the stubbed agent feeds the picker (and auto-selects).
    page.route("**/v1/sessions", lambda r: _fulfill(r, empty_list))

    page.goto(f"{live_server}/")

    landing_input = page.get_by_test_id("new-chat-landing-input")
    expect(landing_input).to_be_visible(timeout=30_000)
    # Wait for the stubbed agent to resolve + auto-select, so its skills
    # populate the menu's command map before we type.
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
        "Helper", timeout=30_000
    )

    # Mid-name fragment of "code-review": prefix of nothing.
    landing_input.fill("/review")
    skill_row = page.get_by_test_id("slash-menu-item-code-review")
    expect(skill_row).to_be_visible()

    # Tab completes the highlighted match to the full canonical name; skills
    # fill (with a trailing space for args) rather than execute.
    landing_input.press("Tab")
    expect(landing_input).to_have_value("/code-review ")
