"""E2E: the blast-radius approval gate fires consistently for equivalent commands.

Guards against interpreter-wrapped commands slipping past the approval gate
("approvals are inconsistent - sometimes it asks for approval & sometimes it
doesn't"). polly and its sub-agents gate risky shell
commands with the ``blast_radius`` guardrail
(``omnigent.policies.builtins.orchestration``, resolved through the legacy
``omnigent.inner.nessie.policies`` path polly's bundles reference). That
policy classifies a destructive command by tokenizing the raw string
(``_shell_statements``) WITHOUT unwrapping shell-interpreter wrappers, so the
same risky action asks for approval in one spelling and silently executes in
another::

    rm -rf /tmp/scratch            -> ASK   (approval card)
    bash -c "rm -rf /tmp/scratch"  -> ALLOW (no card; executes ungated)

Coding agents intermittently wrap shell commands in ``bash -c`` / ``sh -c``,
which is exactly the "sometimes it asks and sometimes it doesn't" a user sees.
The sibling ``_shell.py`` primitives used by the ``github`` / ``working_dir``
policies DO unwrap interpreter wrappers (``unwrap_shell_command``), so this is
a gap specific to the orchestration policy, not a design constraint.

The journey: an agent guarded by ``blast_radius`` (the same policy stack
polly's config wires) is asked twice to delete a scratch directory. Turn one
emits the bare ``rm -rf`` - a pending ApprovalCard appears and is approved.
Turn two emits the SAME action wrapped in ``bash -c`` - the card must appear
again. On the unfixed build the wrapped command executes with no approval
prompt and the turn completes; the test fails naming the observed
inconsistency. After a fix the second card parks the command before it runs
and approving it completes the turn exactly like turn one.

Deterministic (mock LLM emits the scripted tool calls); same registration /
runner-bind contract as the ``approval_session`` fixture in conftest.
"""

from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import (
    _ensure_runner_online,
    _server_state,
    configure_mock_llm,
    set_fallback_mock_llm,
)

_COMPOSER = "Send a message…"
_APPROVAL_CARD = '[data-testid="approval-card"]'

# The agent must boot and take a turn before the first card appears -
# cold-start can be slow, so allow well past the streaming default but under
# the test's 600s ceiling.
_AGENT_TURN_TIMEOUT_MS = 120_000

# After turn two is sent, how long the race between "second card parked" and
# "turn completed ungated" gets before the test declares neither happened.
_SECOND_GATE_WINDOW_S = 90.0

# Content-routing tokens for the mock LLM's per-turn queues. Equal length by
# design: when a request carries both (the harness resends the conversation),
# the mock picks the RIGHTMOST equal-length match - the newest turn's queue.
_ALPHA_TOKEN = "wrapgate-alpha"
_BRAVO_TOKEN = "wrapgate-bravo"

_TURN_ONE_DONE = "Removed scratch A."
_TURN_TWO_DONE = "Removed scratch B."

_AGENT_YAML_TEMPLATE = """\
spec_version: 1
name: wrapgate_probe
prompt: |
  You are a deterministic approval-consistency test assistant. When the user
  asks you to clean a scratch directory, call sys_os_shell with exactly the
  command needed and then reply with one short sentence. Never run any other
  command or call any other tool.

executor:
  model: {model}
  config:
    harness: openai-agents

os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none

guardrails:
  # Generous window: a parked ASK must outlive the UI assertions.
  ask_timeout: 300
  policies:
    # The same policy path polly's bundle wires (examples/polly/config.yaml).
    blast_radius:
      type: function
      function:
        path: omnigent.inner.nessie.policies.blast_radius
        arguments:
          gate_pushes: true
"""


def _pending_elicitations(base_url: str, session_id: str) -> list[dict]:
    """Return the session snapshot's pending elicitation events (owner view)."""
    resp = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
    resp.raise_for_status()
    return resp.json().get("pending_elicitations") or []


def _wait_for(
    predicate: Callable[[], bool],
    *,
    timeout_s: float = 30.0,
    interval_s: float = 0.5,
) -> None:
    """Poll *predicate* until truthy or the deadline passes."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval_s)
    raise AssertionError("condition not met within timeout")


@pytest.fixture
def consistency_session(
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[str, str, Path, Path]]:
    """Session whose agent deletes scratch dir A bare and dir B via ``bash -c``.

    Two content-routed mock queues script the turns: turn one's LLM call
    returns ``sys_os_shell("rm -rf <scratch A>")`` (the spelling the
    blast-radius gate classifies), turn two's returns the SAME action wrapped
    in a shell interpreter, ``sys_os_shell('bash -c "rm -rf <scratch B>"')``.
    Both scratch dirs live under ``/tmp`` so executing the command is
    harmless; the dirs double as physical evidence of when the rm ran.

    :returns: ``(base_url, session_id, scratch_a, scratch_b)``.
    """
    run_id = uuid.uuid4().hex[:8]
    model = f"wrapgate-probe-{run_id}"

    scratch_a = Path(f"/tmp/wrapgate_scratch_a_{run_id}")
    scratch_b = Path(f"/tmp/wrapgate_scratch_b_{run_id}")
    for scratch in (scratch_a, scratch_b):
        scratch.mkdir(parents=True, exist_ok=True)
        (scratch / "payload.txt").write_text("scratch\n")

    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_rm_bare",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": f"rm -rf {scratch_a}"}),
                    }
                ]
            },
            {"text": _TURN_ONE_DONE},
        ],
        key=f"{model}-alpha",
        match=_ALPHA_TOKEN,
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {
                "tool_calls": [
                    {
                        "call_id": "call_rm_wrapped",
                        "name": "sys_os_shell",
                        "arguments": json.dumps({"command": f'bash -c "rm -rf {scratch_b}"'}),
                    }
                ]
            },
            {"text": _TURN_TWO_DONE},
        ],
        key=f"{model}-bravo",
        match=_BRAVO_TOKEN,
    )
    # Safety net if content routing ever misses: a bare text reply ends the
    # turn cleanly instead of hanging it.
    set_fallback_mock_llm(mock_llm_server_url, model, "Done.")

    respawned_runner = _ensure_runner_online(live_server, tmp_path_factory)
    runner_id = str(_server_state["runner_id"])

    yaml_bytes = _AGENT_YAML_TEMPLATE.format(model=model).encode()
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        # arcname config.yaml keeps the bundle on the strict spec_version:1
        # parser - the one that honors `guardrails`.
        info = tarfile.TarInfo(name="config.yaml")
        info.size = len(yaml_bytes)
        tar.addfile(info, io.BytesIO(yaml_bytes))
    create_resp = httpx.post(
        f"{live_server}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", buf.getvalue(), "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    patch_resp = httpx.patch(
        f"{live_server}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()

    try:
        yield (live_server, session_id, scratch_a, scratch_b)
    finally:
        httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
        for scratch in (scratch_a, scratch_b):
            shutil.rmtree(scratch, ignore_errors=True)
        if respawned_runner is not None:
            respawned_runner.terminate()
            try:
                respawned_runner.wait(timeout=5)
            except subprocess.TimeoutExpired:
                respawned_runner.kill()
                respawned_runner.wait(timeout=5)


@pytest.mark.nightly
@pytest.mark.timeout(600)
def test_wrapped_risky_command_is_gated_like_bare(
    page: Page,
    consistency_session: tuple[str, str, Path, Path],
) -> None:
    """``bash -c "rm -rf …"`` must raise the same approval card as ``rm -rf …``."""
    base_url, session_id, scratch_a, scratch_b = consistency_session
    page.goto(f"{base_url}/c/{session_id}")

    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)

    # ── Turn one: the bare spelling. The gate fires - approve it. ──
    composer.fill(f"{_ALPHA_TOKEN}: clean the first scratch directory now.")
    page.get_by_role("button", name="Send", exact=True).click()

    card = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]').first
    expect(card).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    expect(card.get_by_text("Approval required")).to_be_visible()
    # The server is genuinely parked on this prompt, not just an optimistic UI.
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"
    # The rm has NOT run yet - the gate parked it before execution.
    assert scratch_a.exists(), "bare rm ran before the approval was granted"

    card.get_by_role("button", name="Approve", exact=True).click()
    expect(page.locator(f'{_APPROVAL_CARD}[data-state="responded"]').first).to_be_visible(
        timeout=30_000
    )
    expect(page.get_by_text(_TURN_ONE_DONE)).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    # Approval released the command: scratch A is gone.
    _wait_for(lambda: not scratch_a.exists(), timeout_s=30.0)

    # ── Turn two: the SAME destructive action wrapped in ``bash -c``. ──
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    composer.fill(f"{_BRAVO_TOKEN}: clean the second scratch directory now.")
    page.get_by_role("button", name="Send", exact=True).click()

    # Correct behavior: a second pending card parks the turn BEFORE the
    # wrapped rm executes. Buggy behavior: the command executes
    # ungated and the turn completes with no prompt at all. Race the two
    # outcomes instead of burning the full turn timeout waiting on a card
    # that will never come.
    pending = page.locator(f'{_APPROVAL_CARD}[data-state="pending"]')
    done_marker = page.get_by_text(_TURN_TWO_DONE)
    deadline = time.monotonic() + _SECOND_GATE_WINDOW_S
    outcome: str | None = None
    while time.monotonic() < deadline:
        if pending.count() > 0:
            outcome = "gated"
            break
        if done_marker.count() > 0 or not scratch_b.exists():
            outcome = "ungated"
            break
        page.wait_for_timeout(250)
    if outcome is None:
        pytest.fail("turn two produced neither an approval card nor a completed turn")

    if outcome == "ungated":
        # Let the reply land so the transcript (and any recording) shows the
        # completed, never-prompted turn before the test fails.
        expect(done_marker.first).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
        _wait_for(lambda: not scratch_b.exists(), timeout_s=30.0)
        pytest.fail(
            '`bash -c "rm -rf …"` executed with no approval prompt - '
            "the identical bare `rm -rf …` was gated on turn one, so the "
            "blast-radius approval gate is inconsistent across equivalent "
            "spellings of the same destructive command"
        )

    # Fixed behavior from here on: the gate parked the wrapped command
    # before execution...
    assert scratch_b.exists(), "wrapped rm ran before the approval was granted"
    assert _pending_elicitations(base_url, session_id), "server has no parked elicitation"
    # ...and approving it completes the turn exactly like turn one.
    pending.first.get_by_role("button", name="Approve", exact=True).click()
    expect(page.get_by_text(_TURN_TWO_DONE)).to_be_visible(timeout=_AGENT_TURN_TIMEOUT_MS)
    _wait_for(lambda: not scratch_b.exists(), timeout_s=30.0)
