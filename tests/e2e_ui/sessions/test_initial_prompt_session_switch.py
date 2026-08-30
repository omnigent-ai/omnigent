"""E2E: a new chat's initial prompt is delivered once to its own session.

Guards the initial-prompt cross-session leak (sibling of the queued-
message regression in ``test_cross_session_routing``, but a different
code path):

    The user opens the home composer ("New session"), types an initial
    prompt for a brand-new session A, and creates it. The prompt
    auto-sends to A (its runner is online), but the held prompt state is
    NOT cleared while the user stays on A. The user then clicks an
    already-running session B in the sidebar. The held prompt MUST stay
    bound to A — it must never be POSTed into B (which would deliver a
    duplicate of A's prompt to B).

Root cause it catches (an effect-ordering race in ``ChatPage``): the
consume effect (``setInitialPrompt(...)``) and the auto-send effect both
key off ``urlConvId``. The prompt auto-sends to A once A's runner is
online, but the held prompt state is NOT cleared while the user stays on
A. So on the A→B switch the auto-send effect re-runs in the same commit
with the STALE prompt (consumed for A) while ``urlConvId`` is already B
and B's runner reads online. ``send()`` then pins the live store id —
already B — and a DUPLICATE of A's prompt floods B. (The same race also
leaks when A's runner is still offline at switch time; this test drives
the runner-online variant because it needs no health stubbing.) The fix
binds the prompt to the conversation it was consumed for and gates the
auto-send on a match (``shouldSendInitialPrompt``'s ``promptConversationId``).

The retry journeys below additionally cover a transient runner-unavailable
503 across a ChatPage unmount/remount and a terminal 500 response. The
transient case lets the successful retry continue to the real server and
runner, then checks the canonical transcript for one committed prompt.

The composer needs a host + agent catalog the headless harness can't
produce (its runner is directly tunneled, no host daemon), and the
create POST would really launch a runner. So the host list, the agent
catalog, and the create POST are stubbed via ``page.route`` — the REAL
composer still performs the REAL ``setPendingInitialPrompt`` + ``navigate``
handoff into a REAL, pre-seeded session A. ``/events`` is intercepted
(like the sibling test) so no real turn runs and the assertion is purely
on where each POST is addressed. The async-in-a-fresh-thread shape and
the sidebar (client-side) navigation are inherited from
``test_cross_session_routing`` for the same reasons documented there.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import threading
from collections.abc import Coroutine
from pathlib import Path
from typing import Any

import httpx
from playwright.async_api import Route, async_playwright, expect

# Unique sentinels so each POST body is unambiguously identifiable.
_PROMPT = "sentinel-initprompt-7b3e initial prompt bound to session A"
_FOLLOWUP = "sentinel-followup-2d9a live send into session B"
_RETRY_PROMPT = "sentinel-retry-41ca survives a remount"
_RETRY_FOLLOWUP = "sentinel-order-67bf follows the initial prompt"
_FAILED_PROMPT = "sentinel-failed-904e restore this draft"
_AMBIGUOUS_PROMPT = "sentinel-accepted-e772 must not be replayed"
_SLASH_PROMPT = "/code-review 4294"

_EVENTS_RE = re.compile(r"/v1/sessions/([^/]+)/events$")
# Bare create endpoint: ``/v1/sessions`` with an optional query, but NOT
# ``/v1/sessions/{id}/...`` — so the GET list and per-session reads pass
# through to the real server while only the POST create is faked.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")


def _run_in_fresh_loop(coro: Coroutine[Any, Any, None]) -> None:
    """Run *coro* to completion in a dedicated thread with its own event loop.

    The e2e_ui suite runs many pytest-playwright **sync** tests in the same
    session; once one has run, pytest-asyncio can't start a loop on the main
    thread. Running the coroutine from a fresh thread via :func:`asyncio.run`
    sidesteps that. Any exception (including assertion failures) is captured
    and re-raised on the calling thread so the test fails normally.

    :param coro: The coroutine to run to completion.
    :raises BaseException: Whatever the coroutine raised, re-raised here.
    """
    captured: dict[str, BaseException] = {}

    def _worker() -> None:
        try:
            asyncio.run(coro)
        except BaseException as exc:
            captured["error"] = exc

    thread = threading.Thread(target=_worker)
    thread.start()
    thread.join()
    if "error" in captured:
        raise captured["error"]


async def _wait_until(predicate, *, timeout_s: float = 15.0) -> None:
    """Poll ``predicate`` on the event loop until true or timeout.

    :param predicate: Zero-arg callable returning truthy when satisfied.
    :param timeout_s: Max seconds to wait before failing the test.
    :raises AssertionError: If the predicate never becomes truthy.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.05)
    raise AssertionError(f"condition not met within {timeout_s:.0f}s")


def _event_text(body: dict[str, Any]) -> str:
    """Extract visible text from a message or slash-command event body."""
    if body.get("type") == "slash_command":
        data = body["data"]
        args = data.get("arguments", "")
        return f"/{data['name']}{f' {args}' if args else ''}"
    return "".join(
        block.get("text", "")
        for block in body.get("data", {}).get("content", [])
        if block.get("type") == "input_text"
    )


async def _register_landing_routes(
    page,
    *,
    created_session_id: str,
    handle_events,
    skills: list[dict[str, str]] | None = None,
) -> None:
    """Install only the host/catalog/create stubs the landing composer needs."""

    async def handle_hosts(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "hosts": [
                        {
                            "host_id": "host_e2e",
                            "name": "e2e-host",
                            "owner": "e2e",
                            "status": "online",
                        }
                    ]
                }
            ),
        )

    async def handle_agents(route: Route) -> None:
        await route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "data": [
                        {
                            "id": "ag_e2e",
                            "name": "hello_world",
                            "display_name": "Hello World",
                            "description": None,
                            "harness": None,
                            "skills": skills or [],
                        }
                    ]
                }
            ),
        )

    async def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            await route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": created_session_id}),
            )
        else:
            await route.continue_()

    await page.route("**/v1/sessions/*/events", handle_events)
    await page.route("**/v1/hosts", handle_hosts)
    await page.route("**/v1/agents", handle_agents)
    await page.route(_SESSIONS_RE, handle_sessions)


async def _open_landing_and_send(page, base_url: str, prompt: str) -> None:
    """Open the real landing composer with a usable stubbed host and submit."""
    await page.add_init_script(
        """window.localStorage.setItem(
            "omnigent:recent-workspaces",
            JSON.stringify({ host_e2e: ["/tmp"] }),
        )"""
    )
    await page.goto(f"{base_url}/")
    composer = page.get_by_test_id("new-chat-landing-input")
    await composer.wait_for(state="visible", timeout=30_000)
    await composer.fill(prompt)
    await page.get_by_test_id("new-chat-landing-submit").click()


async def _canonical_user_texts(base_url: str, session_id: str) -> list[str]:
    """Read committed user messages from the real server in chronological order."""
    async with httpx.AsyncClient(base_url=base_url, timeout=10.0) as client:
        response = await client.get(
            f"/v1/sessions/{session_id}/items",
            params={"limit": 100, "order": "asc"},
        )
    response.raise_for_status()
    texts: list[str] = []
    for item in response.json().get("data", []):
        if item.get("role") != "user":
            continue
        text = "".join(
            block.get("text", "")
            for block in item.get("content", [])
            if block.get("type") == "input_text"
        )
        if text:
            texts.append(text)
    return texts


async def _wait_for_canonical_user_text(
    base_url: str,
    session_id: str,
    expected: str,
    *,
    timeout_s: float = 30.0,
) -> None:
    """Poll the real transcript until one expected user message commits."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while loop.time() < deadline:
        if expected in await _canonical_user_texts(base_url, session_id):
            return
        await asyncio.sleep(0.1)
    raise AssertionError(f"{expected!r} did not reach the canonical transcript")


async def _save_optional_screenshot(page, name: str) -> None:
    """Save local verification evidence when E2E_SCREENSHOT_DIR is set."""
    screenshot_dir = os.environ.get("E2E_SCREENSHOT_DIR")
    if not screenshot_dir:
        return
    path = Path(screenshot_dir)
    path.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=str(path / f"{name}.png"), full_page=True)


def test_initial_prompt_stays_bound_to_origin_session_after_switch(
    seeded_session_pair: tuple[str, str, str],
) -> None:
    """A held initial prompt for A must not leak into B after switching.

    Failure mode this catches: the prompt typed for the new session A is
    POSTed to session B (the now-active session the user switched to)
    because the auto-send effect fired with a stale prompt during the
    A→B switch commit.
    """
    base_url, session_a, session_b = seeded_session_pair
    _run_in_fresh_loop(_drive_initial_prompt_switch(base_url, session_a, session_b))


async def _drive_initial_prompt_switch(base_url: str, session_a: str, session_b: str) -> None:
    """Async body of the initial-prompt leak test. See the module docstring.

    :param base_url: Spawned server base URL.
    :param session_a: The pre-seeded session the composer "creates"; the
        initial prompt is composed for and correctly sent to it.
    :param session_b: The already-running session the user switches to,
        into which the held prompt must never leak.
    """
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            # Every (session_id, text) POSTed to a /events endpoint.
            event_posts: list[tuple[str, str]] = []

            async def handle_events(route: Route) -> None:
                request = route.request
                match = _EVENTS_RE.search(request.url)
                assert match is not None, f"unexpected /events url: {request.url}"
                body = request.post_data_json
                text = body["data"]["content"][0]["text"]
                event_posts.append((match.group(1), text))
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_e2e"}),
                )

            async def handle_hosts(route: Route) -> None:
                # One online host so the composer can pick a host
                # (the directly-tunneled harness registers no host).
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": "host_e2e",
                                    "name": "e2e-host",
                                    "owner": "e2e",
                                    "status": "online",
                                }
                            ]
                        }
                    ),
                )

            async def handle_agents(route: Route) -> None:
                # The composer's available-agent catalog (GET /v1/agents).
                # The app's own agent list uses GET /v1/sessions, so this
                # route only feeds the composer's auto-selected first agent.
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "data": [
                                {
                                    "id": "ag_e2e",
                                    "name": "hello_world",
                                    "display_name": "Hello World",
                                    "description": None,
                                    "harness": None,
                                }
                            ]
                        }
                    ),
                )

            async def handle_sessions(route: Route) -> None:
                # Fake ONLY the composer's create POST, returning the
                # pre-seeded session A's id so the real handoff
                # (setPendingInitialPrompt + navigate) targets a real
                # session. Everything else (the GET conversation list,
                # per-session reads) goes to the real server.
                if route.request.method == "POST":
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_a}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/sessions/*/events", handle_events)
            await page.route("**/v1/hosts", handle_hosts)
            await page.route("**/v1/agents", handle_agents)
            await page.route(_SESSIONS_RE, handle_sessions)

            # Start in the already-running session B (full reload is fine
            # here — the race only matters once we navigate client-side).
            await page.goto(f"{base_url}/c/{session_b}")

            # Seed a recent working directory for the stubbed host so the home
            # composer auto-fills the working-directory chip. The file browser
            # has its own tests; this test only needs a valid workspace so the
            # Send button enables, and seeding avoids depending on the picker's
            # (stubbed, host-less) filesystem listing. Keyed by the host id the
            # composer auto-selects from the stubbed /v1/hosts.
            await page.evaluate(
                """() => localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({ host_e2e: ["/tmp"] }),
                )"""
            )

            # "New session" now routes to the home composer (the modal is
            # retired). Compose the new session A there: the host + first agent
            # auto-select and the working directory auto-fills from the seeded
            # recent, so only the prompt needs typing.
            await page.get_by_test_id("new-chat-button").click()
            await page.get_by_test_id("new-chat-landing-input").wait_for(
                state="visible", timeout=15_000
            )
            # The composer textarea doubles as the new session's initial prompt.
            await page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
            # Playwright auto-waits for Send to be actionable (enabled) before
            # clicking — it enables only once a message + host + agent + a valid
            # workspace are all set, so this also confirms the form is complete.
            await page.get_by_test_id("new-chat-landing-submit").click()

            # The create handoff navigates to A, whose runner is online, so
            # the prompt correctly auto-sends to A. Wait for that POST: it
            # confirms the real consume + auto-send path ran (and leaves the
            # held prompt state uncleared, which is what the switch re-fires).
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_a)}"))
            await _wait_until(lambda: any(text == _PROMPT for _, text in event_posts))

            # Switch to the running session B via the sidebar link — a
            # client-side navigation that preserves the JS module state.
            await page.locator(f'a[href="/c/{session_b}"]').click()
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_b)}"))

            # Drive a real follow-up send into B. It MUST land in B, and it
            # acts as a barrier: once it is observed the A→B switch commit
            # (where the leak would fire) has fully completed, so any leak
            # of the held prompt into B is already recorded by now.
            composer = page.get_by_label("Message the agent")
            await composer.fill(_FOLLOWUP)
            await page.get_by_role("button", name="Send", exact=True).click()
            await _wait_until(lambda: any(text == _FOLLOWUP for _, text in event_posts))

            followup_targets = [sid for sid, text in event_posts if text == _FOLLOWUP]
            assert followup_targets == [session_b], (
                f"the live follow-up must post to B ({session_b}); targets were {followup_targets}"
            )
            prompt_targets = [sid for sid, text in event_posts if text == _PROMPT]
            # Sanity: the prompt did reach its origin A — proves the real
            # consume + auto-send path ran (so a clean run isn't a no-op).
            assert session_a in prompt_targets, (
                f"the initial prompt never reached its origin session A ({session_a}); "
                f"targets were {prompt_targets} — the test did not exercise auto-send"
            )
            # The core assertion: the prompt composed for A must never have
            # been POSTed into B, the session the user switched to.
            assert session_b not in prompt_targets, (
                f"the initial prompt composed for session A ({session_a}) leaked into "
                f"session B ({session_b}): POST targets were {prompt_targets}. A target "
                f"of B is the cross-session initial-prompt leak."
            )
        finally:
            await browser.close()


def test_initial_prompt_retries_across_chat_page_remount_exactly_once(
    seeded_session: tuple[str, str],
) -> None:
    """A runner 503 plus a ChatPage remount still commits one initial prompt."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_retry_across_remount(base_url, session_id))


async def _drive_retry_across_remount(base_url: str, session_id: str) -> None:
    """Drive a real landing→chat→settings→chat retry journey."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            event_posts: list[dict[str, Any]] = []

            async def handle_events(route: Route) -> None:
                body = route.request.post_data_json
                if _event_text(body) not in {_RETRY_PROMPT, _RETRY_FOLLOWUP}:
                    await route.continue_()
                    return
                event_posts.append(body)
                if (
                    _event_text(body) == _RETRY_PROMPT
                    and len([post for post in event_posts if _event_text(post) == _RETRY_PROMPT])
                    == 1
                ):
                    await route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "runner_unavailable",
                                    "message": "No runner bound for session",
                                }
                            }
                        ),
                    )
                    return
                await route.continue_()

            await _register_landing_routes(
                page,
                created_session_id=session_id,
                handle_events=handle_events,
            )
            await _open_landing_and_send(page, base_url, _RETRY_PROMPT)
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_id)}"))
            await _wait_until(
                lambda: (
                    len([post for post in event_posts if _event_text(post) == _RETRY_PROMPT]) == 1
                )
            )

            # A follow-up must not overtake the retry, and a transient failure
            # must not leak a second copy of the prompt back into the composer.
            composer = page.get_by_label("Message the agent")
            await expect(composer).to_be_disabled()
            await expect(composer).to_have_value("")

            # Unmount ChatPage through client-side routing, then return before
            # the one-second retry delay elapses. Two independent loops would
            # both POST after this remount.
            await page.evaluate(
                """() => {
                    history.pushState({}, "", "/settings/appearance");
                    window.dispatchEvent(new PopStateEvent("popstate"));
                }"""
            )
            await expect(composer).not_to_be_attached()
            await page.evaluate(
                f"""() => {{
                    history.pushState({{}}, "", "/c/{session_id}");
                    window.dispatchEvent(new PopStateEvent("popstate"));
                }}"""
            )
            composer = page.get_by_label("Message the agent")
            await composer.wait_for(state="visible", timeout=30_000)

            await expect(page.get_by_text("Mock LLM response.", exact=True).first).to_be_visible(
                timeout=30_000
            )
            # Let the original loop's retry timer fire; it must observe that
            # the shared delivery finished rather than POSTing a duplicate.
            await asyncio.sleep(1.5)
            prompt_posts = [post for post in event_posts if _event_text(post) == _RETRY_PROMPT]
            assert len(prompt_posts) == 2, (
                "expected one runner-unavailable attempt and one accepted retry; "
                f"saw {len(prompt_posts)} attempts"
            )
            await expect(composer).to_be_enabled()
            await expect(composer).to_have_value("")

            await composer.fill(_RETRY_FOLLOWUP)
            await page.get_by_role("button", name="Send", exact=True).click()
            await _wait_until(
                lambda: any(_event_text(post) == _RETRY_FOLLOWUP for post in event_posts)
            )
            await _wait_for_canonical_user_text(base_url, session_id, _RETRY_FOLLOWUP)

            user_texts = await _canonical_user_texts(base_url, session_id)
            sentinels = [text for text in user_texts if text in {_RETRY_PROMPT, _RETRY_FOLLOWUP}]
            assert sentinels == [_RETRY_PROMPT, _RETRY_FOLLOWUP], sentinels
            await _save_optional_screenshot(page, "initial-prompt-remount-retry")
        finally:
            await browser.close()


def test_initial_prompt_terminal_failure_restores_draft_without_retry(
    seeded_session: tuple[str, str],
) -> None:
    """A non-runner failure is not replayed and restores the original draft."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_terminal_failure(base_url, session_id))


async def _drive_terminal_failure(base_url: str, session_id: str) -> None:
    """Drive a definitive 500 response through the real landing/chat UI."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            event_posts: list[dict[str, Any]] = []

            async def handle_events(route: Route) -> None:
                body = route.request.post_data_json
                event_posts.append(body)
                if len(event_posts) == 1:
                    await route.fulfill(
                        status=500,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "internal_error",
                                    "message": "definite initial-prompt failure",
                                }
                            }
                        ),
                    )
                    return
                # An unsafe retry would turn the failure into a false success,
                # making both the attempt count and restored-error assertions fail.
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_unexpected_retry"}),
                )

            await _register_landing_routes(
                page,
                created_session_id=session_id,
                handle_events=handle_events,
            )
            await _open_landing_and_send(page, base_url, _FAILED_PROMPT)
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_id)}"))

            composer = page.get_by_label("Message the agent")
            await expect(composer).to_have_value(_FAILED_PROMPT, timeout=5_000)
            await page.get_by_role("button", name="Something went wrong").click()
            await expect(
                page.get_by_text("definite initial-prompt failure", exact=True)
            ).to_be_visible()
            await asyncio.sleep(1.2)
            assert len(event_posts) == 1, (
                "a non-runner failure may be ambiguous after transport/proxy errors; "
                f"it must not be retried, but saw {len(event_posts)} POSTs"
            )
            await _save_optional_screenshot(page, "initial-prompt-final-failure")
        finally:
            await browser.close()


def test_initial_prompt_does_not_retry_after_server_persistence(
    seeded_session: tuple[str, str],
) -> None:
    """A runner-unavailable response after persistence is never replayed."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_persisted_runner_failure(base_url, session_id))


async def _drive_persisted_runner_failure(base_url: str, session_id: str) -> None:
    """Hide an accepted response behind the server's post-persistence 503."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            event_posts: list[dict[str, Any]] = []

            async def handle_events(route: Route) -> None:
                body = route.request.post_data_json
                if _event_text(body) != _AMBIGUOUS_PROMPT:
                    await route.continue_()
                    return
                event_posts.append(body)
                if len(event_posts) == 1:
                    accepted = await route.fetch()
                    assert accepted.ok
                    await route.fulfill(
                        response=accepted,
                        status=503,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "runner_unavailable",
                                    "message": (
                                        "Runner is unreachable; message was persisted "
                                        "but could not be delivered."
                                    ),
                                }
                            }
                        ),
                    )
                    return
                # Expose an unsafe automatic retry by letting it reach the server.
                await route.continue_()

            await _register_landing_routes(
                page,
                created_session_id=session_id,
                handle_events=handle_events,
            )
            await _open_landing_and_send(page, base_url, _AMBIGUOUS_PROMPT)
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_id)}"))
            await _wait_for_canonical_user_text(base_url, session_id, _AMBIGUOUS_PROMPT)
            await asyncio.sleep(1.5)

            assert len(event_posts) == 1, (
                "runner_unavailable is also emitted after persistence; replaying it "
                f"duplicates the event, but saw {len(event_posts)} POSTs"
            )
            user_texts = await _canonical_user_texts(base_url, session_id)
            assert user_texts.count(_AMBIGUOUS_PROMPT) == 1, user_texts
            composer = page.get_by_label("Message the agent")
            await expect(composer).to_have_value("")
            await _save_optional_screenshot(page, "initial-prompt-accepted-no-retry")
        finally:
            await browser.close()


def test_initial_prompt_slash_command_retries_only_runner_unavailable(
    seeded_session: tuple[str, str],
) -> None:
    """A matched landing skill keeps its slash-command wire shape across retry."""
    base_url, session_id = seeded_session
    _run_in_fresh_loop(_drive_slash_command_retry(base_url, session_id))


async def _drive_slash_command_retry(base_url: str, session_id: str) -> None:
    """Drive a matched skill through one runner-unavailable response."""
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        page = await browser.new_page()
        try:
            event_posts: list[dict[str, Any]] = []

            async def handle_events(route: Route) -> None:
                event_posts.append(route.request.post_data_json)
                if len(event_posts) == 1:
                    await route.fulfill(
                        status=503,
                        content_type="application/json",
                        body=json.dumps(
                            {
                                "error": {
                                    "code": "runner_unavailable",
                                    "message": "No runner bound for session",
                                }
                            }
                        ),
                    )
                    return
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps({"queued": True, "item_id": "ci_slash_retry"}),
                )

            await _register_landing_routes(
                page,
                created_session_id=session_id,
                handle_events=handle_events,
                skills=[{"name": "code-review", "description": "Review a pull request"}],
            )
            await _open_landing_and_send(page, base_url, _SLASH_PROMPT)
            await page.wait_for_url(re.compile(rf"/c/{re.escape(session_id)}"))
            await _wait_until(lambda: len(event_posts) == 2)

            assert [post["type"] for post in event_posts] == [
                "slash_command",
                "slash_command",
            ]
            assert [post["data"]["name"] for post in event_posts] == [
                "code-review",
                "code-review",
            ]
            assert [post["data"]["arguments"] for post in event_posts] == ["4294", "4294"]
            composer = page.get_by_label("Message the agent")
            await expect(composer).to_be_enabled()
            await expect(composer).to_have_value("")
            await _save_optional_screenshot(page, "initial-prompt-slash-retry")
        finally:
            await browser.close()
