"""Browser e2e: a claude-native fork carries history when Claude's config dir is relocated.

Claude Code keeps its project transcripts under ``$CLAUDE_CONFIG_DIR/projects``
when that variable is set (``~/.claude/projects`` otherwise). A host started
with ``CLAUDE_CONFIG_DIR`` forwards it to its runners, so the source session's
transcript lands in the relocated directory. Forking that session from the web
UI ("Fork from here" -> "Clone & start" on the same host) must still open the
clone's Claude Code TUI with the source conversation, so the assistant can
answer a question about a code word given before the fork.

The host is a real ``omnigent host`` daemon spawned by the test with an
isolated ``HOME`` and a relocated ``CLAUDE_CONFIG_DIR``; its runner launches the
real ``claude`` CLI against the mock Anthropic endpoint. The mock answers with
the code word only when the request carries the pre-fork user turn, and with a
"no earlier conversation" reply otherwise, so the clone's visible answer tells
whether history was carried.
"""

from __future__ import annotations

import contextlib
import json
import re
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests._helpers.host import Host, isolated_host
from tests.e2e_ui.conftest import (
    _CLAUDE_MOCK_MODEL,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import _ASSISTANT, _ensure_chat_view, _send
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _open_terminal_view,
    _pane_text,
    _type_into_tui,
    _wait_terminal_connected,
)

HOST_ID = uuid.uuid5(
    uuid.NAMESPACE_URL, "omnigent://tests/e2e_ui/fork_session/claude-config-dir"
).hex
CLAUDE_NATIVE_AGENT_NAME = "claude-native-ui"
NO_HISTORY_REPLY = (
    "I don't have any earlier conversation in this session, so there is no code word. NONE."
)
RECALL_PROMPT = (
    "Earlier I gave you a code word to remember. Reply with exactly that code word, or NONE."
)

_HOST_ONLINE_TIMEOUT_S = 180.0
_SESSION_CREATE_TIMEOUT_S = 180.0
_EXTERNAL_ID_TIMEOUT_S = 90.0
_REPLY_TIMEOUT_S = 120.0
_TUI_IDLE_TIMEOUT_S = 120.0
_PANE_POLL_S = 1.0
# Claude Code's idle composer: the prompt glyph and the shortcuts footer.
_TUI_IDLE_MARKERS = ("? for shortcuts", "\u276f")


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    size = {"width": 1280, "height": 800}
    return {**browser_context_args, "viewport": size, "record_video_size": size}


@dataclass
class ConfigDirHost:
    """A running host daemon whose Claude config dir is not ``~/.claude``."""

    host: Host
    workspace: Path
    claude_config: Path


@contextmanager
def claude_config_dir_host(root: Path, base_url: str, mock_url: str) -> Iterator[ConfigDirHost]:
    """Run a real host daemon with ``CLAUDE_CONFIG_DIR`` relocated and ``HOME`` isolated.

    The provider config routes Claude Code to *mock_url*; the daemon registers
    against *base_url* with a fixed identity so the journey can name its host.
    """
    claude_config = root / "claude-config"
    workspace = root / "workspace"
    for directory in (claude_config, workspace):
        directory.mkdir(parents=True, exist_ok=True)
    (workspace / "README.md").write_text("fork-history e2e workspace\n")
    (claude_config / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    with isolated_host(
        root,
        base_url,
        host_id=HOST_ID,
        name=f"claude-config-dir-{HOST_ID[:8]}",
        env={
            "CLAUDE_CONFIG_DIR": str(claude_config),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "ANTHROPIC_BASE_URL": None,
            "ANTHROPIC_AUTH_TOKEN": None,
            "CLAUDE_CODE_OAUTH_TOKEN": None,
            "LLM_API_KEY": None,
        },
        config={
            "runner": {"idle_timeout_s": 0},
            "providers": {
                "e2e-claude": {
                    "kind": "key",
                    "default": ["anthropic"],
                    "anthropic": {
                        "base_url": mock_url,
                        "api_key": "mock-key",
                        "models": {"default": _CLAUDE_MOCK_MODEL},
                    },
                }
            },
        },
    ) as host:
        yield ConfigDirHost(host, workspace, claude_config)


def script_history_dependent_recall(mock_url: str, code_word: str) -> None:
    """Answer with *code_word* only when a request's user text carries it; else deny history."""
    reset_mock_llm(mock_url)
    for key in ("default", _CLAUDE_MOCK_MODEL):
        set_fallback_mock_llm(mock_url, key, NO_HISTORY_REPLY)
    queue = f"code-word-{code_word}"
    configure_mock_llm(mock_url, [], key=queue, match=code_word)
    set_fallback_mock_llm(mock_url, queue, code_word)


def agent_id_by_name(client: httpx.Client, name: str) -> str:
    resp = client.get("/v1/agents", params={"limit": 100}, timeout=30.0)
    resp.raise_for_status()
    agent = next((a for a in resp.json()["data"] if a["name"] == name), None)
    assert agent is not None, f"built-in agent {name!r} not registered on the server"
    return str(agent["id"])


def create_claude_native_session(client: httpx.Client, *, host_id: str, workspace: Path) -> str:
    """Create a claude-native session bound to *host_id* in *workspace*; the host launches it."""
    resp = client.post(
        "/v1/sessions",
        json={
            "agent_id": agent_id_by_name(client, CLAUDE_NATIVE_AGENT_NAME),
            "host_id": host_id,
            "workspace": str(workspace),
        },
        timeout=_SESSION_CREATE_TIMEOUT_S,
    )
    resp.raise_for_status()
    return str(resp.json()["id"])


def wait_for_external_session_id(client: httpx.Client, session_id: str, timeout: float) -> str:
    """Wait until Claude's hook records the session's Claude id (needed for a same-host clone)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        snap = client.get(f"/v1/sessions/{session_id}", timeout=30.0)
        if snap.status_code == 200 and snap.json().get("external_session_id"):
            return str(snap.json()["external_session_id"])
        time.sleep(1.0)
    raise AssertionError(
        f"session {session_id} never captured its Claude session id in {timeout}s"
    )


def wait_for_pane_text(
    base_url: str, session_id: str, needles: tuple[str, ...], *, timeout: float
) -> str:
    """Wait until the session pane renders any of *needles*; return the last pane text."""
    deadline = time.monotonic() + timeout
    pane = ""
    while time.monotonic() < deadline:
        pane = _pane_text(base_url, session_id)
        if any(needle in pane for needle in needles):
            return pane
        time.sleep(_PANE_POLL_S)
    raise AssertionError(f"pane of {session_id} showed none of {needles} in {timeout}s:\n{pane}")


def runner_log_excerpt(home: Path, session_id: str) -> str:
    """Return the launch-decision and exit lines from the host runner log of *session_id*."""
    logs = sorted((home / ".omnigent" / "logs" / "runner").glob(f"runner-{session_id}-*.log"))
    if not logs:
        return ""
    keep = (
        "fork-resume",
        "fork-rebuild",
        "cold-resume",
        "exited",
        "Pane is dead",
        "No conversation",
    )
    return "\n".join(
        line[:600]
        for line in logs[-1].read_text(errors="replace").splitlines()
        if any(token in line for token in keep)
    )


def assistant_texts(client: httpx.Client, session_id: str) -> list[str]:
    resp = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}, timeout=30.0
    )
    if resp.status_code != 200:
        return []
    texts: list[str] = []
    for item in resp.json().get("data", []):
        if item.get("role") != "assistant":
            continue
        blocks = item.get("content") or []
        texts.append(
            " ".join(b.get("text", "") for b in blocks if isinstance(b, dict) and b.get("text"))
        )
    return texts


def wait_for_reply(
    client: httpx.Client, session_id: str, *, after_count: int, timeout: float
) -> str:
    """Return the first new assistant text persisted after *after_count* earlier replies."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        texts = assistant_texts(client, session_id)
        if len(texts) > after_count and texts[after_count].strip():
            return texts[after_count]
        time.sleep(1.0)
    raise AssertionError(f"no assistant reply persisted for {session_id} within {timeout}s")


def plant_code_word(page: Page, base_url: str, session_id: str, code_word: str) -> None:
    """Open the source session and give Claude the code word from the web composer."""
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    _send(
        page,
        f"Remember this code word for the rest of our chat: {code_word}. "
        "Reply with just the code word.",
    )
    expect(page.locator(_ASSISTANT, has_text=code_word).first).to_be_visible(
        timeout=int(_REPLY_TIMEOUT_S * 1000)
    )


def fork_from_last_reply(page: Page, source_id: str) -> str:
    """Use the assistant bubble's "Fork from here" -> "Clone & start"; return the clone id."""
    _ensure_chat_view(page)
    page.locator(_ASSISTANT).last.hover()
    page.get_by_test_id("fork-from-response").last.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()
    submit = page.get_by_test_id("fork-session-submit")
    expect(submit).to_have_text("Clone & start", timeout=30_000)
    expect(submit).to_be_enabled(timeout=30_000)
    submit.click()
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(source_id)})(conv_)?[0-9a-f]+"), timeout=60_000
    )
    expect(dialog).not_to_be_visible()
    return page.url.rsplit("/c/", 1)[1].split("?", 1)[0]


def wait_for_clone_tui(page: Page, base_url: str, session_id: str, home: Path) -> None:
    """Wait for the clone's Claude TUI to reach its idle prompt; fail with the runner's decisions.

    A pane can attach and then die before Claude becomes interactive, so a
    connected terminal alone is not enough.
    """
    try:
        _wait_terminal_connected(page)
        wait_for_pane_text(base_url, session_id, _TUI_IDLE_MARKERS, timeout=_TUI_IDLE_TIMEOUT_S)
    except (AssertionError, PlaywrightTimeoutError) as exc:
        pytest.fail(
            f"the forked claude-native session never reached Claude's prompt: {exc}\n"
            f"runner log:\n{runner_log_excerpt(home, session_id)}"
        )


@pytest.mark.timeout(900)
def test_fork_claude_native_clone_carries_history_with_relocated_config_dir(
    live_server: str,
    mock_llm_server_url: str,
    request: pytest.FixtureRequest,
    tmp_path: Path,
) -> None:
    """Forking a claude-native session on a CLAUDE_CONFIG_DIR host keeps the source history.

    :param live_server: Live server base URL.
    :param mock_llm_server_url: Mock LLM base URL the host's Claude is routed to.
    :param request: Used to create the recorded ``page`` only after non-browser setup.
    :param tmp_path: Per-test root for the host's HOME, config dirs and workspace.
    """
    code_word = f"FORKWORD-{uuid.uuid4().hex[:6].upper()}"
    script_history_dependent_recall(mock_llm_server_url, code_word)

    with (
        httpx.Client(base_url=live_server, trust_env=False, timeout=30.0) as client,
        claude_config_dir_host(
            tmp_path / "config-dir-host", live_server, mock_llm_server_url
        ) as host,
    ):
        host.host.wait_online(client, timeout=_HOST_ONLINE_TIMEOUT_S, poll_interval=1.0)
        source_id = create_claude_native_session(
            client, host_id=host.host.host_id, workspace=host.workspace
        )
        fork_id: str | None = None
        try:
            page: Page = request.getfixturevalue("page")
            plant_code_word(page, live_server, source_id, code_word)
            wait_for_external_session_id(client, source_id, _EXTERNAL_ID_TIMEOUT_S)

            fork_id = fork_from_last_reply(page, source_id)
            _open_terminal_view(page)
            wait_for_clone_tui(page, live_server, fork_id, host.host.home)

            replies_before = len(assistant_texts(client, fork_id))
            _type_into_tui(page, RECALL_PROMPT)
            reply = wait_for_reply(
                client, fork_id, after_count=replies_before, timeout=_REPLY_TIMEOUT_S
            )
            assert code_word in reply, (
                f"the clone did not recall {code_word!r}; it answered {reply!r}. The forked "
                "claude-native session launched without the source conversation."
            )
            pane = wait_for_pane_text(live_server, fork_id, (code_word,), timeout=30.0)
            assert code_word in pane
            # Hold the answered prompt so a recording ends on the visible result.
            page.wait_for_timeout(3_000)
        finally:
            for session_id in (fork_id, source_id):
                if session_id:
                    # Best-effort cleanup; a dead host must not mask the test's own failure.
                    with contextlib.suppress(httpx.HTTPError):
                        client.delete(f"/v1/sessions/{session_id}", timeout=30.0)
