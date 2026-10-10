"""A claude-native session must run on the profile selected by ``CLAUDE_CONFIG_DIR``.

A person who keeps a second Claude Code profile exports ``CLAUDE_CONFIG_DIR`` and
starts the host from that shell. Their default ``~/.claude`` profile carries a
different statusLine. When they start a Claude Code session from the web UI in a
folder Claude has not seen, the pane's ``claude`` must receive the variable,
folder trust must be seeded into that profile's ``.claude.json``, and the Claude
TUI status bar must render that profile's statusLine — not the default one.

The real ``claude`` CLI runs against the mock Anthropic endpoint; the runner's
HOME holds the default profile and ``CLAUDE_CONFIG_DIR`` the selected one.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
from playwright.sync_api import Page

from omnigent.harnesses.claude_native.bridge import BRIDGE_ID_LABEL_KEY, bridge_dir_for_bridge_id
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner
from tests.e2e.conftest import isolated_mock_llm_server_url as isolated_mock_llm_server_url
from tests.e2e_ui.conftest import set_fallback_mock_llm
from tests.e2e_ui.messages.test_native_claude_render_parity import (
    _marker_rendered,
    _open_terminal_view,
    _pane_process_ids,
    _type_into_tui,
    _wait_terminal_connected,
)

_REPO = Path(__file__).resolve().parents[3]
_MODEL = "claude-sonnet-4-20250514"
_WORK_STATUS = "WORK-PROFILE-STATUSLINE"
_DEFAULT_STATUS = "DEFAULT-PROFILE-STATUSLINE"
_TERMINAL_READY_S = 120.0
_REPLY_S = 90.0
_STATUS_BAR_S = 60.0
_TRANSCRIPT_S = 30.0


@pytest.fixture
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    size = {"width": 1280, "height": 800}
    return {**browser_context_args, "viewport": size, "record_video_size": size}


def _poll(what: str, timeout: float, probe: Callable[[], object]) -> object:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = probe()
        if value:
            return value
        time.sleep(0.5)
    raise AssertionError(f"timed out after {timeout:.0f}s waiting for {what}")


def _pane_text(advert: dict[str, str]) -> str:
    proc = subprocess.run(
        ["tmux", "-S", advert["socket_path"], "capture-pane", "-t", advert["tmux_target"], "-p"],
        check=False,
        capture_output=True,
        text=True,
        timeout=10,
    )
    return proc.stdout if proc.returncode == 0 else ""


def _pane_claude_env(advert: dict[str, str]) -> dict[str, str] | None:
    """Environment of the pane's ``claude`` process, or ``None`` if not found."""
    for pid in _pane_process_ids(advert):
        try:
            cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
            if b"claude" not in cmdline:
                continue
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        return dict(
            entry.decode(errors="replace").split("=", 1)
            for entry in raw.split(b"\0")
            if b"=" in entry
        )
    return None


def _assistant_replied(client: httpx.Client, session_id: str, token: str) -> bool:
    items = client.get(
        f"/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
    ).json()
    for item in items.get("data", []):
        if item.get("type") != "message" or item.get("role") != "assistant":
            continue
        blocks = item.get("content") or []
        if any(token in str(block.get("text", "")) for block in blocks if isinstance(block, dict)):
            return True
    return False


def _transcripts(work_profile: Path, default_home: Path) -> dict[str, list[str]]:
    return {
        "work_profile": sorted(
            str(p.relative_to(work_profile)) for p in work_profile.glob("projects/*/*.jsonl")
        ),
        "default_profile": sorted(
            str(p.relative_to(default_home))
            for p in default_home.glob(".claude/projects/*/*.jsonl")
        ),
    }


def _trusted_projects(claude_json: Path) -> list[str]:
    if not claude_json.exists():
        return []
    projects = json.loads(claude_json.read_text()).get("projects") or {}
    return [path for path, entry in projects.items() if entry.get("hasTrustDialogAccepted")]


def _profile_marker(text: str | None) -> str:
    if text is None:
        return "none"
    if _marker_rendered(text, _WORK_STATUS):
        return "work"
    if _marker_rendered(text, _DEFAULT_STATUS):
        return "default"
    return "none"


@pytest.mark.timeout(600)
def test_claude_native_session_uses_claude_config_dir_profile(
    tmp_path: Path,
    request: pytest.FixtureRequest,
    isolated_mock_llm_server_url: str,
) -> None:
    mock_url = isolated_mock_llm_server_url
    stack_root = tmp_path / "stack"
    default_home = stack_root / "home"
    work_profile = tmp_path / "profiles" / "work"
    workspace = tmp_path / "fresh-project"
    config = tmp_path / "config"
    evidence = tmp_path / "evidence"
    for path in (default_home / ".claude", work_profile, workspace, config, evidence):
        path.mkdir(parents=True, exist_ok=True)

    onboarded = {"hasCompletedOnboarding": True, "theme": "dark", "projects": {}}
    (work_profile / ".claude.json").write_text(json.dumps(onboarded))
    (work_profile / "settings.json").write_text(
        json.dumps(
            {
                "statusLine": {"type": "command", "command": f"echo {_WORK_STATUS}"},
                "effortLevel": "high",
            }
        )
    )
    (default_home / ".claude.json").write_text(json.dumps(onboarded))
    (default_home / ".claude" / "settings.json").write_text(
        json.dumps(
            {
                "statusLine": {"type": "command", "command": f"echo {_DEFAULT_STATUS}"},
                "effortLevel": "low",
            }
        )
    )
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
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CLAUDE_CONFIG_DIR": str(work_profile),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
    }
    token = f"ast-{uuid.uuid4().hex[:8]}"
    prompt = f"Reply with exactly this token and nothing else: {token}"
    observed: dict[str, object] = {
        "work_profile": str(work_profile),
        "default_home": str(default_home),
    }

    with ExitStack() as resources:
        stack = resources.enter_context(
            server_runner(
                stack_root,
                server_cwd=_REPO,
                base_env=base_env,
                server_env=env,
                workspace=workspace,
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
        set_fallback_mock_llm(mock_url, "default", token)
        set_fallback_mock_llm(mock_url, _MODEL, token)
        session_id = create_native_session(
            client, stack.base_url, harness="claude", metadata={"workspace": str(workspace)}
        )["session_id"]
        observed["session_id"] = session_id
        bind_session_runner(client.patch, stack.base_url, session_id, stack.runner_id)

        page: Page = request.getfixturevalue("page")
        page.goto(f"{stack.base_url}/c/{session_id}")
        _open_terminal_view(page)
        _wait_terminal_connected(page)

        labels = client.get(f"/v1/sessions/{session_id}").json().get("labels") or {}
        bridge_dir = bridge_dir_for_bridge_id(labels.get(BRIDGE_ID_LABEL_KEY) or session_id)
        advert_path = bridge_dir / "tmux.json"
        _poll("the terminal's tmux advert", _TERMINAL_READY_S, advert_path.exists)
        advert = json.loads(advert_path.read_text())
        pane_env = _poll("the pane's claude process", 60.0, lambda: _pane_claude_env(advert))
        observed["pane_claude_config_dir"] = pane_env.get("CLAUDE_CONFIG_DIR", "<unset>")
        observed["pane_home"] = pane_env.get("HOME", "<unset>")
        observed["trust"] = {
            "work_profile": _trusted_projects(work_profile / ".claude.json"),
            "default_profile": _trusted_projects(default_home / ".claude.json"),
        }
        settings_path = bridge_dir / "claude-settings.json"
        chained = json.loads(settings_path.read_text()).get("statusLine", {}).get("command")
        observed["settings_chain"] = _profile_marker(chained)
        observed["settings_chain_command"] = chained

        _type_into_tui(page, prompt)
        _poll(
            f"the assistant reply {token} in the session transcript",
            _REPLY_S,
            lambda: _assistant_replied(client, session_id, token),
        )
        for what, timeout, probe in (
            (
                "a profile statusLine marker in the TUI",
                _STATUS_BAR_S,
                lambda: _profile_marker(_pane_text(advert)) != "none",
            ),
            (
                "Claude's transcript on disk",
                _TRANSCRIPT_S,
                lambda: any(_transcripts(work_profile, default_home).values()),
            ),
        ):
            with contextlib.suppress(AssertionError):
                _poll(what, timeout, probe)
        pane = _pane_text(advert)
        observed["status_bar"] = _profile_marker(pane)
        observed["transcripts"] = _transcripts(work_profile, default_home)
        (evidence / "pane.txt").write_text(pane)
        page.screenshot(path=str(evidence / "terminal-after-turn.png"))
        (evidence / "observations.json").write_text(json.dumps(observed, indent=2))
        print(f"observations: {json.dumps(observed, indent=2)}")
        # Hold the final TUI state so the recorded status bar is readable.
        page.wait_for_timeout(3000)
        page.context.close()

    detail = json.dumps(observed, indent=2)
    assert observed["pane_claude_config_dir"] == str(work_profile), detail
    assert str(workspace.resolve()) in observed["trust"]["work_profile"], detail
    assert str(workspace.resolve()) not in observed["trust"]["default_profile"], detail
    assert observed["transcripts"]["work_profile"], detail
    assert not observed["transcripts"]["default_profile"], detail
    assert observed["settings_chain"] == "work", detail
    assert observed["status_bar"] == "work", detail
