"""A real SDK harness messages other sessions: the sender shows "To…", recipients "From…".

A manager session runs the openai-agents harness on a real host. The mock LLM scripts
its replies, but every tool call runs for real: the runner executes sys_session_message
and sys_session_create(placement="same_host", message=...) against the server, which
delivers each task. The manager's chat must show both accepted sends as "To @<session>"
rows and the send the server refused as "Not sent"; each recipient's chat shows its task
under "From @Manager".
"""

from __future__ import annotations

import json
import os
import re
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
import yaml
from playwright.sync_api import Locator, Page, expect

from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e_ui.conftest import configure_mock_llm, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[3]
_TURN_TIMEOUT_MS = 120_000
_SENT = '[data-testid="outbound-session-message"][data-status="sent"]'
_NOT_SENT = '[data-testid="outbound-session-message"][data-status="not_sent"]'
_MANAGER_REPLY = "Both workers have their tasks."
_WORKER_REPLY = "Repro confirmed on main."
_FIX_WORKER_REPLY = "Drafting the fix now."


@dataclass(frozen=True)
class _Tasks:
    """
    What the manager is asked and sends; each text carries the token the mock routes on.

    :param nonce: Per-run token, e.g. ``"3f9a1c2e"``, so queues never cross tests.
    """

    nonce: str

    @property
    def prompt(self) -> str:
        return f"Brief the repro and fix workers (brief-{self.nonce})."

    @property
    def repro(self) -> str:
        return (
            f"Reproduce the cart rounding bug on main (repro-{self.nonce}).\n\n"
            "List each cart that rounds the wrong way."
        )

    @property
    def fix(self) -> str:
        return f"Draft a fix for the cart rounding bug (fix-{self.nonce})."


@dataclass(frozen=True)
class _HostedStack:
    """
    An isolated server and host, with an API client and the host's id.

    :param host_id: Host the sessions run on, e.g. ``"9f2c…"``.
    """

    stack: ServerRunner
    client: httpx.Client
    host_id: str


@contextmanager
def _hosted_stack(tmp_path: Path) -> Iterator[_HostedStack]:
    """Start a private server and real host from this checkout, serving the built SPA."""
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "PYTHONPATH": str(_REPO),
        "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    web_ui = os.environ.get("OMNIGENT_WEB_UI_DIST") or str(_REPO / "omnigent/server/static/web-ui")
    server_env = {**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": None, "OMNIGENT_WEB_UI_DIST": web_ui}
    headers = {
        "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
        "x-omnigent-background-session-titles": "off",
    }
    with (
        server_runner(
            tmp_path / "stack",
            server_cwd=_REPO,
            base_env=base_env,
            server_env=server_env,
            poll_interval=0.2,
        ) as stack,
        httpx.Client(
            base_url=stack.base_url, timeout=60, trust_env=False, headers=headers
        ) as client,
    ):
        stack.start_host(env=env, cwd=_REPO)

        def online_hosts() -> list[dict[str, str]]:
            hosts = client.get("/v1/hosts").json()["hosts"]
            return [host for host in hosts if host["status"] == "online"]

        poll_until(online_hosts, timeout_s=60)
        yield _HostedStack(stack=stack, client=client, host_id=online_hosts()[0]["host_id"])


def _create_session(hosted: _HostedStack, bundle: bytes, title: str) -> str:
    """Create session *title* on the stack's host from agent *bundle*."""
    metadata = {
        "host_id": hosted.host_id,
        "workspace": str(hosted.stack.workspace),
        "title": title,
    }
    response = post_session_bundle(hosted.client.post, "/v1/sessions", bundle, metadata=metadata)
    response.raise_for_status()
    return response.json()["session_id"]


def _agent_bundle(mock_url: str) -> bytes:
    """An openai-agents agent that may start sessions and answers from the mock LLM."""
    spec = {
        "name": "session-messaging",
        "prompt": "Coordinate session work.",
        "spawn": True,
        "executor": {
            "harness": "openai-agents",
            "model": "gpt-4o-mini",
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock_url}/v1"},
        },
        "os_env": {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}},
        "skills_filter": "none",
    }
    return bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()})


def _script_turns(
    mock_url: str, tasks: _Tasks, *, worker_id: str, agent_id: str, missing_id: str
) -> None:
    """
    Script the manager's three sends and each recipient's reply.

    :param worker_id: Existing session the first message goes to.
    :param agent_id: Agent the same-host worker is created from.
    :param missing_id: A well-formed id of no session, which the server refuses.
    """
    set_fallback_mock_llm(mock_url, "_policy_llm_", '{"action":"allow","reason":""}')
    tools = ["sys_session_message"]
    replies = {f"repro-{tasks.nonce}": _WORKER_REPLY, f"fix-{tasks.nonce}": _FIX_WORKER_REPLY}
    for token, reply in replies.items():
        configure_mock_llm(mock_url, [{"text": reply}], match=token, required_tools=tools)
    create = {
        "agent_id": agent_id,
        "placement": "same_host",
        "title": "Fix worker",
        "message": tasks.fix,
    }
    calls = [
        ("send", "sys_session_message", {"session_id": worker_id, "message": tasks.repro}),
        ("create", "sys_session_create", create),
        (
            "refused",
            "sys_session_message",
            {"session_id": missing_id, "message": "Check retries."},
        ),
    ]
    turns = [
        {"tool_calls": [{"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]}
        for call_id, name, arguments in calls
    ]
    configure_mock_llm(
        mock_url,
        [*turns, {"text": _MANAGER_REPLY}],
        match=f"brief-{tasks.nonce}",
        required_tools=tools,
    )


def _sent_row(page: Page, title: str) -> Locator:
    """The accepted "To…" row whose pill names session *title*."""
    pill = page.get_by_role("link", name=f"@{title}", exact=True)
    return page.locator(_SENT).filter(has=pill)


def _assert_sender_rows(page: Page, tasks: _Tasks) -> None:
    """Both accepted sends read "To @…" with their first line; the refusal reads "Not sent"."""
    rows = page.get_by_test_id("outbound-session-message")
    expect(rows).to_have_count(3, timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_SENT)).to_have_count(2)
    to_worker = _sent_row(page, "Worker")
    expect(to_worker.get_by_test_id("message-destination")).to_have_text("To@Worker")
    first_line = tasks.repro.split("\n")[0]
    expect(to_worker.get_by_test_id("expand-sent-message")).to_have_text(first_line)
    to_fix_worker = _sent_row(page, "Fix worker")
    expect(to_fix_worker.get_by_test_id("message-destination")).to_have_text("To@Fix worker")
    expect(to_fix_worker.get_by_test_id("expand-sent-message")).to_have_text(tasks.fix)
    refused = page.locator(_NOT_SENT)
    label = "Not sent toanother session"
    expect(refused.get_by_test_id("message-destination")).to_have_text(label)
    note = "· Session message refused"
    # The note sits beside the call's runtime, as on a tool row.
    expect(refused.get_by_test_id("toggle-unsent-message")).to_contain_text(note)


def _assert_recipient(
    page: Page, title: str, first_line: str, reply: str, session_id: str
) -> None:
    """Open recipient *title* from its "To…" pill; its task reads "From @Manager"."""
    _sent_row(page, title).get_by_role("link", name=f"@{title}", exact=True).click()
    expect(page).to_have_url(re.compile(rf"/c/{session_id}$"))
    received = page.get_by_test_id("collapsed-session-message")
    origin = received.get_by_test_id("message-origin")
    expect(origin).to_have_text("From@Manager", timeout=_TURN_TIMEOUT_MS)
    expect(received.get_by_test_id("expand-session-message")).to_have_text(first_line)
    expect(page.get_by_text(reply, exact=True)).to_be_visible(timeout=_TURN_TIMEOUT_MS)


def _send_prompt(page: Page, session_url: str, prompt: str) -> None:
    """Type *prompt* into the session's composer and wait for the manager's reply."""
    page.goto(session_url)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_enabled(timeout=_TURN_TIMEOUT_MS)
    composer.fill(prompt)
    page.get_by_role("button", name="Send", exact=True).click()
    reply = page.get_by_text(_MANAGER_REPLY, exact=True)
    expect(reply).to_be_visible(timeout=_TURN_TIMEOUT_MS)


def _created_session(client: httpx.Client, title: str, creator: str) -> str:
    """The id of session *title*, after checking that *creator* started it."""
    sessions = client.get("/v1/sessions", params={"visibility": "mine"}).json()["data"]
    session_id = next(row["id"] for row in sessions if row["title"] == title)
    labels = client.get(f"/v1/sessions/{session_id}").json()["labels"]
    assert labels["omnigent.created_by_session_id"] == creator
    return session_id


@pytest.mark.timeout(420)
def test_sent_messages_read_to_and_arrive_from(
    page: Page,
    built_spa: None,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """A real send and a real create's first task read "To…" here and "From…" there."""
    if os.environ.get("OMNIGENT_REPRO_SERVER_URL"):
        pytest.fail("This test owns its host and runners; run it outside verify-env run.")
    mock_url = isolated_mock_llm_server_url
    tasks = _Tasks(uuid.uuid4().hex[:8])
    with _hosted_stack(tmp_path) as hosted:
        bundle = _agent_bundle(mock_url)
        manager = _create_session(hosted, bundle, "Manager")
        worker = _create_session(hosted, bundle, "Worker")
        agent_id = hosted.client.get(f"/v1/sessions/{worker}").json()["agent_id"]
        missing_id = uuid.uuid4().hex
        _script_turns(mock_url, tasks, worker_id=worker, agent_id=agent_id, missing_id=missing_id)

        _send_prompt(page, f"{hosted.stack.base_url}/c/{manager}", tasks.prompt)
        _assert_sender_rows(page, tasks)
        _assert_recipient(page, "Worker", tasks.repro.split("\n")[0], _WORKER_REPLY, worker)
        # Back to the chat watched live: its reconnect must not repeat the sends.
        page.go_back()
        _assert_sender_rows(page, tasks)
        # The same rows rebuilt from the saved transcript.
        page.reload()
        _assert_sender_rows(page, tasks)
        to_worker = _sent_row(page, "Worker")
        to_worker.get_by_test_id("expand-sent-message").click()
        expect(to_worker.get_by_text("List each cart that rounds the wrong way.")).to_be_visible()

        fix_worker = _created_session(hosted.client, "Fix worker", manager)
        _assert_recipient(page, "Fix worker", tasks.fix, _FIX_WORKER_REPLY, fix_worker)
