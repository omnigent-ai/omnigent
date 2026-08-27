"""Browser e2e for the Clone session flow (ForkSessionDialog).

Drives the real chain the unit layer can't: per-message "Fork from
here" action → Radix dialog → ``POST /v1/sessions/{id}/fork`` → close +
navigate into the clone → the copied transcript renders from the fork's
snapshot. (The desktop header has no Clone button — the per-message
action is the desktop entry point; mobile keeps a three-dot menu entry.)

The seeded session is runner-bound with no workspace, so the dialog takes
the non-coding path (plain "Clone", no host/directory picker, no runner
launch). The host-bound "Clone & start" variant needs a connected
``omnigent host`` daemon this harness doesn't spawn, so the worktree test
below stubs the host-side wire (hosts list, filesystem pre-flight, runner
launch) at the network layer instead and drives the real dialog against
it; the remaining launch pieces are covered by the dialog unit tests
(background launch + error handoff to ``useForkLaunchStore``) and the
``ResumeWithDirectoryDialog`` ``initialError`` test.
"""

from __future__ import annotations

import json
import re
import time
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import configure_mock_llm, fetch_with_retry, seed_committed_turn

# Unique marker so the copied-transcript assertion can't match
# UI chrome or another test's message.
_MARKER = "kumquat-clone-marker"

# Marker for the cross-family fork test, distinct from _MARKER so the two
# tests' transcripts can't satisfy each other's assertions.
_XFAM_MARKER = "loquat-xfam-marker"


def test_clone_session_copies_transcript_and_navigates(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Clone a session from a message's Fork action and land in a fork with history.

    Failure modes this catches that the mocked dialog tests can't:

    - The dialog submits but the fork request 4xxs (client/server wire
      shape drift on ``SessionForkRequest`` — e.g. ``extra="forbid"``
      rejecting a new field).
    - The fork succeeds but navigation doesn't happen or lands on the
      SOURCE session (the dialog's close+navigate ordering broke).
    - The fork navigates but renders an empty chat (the server-side
      transcript deep-copy or the fork snapshot hydration broke).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    # Route this turn on the mock by marker so an exhausted queue left by
    # an earlier test in the shard cannot swallow the request.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key="clone-seed",
        match=_MARKER,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # Seed the transcript with a uniquely-marked user turn and wait for
    # the assistant reply so the fork has BOTH roles to copy.
    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    # Open the fork dialog from the assistant bubble's "Fork from here"
    # action (the desktop entry point; the action bar is dimmed until
    # hover but stays clickable). Forking from the LAST response is a
    # full clone, so this covers the same copy-everything path the old
    # header button drove. Non-coding source → the submit button reads
    # "Clone" (no host/directory section).
    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone")
    submit.click()

    # ONE call → dialog closes and the URL moves to a DIFFERENT /c/<id>.
    # A URL still on the source id means navigation never fired (or
    # landed back on the source); a visible dialog means the fork call
    # failed and surfaced an inline error instead.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # The clone's transcript carries the source's marked user turn —
    # rendered from the fork's own snapshot (the clone has no runner, so
    # nothing here can come from a live stream). An empty chat means the
    # deep-copy or fork hydration regressed.
    copied_user = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_MARKER
    )
    expect(copied_user.first).to_be_visible(timeout=30_000)


# Forking needs a real assistant bubble to anchor "Fork from here", so
# this sends a turn and waits on the reply. The in-process harness
# occasionally produces no assistant output on the first turn (the
# runner goes idle after dispatch — a nondeterministic harness
# scheduling stall, not a real-LLM artifact since this drives the mock
# LLM). Rerun on failure rather than widen the already-generous 60s
# wait, which a stalled turn would never satisfy.
@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_clone_dialog_offers_cross_family_native_target_and_forks(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """The fork dialog offers a CROSS-FAMILY native target and forks into it.

    The seeded source runs ``openai-agents`` (openai family); the packaged
    ``claude-native-ui`` built-in is an anthropic NATIVE harness. The picker
    used to hide cross-family native targets (``forkSwitchPreservesHistory``
    returned false), so this guards the new rule end-to-end against the real
    agent catalog: the server must report a classifiable harness for the
    built-in AND the dialog must offer it. It then submits the switch and
    asserts the fork is created with the carry-history label stamped and the
    source-session directive absent — the server-side gates that route the
    runner to the rebuild path.

    Failure modes this catches that the dialog unit tests can't:

    - The catalog stops reporting a harness for the native built-ins
      (``harness: null`` → the option vanishes from the picker).
    - The fork request with a cross-family ``agent_id`` 4xxs (wire drift).
    - The route/store label gating regresses (wrong labels on the fork).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    # Resolve the packaged claude-native-ui built-in from the live catalog.
    agents_resp = httpx.get(f"{base_url}/v1/agents", params={"limit": 100}, timeout=30.0)
    agents_resp.raise_for_status()
    claude_native = next(
        (a for a in agents_resp.json()["data"] if a["name"] == "claude-native-ui"), None
    )
    assert claude_native is not None, (
        "claude-native-ui built-in not registered on the test server — it is "
        "seeded unconditionally at startup, so its absence is a server bug"
    )

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key="clone-xfam-seed",
        match=_XFAM_MARKER,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # One marked turn so the fork has content and an assistant bubble to
    # anchor the "Fork from here" action.
    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_XFAM_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # Open the agent picker: the cross-family native target must be offered
    # (the regression: it used to be filtered out as non-history-preserving).
    page.get_by_test_id("fork-session-agent-select").click()
    option = page.get_by_test_id(f"fork-session-agent-option-{claude_native['id']}")
    expect(option).to_be_visible()
    option.click()
    page.get_by_test_id("fork-session-submit").click()

    # The fork succeeds and navigates to a NEW session id.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})[0-9a-f]{{32}}"),
        timeout=30_000,
    )
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # Server-side gating made observable: the fork must carry history into
    # the native target (label stamped) WITHOUT the source-session directive
    # (the SDK source has no native session; presence would mean the store
    # stamped a wrong-format resume pointer), and present as the TARGET
    # harness (terminal-first claude wrapper), not the source's chat mode.
    snap = httpx.get(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)
    snap.raise_for_status()
    labels: dict[str, str] = snap.json().get("labels") or {}
    assert labels.get("omnigent.fork.carry_history") == "1", (
        f"cross-family native fork must stamp carry-history, got {labels!r}"
    )
    assert "omnigent.fork.source_external_session_id" not in labels, (
        f"fork of an SDK source must not stamp a source native session id, got {labels!r}"
    )
    assert labels.get("omnigent.wrapper") == "claude-code-native-ui", (
        f"fork must present as the TARGET (claude-native) harness, got {labels!r}"
    )


# Marker for the worktree-source test, distinct from the other markers so
# the tests' transcripts can't satisfy each other's assertions.
_WT_MARKER = "papaya-worktree-marker"

# Fake host + worktree geometry injected at the network layer: the source
# session reads as bound to this host inside a server-created worktree
# (``<repo>-worktrees/<branch>``), which is what the dialog's repo +
# worktree prefill peels apart.
_WT_HOST_ID = "host_e2e_wt"
_WT_REPO = "/work/repo"
_WT_DIR = "/work/repo-worktrees/fix-1"
_WT_BRANCH = "fix-1"


@pytest.mark.flaky(reruns=2, reruns_delay=5)
def test_clone_worktree_source_prefills_repo_and_validates_directory(
    page: Page,
    seeded_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """Worktree-backed source: repo + worktree prefill, dir pre-flight, bind wire shape.

    The harness spawns no ``omnigent host`` daemon, so the host-side wire is
    stubbed at the network layer: the hosts list reports one online host, the
    source session reads as bound to it inside a worktree, the filesystem
    pre-flight 404s for a bogus path, and the runner launch records its body.
    The dialog, fork call, and navigation are all real.

    Covers the three behaviors the dialog unit tests can't prove end-to-end:

    - Prefill: the Working directory shows the ORIGINAL repo and the Git
      worktree field the source branch — not the worktree path as the
      directory with a blank branch.
    - Validation: a nonexistent directory blocks creation BEFORE the fork
      request fires (no zombie unbound clone), surfacing an inline error.
    - Untouched submit: the launch binds the source's existing worktree
      directory with NO git options (the branch already exists — sending it
      would make the host fail worktree creation).

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    runner_bodies: list[dict[str, Any]] = []
    fork_calls: list[str] = []

    def handle_hosts(route: Route) -> None:
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": _WT_HOST_ID,
                            "name": "e2e-wt-host",
                            "owner": "e2e",
                            "status": "online",
                            "configured_harnesses": {},
                        }
                    ]
                }
            ),
        )

    def handle_session_detail(route: Route) -> None:
        # Patch the REAL session payload so the source reads as a coding
        # session bound to the fake host inside a worktree. Non-GET
        # traffic (e.g. PATCH) passes through untouched.
        if route.request.method != "GET":
            route.continue_()
            return
        response = fetch_with_retry(route)
        body = response.json()
        body["host_id"] = _WT_HOST_ID
        body["workspace"] = _WT_DIR
        body["git_branch"] = _WT_BRANCH
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle_filesystem(route: Route) -> None:
        # The submit pre-flight (and the path field's completions): 404 the
        # bogus path, empty-but-listable everything else.
        if "/does/not/exist" in route.request.url:
            route.fulfill(
                status=404,
                content_type="application/json",
                body=json.dumps({"detail": "no such directory"}),
            )
        else:
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"object": "list", "data": [], "has_more": False}),
            )

    def handle_runners(route: Route) -> None:
        runner_bodies.append(route.request.post_data_json)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"runner_id": "runner_e2e_wt", "status": "launching"}),
        )

    def handle_fork(route: Route) -> None:
        # Count fork creations (the real server handles them) so the
        # validation assertion can prove nothing was created.
        fork_calls.append(route.request.url)
        route.continue_()

    page.route("**/v1/hosts", handle_hosts)
    # Regex, not glob: the chat store hydrates via the slim snapshot
    # (``?include_items=false&…``), and a glob without the query part
    # would silently miss it — the props feeding the dialog would then
    # read the UNPATCHED session and take the non-coding path.
    page.route(
        re.compile(rf".*/v1/sessions/{re.escape(session_id)}(\?.*)?$"),
        handle_session_detail,
    )
    page.route(f"**/v1/hosts/{_WT_HOST_ID}/filesystem/**", handle_filesystem)
    page.route(f"**/v1/hosts/{_WT_HOST_ID}/runners", handle_runners)
    page.route("**/v1/sessions/*/fork", handle_fork)

    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "OK"}],
        key="clone-worktree-seed",
        match=_WT_MARKER,
    )

    page.goto(f"{base_url}/c/{session_id}")

    # One marked turn so the fork has content and an assistant bubble to
    # anchor the "Fork from here" action.
    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_WT_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    # A coding source (workspace present + online host) → "Clone & start".
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone & start")

    # Prefill: original repo as the directory, source branch as the worktree
    # — not the worktree path with a blank branch.
    page.get_by_test_id("fork-session-advanced-toggle").click()
    expect(page.get_by_test_id("workspace-path-input")).to_have_value(_WT_REPO)
    expect(page.get_by_test_id("fork-session-branch-input")).to_have_value(_WT_BRANCH)

    # A nonexistent directory must refuse to create ANYTHING: inline error,
    # no fork call, no runner launch, no navigation. (No Escape to dismiss
    # the path field's dropdown — it never opens here, and Escape would
    # close the whole Radix dialog.)
    page.get_by_test_id("workspace-path-input").fill("/does/not/exist")
    submit.click()
    expect(page.get_by_test_id("fork-session-error")).to_contain_text("doesn't exist")
    assert fork_calls == [], "fork must not be created when the directory doesn't exist"
    assert runner_bodies == [], "runner must not launch when the directory doesn't exist"
    assert f"/c/{session_id}" in page.url

    # Restore the prefilled repo (branch still the source's) and submit: the
    # clone binds the source's EXISTING worktree directory with no git
    # options, then navigates into the fork.
    page.get_by_test_id("workspace-path-input").fill(_WT_REPO)
    submit.click()
    # Accept both session-id shapes ("conv_<hex>" and bare hex) — this
    # harness's seeded sessions and their forks navigate with bare ids.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})(conv_)?[0-9a-f]+"),
        timeout=30_000,
    )
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]

    deadline = time.monotonic() + 30.0
    while not runner_bodies and time.monotonic() < deadline:
        time.sleep(0.2)
    assert len(runner_bodies) == 1, "expected exactly one background runner launch"
    launch = runner_bodies[0]
    assert launch["session_id"] == fork_id, launch
    assert launch["workspace"] == _WT_DIR, launch
    assert "git" not in launch, (
        f"untouched worktree prefill must bind the existing worktree without git options: {launch}"
    )


def test_clone_submit_spins_while_the_fork_is_in_flight(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The Clone button shows a spinner (not just a fade) until the fork returns.

    A fork waits on the server round trip, so a button that only greys out
    reads as a hang and users re-click or close the dialog. Guards the
    in-flight affordance end-to-end: the real dialog, the real fetch, and
    the real ``Button`` loading overlay.

    Determinism comes from holding the fork response rather than racing it:
    the route handler parks the request without resolving it, so the
    in-flight state stays up for as long as the assertions need, then the
    captured route is released and the flow completes as usual. Catching a
    naturally-fast fork mid-flight would be a coin flip.

    The transcript is seeded straight into the store (the per-message "Fork
    from here" action needs a committed assistant response to anchor on),
    so this neither drives the LLM nor waits on a turn.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound session.
    """
    base_url, session_id = seeded_session

    seed_committed_turn(
        session_id,
        prompt="ping",
        reply="pong",
        response_id="resp_spinner",
    )

    parked: list[Route] = []

    # Returning without resolving parks the request in the browser and
    # leaves the test's thread free to assert. The route is released below.
    # Must be a plain function, not ``parked.append``: Playwright stamps an
    # attribute onto the handler it is given, which a builtin method rejects.
    def park_fork(route: Route) -> None:
        parked.append(route)

    page.route(re.compile(r"/v1/sessions/[^/]+/fork"), park_fork)

    page.goto(f"{base_url}/c/{session_id}")

    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=30_000)
    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    expect(page.get_by_test_id("fork-session-dialog")).to_be_visible()

    submit = page.get_by_test_id("fork-session-submit")
    # Before the click: idle — enabled, no spinner. Without this the
    # after-state below would also pass on a button that always spins.
    expect(submit).to_be_enabled()
    expect(submit).not_to_have_attribute("aria-busy", "true")
    assert submit.locator("svg.animate-spin").count() == 0

    submit.click()

    # In flight: spinner up, button busy and click-proof so the fork can't
    # be double-submitted.
    expect(submit.locator("svg.animate-spin")).to_be_visible(timeout=10_000)
    expect(submit).to_have_attribute("aria-busy", "true")
    expect(submit).to_be_disabled()

    # Releasing the fork completes the real flow — proving the spinner is a
    # transient in-flight state and not a stuck button.
    deadline = time.monotonic() + 10.0
    while not parked and time.monotonic() < deadline:
        time.sleep(0.05)
    assert parked, "fork request never reached the route handler"
    parked[0].continue_()

    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})(conv_)?[0-9a-f]+"),
        timeout=30_000,
    )
    expect(page.get_by_test_id("fork-session-dialog")).not_to_be_visible()
