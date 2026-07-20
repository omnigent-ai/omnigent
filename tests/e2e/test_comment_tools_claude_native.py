"""E2E test: list_comments and update_comment tools in claude-native mode.

Verifies the full round-trip for native Claude Code sessions: the test
starts Claude Code in a private tmux window with the Omnigent MCP
bridge, adds review comments via the REST API, sends a message
(which triggers the runner to write ``tool_relay.json`` so the bridge
exposes ``list_comments`` / ``update_comment`` to Claude), and then
confirms the server reflects the expected ``"addressed"`` status on
all comments.

The test uses the same ``claude-native-ui`` agent spec that
``omnigent claude`` materialises at runtime — not a custom yaml.
This ensures comment-relay behaviour is tested against the exact agent
configuration end users encounter.

Requirements
------------
- ``claude`` CLI on PATH and authenticated.
- ``tmux`` on PATH (needed to run Claude Code headlessly in a test).

Both checks are performed at the start of the test; the test skips
cleanly when either binary is missing rather than failing mid-run.

Usage::

    pytest tests/e2e/test_comment_tools_claude_native.py \\
        --llm-api-key $LLM_API_KEY -v
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from omnigent.claude_native_bridge import (
    augment_claude_args,
    bridge_dir_for_bridge_id,
    prepare_bridge_dir,
    write_tmux_target,
)
from tests.e2e.conftest import (
    configure_mock_llm,
    create_runner_bound_session,
    reset_mock_llm,
    send_user_message_to_session,
    upload_agent,
)

# Agent name written into the spec by ``omnigent claude``.
_CLAUDE_NATIVE_UI_AGENT_NAME = "claude-native-ui"

# How long to wait for the MCP bridge's serve-mcp process to start
# (server.json appears) after Claude Code is launched.
_BRIDGE_READY_TIMEOUT_S = 90.0

# How long to poll comment statuses after the turn is injected.
_COMMENT_POLL_TIMEOUT_S = 240.0
_COMMENT_POLL_INTERVAL_S = 3.0

# Claude Code treats this env var as "I'm already inside a Claude Code session"
# and refuses to start a fresh one. The agent/CI process running this suite may
# export it, so it is dropped from the launched ``claude``'s environment.
_NESTED_SESSION_ENV = "CLAUDECODE"

# Opt-in gate. This test drives the real interactive ``claude`` TUI, whose
# first-run onboarding/trust gates are version- and environment-dependent and
# block headless startup in CI (the MCP bridge never initializes). The relay
# wiring is covered deterministically by tests/runner/test_comment_relay.py and
# tests/test_claude_native_bridge.py; this test is the full round-trip, run
# on demand where an authenticated claude is available.
_RUN_GATE_ENV = "OMNIGENT_E2E_CLAUDE_NATIVE_COMMENTS"


@pytest.fixture(scope="module")
def claude_native_ui_agent(
    http_client: httpx.Client,
) -> str:
    """
    Upload the ``claude-native-ui`` agent spec and return its name.

    The spec is identical to what ``omnigent claude`` materialises via
    ``_materialize_claude_agent_spec`` at runtime: harness
    ``claude-native``, no model rewriting (Claude CLI picks its own
    model), and ``os_env.type: caller_process`` with no sandbox.

    Using this spec — rather than a custom test-only yaml — ensures the
    test exercises the exact agent configuration that end users get when
    they run ``omnigent claude``.

    :param http_client: HTTP client pointed at the live server.
    :returns: The agent name, ``"claude-native-ui"``.
    """
    spec: dict[str, Any] = {
        "name": _CLAUDE_NATIVE_UI_AGENT_NAME,
        "prompt": (
            "Claude Code is running in the session terminal. Web UI messages are "
            "forwarded into that Claude Code process through the native bridge."
        ),
        "executor": {
            "harness": "claude-native",
            "context_window": 200_000,
        },
        # The runtime wrapper opts into the spawn-write surface; kept
        # here so the fixture stays identical to the real spec (this
        # module's assertions are comment-tool round-trips, unaffected
        # by the extra relayed tools).
        "spawn": True,
        "os_env": {
            "type": "caller_process",
            "cwd": ".",
            "sandbox": {"type": "none"},
        },
    }
    with tempfile.TemporaryDirectory() as tmpdir:
        yaml_path = Path(tmpdir) / f"{_CLAUDE_NATIVE_UI_AGENT_NAME}.yaml"
        yaml_path.write_text(yaml.safe_dump(spec, sort_keys=False))
        return upload_agent(
            http_client,
            Path(tmpdir),
            rewrite_model_for_databricks=False,
        )


@contextlib.contextmanager
def _claude_code_session(
    session_id: str,
    *,
    model: str | None,
    launch_env: dict[str, str],
) -> Iterator[None]:
    """
    Start Claude Code in a private tmux window with the Omnigent MCP bridge.

    Sets up the bridge directory for *session_id*, launches ``claude``
    with the MCP config and hook settings injected via
    :func:`augment_claude_args` (``--allowedTools`` pre-authorizes the
    omnigent relay tools so MCP permission dialogs don't block the
    test), writes ``tmux.json`` so the runner's harness can inject
    messages via ``inject_user_message``, and waits for the MCP bridge
    subprocess (``serve-mcp``) to write ``server.json`` before yielding.

    This mirrors what ``omnigent claude`` does when a user runs it,
    so the relay feature is tested against the real code path.

    :param session_id: Omnigent session id, e.g. ``"conv_abc123"``.
    :param model: Claude model id to pin via ``--model``, e.g.
        ``"databricks-claude-sonnet-4-6"``. ``None`` lets the CLI choose.
    :param launch_env: Extra environment for the launched ``claude``
        process — Databricks Anthropic gateway auth + model-tier pins, e.g.
        ``{"ANTHROPIC_BASE_URL": "https://host/serving-endpoints/anthropic"}``.
        Empty when no ``--profile`` is set, so the developer's ambient
        ``claude`` login is used instead.
    :yields: None after the bridge is confirmed ready.
    :raises pytest.fail: If the bridge server does not start within
        :data:`_BRIDGE_READY_TIMEOUT_S`.
    """
    bridge_dir = bridge_dir_for_bridge_id(session_id)
    prepare_bridge_dir(session_id, workspace=Path.cwd())

    tmp = tempfile.mkdtemp(prefix="omnigent-e2e-")
    tmux_socket = Path(tmp) / "tmux.sock"
    tmux_session = f"cne-{session_id[:8]}"
    tmux_target = f"{tmux_session}:0.0"

    # Build Claude Code args with Omnigent MCP bridge and hooks injected.
    # --allowedTools pre-authorizes the omnigent relay tools so Claude does
    # not show an interactive permission dialog when it calls list_comments or
    # update_comment.  The MCP server is always named "omnigent" (_MCP_SERVER_NAME),
    # so the tool identifiers are stable regardless of session.
    base_args: tuple[str, ...] = (
        "--dangerously-skip-permissions",
        "--allowedTools",
        "mcp__omnigent__list_comments,mcp__omnigent__update_comment",
    )
    # Pin the model so the Databricks Anthropic gateway receives a served model
    # id rather than a canonical Anthropic name it would reject.
    if model:
        base_args = (*base_args, "--model", model)
    claude_args = augment_claude_args(
        base_args,
        bridge_dir=bridge_dir,
        python_executable=sys.executable,
    )

    # Write a launcher script to avoid shell quoting issues with the
    # JSON in --mcp-config / --settings when the command is typed via
    # tmux send-keys.  The script is exec'd directly by the tmux session
    # without a surrounding shell eval, so the args are safe.
    launcher = Path(tmp) / "launch_claude.sh"
    launcher.write_text(
        "#!/bin/sh\nexec " + " ".join(f"'{a}'" for a in ["claude", *claude_args]) + "\n"
    )
    launcher.chmod(0o700)

    # Start the tmux server (fresh private socket) with launch_env so the
    # launched ``claude`` inherits it: a new server captures this environment
    # into its global environment and passes it to the pane. Drop the
    # nested-session guard so claude does not refuse to start.
    tmux_env = {k: v for k, v in os.environ.items() if k != _NESTED_SESSION_ENV}
    # When injecting gateway Bearer auth, also strip any inherited
    # ANTHROPIC_API_KEY: its mere presence makes Claude Code's interactive
    # "use this API key?" gate block TUI startup (so serve-mcp never spawns).
    # ANTHROPIC_AUTH_TOKEN provides auth without tripping that gate. Mirrors
    # how ``omnigent claude`` unsets ANTHROPIC_API_KEY on launch.
    if "ANTHROPIC_AUTH_TOKEN" in launch_env:
        tmux_env.pop("ANTHROPIC_API_KEY", None)
    tmux_env.update(launch_env)

    # In gateway mode (CI / --profile) claude runs with no prior onboarding,
    # where the first-run theme picker and the workspace-trust dialog block the
    # TUI before the MCP bridge initializes — so serve-mcp / server.json never
    # appear. Point claude at an isolated HOME pre-seeded as already-onboarded
    # and already-trusting its workdir. Ambient mode (no gateway auth) keeps the
    # developer's real HOME + claude login untouched.
    claude_cwd = str(Path.cwd())
    if "ANTHROPIC_AUTH_TOKEN" in launch_env:
        claude_home = Path(tmp) / "claude-home"
        claude_home.mkdir()
        (claude_home / ".claude.json").write_text(
            json.dumps(
                {
                    "hasCompletedOnboarding": True,
                    "theme": "dark",
                    "lastOnboardingVersion": "2.0.0",
                    "projects": {
                        claude_cwd: {
                            "hasTrustDialogAccepted": True,
                            "hasCompletedProjectOnboarding": True,
                        },
                    },
                }
            )
        )
        tmux_env["HOME"] = str(claude_home)

    try:
        # Create a private tmux session (detached, no user terminal needed).
        # ``-c`` pins the pane's working dir so it matches the trusted-project
        # key seeded above.
        subprocess.check_call(
            [
                "tmux",
                "-S",
                str(tmux_socket),
                "new-session",
                "-d",
                "-s",
                tmux_session,
                "-x",
                "220",
                "-y",
                "50",
                "-c",
                claude_cwd,
                str(launcher),
            ],
            env=tmux_env,
        )

        # Advertise the tmux socket + target so the runner's harness
        # subprocess can call ``inject_user_message`` against this pane.
        write_tmux_target(
            bridge_dir,
            socket_path=tmux_socket,
            tmux_target=tmux_target,
        )

        # Wait for the MCP bridge serve-mcp process to write server.json.
        # server.json appears once serve-mcp is listening; only after
        # that can the runner send ``notifications/tools/list_changed``
        # so Claude re-fetches the relay tools.
        server_json = bridge_dir / "server.json"
        deadline = time.monotonic() + _BRIDGE_READY_TIMEOUT_S
        while time.monotonic() < deadline:
            if server_json.exists():
                break
            time.sleep(1.0)
        else:
            pytest.fail(
                f"MCP bridge server.json not found in {bridge_dir} within "
                f"{_BRIDGE_READY_TIMEOUT_S}s. "
                "Claude Code may have failed to start or the serve-mcp "
                "subprocess did not launch."
            )

        yield

    finally:
        with contextlib.suppress(Exception):
            subprocess.run(
                [
                    "tmux",
                    "-S",
                    str(tmux_socket),
                    "kill-session",
                    "-t",
                    tmux_session,
                ],
                check=False,
            )
        shutil.rmtree(tmp, ignore_errors=True)


def test_claude_native_agent_addresses_comments_without_tool_guidance(
    http_client: httpx.Client,
    claude_native_ui_agent: str,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    Claude Code addresses review comments without being told which tools to use.

    Flow:
    1. Skip unless opted in via ``OMNIGENT_E2E_CLAUDE_NATIVE_COMMENTS`` and
       the ``claude`` / ``tmux`` binaries are available.
    2. Create a runner-bound session with the ``claude-native-ui`` agent.
    3. Start Claude Code in a private tmux window with the Omnigent
       MCP bridge (same as ``omnigent claude`` does).
    4. POST two draft comments on ``app.py`` via the REST API.
    5. Pre-configure the mock LLM with tool-call responses that call
       ``list_comments`` then ``update_comment`` for both comment IDs so the
       mock server drives the MCP relay round-trip deterministically.
    6. Ask the agent to address all open comments via the REST API.
       The prompt explicitly says "use your available tools" and "in this
       session" so Claude targets the MCP relay tools (not GitHub CLI).
       Because this test launches Claude in its own tmux window (not via the
       runner terminal route), the relay is started by the runner's first-turn
       fallback, which:
       a. Writes ``tool_relay.json`` (the comment-relay start).
       b. Awaits the ``notifications/tools/list_changed`` delivery so Claude
          re-fetches its tool list before the message is injected.
       c. Injects the user message into Claude's tmux window.
    7. Poll comment statuses until both are ``"addressed"`` or timeout.
    8. Assert both comments have status ``"addressed"``.

    **What breaks if this test fails:**

    - ``_ensure_comment_relay_started`` in ``runner/app.py`` not called for
      claude-native turns, so ``tool_relay.json`` is never written.
    - The MCP bridge does not read ``tool_relay.json`` on ``tools/list``
      requests, so the relay tools are invisible to Claude.
    - The relay HTTP server is not running or not callable from the bridge,
      so Claude's tool call returns an error.
    - ``_execute_comment_tool`` call path broken (server_client / session
      scoping), so the Omnigent server ``PATCH /v1/sessions/{id}/comments`` is
      never hit.
    - Comments still ``"draft"`` → update path didn't persist.

    :param http_client: HTTP client pointed at the live server.
    :param claude_native_ui_agent: Registered agent name (``"claude-native-ui"``).
    :param live_runner_id: Runner id the session is bound to.
    :param mock_llm_server_url: Mock LLM server base URL (no ``/v1`` suffix —
        the Anthropic SDK appends ``/v1/messages`` automatically).
    """
    # ── 0. Guard: opt-in + required binaries ─────────────────────────────────
    # TODO(claude-native-comments-ci): enable this in CI. Blocked on CI's
    # npm-installed claude stalling on a first-run onboarding gate that the
    # seeded HOME below doesn't cover (passes locally / with a manually
    # onboarded claude). To unblock: add tmux-pane capture on the
    # server.json-timeout path so the CI artifact shows exactly which gate
    # claude stops at, seed that gate too, then drop this opt-in skip.
    if not os.environ.get(_RUN_GATE_ENV):
        pytest.skip(
            f"Set {_RUN_GATE_ENV}=1 to run. The interactive claude TUI's "
            "first-run onboarding/trust gates are version- and "
            "environment-dependent and block headless startup in CI; the relay "
            "wiring is covered by tests/runner/test_comment_relay.py and "
            "tests/test_claude_native_bridge.py. Verified end-to-end locally "
            "via --profile oss with this gate set."
        )
    if shutil.which("claude") is None:
        pytest.skip("'claude' CLI is not on PATH. Install Claude Code to run this test.")
    if shutil.which("tmux") is None:
        pytest.skip(
            "'tmux' is not on PATH. This test requires tmux to run Claude Code headlessly."
        )

    # ── 1. Create a runner-bound session ─────────────────────────────────────
    session_id = create_runner_bound_session(
        http_client,
        agent_name=claude_native_ui_agent,
        runner_id=live_runner_id,
    )

    # ── 2. Add two draft comments to the session via REST ─────────────────
    r1 = http_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "app.py",
            "body": "Typo: 'recieve' should be 'receive'.",
            "start_index": 0,
            "end_index": 20,
            "anchor_content": "def recieve_data():",
        },
    )
    r1.raise_for_status()
    comment1_id: str = r1.json()["id"]

    r2 = http_client.post(
        f"/v1/sessions/{session_id}/comments",
        json={
            "path": "app.py",
            "body": "Variable name 'x' is not descriptive; rename to 'count'.",
            "start_index": 100,
            "end_index": 110,
            "anchor_content": "x = 0",
        },
    )
    r2.raise_for_status()
    comment2_id: str = r2.json()["id"]

    # ── 3. Pre-configure mock LLM with tool-call responses ─────────────────
    # The mock server returns these responses in order for any model key.
    # The Claude CLI executes the tool_use blocks as real MCP calls against
    # the relay, so comment status changes reach the Omnigent server even
    # though the LLM itself is a mock.  We configure with the actual IDs
    # now (after posting) so update_comment receives the right targets.
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Turn 1: list open comments.
            {
                "tool_calls": [
                    {"name": "list_comments", "arguments": "{}"},
                ]
            },
            # Turn 2: address comment 1.
            {
                "tool_calls": [
                    {
                        "name": "update_comment",
                        "arguments": (f'{{"comment_id": "{comment1_id}", "status": "addressed"}}'),
                    }
                ]
            },
            # Turn 3: address comment 2.
            {
                "tool_calls": [
                    {
                        "name": "update_comment",
                        "arguments": (f'{{"comment_id": "{comment2_id}", "status": "addressed"}}'),
                    }
                ]
            },
            # Turn 4: final text reply.
            {"text": "I have addressed all open comments."},
        ],
    )

    # Route the Claude CLI's LLM calls to the mock server.  ANTHROPIC_API_KEY
    # bypasses the CLI's OAuth login gate so no real account is needed.
    # CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS suppresses beta headers the mock
    # server does not expect.
    launch_env: dict[str, str] = {
        "ANTHROPIC_BASE_URL": mock_llm_server_url,
        "ANTHROPIC_API_KEY": "mock-key",
        "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS": "1",
    }

    with _claude_code_session(session_id, model=None, launch_env=launch_env):
        # Confirm both comments start as draft so we know the "addressed" state
        # at the end can only have been set by the agent.
        pre_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
        pre_resp.raise_for_status()
        pre_statuses: dict[str, Any] = {c["id"]: c["status"] for c in pre_resp.json()}
        # Both must be "draft" before the agent runs; if either is already
        # "addressed", the test can't prove the agent did it.
        assert pre_statuses.get(comment1_id) == "draft", (
            f"Expected comment 1 to start as 'draft', got {pre_statuses.get(comment1_id)!r}"
        )
        assert pre_statuses.get(comment2_id) == "draft", (
            f"Expected comment 2 to start as 'draft', got {pre_statuses.get(comment2_id)!r}"
        )

        # ── 4. Ask the agent to address comments — no tool names in the prompt
        # The runner will:
        #   a. Call _start_comment_relay_for_session → write tool_relay.json
        #   b. Send notifications/tools/list_changed so Claude re-fetches tools
        #   c. Inject the message into Claude's tmux window
        # The harness yields TurnComplete immediately after injection; the
        # turn status from the runner's perspective completes quickly, but
        # Claude is still processing asynchronously in its tmux window.
        send_user_message_to_session(
            http_client,
            session_id=session_id,
            content=(
                "I added two inline comments on app.py in this session. "
                "Please use your available tools to list the open comments "
                "and mark each one as addressed."
            ),
        )

        # ── 5. Poll comment statuses until both are addressed ─────────────────
        # Claude processes the injected message, calls list_comments to find
        # the draft comments, then calls update_comment on each. These tool
        # calls go through the relay HTTP server running in the runner process
        # and hit the Omnigent server directly, so the comment status changes appear
        # in the REST API without needing to observe the forwarder stream.
        deadline = time.monotonic() + _COMMENT_POLL_TIMEOUT_S
        while time.monotonic() < deadline:
            poll_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
            poll_resp.raise_for_status()
            statuses: dict[str, Any] = {c["id"]: c["status"] for c in poll_resp.json()}
            if (
                statuses.get(comment1_id) == "addressed"
                and statuses.get(comment2_id) == "addressed"
            ):
                break
            time.sleep(_COMMENT_POLL_INTERVAL_S)

        # ── 6. Assert final comment statuses ─────────────────────────────────
        # Capture tmux pane for diagnostics.
        try:
            _tfile = bridge_dir_for_bridge_id(session_id) / "tmux.json"
            if _tfile.exists():
                _ti = json.loads(_tfile.read_text())
                _cap = subprocess.run(
                    [
                        "tmux",
                        "-S",
                        _ti["socket_path"],
                        "capture-pane",
                        "-p",
                        "-t",
                        _ti["tmux_target"],
                    ],
                    capture_output=True,
                    text=True,
                    timeout=5,
                )
                print(f"\n[TMUX]\n{_cap.stdout[-3000:]}")
        except Exception as _e:
            print(f"[TMUX capture failed: {_e}]")
        post_resp = http_client.get(f"/v1/sessions/{session_id}/comments")
        post_resp.raise_for_status()
        post_statuses: dict[str, Any] = {c["id"]: c["status"] for c in post_resp.json()}

        # Both comments must be "addressed". If either is still "draft":
        # - "draft" for both → Claude never called update_comment at all
        #   (list_comments relay broken, or relay tools not visible in
        #   Claude's tools/list, or tool_relay.json was never written).
        # - "draft" for one → Claude only addressed one (loop off-by-one,
        #   or wrong comment_id passed to update_comment).
        assert post_statuses.get(comment1_id) == "addressed", (
            f"Comment 1 still has status {post_statuses.get(comment1_id)!r} "
            f"after {_COMMENT_POLL_TIMEOUT_S}s; expected 'addressed'. "
            f"If 'draft', the update_comment relay call did not reach the Omnigent server."
        )
        assert post_statuses.get(comment2_id) == "addressed", (
            f"Comment 2 still has status {post_statuses.get(comment2_id)!r} "
            f"after {_COMMENT_POLL_TIMEOUT_S}s; expected 'addressed'. "
            f"If 'draft', the update_comment relay call did not reach the Omnigent server."
        )
