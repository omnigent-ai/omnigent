"""E2E: effort levels present consistently across the model/effort picker.

Guards against effort levels being presented inconsistently across the
model / effort selector: presentation differing between the new-session and
existing-session composers, harness-dependent label casing, missing effort
sections, and Advanced settings duplicating the picker's own selectors.

Per-facet regression guards, each keyed to one claimed sub-symptom:

1. ``test_new_session_codex_picker_offers_effort_selection`` — the
   new-session picker shows an inline Effort section for Claude Code but
   none at all for Codex, even though Codex's catalog advertises per-model
   effort ladders (and the composer pill renders one).
2. ``test_effort_pill_casing_consistent_across_harnesses`` — the composer
   pill renders the same effort value as ``xHigh`` on a claude-native
   session but raw ``xhigh`` on a codex-native session. Direction-neutral:
   asserts equality, not a particular casing.
3. ``test_effort_presentation_consistent_between_composers`` — the
   new-session composer offers effort as an inline checkbox list while the
   existing-session composer nests it behind an ``Effort >`` submenu.
   Direction-neutral: asserts the presentation shape matches, not which
   shape wins.
4. ``test_advanced_settings_listed_once_in_session_picker`` — the
   existing-session picker lists ``Advanced settings…`` twice (bottom of
   the main panel and again inside the ``Edit >`` flyout).
5. ``test_advanced_settings_modal_does_not_duplicate_picker_controls`` —
   the Advanced settings dialog replicates the Model and Effort selectors
   that the picker itself already offers. Direction-neutral: asserts the
   two surfaces don't both offer the same two controls.

The new-session facets drive the real landing composer against stubbed
``/v1/hosts`` + ``/v1/agents`` + per-harness ``model-options`` responses
(the established pattern from ``test_hide_unconfigured_harnesses.py``); the
existing-session facets reshape a seeded session's snapshot into a
claude-/codex-native session via a ``page.route`` patch (the pattern from
``test_codex_model_metadata.py``).
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import fetch_with_retry


@pytest.fixture(autouse=True)
def _teardown_routes(page: Page):
    """Detach route handlers before the page closes.

    The stubs poll-refresh (host catalogs every 15s, session snapshots on
    rebind), so a request can be in flight inside a handler when the page
    fixture tears down; unrouting with ``ignoreErrors`` first keeps that
    race from surfacing as a teardown error after an intentional failure.
    """
    yield
    page.unroute_all(behavior="ignoreErrors")


# Stubbed host the landing composer auto-selects (the tunneled e2e runner
# registers no host). Keyed identically in the recent-workspaces seed.
_HOST_ID = "host_e2e"
_HOST_NAME = "e2e-host"

_CLAUDE_AGENT_ID = "ag_claude_e2e"
_CODEX_AGENT_ID = "ag_codex_e2e"

# Landing-picker catalog rows served from the stubbed per-harness
# ``model-options`` endpoint. The Codex rows advertise per-model effort
# ladders — exactly what a real ``model/list`` probe returns — so the
# picker has everything it needs to offer an Effort section.
_CODEX_MODEL_OPTIONS = [
    {
        "id": "gpt-5.6-sol",
        "model": "gpt-5.6-sol",
        "displayName": "GPT-5.6-Sol",
        "isDefault": True,
        "defaultReasoningEffort": "xhigh",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
            {"reasoningEffort": "xhigh"},
        ],
    },
    {
        "id": "gpt-5.5",
        "model": "gpt-5.5",
        "displayName": "GPT-5.5",
        "supportedReasoningEfforts": [
            {"reasoningEffort": "low"},
            {"reasoningEffort": "medium"},
            {"reasoningEffort": "high"},
        ],
    },
]

_CLAUDE_MODEL_OPTIONS = [
    {"id": "fable", "displayName": "Fable 5.1", "isDefault": True},
    {"id": "opus", "displayName": "Opus 5"},
]

# Effort levels any harness ladder can contain, as they render in menus
# (either casing), used to detect an "inline effort list" presentation
# without assuming which casing or item role a fix settles on.
_EFFORT_LEVEL_NAME = re.compile(r"^(low|medium|high|xhigh|max|default)$", re.IGNORECASE)


def _register_landing_stubs(page: Page) -> None:
    """Stub hosts/agents/catalogs so the landing picker renders both harnesses.

    :param page: Playwright page, before navigation.
    """

    def _hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _HOST_ID,
                            "name": _HOST_NAME,
                            "owner": "e2e",
                            "status": "online",
                            "configured_harnesses": {
                                "claude-native": True,
                                "codex-native": True,
                            },
                        }
                    ]
                }
            ),
        )

    def _agents(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "data": [
                        {
                            "id": _CLAUDE_AGENT_ID,
                            "name": "claude-native-ui",
                            "display_name": "Claude Code",
                            "description": "Anthropic's coding agent",
                            "harness": "claude-native",
                            "skills": [],
                        },
                        {
                            "id": _CODEX_AGENT_ID,
                            "name": "codex-native-ui",
                            "display_name": "Codex",
                            "description": "OpenAI's coding agent",
                            "harness": "codex-native",
                            "skills": [],
                        },
                    ]
                }
            ),
        )

    def _claude_models(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"models": _CLAUDE_MODEL_OPTIONS}),
        )

    def _codex_models(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"models": _CODEX_MODEL_OPTIONS}),
        )

    def _agent_scan(route: Route) -> None:
        # Neutralize agent discovery so sessions other tests left behind
        # can't leak into the picker and steal the selection.
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"data": []}))

    page.route("**/v1/hosts", _hosts)
    page.route("**/v1/agents", _agents)
    page.route(
        re.compile(r"/v1/hosts/[^/]+/harnesses/claude-native/model-options"),
        _claude_models,
    )
    page.route(
        re.compile(r"/v1/hosts/[^/]+/harnesses/codex-native/model-options"),
        _codex_models,
    )
    page.route(re.compile(r"/v1/sessions\?.*kind=any"), _agent_scan)


def _seed_recent_workspace(page: Page) -> None:
    """Pre-fill a recent working directory so the composer needs no host browse.

    :param page: Playwright page, before navigation.
    """
    page.add_init_script(
        f"""window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }})
        );"""
    )


def _open_landing_harness_config(page: Page, base_url: str, agent_id: str) -> None:
    """Open the landing picker and drill into *agent_id*'s config page.

    The config page is the integrated panel the report shows: Smart Routing,
    the ``Models`` list, and (for harnesses that get one) the inline
    ``Effort`` section.

    :param page: Playwright page with the landing stubs registered.
    :param base_url: Live server base URL.
    :param agent_id: Stubbed agent row to drill into.
    """
    page.goto(f"{base_url}/")
    page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    page.get_by_test_id("new-chat-landing-agent-select").click()
    row = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    expect(row).to_be_visible(timeout=30_000)
    row.hover()
    page.get_by_test_id(f"new-chat-landing-agent-config-{agent_id}").click()
    expect(page.get_by_test_id("new-chat-landing-agent-models")).to_be_visible(timeout=30_000)


def _patch_session_as_native(
    page: Page,
    session_id: str,
    *,
    wrapper: str,
    harness: str,
    llm_model: str,
    reasoning_effort: str,
    model_options: list[dict],
):
    """Reshape the browser's session snapshot into a native-harness session.

    The server fixture seeds a normal ``hello_world`` session so the page
    boots against the real app/server; this route patch changes only the
    ``GET /v1/sessions/{session_id}`` response the browser sees, simulating
    the snapshot a native runner reports (wrapper label, harness, bound
    model, reasoning effort, and catalog rows).

    :param page: Playwright page, before navigation.
    :param session_id: The seeded session's id.
    :param wrapper: ``omnigent.wrapper`` label, e.g. ``claude-code-native-ui``.
    :param harness: Session harness value, e.g. ``claude``.
    :param llm_model: Bound model id reported for the session.
    :param reasoning_effort: Session reasoning-effort value.
    :param model_options: Catalog rows the pickers should render.
    :returns: The live config dict the handler reads per request — mutate it
        (and re-navigate) to reshape subsequent responses. Mutating beats
        unrouting and re-routing mid-test, which races a poll refresh still
        in flight and trips "Route is already handled".
    """
    config = {
        "wrapper": wrapper,
        "harness": harness,
        "llm_model": llm_model,
        "reasoning_effort": reasoning_effort,
        "model_options": model_options,
    }

    def _handle(route: Route) -> None:
        request = route.request
        if urlparse(request.url).path != f"/v1/sessions/{session_id}" or request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        payload = response.json()
        payload["labels"] = {**payload.get("labels", {}), "omnigent.wrapper": config["wrapper"]}
        payload["harness"] = config["harness"]
        payload["llm_model"] = config["llm_model"]
        payload["reasoning_effort"] = config["reasoning_effort"]
        payload["model_options"] = config["model_options"]
        route.fulfill(
            status=200,
            headers={**response.headers, "content-type": "application/json"},
            body=json.dumps(payload),
        )

    page.route("**/v1/sessions/**", _handle)
    return config


def _open_session_harness_flyout(page: Page, base_url: str, session_id: str) -> None:
    """Open the session composer's picker and its harness ``Edit >`` flyout.

    :param page: Playwright page with the session snapshot patch registered.
    :param base_url: Live server base URL.
    :param session_id: Session to open.
    """
    page.goto(f"{base_url}/c/{session_id}")
    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=30_000)
    gear.click()
    expect(page.get_by_test_id("composer-agent-menu")).to_be_visible()
    page.get_by_test_id("composer-agent-edit").click()
    expect(page.get_by_test_id("composer-agent-config-menu")).to_be_visible()


def _effort_presentation(page: Page) -> tuple[bool, bool]:
    """Classify how the currently open picker panel presents effort.

    Direction-neutral probe shared by both composers: does the open panel
    show effort *levels* inline (any menu item whose accessible name is an
    effort level), and does it show an ``Effort`` submenu trigger? A
    consistent design answers the same way in both composers, whichever
    presentation it standardizes on.

    :param page: Playwright page with a picker panel open.
    :returns: ``(inline_levels_visible, effort_submenu_trigger_visible)``.
    """
    items = page.get_by_role("menuitem").or_(page.get_by_role("menuitemcheckbox"))
    inline = False
    for i in range(items.count()):
        name = (items.nth(i).text_content() or "").strip()
        if _EFFORT_LEVEL_NAME.match(name):
            inline = True
            break
    submenu = page.get_by_role("menuitem", name=re.compile(r"^Effort$")).count() > 0
    return inline, submenu


def test_new_session_codex_picker_offers_effort_selection(
    page: Page,
    live_server: str,
) -> None:
    """The new-session picker must offer Effort for Codex as it does for Claude.

    Journey (report steps 1–2): open the new-session composer → open the
    model pill → drill into the Claude Code harness (inline ``Effort``
    section renders under ``Models``) → back → drill into the Codex harness.
    Codex's catalog advertises per-model effort ladders and the composer
    pill renders an effort, yet the panel offers no Effort section at all.
    """
    _register_landing_stubs(page)
    _seed_recent_workspace(page)

    # Claude Code: the inline Effort section renders under the Models list.
    _open_landing_harness_config(page, live_server, _CLAUDE_AGENT_ID)
    expect(page.get_by_test_id("new-chat-landing-agent-efforts")).to_be_visible()
    page.keyboard.press("Escape")

    # Codex: same drill-in. Its stubbed catalog carries effort ladders for
    # every model, so the picker has all the data an Effort section needs.
    _open_landing_harness_config(page, live_server, _CODEX_AGENT_ID)
    expect(page.get_by_test_id(f"new-chat-landing-agent-model-{'gpt-5.6-sol'}")).to_be_visible()

    # BUG: no Effort section renders for Codex.
    expect(
        page.get_by_test_id("new-chat-landing-agent-efforts"),
        "Codex harness config page offers no Effort section even though its "
        "catalog advertises per-model effort ladders",
    ).to_be_visible()


def test_effort_pill_casing_consistent_across_harnesses(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The composer pill must render one effort value with one casing.

    The same session-level effort (``xhigh``) is rendered by the composer
    pill as ``xHigh`` when the session is claude-native but raw ``xhigh``
    when it is codex-native. Direction-neutral: any single casing passes.
    """
    base_url, session_id = seeded_session

    config = _patch_session_as_native(
        page,
        session_id,
        wrapper="claude-code-native-ui",
        harness="claude",
        llm_model="fable",
        reasoning_effort="xhigh",
        model_options=_CLAUDE_MODEL_OPTIONS,
    )
    page.goto(f"{base_url}/c/{session_id}")
    pill = page.get_by_test_id("composer-agent-effort-value")
    expect(pill).to_be_visible(timeout=30_000)
    claude_label = (pill.text_content() or "").strip()

    # Reshape the same snapshot into a codex-native session and reload.
    config.update(
        wrapper="codex-native-ui",
        harness="codex",
        llm_model="gpt-5.6-sol",
        model_options=_CODEX_MODEL_OPTIONS,
    )
    page.goto(f"{base_url}/c/{session_id}")
    expect(pill).to_be_visible(timeout=30_000)
    codex_label = (pill.text_content() or "").strip()

    # BUG: "xHigh" (claude) vs "xhigh" (codex).
    assert claude_label == codex_label, (
        f"composer pill renders the same effort value with different casing "
        f"per harness: claude-native shows {claude_label!r}, codex-native "
        f"shows {codex_label!r}"
    )


def test_effort_presentation_consistent_between_composers(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Both composers must present effort selection the same way.

    Journey (report steps 1 + 3): the new-session picker presents effort as
    an inline checkbox list under ``Models``; the existing-session picker
    presents it as a nested ``Effort >`` submenu inside the harness row's
    ``Edit >`` flyout. Direction-neutral: either shape passes, as long as
    the two composers agree.
    """
    base_url, session_id = seeded_session
    _register_landing_stubs(page)
    _seed_recent_workspace(page)

    _open_landing_harness_config(page, base_url, _CLAUDE_AGENT_ID)
    new_session_shape = _effort_presentation(page)
    page.keyboard.press("Escape")
    page.keyboard.press("Escape")

    _patch_session_as_native(
        page,
        session_id,
        wrapper="claude-code-native-ui",
        harness="claude",
        llm_model="fable",
        reasoning_effort="max",
        model_options=_CLAUDE_MODEL_OPTIONS,
    )
    _open_session_harness_flyout(page, base_url, session_id)
    existing_session_shape = _effort_presentation(page)

    # BUG: inline list (new-session) vs nested submenu
    # (existing-session) for the same harness.
    assert new_session_shape == existing_session_shape, (
        f"effort presentation differs between composers for the same harness: "
        f"new-session (inline, submenu)={new_session_shape}, "
        f"existing-session (inline, submenu)={existing_session_shape}"
    )


def test_advanced_settings_listed_once_in_session_picker(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The session picker must reach Advanced settings from exactly one entry.

    Journey (report step 3): open an existing claude-native session's model
    pill → open the harness row's ``Edit >`` flyout. ``Advanced settings…``
    is listed twice — at the bottom of the main panel and again inside the
    flyout.
    """
    base_url, session_id = seeded_session
    _patch_session_as_native(
        page,
        session_id,
        wrapper="claude-code-native-ui",
        harness="claude",
        llm_model="fable",
        reasoning_effort="max",
        model_options=_CLAUDE_MODEL_OPTIONS,
    )
    _open_session_harness_flyout(page, base_url, session_id)

    entries = page.get_by_role("menuitem", name=re.compile(r"^Advanced settings"))
    # BUG: two entries render (main panel + Edit flyout).
    expect(
        entries,
        "the picker lists 'Advanced settings…' more than once (main panel "
        "bottom + inside the Edit flyout)",
    ).to_have_count(1)


def test_advanced_settings_modal_does_not_duplicate_picker_controls(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Model/effort selectors must not be offered by picker AND modal alike.

    Journey (report step 4): the picker's ``Edit >`` flyout already offers
    ``Model >`` and ``Effort >``; opening ``Advanced settings…`` then shows
    a dialog with its own Model and Effort selectors — the same two controls
    a third time. Direction-neutral: a fix may drop either surface's copy
    (or make the picker defer to the modal); both offering both fails.
    """
    base_url, session_id = seeded_session
    _patch_session_as_native(
        page,
        session_id,
        wrapper="claude-code-native-ui",
        harness="claude",
        llm_model="fable",
        reasoning_effort="max",
        model_options=_CLAUDE_MODEL_OPTIONS,
    )
    _open_session_harness_flyout(page, base_url, session_id)

    picker_offers_model = page.get_by_role("menuitem", name=re.compile(r"^Model$")).count() > 0
    inline_levels, effort_submenu = _effort_presentation(page)
    picker_offers_effort = inline_levels or effort_submenu

    page.get_by_test_id("composer-advanced-settings").click()
    modal = page.get_by_test_id("composer-config-modal")
    expect(modal).to_be_visible()
    modal_offers_model = page.get_by_test_id("composer-config-model").count() > 0
    modal_offers_effort = page.get_by_test_id("composer-config-effort").count() > 0

    # BUG: both surfaces offer both selectors.
    assert not (
        picker_offers_model and picker_offers_effort and modal_offers_model and modal_offers_effort
    ), (
        "the Advanced settings dialog duplicates the Model and Effort "
        "selectors the picker already offers "
        f"(picker: model={picker_offers_model}, effort={picker_offers_effort}; "
        f"modal: model={modal_offers_model}, effort={modal_offers_effort})"
    )
