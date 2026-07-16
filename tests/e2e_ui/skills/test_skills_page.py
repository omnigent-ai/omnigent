"""E2E: the Skills page (`/skills`) renders the harness-neutral catalog.

Covers the cross-harness Skill Registry UI (``web/src/pages/SkillsPage.tsx``)
reached from a first-class sidebar entry (``web/src/shell/Sidebar.tsx``) and its
route (``web/src/App.tsx``). The page is a compact master-detail: a ~292px list
grouped into Built in / Workspace / Personal, a persistent detail pane with
rendered/source instructions and a copy control, and one collapsed Advanced
details disclosure holding all harness/vendor provenance.

The ``/v1/skills`` backend lands on a sibling branch, so the API client has no
runtime fixture fallback — a missing endpoint would surface the page's error
state. To keep this suite deterministic and self-contained, we intercept the
catalog / detail / trust routes here and fulfill them with fixtures matching the
frozen wire shape (``web/src/lib/skillsApi.ts``). That exercises the exact
projected shapes and every interaction (search, group collapse, selection, the
include-other-tools trust switch, instructions rendered/source, and Advanced
details) against a real browser without depending on production mock data.
Backend-integration behavior is covered by the Vitest suites; this asserts the
page is wired into the shell and the interactions work end-to-end.

No LLM turn is involved — pure client-side routing + rendering over the
intercepted routes.
"""

from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, unquote, urlparse

from playwright.sync_api import Page, Route, expect

# ── Fixtures matching the frozen /v1/skills wire shape (snake_case) ───────────
#
# Ordered so the catalog's first row is `review-diff` (the page auto-selects
# `data[0]`). Two built-in, one workspace, one native-personal, and one
# personal skill discovered from another tool (hidden until include is on).

_SUMMARY_REVIEW_DIFF = {
    "id": "bundle:review-diff",
    "name": "review-diff",
    "description": "Review the working diff for correctness and security.",
    "origin": "built_in",
    "enabled": True,
    "available": True,
    "has_conflict": False,
    "updated_at": None,
}
_SUMMARY_SHIP = {
    "id": "bundle:ship",
    "name": "ship",
    "description": "Commit, push, and open a PR.",
    "origin": "built_in",
    "enabled": True,
    "available": True,
    "has_conflict": False,
    "updated_at": None,
}
_SUMMARY_RELEASE_NOTES = {
    "id": "workspace:release-notes",
    "name": "release-notes",
    "description": "Draft concise notes organized by user impact.",
    "origin": "workspace",
    "enabled": True,
    "available": True,
    "has_conflict": False,
    "updated_at": None,
}
_SUMMARY_FRONTEND_DESIGN = {
    "id": "personal:claude:frontend-design",
    "name": "frontend-design",
    "description": "Design distinctive, production-grade interfaces.",
    "origin": "personal",
    "enabled": True,
    "available": True,
    "has_conflict": False,
    "updated_at": None,
}
_SUMMARY_MIGRATE = {
    "id": "personal:codex:migrate",
    "name": "migrate",
    "description": "Plan and execute a migration across sites.",
    "origin": "personal",
    "enabled": True,
    "available": True,
    "has_conflict": False,
    "updated_at": None,
}

# The native set (include off) and the widened set (include on, + other-tool).
_BASE_SUMMARIES = [
    _SUMMARY_REVIEW_DIFF,
    _SUMMARY_SHIP,
    _SUMMARY_RELEASE_NOTES,
    _SUMMARY_FRONTEND_DESIGN,
]
_OTHER_TOOL_SUMMARIES = [_SUMMARY_MIGRATE]


def _detail_body(summary: dict, *, provider: str, content: str) -> dict:
    """Build a detail wire payload from a summary + provenance provider."""
    return {
        **summary,
        "content": content,
        "provenance": {
            "provider": provider,
            "original_path": f"./skills/{summary['name']}",
            "source_kind": "agent bundle" if provider == "omnigent" else "personal library",
            "source_coords": summary["id"],
            "digest": "deadbeef",
        },
        "selected_winner": summary["id"],
        "conflict_candidates": [],
        "delivery": {"mode": "automatic"},
    }


# Keyed by canonical id → detail payload. Instructions carry SKILL.md
# frontmatter so the rendered/source toggle test can assert both views.
_DETAILS: dict[str, dict] = {
    "bundle:review-diff": _detail_body(
        _SUMMARY_REVIEW_DIFF,
        provider="omnigent",
        content=(
            "---\nname: review-diff\n"
            "description: Review the working diff.\n---\n\n"
            "# review-diff\n\nUse this skill before opening a PR."
        ),
    ),
    "bundle:ship": _detail_body(
        _SUMMARY_SHIP,
        provider="omnigent",
        content="---\nname: ship\ndescription: Commit and PR.\n---\n\n# ship\n\nWrap up a change.",
    ),
    "workspace:release-notes": _detail_body(
        _SUMMARY_RELEASE_NOTES,
        provider="omnigent",
        content="---\nname: release-notes\n---\n\n# release-notes\n\nGroup changes by outcome.",
    ),
    "personal:claude:frontend-design": _detail_body(
        _SUMMARY_FRONTEND_DESIGN,
        provider="claude",
        content="---\nname: frontend-design\n---\n\n# frontend-design\n\nBuild distinctive UIs.",
    ),
    "personal:codex:migrate": _detail_body(
        _SUMMARY_MIGRATE,
        provider="codex",
        content="---\nname: migrate\n---\n\n# migrate\n\nPlan + execute a migration.",
    ),
}

# The persisted trust setting the GET returns and the PUT toggles. A mutable
# holder so PUT /v1/skills/trust flips it and the follow-up GET/catalog agree.
_SKILLS_ROUTE = re.compile(r"/v1/skills(/|\?|$)")


def _install_skills_routes(page: Page) -> None:
    """Intercept every /v1/skills* request with deterministic fixtures.

    A single handler dispatches on the path so the ordering constraints between
    the list, detail, and trust routes are explicit rather than dependent on
    Playwright's registration order.
    """
    trust_state = {"include_other_tools": False}

    def _fulfill(route: Route, body: object, status: int = 200) -> None:
        route.fulfill(
            status=status,
            headers={"content-type": "application/json"},
            body=json.dumps(body),
        )

    def _handler(route: Route) -> None:
        parsed = urlparse(route.request.url)
        path = parsed.path
        query = parse_qs(parsed.query)

        # Trust read/write.
        if path.endswith("/v1/skills/trust"):
            if route.request.method == "PUT":
                payload = route.request.post_data_json or {}
                trust_state["include_other_tools"] = payload.get("value") == "all-host"
            _fulfill(
                route,
                {
                    "value": "all-host" if trust_state["include_other_tools"] else "current",
                    "include_other_tools": trust_state["include_other_tools"],
                },
            )
            return

        # Detail: /v1/skills/<url-encoded id>?...
        detail_match = re.search(r"/v1/skills/(?P<id>[^/?]+)$", path)
        if detail_match:
            skill_id = unquote(detail_match.group("id"))
            detail = _DETAILS.get(skill_id)
            if detail is None:
                _fulfill(route, {"error": {"code": "not_found", "message": skill_id}}, status=404)
            else:
                _fulfill(route, detail)
            return

        # Catalog: /v1/skills?session_id&include_other_tools
        if path.endswith("/v1/skills"):
            include = query.get("include_other_tools", ["false"])[0] == "true"
            summaries = list(_BASE_SUMMARIES)
            if include:
                summaries += _OTHER_TOOL_SUMMARIES
            _fulfill(
                route,
                {
                    "object": "list",
                    "data": summaries,
                    "include_other_tools": include,
                    "hidden_count": 0 if include else len(_OTHER_TOOL_SUMMARIES),
                },
            )
            return

        route.fallback()

    page.route(_SKILLS_ROUTE, _handler)


def _open_skills(page: Page, base_url: str, session_id: str) -> None:
    """Open a seeded session, then navigate to /skills and wait for the list.

    The catalog is session-contextual: the page resolves the bound session from
    the last-viewed chat (chat store) or the most-recent session. Visiting the
    seeded chat first makes that resolution deterministic, so the master list
    renders rather than the no-session empty state.
    """
    _install_skills_routes(page)
    page.goto(f"{base_url}/c/{session_id}")
    # Sidebar must be up so the Skills nav + session context are available.
    expect(page.get_by_test_id("skills-button")).to_be_visible(timeout=30_000)
    page.get_by_test_id("skills-button").click()
    expect(page.get_by_test_id("skills-page")).to_be_visible(timeout=30_000)
    # The list settles once the first row appears (the page auto-selects it).
    expect(page.get_by_test_id("skill-row-review-diff")).to_be_visible(timeout=30_000)


def test_skills_nav_entry_opens_the_page(page: Page, seeded_session: tuple[str, str]) -> None:
    """The sidebar Skills entry routes to /skills and renders the catalog."""
    base_url, session_id = seeded_session
    _install_skills_routes(page)
    page.goto(f"{base_url}/c/{session_id}")

    skills_button = page.get_by_test_id("skills-button")
    expect(skills_button).to_be_visible(timeout=30_000)
    skills_button.click()

    expect(page).to_have_url(f"{base_url}/skills")
    expect(page.get_by_test_id("skills-page")).to_be_visible()
    expect(page.get_by_role("heading", name="Skills")).to_be_visible()


def test_skills_groups_and_auto_selected_detail(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The list is grouped by origin and the first skill's detail is shown."""
    base_url, session_id = seeded_session
    _open_skills(page, base_url, session_id)

    # The three user-facing origin groups render (no harness/vendor groups).
    expect(page.get_by_test_id("skills-group-built_in")).to_be_visible()
    expect(page.get_by_test_id("skills-group-workspace")).to_be_visible()
    expect(page.get_by_test_id("skills-group-personal")).to_be_visible()

    # The first skill is auto-selected and its detail heading is visible.
    detail = page.get_by_test_id("skill-detail")
    expect(detail).to_be_visible()
    expect(detail.get_by_role("heading", name="/review-diff")).to_be_visible()


def test_selecting_a_row_swaps_the_detail(page: Page, seeded_session: tuple[str, str]) -> None:
    """Clicking a different row swaps the persistent detail pane."""
    base_url, session_id = seeded_session
    _open_skills(page, base_url, session_id)

    page.get_by_test_id("skill-row-ship").click()
    detail = page.get_by_test_id("skill-detail")
    expect(detail).to_have_attribute("data-skill-id", "bundle:ship")
    expect(detail.get_by_role("heading", name="/ship")).to_be_visible()


def test_search_filters_the_list(page: Page, seeded_session: tuple[str, str]) -> None:
    """Typing in the search box filters the visible rows."""
    base_url, session_id = seeded_session
    _open_skills(page, base_url, session_id)

    page.get_by_test_id("skills-search").fill("release-notes")
    expect(page.get_by_test_id("skill-row-release-notes")).to_be_visible()
    expect(page.get_by_test_id("skill-row-review-diff")).to_have_count(0)


def test_include_other_tools_reveals_hidden_skills(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The trust switch reveals personal skills discovered from other tools."""
    base_url, session_id = seeded_session
    _open_skills(page, base_url, session_id)

    # A codex-discovered personal skill is hidden until the switch is on.
    expect(page.get_by_test_id("skill-row-migrate")).to_have_count(0)

    page.get_by_test_id("include-other-tools").click()

    expect(page.get_by_test_id("skill-row-migrate")).to_be_visible(timeout=15_000)


def test_advanced_details_holds_provenance(page: Page, seeded_session: tuple[str, str]) -> None:
    """Harness/vendor provenance lives only under the Advanced details section."""
    base_url, session_id = seeded_session
    _open_skills(page, base_url, session_id)

    # Select a personal skill discovered from Claude Code — its provider label
    # must NOT appear in the primary detail, only inside Advanced details.
    page.get_by_test_id("skill-row-frontend-design").click()
    detail = page.get_by_test_id("skill-detail")
    expect(detail).to_have_attribute("data-skill-id", "personal:claude:frontend-design")

    advanced = page.get_by_test_id("skill-advanced")
    expect(advanced).to_be_visible()
    # Expand it, then the provider label surfaces.
    advanced.get_by_text("Advanced details").click()
    expect(advanced.get_by_text("Claude Code")).to_be_visible()


def test_instructions_toggle_rendered_and_source(
    page: Page, seeded_session: tuple[str, str]
) -> None:
    """The instructions block toggles between rendered markdown and source."""
    base_url, session_id = seeded_session
    _open_skills(page, base_url, session_id)

    instructions = page.get_by_test_id("skill-instructions")
    expect(instructions).to_be_visible()

    # Rendered view: the SKILL.md heading renders as an <h1>, not literal "#".
    expect(instructions.get_by_role("heading", name="review-diff")).to_be_visible()

    # Source view: the raw markdown (frontmatter) becomes visible.
    page.get_by_role("button", name="Source", exact=True).click()
    expect(instructions.get_by_text("name: review-diff")).to_be_visible()
