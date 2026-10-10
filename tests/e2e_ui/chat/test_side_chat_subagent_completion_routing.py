"""A background sub-agent spawned from the main chat must report back to the main chat.

The user converses in a claude-native main chat, delegates an investigation to a
Claude Code background sub-agent (``Agent`` with ``subagent_type: "fork"``, which
reports back through a ``<task-notification>`` in a later turn), and opens a side
chat from the same main chat. A side chat is a server-side fork that clones the
main chat's Claude transcript, so when it is opened WHILE the background fork is
still running, its Claude process resumes a transcript that carries the pending
agent and receives that agent's completion notification. The sub-agent's
completion — and the agent turn reacting to it — must land in the MAIN chat only.

``before_spawn`` opens the side chat before the delegation (the control: the
clone carries no pending agent). ``during_fork`` opens it while the fork runs
(the reported condition). Model replies are scripted through a mock Anthropic
endpoint, and the child's reply is held on the mock's gate until the side chat is
in place, so ``during_fork`` always forks with the agent pending; the Claude CLI,
the fork, the side-chat clone, the forwarder and the web rendering are real. The
clone lookup reads ``~/.claude/projects``, so the runner must keep Claude's
default home (no ``CLAUDE_CONFIG_DIR`` override).
"""

from __future__ import annotations

import json
import os
import secrets
import shutil
import time
import uuid
from contextlib import ExitStack, suppress
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, Route, expect

from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e.conftest import release_mock_gate
from tests.e2e_ui.chat.test_side_chat_entrypoints import _ASSISTANT, _items, _start_side_chat
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[3]
_MODEL = "claude-sonnet-4-20250514"
_MAIN_REPLY = "The main chat context is ready."
_SIDE_REPLY = "This is the side chat's own answer."
_DISPATCH_ACK = "Investigation dispatched; I will report when its notification arrives."
_FOLLOWUP_REPLY = "The investigation reported back; findings summarized above."
_LATER_REPLY = "Still here in the main chat."
_SIDE_PANE = ".side-chat-backdrop"
_COMPLETION_MARKERS = ("<task-notification>",)


class _Prompts:
    """Prompt tokens graded by length so the mock routes each turn to its own queue."""

    def __init__(self, nonce: str) -> None:
        self.nonce = nonce
        self.main = f"m1-{nonce}: remember this main chat context"
        self.spawn = f"spawn-inv-{nonce}: investigate the module in the background and report back"
        self.side = f"side-question-{nonce}-{nonce}: answer this in the side chat only"
        self.child = f"child-directive-{nonce}-{nonce}-{nonce}: inspect the module for leaks"
        self.later = f"later-main-chat-{nonce}-{nonce}: are you still in the main chat?"

    def token(self, name: str) -> str:
        return getattr(self, name).split(":", 1)[0]


def _script(mock_url: str, prompts: _Prompts) -> None:
    reset_mock_llm(mock_url)
    set_fallback_mock_llm(mock_url, _MODEL, "Acknowledged.")
    set_fallback_mock_llm(mock_url, "_policy_llm_", '{"action":"allow","reason":""}')
    for name, reply in (("main", _MAIN_REPLY), ("side", _SIDE_REPLY), ("later", _LATER_REPLY)):
        configure_mock_llm(
            mock_url, [{"text": reply}], match=prompts.token(name), required_tools=["Bash"]
        )
    # The fork tool call, the parent's dispatch ack, and the turn reacting to the
    # completion notice. The reacting request still carries the cloned "spawn"
    # prompt, so a side chat opened mid-fork draws the follow-up from this queue.
    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": f"toolu_fork_{prompts.nonce}",
                        "name": "Agent",
                        "arguments": json.dumps(
                            {
                                "subagent_type": "fork",
                                "description": "Background module investigation",
                                "prompt": prompts.child,
                            }
                        ),
                    }
                ]
            },
            {"text": _DISPATCH_ACK},
            {"text": _FOLLOWUP_REPLY},
        ],
        match=prompts.token("spawn"),
        required_tools=["Agent"],
    )
    # The child's reply is parked on the mock's gate, so the fork stays pending
    # until the test releases it.
    configure_mock_llm(
        mock_url,
        [{"text": "SUB-AGENT FINDINGS: the investigated module has no leaks.", "block": True}],
        match=prompts.token("child"),
        required_tools=["Bash"],
    )


def _completion_items(items: list[dict[str, object]]) -> list[dict[str, object]]:
    """Items carrying the sub-agent's completion: the returned-notice card or the
    completion notification Claude received."""
    found: list[dict[str, object]] = []
    for item in items:
        if item.get("event_type") == "session.subagent.returned":
            found.append(item)
            continue
        blob = json.dumps(item.get("content") or item.get("output") or "")
        if any(marker in blob for marker in _COMPLETION_MARKERS):
            found.append(item)
    return found


def _wait_for_completion(
    base_url: str, main_id: str, side_id: str, *, timeout_s: float, until_main: bool = False
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    deadline = time.monotonic() + timeout_s
    while True:
        main_hits = _completion_items(_items(base_url, main_id))
        side_hits = _completion_items(_items(base_url, side_id))
        settled = main_hits if until_main else (main_hits or side_hits)
        if settled or time.monotonic() > deadline:
            return main_hits, side_hits
        time.sleep(2.0)


def _wait_for_gate(mock_url: str, *, timeout_s: float) -> None:
    """Wait until the background child's model request is parked on the mock gate."""
    deadline = time.monotonic() + timeout_s
    while not httpx.get(f"{mock_url}/gate/pending", timeout=5.0).json()["pending"]:
        assert time.monotonic() < deadline, "the background sub-agent never reached the mock gate"
        time.sleep(1.0)


def _wait_for_reaction(
    base_url: str, main_id: str, completion_id: str, *, timeout_s: float
) -> None:
    """Wait for the main chat's assistant turn that follows the completion item."""
    deadline = time.monotonic() + timeout_s
    while True:
        items = _items(base_url, main_id)
        ids = [item.get("id") for item in items]
        if completion_id in ids and any(
            item.get("type") == "message" and item.get("role") == "assistant"
            for item in items[ids.index(completion_id) + 1 :]
        ):
            return
        assert time.monotonic() < deadline, (
            "the agent never reacted to the completion in the main chat"
        )
        time.sleep(2.0)


def _dump_evidence(
    folder: Path, page: Page, base_url: str, main_id: str, side_id: str, label: str
) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{label}-main-items.json").write_text(
        json.dumps(_items(base_url, main_id), indent=1)
    )
    (folder / f"{label}-side-items.json").write_text(
        json.dumps(_items(base_url, side_id), indent=1)
    )
    pane = page.locator(_SIDE_PANE)
    (folder / f"{label}-side-pane.txt").write_text(
        pane.inner_text() if pane.count() else "<side chat pane not mounted>"
    )
    page.screenshot(path=str(folder / f"{label}.png"), full_page=True)


def _send(page: Page, text: str) -> None:
    composer = page.get_by_placeholder("Send a message…")
    expect(composer).to_be_enabled(timeout=120_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _drive_journey(
    page: Page,
    base_url: str,
    session_id: str,
    prompts: _Prompts,
    *,
    side_chat_order: str,
    evidence: Path,
    mock_url: str,
) -> None:
    side_ids: list[str] = []
    fork_at: list[float] = []

    def track_fork(route: Route) -> None:
        fork_at.append(time.time())
        response = route.fetch()
        if response.ok:
            side_ids.append(response.json()["id"])
        route.fulfill(response=response)

    page.route(f"**/v1/sessions/{session_id}/fork", track_fork)
    page.goto(f"{base_url}/c/{session_id}")
    page.get_by_test_id("view-mode-chat").click(timeout=120_000)

    _send(page, prompts.main)
    expect(page.locator(_ASSISTANT).filter(has_text=_MAIN_REPLY)).to_be_visible(timeout=120_000)

    def open_side_chat() -> str:
        _start_side_chat(page, "slash", prompts.side)
        pane = page.locator(_SIDE_PANE)
        expect(pane.locator(_ASSISTANT).filter(has_text=_SIDE_REPLY)).to_be_visible(
            timeout=180_000
        )
        assert len(side_ids) == 1, side_ids
        side = httpx.get(f"{base_url}/v1/sessions/{side_ids[0]}", timeout=10.0)
        side.raise_for_status()
        assert side.json()["labels"].get("omnigent.side_chat") == "1"
        return side_ids[0]

    def spawn() -> None:
        _send(page, prompts.spawn)
        expect(page.locator(_ASSISTANT).filter(has_text=_DISPATCH_ACK)).to_be_visible(
            timeout=120_000
        )

    if side_chat_order == "before_spawn":
        side_id = open_side_chat()
        spawn()
        _wait_for_gate(mock_url, timeout_s=120.0)
    else:
        spawn()
        # Fork the side chat while the child's reply is parked on the gate, so
        # the clone is taken with the background agent still pending.
        _wait_for_gate(mock_url, timeout_s=120.0)
        side_id = open_side_chat()
    release_mock_gate(mock_url)

    evidence.mkdir(parents=True, exist_ok=True)
    (evidence / "ids.json").write_text(json.dumps({"main": session_id, "side": side_id}))
    main_hits, _ = _wait_for_completion(
        base_url, session_id, side_id, timeout_s=150.0, until_main=True
    )
    assert main_hits, "sub-agent completion never reached the main chat within 150s"
    completion = main_hits[0]
    if side_chat_order == "during_fork":
        # The reported condition: the clone was taken while the fork was pending.
        assert int(fork_at[0]) <= completion["created_at"], (
            "side chat was opened after the fork completed; nothing was pending to inherit"
        )
        assert any(
            item.get("type") == "function_call" and item.get("name") == "Agent"
            for item in _items(base_url, side_id)
        ), "side chat clone did not carry the pending Agent launch"
    # Let the agent's turn reacting to the completion land in the main chat, then
    # hold a short quiet period so a late leak into the side chat would be seen.
    _wait_for_reaction(base_url, session_id, completion["id"], timeout_s=120.0)
    time.sleep(5.0)
    _dump_evidence(evidence, page, base_url, session_id, side_id, "after-completion")

    _send(page, prompts.later)
    expect(page.locator("main").locator(_ASSISTANT).filter(has_text=_LATER_REPLY)).to_be_visible(
        timeout=120_000
    )
    time.sleep(3.0)
    _dump_evidence(evidence, page, base_url, session_id, side_id, "after-followup")

    main_hits, side_hits = _wait_for_completion(base_url, session_id, side_id, timeout_s=1.0)
    pane = page.locator(_SIDE_PANE)
    assert main_hits, "no sub-agent completion reached the main chat"
    assert not side_hits, f"sub-agent completion leaked into the side chat: {side_hits}"
    expect(pane.get_by_text(_FOLLOWUP_REPLY, exact=True)).to_have_count(0)
    expect(pane.get_by_text(_LATER_REPLY, exact=True)).to_have_count(0)


@pytest.mark.nightly
@pytest.mark.timeout(900)
@pytest.mark.parametrize("side_chat_order", ["before_spawn", "during_fork"])
def test_fork_completion_reports_to_main_chat_not_side_chat(
    request: pytest.FixtureRequest,
    built_spa: None,
    isolated_mock_llm_server_url: str,
    tmp_path: Path,
    side_chat_order: str,
) -> None:
    """A background fork's completion must reach the main chat, not the cloned side chat.

    The self-contained server+runner stack keeps Claude's default ``~/.claude``
    home so the side-chat fork genuinely clones the main transcript (and, in
    ``during_fork``, inherits the still-running background agent).
    """
    for binary in ("claude", "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    if any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip(
            "machine-managed Claude settings override mock auth; use an isolated container"
        )

    mock_url = isolated_mock_llm_server_url
    config = tmp_path / "config"
    config.mkdir()
    (config / "config.yaml").write_text(
        json.dumps(
            {
                "runner": {"idle_timeout_s": 0},
                "providers": {
                    "repro-claude": {
                        "kind": "key",
                        "default": ["anthropic"],
                        "anthropic": {
                            "base_url": mock_url,
                            "api_key": "mock-key",
                            "models": {"default": _MODEL},
                        },
                    }
                },
            }
        )
    )
    # HOME is owned by the isolated stack and CLAUDE_CONFIG_DIR is deliberately
    # unset so the runner's Claude writes under its stack HOME's ``~/.claude``,
    # which is exactly where the fork clone lookup reads.
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    prompts = _Prompts(uuid.uuid4().hex[:8])
    with ExitStack() as resources:
        stack = resources.enter_context(
            server_runner(
                tmp_path / "stack",
                server_cwd=_REPO,
                base_env=base_env,
                server_env={
                    **env,
                    "OMNIGENT_RUNNER_TUNNEL_TOKEN": None,
                    "OMNIGENT_WEB_UI_DIST": os.environ.get("OMNIGENT_WEB_UI_DIST")
                    or str(_REPO / "omnigent/server/static/web-ui"),
                },
                binding_token=secrets.token_urlsafe(32),
                poll_interval=0.2,
            )
        )
        stack.start_runner(cwd=_REPO, env=env)
        client = resources.enter_context(
            httpx.Client(
                base_url=stack.base_url,
                trust_env=False,
                timeout=30,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    "x-omnigent-background-session-titles": "off",
                },
            )
        )
        session_id = create_native_session(
            client,
            stack.base_url,
            harness="claude",
            metadata={
                "workspace": str(stack.workspace),
                "terminal_launch_args": ["--dangerously-skip-permissions"],
            },
        )["session_id"]
        bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)
        _script(mock_url, prompts)
        page: Page = request.getfixturevalue("page")
        evidence = tmp_path / "evidence"
        try:
            _drive_journey(
                page,
                stack.base_url,
                session_id,
                prompts,
                side_chat_order=side_chat_order,
                evidence=evidence,
                mock_url=mock_url,
            )
        finally:
            # Never leave the child parked on the gate: a wedged sub-agent outlives
            # the test and blocks the stack teardown.
            with suppress(httpx.HTTPError):
                release_mock_gate(mock_url)
            evidence.mkdir(parents=True, exist_ok=True)
            for log in sorted((tmp_path / "stack").rglob("*.log")):
                shutil.copy(log, evidence / f"stack-{log.name}")
            try:
                requests = httpx.get(f"{mock_url}/mock/requests", timeout=10.0).json()
                (evidence / "mock-requests.json").write_text(json.dumps(requests, indent=1))
            except httpx.HTTPError:
                pass
