"""E2E: the temp->real session handoff must not show the previous session's model.

The bug: create a session bound to agent/model A and view it, then start
a NEW session from the
landing composer with a different agent/model B. While the create POST is
still in flight (URL still ``/c/temp:*``), the session composer's model
pill renders session A's identity -- the previously viewed session's
agent/model -- instead of the selection just made for the new session. It
only flips to B once the real session's metadata loads.

Journey (the report's steps, mapped onto the harness):

1. Session A (agent ``temphandoff_prev_agent``, model ``temphandoff-prev-model``)
   is real and runner-bound; the test views it and runs one mock-LLM turn
   ("let it start").
2. Sidebar "New session" -> landing composer; pick agent B
   (``temphandoff_next_agent``, model ``temphandoff-next-model``); send the first
   message.
3. While the URL is ``/c/temp:*`` (the create POST is held open via a gated
   route, mirroring ``test_start_session_navigates_while_create_is_pending``),
   sample the composer's model pill.
4. Release the create (fulfilled with the REAL, pre-created session B id);
   the pill must settle on B's model.

Why the stubs (same rationale as ``test_start_session.py``): the e2e
harness's runner is tunneled directly into the server and registers no
*host*, so ``/v1/hosts`` is faked and the bare create ``POST /v1/sessions``
is intercepted -- held open to make the temp window deterministically
observable, then fulfilled with a real pre-seeded session id so the
post-send navigation lands on a real session. Everything else (both
sessions, the agents discovery scan, the first-message dispatch, the
mock-LLM turns) is real.

Assertions keyed to the bug:

- during the temp window the pill must NEVER render session A's identity
  (its model id or its agent name) -- the report's "it should never render
  the previous session's model, even briefly";
- by the end of the temp window (still before the create resolves) the
  pill must identify the NEW selection B -- the report's "carried over into
  the optimistic/temp session state";
- the workspace chip shows the same stale-then-correct behavior in the
  report (``No workspace`` -> the picked workspace), so by the end of the
  temp window the chip must show the workspace the landing composer picked
  for the new session, not the ``No workspace`` placeholder.
"""

from __future__ import annotations

import asyncio
import io
import json
import re
import tarfile
import threading
from collections.abc import Coroutine
from typing import Any

import httpx
import pytest
from playwright.async_api import Route, async_playwright, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    set_fallback_mock_llm,
)

# Distinctive identity tokens: "prev" only ever describes session A's
# agent/model, "next" only ever describes the new selection B, so a pill
# sample can be classified by substring regardless of how the UI formats
# the raw id (raw model id vs. prettified agent display name).
_PREV_AGENT_NAME = "temphandoff_prev_agent"
_PREV_MODEL = "temphandoff-prev-model"
_NEXT_AGENT_NAME = "temphandoff_next_agent"
_NEXT_MODEL = "temphandoff-next-model"

_AGENT_YAML_TEMPLATE = """\
name: {name}
prompt: You are a terse assistant. Reply with one short sentence.

executor:
  model: {model}
  harness: openai-agents
"""

# Stubbed host the landing composer auto-selects (the tunneled runner
# registers no host). Keyed identically in the recent-workspaces seed.
_HOST_ID = "host_temp_handoff"

# Bare create endpoint: ``/v1/sessions`` with an optional query, but NOT
# ``/v1/sessions/{id}/...`` -- the GET conversation list, the agent
# discovery scan, and the per-session reads/turn dispatch all pass through
# to the real server; only the POST create is gated.
_SESSIONS_RE = re.compile(r"/v1/sessions(\?.*)?$")

_TEMP_URL_RE = re.compile(r"/c/temp:[0-9a-f]{32}")

# How long the gated create holds the temp window open while the pill is
# sampled. The window is deterministic (the POST cannot resolve until the
# gate releases), so this is pure observation time, not a race budget.
_SAMPLE_WINDOW_S = 2.0


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


def _register_runner_bound_session(
    base_url: str, runner_id: str, name: str, model: str
) -> tuple[str, str]:
    """Create a real session for a fresh agent spec and bind it to the runner.

    Registers the agent through the multipart create (compat single-file
    spec, like ``_build_hello_world_bundle``) so the session-scoped agent
    carries a distinctive name + model, then PATCH-binds the session to the
    live runner so its turns can dispatch.

    :param base_url: Spawned server base URL.
    :param runner_id: The live runner id from ``_server_state``.
    :param name: Agent ``name:`` for the spec (shows in the picker).
    :param model: Executor model id (also the mock-LLM queue key).
    :returns: ``(session_id, agent_id)``.
    """
    yaml_bytes = _AGENT_YAML_TEMPLATE.format(name=name, model=model).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        info = tarfile.TarInfo(name=f"{name}.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    agent_resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/agent", timeout=10.0)
    agent_resp.raise_for_status()
    agent_id = agent_resp.json()["id"]

    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id, agent_id


def test_temp_session_composer_never_shows_previous_sessions_model(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The composer pill in the temp window must show B's pick, never A's.

    Failure mode this catches: during the temp->real handoff the
    composer model pill renders the previously
    viewed session's agent/model instead of the model picked for the new
    session, flipping only once the real session's metadata loads.
    """
    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    session_a, _agent_a = _register_runner_bound_session(
        live_server, runner_id, _PREV_AGENT_NAME, _PREV_MODEL
    )
    session_b, agent_b = _register_runner_bound_session(
        live_server, runner_id, _NEXT_AGENT_NAME, _NEXT_MODEL
    )
    # Real mock-LLM turns for both sessions: A's warm-up turn and B's
    # auto-sent first message after the create resolves.
    set_fallback_mock_llm(mock_llm_server_url, _PREV_MODEL, "Previous session ready.")
    set_fallback_mock_llm(mock_llm_server_url, _NEXT_MODEL, "New session under way.")

    try:
        _run_in_fresh_loop(_drive_temp_window_pill(live_server, session_a, session_b, agent_b))
    finally:
        for sid in (session_a, session_b):
            httpx.delete(f"{live_server}/v1/sessions/{sid}", timeout=10.0)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except Exception:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


async def _select_landing_custom_agent(page, agent_id: str) -> None:
    """Pick a session-scoped custom agent from the landing picker.

    Custom (session-discovered) agents fold into the "Other..." submenu
    (``new-chat-landing-custom-agents``) rather than the top-level rows the
    shared ``select_landing_agent`` helper targets, so this drills into the
    flyout first. The Escape/aria-expanded dance mirrors that helper: the
    selected row becomes the integrated config submenu and Radix can keep
    the root layer open across that rerender.
    """
    trigger = page.get_by_test_id("new-chat-landing-agent-select")
    await trigger.click()
    submenu = page.get_by_test_id("new-chat-landing-custom-agents")
    await expect(submenu).to_be_visible(timeout=60_000)
    await submenu.click()
    option = page.get_by_test_id(f"new-chat-landing-agent-{agent_id}")
    await expect(option).to_be_visible(timeout=60_000)
    await option.click()
    if await trigger.get_attribute("aria-expanded") == "true":
        await page.keyboard.press("Escape")
    await expect(trigger).to_have_attribute("aria-expanded", "false")


async def _drive_temp_window_pill(
    base_url: str, session_a: str, session_b: str, agent_b: str
) -> None:
    async with async_playwright() as pw:
        browser = await pw.chromium.launch()
        # Explicit context so the recording harness's injected
        # ``record_video_dir`` is finalized by ``context.close()`` below even
        # when an assertion fails mid-journey.
        context = await browser.new_context()
        page = await context.new_page()
        try:
            create_bodies: list[dict[str, Any]] = []
            # A gate the create handler awaits before responding, so the POST
            # stays pending long enough to observe the temp-window pill.
            release_create = asyncio.Event()

            async def handle_hosts(route: Route) -> None:
                await route.fulfill(
                    status=200,
                    content_type="application/json",
                    body=json.dumps(
                        {
                            "hosts": [
                                {
                                    "host_id": _HOST_ID,
                                    "name": "e2e-host-temp-handoff",
                                    "owner": "e2e",
                                    "status": "online",
                                }
                            ]
                        }
                    ),
                )

            async def handle_sessions(route: Route) -> None:
                if route.request.method == "POST":
                    create_bodies.append(route.request.post_data_json)
                    # Hold the create open so the temp-id window stays visible,
                    # then land the navigation on the REAL pre-created session B.
                    await release_create.wait()
                    await route.fulfill(
                        status=200,
                        content_type="application/json",
                        body=json.dumps({"id": session_b}),
                    )
                else:
                    await route.continue_()

            await page.route("**/v1/hosts", handle_hosts)
            await page.route(_SESSIONS_RE, handle_sessions)
            await page.add_init_script(
                f"""window.localStorage.setItem(
                    "omnigent:recent-workspaces",
                    JSON.stringify({{ "{_HOST_ID}": ["/work/repo"] }})
                );"""
            )

            # --- Step 1: view real session A and let it run a turn. -------
            await page.goto(f"{base_url}/c/{session_a}")
            composer = page.get_by_role("textbox", name="Message the agent")
            await composer.wait_for(state="visible", timeout=30_000)
            pill = page.get_by_test_id("composer-agent-model-value")
            # The pill shows session A's bound model -- the "previous
            # session" identity the temp window must never render.
            await expect(pill).to_have_text(_PREV_MODEL, timeout=30_000)
            await composer.fill("hello from the previous session")
            await composer.press("Enter")
            await expect(page.get_by_text("Previous session ready.", exact=True)).to_be_visible(
                timeout=60_000
            )

            # --- Step 2: New session -> pick agent B -> send. -------------
            await page.get_by_test_id("new-chat-button").click()
            landing_input = page.get_by_test_id("new-chat-landing-input")
            await landing_input.wait_for(state="visible", timeout=30_000)
            await _select_landing_custom_agent(page, agent_b)
            await landing_input.fill("start the new session with the other model")
            await page.get_by_test_id("new-chat-landing-submit").click()

            # --- Step 3: sample the pill while the URL is /c/temp:*. ------
            await _wait_until(lambda: len(create_bodies) == 1)
            await expect(page).to_have_url(
                re.compile(rf"{re.escape(base_url)}/c/temp:[0-9a-f]{{32}}")
            )
            # Test-integrity guards: the landing pick really targeted B and
            # the recent-workspace auto-pick really rode along on the create.
            assert create_bodies[0]["agent_id"] == agent_b
            assert create_bodies[0].get("workspace") == "/work/repo"

            ws_chip = page.get_by_test_id("composer-workspace-controls").locator("button").first
            samples: list[str] = []
            ws_samples: list[str] = []
            loop = asyncio.get_running_loop()
            deadline = loop.time() + _SAMPLE_WINDOW_S
            while loop.time() < deadline:
                if await pill.count() > 0:
                    samples.append((await pill.first.text_content()) or "")
                else:
                    samples.append("")
                if await ws_chip.count() > 0:
                    ws_samples.append((await ws_chip.text_content()) or "")
                else:
                    ws_samples.append("")
                await asyncio.sleep(0.05)
            # The window really stayed open: the gate is still held, so the
            # samples above are all from the temp (pre-create) state.
            assert _TEMP_URL_RE.search(page.url), f"temp window closed early: url={page.url}"

            # --- Step 4: release the create; the real session hydrates. ---
            release_create.set()
            await expect(page).to_have_url(f"{base_url}/c/{session_b}", timeout=30_000)
            await expect(pill).to_have_text(_NEXT_MODEL, timeout=30_000)

            # Shown in pytest's captured stdout on failure, so a single run
            # documents both facets even when the first assert trips.
            print(f"temp-window pill samples: {samples!r}")
            print(f"temp-window workspace-chip samples: {ws_samples!r}")

            # --- The bug: the temp window rendered session A's identity. --
            stale = [s for s in samples if "prev" in s.lower()]
            assert not stale, (
                "composer pill showed the PREVIOUS session's model/agent "
                f"during the temp->real handoff: stale samples={stale!r}, "
                f"all samples={samples!r}"
            )
            # Expected behavior from the report: the selection made in the
            # new-session composer is carried into the temp state, so by the
            # end of the temp window the pill identifies B.
            assert samples and "next" in samples[-1].lower(), (
                "composer pill never identified the newly selected "
                f"agent/model during the temp window: samples={samples!r}"
            )
            # Same stale-then-correct behavior on the workspace chip: the
            # workspace picked in the landing composer (/work/repo -> chip
            # label "repo") must be carried into the temp state instead of
            # the "No workspace" placeholder.
            assert ws_samples and "repo" in ws_samples[-1], (
                "workspace chip never showed the picked workspace during "
                f"the temp window: samples={ws_samples!r}"
            )
        finally:
            # Close the context before the browser so a video, when the
            # recording harness injected one, is flushed even on failure.
            await context.close()
            await browser.close()
