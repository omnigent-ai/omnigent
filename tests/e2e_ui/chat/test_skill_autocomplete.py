"""E2E: completing a skill from a ``/`` typed anywhere, and host-discovered skills.

Two user-facing behaviors, driven through the real SPA in a browser:

- **A ``/`` mid-draft opens the menu and completes in place.** The menu used
  to require the draft to *start* with a slash and carry no space, so a skill
  could only be named as the very first thing typed — reaching for one
  mid-sentence ("please run /desl…") got nothing. Both composers now read the
  token at the caret (``detectSlashTokenAt``) and rewrite only that token
  (``spliceSlashToken``), so the words around it survive.
- **The new-chat menu lists skills the chosen host discovered.** Before, the
  landing composer knew only the agent's *bundled* skills, so a user's own
  ``~/.claude/skills`` entry wasn't completable until a runner had bound.
  ``GET /v1/hosts/{id}/agents/{id}/skills`` asks the host for them.

Selectors mirror the component: rows are
``data-testid="slash-menu-item-<name-sans-slash>"`` and the highlighted row
carries ``data-active="true"`` (see ``SlashCommandMenu.tsx``).
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

# Bundled skill on the stubbed landing agent.
_BUNDLED_SKILL = {"name": "code-review", "description": "Review a pull request"}
# A skill only the host can see (the user's own ~/.claude/skills), namespaced
# the way an enabled Claude Code plugin's skills are.
_HOST_SKILL = {"name": "dev-productivity:deslop", "description": "Remove AI slop"}
_HOST_ID = "host_skills_e2e"
# The pre-launch skill endpoint, with or without its ``?path=`` query.
_HOST_SKILLS_RE = re.compile(r"/v1/hosts/[^/]+/agents/[^/]+/skills")


def _fulfill(route: Route, body: dict[str, object]) -> None:
    """Answer *route* with *body* as JSON."""
    route.fulfill(status=200, content_type="application/json", body=json.dumps(body))


def _stub_landing(
    page: Page,
    *,
    bundled: list[dict[str, str]],
    host: list[dict[str, str]],
    harness: str = "claude-sdk",
    agent_name: str = "helper",
) -> None:
    """Stub the landing screen's edges: one host, one agent, its skills.

    The agent is the only row, so it auto-selects. The session list is stubbed
    empty so no agent discovered by the ``kind=any`` scan sorts ahead and
    steals that auto-selection.

    :param page: The Playwright page to install routes on.
    :param bundled: Skills reported by ``GET /v1/agents`` for the agent.
    :param host: Skills the host reports for it (the pre-launch endpoint).
    :param harness: The agent's harness — drives whether the frontend treats it
        as a native terminal agent.
    :param agent_name: The agent's name, which the native-agent mapping also
        keys on.
    """
    page.route(
        "**/v1/hosts",
        lambda r: _fulfill(
            r,
            {
                "hosts": [
                    {
                        "host_id": _HOST_ID,
                        "name": "e2e-host",
                        "owner": "e2e",
                        "status": "online",
                    }
                ]
            },
        ),
    )
    page.route(
        "**/v1/agents",
        lambda r: _fulfill(
            r,
            {
                "data": [
                    {
                        "id": "ag_helper_e2e",
                        "name": agent_name,
                        "display_name": "Helper",
                        "description": "A helper agent",
                        "harness": harness,
                        "skills": bundled,
                    }
                ]
            },
        ),
    )
    page.route(_HOST_SKILLS_RE, lambda r: _fulfill(r, {"skills": host}))
    page.route(
        "**/v1/sessions", lambda r: _fulfill(r, {"object": "list", "data": [], "has_more": False})
    )


def _open_landing(page: Page, base_url: str) -> None:
    """Load the landing screen and wait for the stubbed agent to auto-select."""
    page.goto(f"{base_url}/")
    expect(page.get_by_test_id("new-chat-landing-input")).to_be_visible(timeout=30_000)
    # The menu's command map is fed by the selected agent, so wait for the
    # pick to settle before typing.
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
        "Helper", timeout=30_000
    )


def test_landing_composer_completes_a_skill_typed_mid_draft(
    page: Page,
    live_server: str,
) -> None:
    """A ``/`` inside a sentence opens the menu, and Tab completes it in place.

    Under the old whole-draft check this draft was invisible to the menu twice
    over: the slash isn't leading, and the draft already carries a space.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    """
    _stub_landing(page, bundled=[_BUNDLED_SKILL], host=[])
    _open_landing(page, live_server)

    landing_input = page.get_by_test_id("new-chat-landing-input")
    # ``fill`` leaves the caret at the end, i.e. on the "/revi" token.
    landing_input.fill("please run /revi")
    skill_row = page.get_by_test_id("slash-menu-item-code-review")
    expect(skill_row).to_be_visible()
    expect(skill_row).to_have_attribute("data-active", "true")

    landing_input.press("Tab")
    # Only the token is rewritten — the words before it are still there.
    expect(landing_input).to_have_value("please run /code-review ")


def test_landing_composer_lists_a_skill_only_the_host_can_see(
    page: Page,
    live_server: str,
) -> None:
    """A skill from the host (not the bundle) is offered and completes.

    The agent bundles nothing, so this row can only come from the pre-launch
    host lookup — the gap this closes is that a user's own skill was
    uncompletable on the first message.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    """
    _stub_landing(page, bundled=[], host=[_HOST_SKILL])
    _open_landing(page, live_server)

    landing_input = page.get_by_test_id("new-chat-landing-input")
    landing_input.fill("/deslop")
    host_row = page.get_by_test_id("slash-menu-item-dev-productivity:deslop")
    expect(host_row).to_be_visible(timeout=15_000)
    expect(host_row).to_have_attribute("data-active", "true")

    landing_input.press("Tab")
    expect(landing_input).to_have_value("/dev-productivity:deslop ")


def test_landing_composer_offers_skills_for_a_native_terminal_agent(
    page: Page,
    live_server: str,
) -> None:
    """Claude Code — what auto-selects for most people — offers its skills too.

    The landing menu used to suppress itself for every native terminal agent on
    the theory that the vendor CLI owns slash commands. But the host-discovered
    skills *are* that CLI's own commands, and the in-session composer never
    gated on the harness — so the new-chat screen was the one place a Claude
    Code user's own skills were invisible.

    :param page: Playwright page (fresh context per test).
    :param live_server: Base URL of the spawned server serving the SPA.
    """
    _stub_landing(
        page,
        bundled=[],
        host=[_HOST_SKILL],
        harness="claude-native",
        agent_name="claude-native-ui",
    )
    page.goto(f"{live_server}/")
    landing_input = page.get_by_test_id("new-chat-landing-input")
    expect(landing_input).to_be_visible(timeout=30_000)
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text(
        "Claude Code", timeout=30_000
    )

    landing_input.fill("/deslop")
    host_row = page.get_by_test_id("slash-menu-item-dev-productivity:deslop")
    expect(host_row).to_be_visible(timeout=15_000)

    landing_input.press("Tab")
    # Completing the name is all this does — the CLI interprets it from there.
    expect(landing_input).to_have_value("/dev-productivity:deslop ")


def test_in_session_composer_completes_a_skill_typed_mid_draft(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The in-session ``/`` menu completes a skill named inside a sentence.

    The session's skills come from its snapshot, so the real ``GET
    /v1/sessions/{id}`` response is patched with one rather than faked
    wholesale — everything else about the session stays real.

    Built-ins are deliberately absent mid-draft (they act on the session,
    which completing a word in a sentence cannot do), so the assertion also
    pins that only the skill is offered there.

    :param page: Playwright page (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` from the fixture.
    """
    base_url, session_id = seeded_session

    def handle_session(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = route.fetch()
        payload = response.json()
        payload["skills"] = [_BUNDLED_SKILL]
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route(f"**/v1/sessions/{session_id}**", handle_session)
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill("take a look and /revi")

    skill_row = page.get_by_test_id("slash-menu-item-code-review")
    expect(skill_row).to_be_visible(timeout=15_000)
    # Mid-draft the menu is skills-only: a built-in that a leading "/" would
    # offer (and that "revi" doesn't narrow away) must not be reachable here.
    expect(page.get_by_test_id("slash-menu-item-context")).to_have_count(0)

    composer.press("Tab")
    expect(composer).to_have_value("take a look and /code-review ")
