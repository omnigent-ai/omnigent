"""Side-chat composer slash-command menu and skill invocation.

Typing ``/`` opens the suggestions menu, submitting ``/<skill>`` routes through
the server's ``slash_command`` event, and a built-in such as ``/help`` is handled
locally. The session runs on a real ``omnigent host`` so skill discovery and
resolution exercise the real backend; only the model is mocked.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Route, expect

from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e_ui.conftest import set_fallback_mock_llm

_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_SKILL_NAME = "side-chat-probe"
_MODEL = "gpt-4o-mini"
_HOST_ONLINE_TIMEOUT_S = 180.0

_AGENT_YAML = f"""\
name: side_chat_skills
prompt: You are a terse assistant.

executor:
  model: {_MODEL}
  harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
"""


@pytest.fixture
def side_chat_host_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> Iterator[tuple[str, str]]:
    """A real host launches an in-process ``openai-agents`` session with one skill."""
    workspace = tmp_path / "workspace"
    skill_dir = workspace / ".agents" / "skills" / _SKILL_NAME
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {_SKILL_NAME}\ndescription: Probe skill for the side-chat repro\n---\n\n"
        "Reply with the single token DONE.\n"
    )
    base_env = {
        "PATH": os.environ["PATH"],
        "TMPDIR": os.environ.get("TMPDIR", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "NO_PROXY": "localhost,127.0.0.1,::1",
        "no_proxy": "localhost,127.0.0.1,::1",
    }
    host_env = {"OPENAI_BASE_URL": f"{mock_llm_server_url}/v1", "OPENAI_API_KEY": "mock-key"}
    with (
        httpx.Client(base_url=live_server, trust_env=False, timeout=30.0) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            env=host_env,
            base_env=base_env,
            name=f"side-chat-slash-{uuid.uuid4().hex[:8]}",
            command=[
                runner_executable(),
                "-m",
                "omnigent",
                "host",
                "--server",
                live_server,
                "--non-interactive",
            ],
        ) as host,
    ):
        host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1)
        create = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"side_chat_skills.yaml": _AGENT_YAML.encode()}),
            metadata={"workspace": str(workspace), "host_id": host.host_id},
            timeout=120.0,
        )
        create.raise_for_status()
        session_id = str(create.json()["session_id"])
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            if snapshot.get("runner_online") and snapshot.get("runner_id"):
                break
            time.sleep(1)
        else:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
            pytest.fail("host-launched runner never came online for the session")
        try:
            yield (live_server, session_id)
        finally:
            with contextlib.suppress(httpx.HTTPError):
                httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)


def _items(base_url: str, session_id: str) -> list[dict[str, Any]]:
    response = httpx.get(
        f"{base_url}/v1/sessions/{session_id}/items",
        params={"limit": 100, "order": "asc"},
        timeout=10.0,
    )
    response.raise_for_status()
    return response.json()["data"]


def _skill_invoked(base_url: str, session_id: str, arg_substr: str) -> bool:
    return any(
        item.get("type") == "slash_command"
        and item.get("name") == _SKILL_NAME
        and arg_substr in str(item.get("arguments", ""))
        for item in _items(base_url, session_id)
    )


def _user_said(base_url: str, session_id: str, substr: str) -> bool:
    return any(
        item.get("type") == "message"
        and item.get("user_authored")
        and substr in json.dumps(item.get("content", ""))
        for item in _items(base_url, session_id)
    )


def _poll(predicate: Callable[[], bool], timeout_s: float = 20.0, interval: float = 1.0) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return predicate()


def _delete_forks(base_url: str, child_ids: list[str]) -> None:
    """Remove the side-chat forks a test created from the fixture-owned server."""
    for child_id in child_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{child_id}", timeout=10.0)


def test_side_chat_slash_menu_and_skill_invocation(
    request: pytest.FixtureRequest,
    side_chat_host_session: tuple[str, str],
    mock_llm_server_url: str,
) -> None:
    """``/`` opens the menu in the side chat and ``/<skill>`` invokes the skill there."""
    base_url, session_id = side_chat_host_session
    side_token = f"sidechat-{session_id}"
    main_token = f"mainchat-{session_id}"
    skill_row = f"slash-menu-item-{_SKILL_NAME}"
    set_fallback_mock_llm(mock_llm_server_url, _MODEL, "Acknowledged.")

    page = request.getfixturevalue("page")
    child_ids: list[str] = []

    def _track_fork(route: Route) -> None:
        response = route.fetch()
        if response.ok:
            child_ids.append(str(response.json()["id"]))
        route.fulfill(response=response)

    page.route(f"**/v1/sessions/{session_id}/fork", _track_fork)
    request.addfinalizer(lambda: _delete_forks(base_url, child_ids))

    page.goto(f"{base_url}/c/{session_id}")
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_visible(timeout=30_000)

    # Control: the main composer opens the menu, lists the session skill, and
    # invokes it via the slash_command route (a slash_command item is persisted).
    composer.fill("/")
    expect(page.get_by_test_id(skill_row)).to_be_visible(timeout=20_000)
    composer.fill("")
    composer.fill(f"/{_SKILL_NAME} {main_token}")
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.get_by_test_id("slash-command-card").first).to_be_visible(timeout=30_000)
    assert _poll(lambda: _skill_invoked(base_url, session_id, main_token)), (
        "control failed: the main composer did not route the skill through slash_command"
    )

    # Open a side chat with /side; the seeded question auto-sends to the child.
    composer.fill(f"/side {side_token}: open a side chat")
    page.get_by_role("button", name="Send", exact=True).click()
    pane = page.locator(".side-chat-backdrop")
    side_input = page.get_by_test_id("side-chat-input")
    expect(side_input).to_be_visible(timeout=30_000)
    expect(pane.locator(_ASSISTANT).first).to_be_visible(timeout=60_000)
    assert _poll(lambda: bool(child_ids), timeout_s=15), "side-chat fork was never created"
    child_id = child_ids[0]
    base_cards = pane.get_by_test_id("slash-command-card").count()

    problems: list[str] = []

    # Facet A: typing "/" in the side-chat composer should open the same menu.
    # Poll rather than a fixed sleep so a loaded runner cannot fail it falsely.
    side_input.click()
    page.keyboard.type("/")
    try:
        expect(pane.get_by_test_id(skill_row)).to_be_visible(timeout=10_000)
    except AssertionError:
        problems.append("facet A: no slash-command menu opened in the side-chat composer")
    side_input.fill("")

    # Facet B: submitting "/<skill>" from the side chat should invoke the skill —
    # a receipt appears and the server records a slash_command (not plaintext).
    assistant_before = pane.locator(_ASSISTANT).count()
    side_input.fill(f"/{_SKILL_NAME} {side_token}-invoke")
    page.get_by_test_id("side-chat-send").click()
    if not _poll(
        lambda: pane.get_by_test_id("slash-command-card").count() > base_cards, timeout_s=30
    ):
        problems.append("facet B: side-chat skill submit showed no slash-command receipt")
    if not _poll(lambda: _skill_invoked(base_url, child_id, f"{side_token}-invoke"), timeout_s=30):
        problems.append(
            "facet B: the side-chat /skill was not invoked (sent to the agent as plaintext)"
        )
    # The receipt must survive the skill's own reply: once a later assistant turn
    # streams in, the slash-command card still has to be visible so the user can
    # see that a command ran.
    if not _poll(lambda: pane.locator(_ASSISTANT).count() > assistant_before, timeout_s=60):
        problems.append("facet B: the invoked skill never produced an assistant reply")
    elif pane.get_by_test_id("slash-command-card").count() <= base_cards:
        problems.append("facet B: the slash-command receipt disappeared after the skill replied")

    # Facet C: a built-in typed in the side chat should be handled locally, not
    # forwarded to the agent as a turn. Assert the local notice actually rendered
    # (so the facet cannot pass if the submit were silently dropped), then that
    # nothing was forwarded to the child session.
    side_input.fill("/help")
    page.get_by_test_id("side-chat-send").click()
    if not _poll(
        lambda: pane.get_by_test_id("side-chat-command-notice").count() > 0, timeout_s=10
    ):
        problems.append("facet C: the local /help notice never rendered in the side chat")
    if _poll(lambda: _user_said(base_url, child_id, "/help"), timeout_s=20):
        problems.append("facet C: /help was sent to the agent instead of handled locally")

    assert not problems, "; ".join(problems)
