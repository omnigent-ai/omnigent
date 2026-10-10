"""E2E: the landing composer's first message survives a forced re-login.

A user starts a new session with Smart Routing, the computer sleeps with the
first-message POST in flight, and on wake the lapsed session cookie forces the
SPA through ``/auth/login`` and the IdP back to ``/c/<id>``. That hard
navigation discards every in-memory handoff, so the message must be delivered
afterwards. Two interruption shapes are covered: the POST fails before the
re-login (``severed``) and the POST is still pending when the re-login happens
(``hung``).

Stand-ins, as in ``test_smart_routing.py`` and ``test_oidc_login_flow.py``:
the OIDC-mode server with the fake IdP replaces the deployment's SSO, the
expired cookie replaces the session lapse, a route abort or hold replaces the
sleep, and ``/v1/info`` / ``/v1/hosts`` / ``/v1/agents`` plus the create ``POST`` are
stubbed because the headless harness has no host, native CLIs or router. The
session the stubbed create returns is real, runner-bound and carries a seeded
create-time routing decision, which is the state the reporter saw.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import signal
import subprocess
import sys
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Browser, Page, Route, expect

from omnigent.entities import NewConversationItem
from omnigent.entities.conversation import parse_item_data
from omnigent.runner.identity import token_bound_runner_id
from omnigent.stores.conversation_store.sqlalchemy_store import SqlAlchemyConversationStore
from tests._helpers.session import bind_session_runner, post_session_bundle
from tests.e2e_ui.auth._oidc_server import OIDCServer, spawn_oidc_server
from tests.e2e_ui.conftest import _REPO_ROOT, _build_hello_world_bundle
from tests.e2e_ui.start_session.test_smart_routing import (
    _ROUTING_AGENTS_BODY,
    _ROUTING_HOSTS_BODY,
)
from tests.e2e_ui.start_session.test_start_session import _HOST_ID, _SESSIONS_RE

_PROMPT = "sentinel first message that must survive sleep and re-login"
_EVENTS_RE = re.compile(r"/v1/sessions/([^/]+)/events$")
_MINE_LIST_RE = re.compile(r"/v1/sessions\?(?!.*pinned=).*visibility=mine")
_OBSERVATION_WINDOW_S = 20.0


@dataclass
class OIDCLane:
    server: OIDCServer
    db_path: Path
    log_dir: Path


@dataclass
class ReloginJourney:
    lane: OIDCLane
    cookies: list[dict[str, object]]
    session_id: str
    runner_id: str


@dataclass
class EventsLog:
    """What the SPA POSTed to ``/events``, split around the forced re-login."""

    woke: bool = False
    before: list[tuple[str, bool]] = field(default_factory=list)
    after: list[tuple[str, bool]] = field(default_factory=list)

    def carried(self, posts: list[tuple[str, bool]], session_id: str) -> bool:
        return any(sid == session_id and found for sid, found in posts)


def _wait_until(page: Page, predicate: Callable[[], bool], *, timeout_s: float, what: str) -> None:
    # Route handlers only run while Playwright pumps events, so never time.sleep here.
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        page.wait_for_timeout(100)
    raise AssertionError(f"{what} not met within {timeout_s:.0f}s")


@pytest.fixture(scope="module")
def oidc_lane(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[OIDCLane]:
    server_tmp = tmp_path_factory.mktemp("e2e_ui_relogin")
    for server in spawn_oidc_server(mock_llm_server_url, server_tmp):
        yield OIDCLane(server=server, db_path=server_tmp / "test.db", log_dir=server_tmp)


def _sign_in(browser: Browser, server: OIDCServer) -> list[dict[str, object]]:
    """Authenticate once through the fake IdP in an unrecorded context."""
    context = browser.new_context(record_video_dir=None)
    try:
        page = context.new_page()
        page.goto(server.public_url)
        page.locator("#fake-idp-continue").click(timeout=15_000)
        page.locator('[data-testid="sidebar-brand"]').wait_for(state="visible", timeout=15_000)
        return context.cookies()
    finally:
        context.close()


def _spawn_user_runner(
    server: OIDCServer, jwt: str, log_path: Path
) -> tuple[subprocess.Popen[bytes], str]:
    """Tunnel a runner into the OIDC server owned by the signed-in user."""
    binding_token = secrets.token_urlsafe(32)
    runner_id = token_bound_runner_id(binding_token)
    env = {
        **os.environ,
        "PYTHONPATH": f"{_REPO_ROOT}{os.pathsep}{os.environ.get('PYTHONPATH', '')}",
        "OMNIGENT_RUNNER_ID": runner_id,
        "OMNIGENT_RUNNER_TUNNEL_BINDING_TOKEN": binding_token,
        "OMNIGENT_RUNNER_PARENT_PID": str(os.getpid()),
        "RUNNER_SERVER_URL": server.base_url,
        "OMNIGENT_RUNNER_INITIAL_AUTH_TOKEN": jwt,
    }
    log_handle = open(log_path, "w")  # noqa: SIM115 — fd dup'd into child; closed below
    proc = subprocess.Popen(
        [sys.executable, "-m", "omnigent.runner._entry"],
        env=env,
        stdout=log_handle,
        stderr=subprocess.STDOUT,
    )
    log_handle.close()  # the child holds its own dup of the fd
    auth = {"Authorization": f"Bearer {jwt}"}
    deadline = time.monotonic() + 45
    online = False
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                raise RuntimeError(f"runner exited early with code {proc.returncode}")
            status = httpx.get(f"{server.base_url}/v1/runners/{runner_id}/status", headers=auth)
            if status.status_code == 200 and status.json().get("online") is True:
                online = True
                return proc, runner_id
            time.sleep(0.5)
        raise RuntimeError("runner never came online")
    finally:
        # Only the success path hands proc to the fixture teardown; a failed
        # readiness check must not leak the runner subprocess.
        if not online:
            proc.kill()


def _seed_routing_decision(db_path: Path, session_id: str) -> None:
    data = parse_item_data(
        "routing_decision",
        {
            "model": "gpt-5-6-sol",
            "applied": True,
            "rationale": "Create-time Smart Routing pick",
            "scope": "session",
            "harness": "codex-native",
            "decision_id": str(uuid.uuid4()),
            "raw_model": None,
            "attempted_override": None,
            "router_source": None,
        },
    )
    SqlAlchemyConversationStore(f"sqlite:///{db_path}").append(
        session_id,
        [
            NewConversationItem(
                type="routing_decision", response_id=f"routing_{uuid.uuid4().hex}", data=data
            )
        ],
    )


@pytest.fixture
def relogin_journey(oidc_lane: OIDCLane, browser: Browser) -> Iterator[ReloginJourney]:
    """A signed-in user, their online runner, and the session the create will return."""
    server = oidc_lane.server
    cookies = _sign_in(browser, server)
    cookie_names = [c["name"] for c in cookies]
    jwt = next((str(c["value"]) for c in cookies if c["name"] == "ap_session"), None)
    assert jwt is not None, f"ap_session cookie missing after sign-in; got {cookie_names}"
    auth = {"Authorization": f"Bearer {jwt}"}
    proc, runner_id = _spawn_user_runner(server, jwt, oidc_lane.log_dir / "runner.log")
    try:
        created = post_session_bundle(
            lambda *a, **k: httpx.post(*a, headers=auth, **k),
            f"{server.base_url}/v1/sessions",
            _build_hello_world_bundle(),
            timeout=30.0,
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]
        bind_session_runner(
            lambda *a, **k: httpx.patch(*a, headers=auth, **k),
            server.base_url,
            session_id,
            runner_id,
            timeout=10.0,
        )
        _seed_routing_decision(oidc_lane.db_path, session_id)
        yield ReloginJourney(
            lane=oidc_lane, cookies=cookies, session_id=session_id, runner_id=runner_id
        )
    finally:
        if proc.poll() is None:
            proc.send_signal(signal.SIGTERM)
            try:
                proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                proc.kill()


def _install_routes(
    page: Page, journey: ReloginJourney, log: EventsLog, interruption: str
) -> None:
    server = journey.lane.server
    created = {"v": False}

    def handle_info(route: Route) -> None:
        body = route.fetch(url=f"{server.base_url}/v1/info").json()
        body["smart_routing_enabled"] = True
        body["smart_routing_sources"] = {"external": True, "oss": False}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle_events(route: Route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        match = _EVENTS_RE.search(route.request.url)
        assert match is not None, route.request.url
        entry = (match.group(1), _PROMPT in (route.request.post_data or ""))
        if log.woke:
            log.after.append(entry)
            route.continue_()
            return
        log.before.append(entry)
        if interruption == "severed":
            route.abort("connectionaborted")
        # "hung": leave the request unanswered; the re-login navigation discards it.

    def handle_sessions(route: Route) -> None:
        if route.request.method == "POST":
            created["v"] = True
            route.fulfill(
                status=200,
                content_type="application/json",
                body=json.dumps({"id": journey.session_id}),
            )
        else:
            route.continue_()

    def handle_mine_list(route: Route) -> None:
        # Hide the pre-seeded session until the create "returns" it.
        if created["v"]:
            route.continue_()
        else:
            route.fulfill(status=200, content_type="application/json", body='{"data": []}')

    page.route("**/v1/info", handle_info)
    page.route("**/v1/hosts", lambda r: r.fulfill(json=json.loads(_ROUTING_HOSTS_BODY)))
    page.route(
        f"**/v1/hosts/{_HOST_ID}/harnesses/*/model-options",
        lambda r: r.fulfill(json={"models": []}),
    )
    page.route(f"**/v1/hosts/{_HOST_ID}/worktrees?*", lambda r: r.fulfill(json={"data": []}))
    page.route("**/v1/agents", lambda r: r.fulfill(json=json.loads(_ROUTING_AGENTS_BODY)))
    page.route("**/v1/sessions/*/events", handle_events)
    # Playwright matches routes in reverse registration order and _SESSIONS_RE
    # also matches the mine-list URL, so _MINE_LIST_RE must stay registered last.
    page.route(_SESSIONS_RE, handle_sessions)
    page.route(_MINE_LIST_RE, handle_mine_list)
    page.add_init_script(
        f'window.localStorage.setItem("omnigent:recent-workspaces", '
        f'JSON.stringify({{ {_HOST_ID}: ["/work/repo"] }}));'
    )


@pytest.mark.parametrize("interruption", ["severed", "hung"])
def test_first_message_survives_forced_relogin(
    request: pytest.FixtureRequest,
    relogin_journey: ReloginJourney,
    tmp_path: Path,
    interruption: str,
) -> None:
    """An interrupted first send is still delivered after the wake re-login."""
    journey = relogin_journey
    server = journey.lane.server
    session_url = re.compile(rf"/c/{re.escape(journey.session_id)}$")
    log = EventsLog()

    page: Page = request.getfixturevalue("page")
    page.context.add_cookies(journey.cookies)
    _install_routes(page, journey, log, interruption)

    page.goto(f"{server.public_url}/")
    page.get_by_test_id("new-chat-landing-input").wait_for(state="visible", timeout=30_000)
    page.get_by_test_id("new-chat-landing-agent-select").click()
    page.get_by_test_id("new-chat-landing-harness-smart-routing").click()
    expect(page.get_by_test_id("new-chat-landing-agent-select")).to_contain_text("Smart Routing")
    page.get_by_test_id("new-chat-landing-input").fill(_PROMPT)
    page.get_by_test_id("new-chat-landing-submit").click()

    page.wait_for_url(session_url, timeout=30_000)
    _wait_until(
        page,
        lambda: log.carried(log.before, journey.session_id),
        timeout_s=15.0,
        what="first-message POST before the re-login",
    )
    # The laptop sleeps with the send interrupted; wake finds the session
    # lapsed. The _wait_until above already proved the POST reached the server,
    # so the interrupted send is fully simulated by the route hold plus the
    # cookie clear below — no fixed sleep is needed.
    log.woke = True
    page.context.clear_cookies()
    page.reload()
    continue_link = page.locator("#fake-idp-continue")
    expect(continue_link).to_be_visible(timeout=15_000)
    continue_link.click()
    page.wait_for_url(session_url, timeout=30_000)
    expect(page.get_by_test_id("routing-decision-card").first).to_be_visible(timeout=30_000)

    bubble = page.get_by_test_id("message-bubble").filter(has_text=_PROMPT).first
    deadline = time.monotonic() + _OBSERVATION_WINDOW_S
    while time.monotonic() < deadline and not (
        bubble.is_visible() and log.carried(log.after, journey.session_id)
    ):
        page.wait_for_timeout(250)
    observed = {
        "transcript_has_message": bubble.is_visible(),
        "server_received_after_relogin": log.carried(log.after, journey.session_id),
        "composer_text": page.get_by_label("Message the agent").input_value(),
        "alerts": page.get_by_role("alert").all_inner_texts(),
        "posts_before": log.before,
        "posts_after": log.after,
    }
    page.screenshot(path=str(tmp_path / "after-relogin.png"))
    assert observed["transcript_has_message"] and observed["server_received_after_relogin"], (
        f"first message not delivered after the forced re-login: {observed}"
    )
