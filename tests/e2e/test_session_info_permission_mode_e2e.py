"""An agent reads the permission mode a session is in with ``sys_session_get_info``.

Real local server, host, runners, Codex and Claude Code processes; only the
reading agent's model is scripted. Codex and Claude Code report and switch modes
in their own terminals without calling a model. The web picker switches each
one mid-session, and the reader must then see the new mode. The reader runs a
harness without permission modes, so its own mode reads as null.
"""

import ast
import json
import os
import shutil
import tempfile
from pathlib import Path

import httpx
import pytest
import yaml

from dev.repro_env.runtime import write_model_config
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle, send_user_text
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_CLAUDE_MODE_LABEL_KEY = "omnigent.claude_native.permission_mode"
# The second marker extends the first, so the mock routes the resent
# conversation to the newer turn.
_READ_TASK = "READ_PERMISSION_MODES"
_REREAD_TASK = f"{_READ_TASK}_AFTER_SWITCH"


@pytest.mark.posix_only
@pytest.mark.timeout(300)
def test_session_info_reports_the_mode_each_session_is_in(tmp_path, isolated_mock_llm_server_url):
    if any(shutil.which(cmd) is None for cmd in ("claude", "codex", "tmux")):
        pytest.skip("requires Claude Code, Codex and tmux")
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    write_model_config(config, mock, "claude-sonnet-4-20250514", "gpt-4o")
    claude_config = tmp_path / "claude-config"
    claude_config.mkdir()
    base_env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TMPDIR"}}
    with tempfile.TemporaryDirectory(prefix="info-mode-", dir="/tmp") as native_state:
        env = {
            "OMNIGENT_CONFIG_HOME": str(config),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": native_state,
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "CLAUDE_CONFIG_DIR": str(claude_config),
            "ANTHROPIC_BASE_URL": mock,
            "ANTHROPIC_API_KEY": "local-test-key",
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_AUTOUPDATER": "1",
            "TERM": "xterm-256color",
        }
        with server_runner(
            tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
        ) as stack:
            claude_config.joinpath(".claude.json").write_text(
                json.dumps(
                    {
                        "hasCompletedOnboarding": True,
                        "customApiKeyResponses": {"approved": ["local-test-key"]},
                        "projects": {str(stack.workspace): {"hasTrustDialogAccepted": True}},
                    }
                )
            )
            stack.start_host(env=env, cwd=_REPO)
            with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
                set_fallback_mock_llm(
                    mock, key="_policy_llm_", text='{"action":"allow","reason":""}'
                )
                placement = {"host_id": stack.host_id, "workspace": str(stack.workspace)}
                reader_spec = {
                    "name": "mode-reader",
                    "prompt": "Report session settings.",
                    "executor": {
                        "harness": "openai-agents",
                        "model": "gpt-4o",
                        "auth": {
                            "type": "api_key",
                            "api_key": "mock-key",
                            "base_url": f"{mock}/v1",
                        },
                    },
                    "skills_filter": "none",
                }
                created = post_session_bundle(
                    client.post,
                    "/v1/sessions",
                    bundle_files({"agent.yaml": yaml.safe_dump(reader_spec).encode()}),
                    metadata={"title": "Reader", **placement},
                )
                created.raise_for_status()
                reader = created.json()["session_id"]
                codex_launch_args = PERMISSION_LAUNCH_ARGS["codex-native"]["approve-for-me"]
                codex = create_native_session(
                    client,
                    stack.base_url,
                    harness="codex",
                    model="gpt-4o",
                    metadata={
                        "title": "Codex",
                        "terminal_launch_args": codex_launch_args,
                        **placement,
                    },
                )["session_id"]
                claude = create_native_session(
                    client,
                    stack.base_url,
                    harness="claude",
                    metadata={"title": "Claude", **placement},
                )["session_id"]

                def session(session_id):
                    response = client.get(f"/v1/sessions/{session_id}")
                    response.raise_for_status()
                    return response.json()

                def tool_output(call_id):
                    outputs = [
                        entry.get("output", entry.get("content"))
                        for request in get_mock_requests(mock)
                        for entry in request.get("input", request.get("messages", []))
                        if isinstance(entry, dict)
                        and (
                            (
                                entry.get("type") == "function_call_output"
                                and entry.get("call_id") == call_id
                            )
                            or (
                                entry.get("role") == "tool"
                                and entry.get("tool_call_id") == call_id
                            )
                        )
                    ]
                    assert outputs, f"no output for {call_id}"
                    # The agent loop hands the model the tool's result as a Python repr.
                    return ast.literal_eval(outputs[-1])

                def read_modes(marker, targets):
                    done = f"{marker}_DONE"
                    calls = [
                        {
                            "name": "sys_session_get_info",
                            "arguments": json.dumps(args),
                            "call_id": f"{marker}-{name}",
                        }
                        for name, args in targets.items()
                    ]
                    configure_mock_llm(
                        mock,
                        [{"tool_calls": calls}, {"text": done}],
                        match=marker,
                        required_tools=["sys_session_get_info"],
                    )
                    send_user_text(client, session_id=reader, text=marker)

                    def answered():
                        response = client.get(
                            f"/v1/sessions/{reader}/items", params={"limit": 100}
                        )
                        return response.status_code == 200 and done in response.text

                    poll_until(answered, timeout_s=90)
                    return {name: tool_output(f"{marker}-{name}") for name in targets}

                def switch(session_id, change):
                    # The live terminal applies a switch, so retry until it is up to it.
                    attempts = []

                    def applied():
                        response = client.patch(f"/v1/sessions/{session_id}", json=change)
                        attempts.append(f"{response.status_code}: {response.text[:300]}")
                        return response.status_code == 200

                    try:
                        poll_until(applied, timeout_s=90, interval_s=2)
                    except AssertionError as error:
                        error.add_note(f"last PATCH: {attempts[-1:]}")
                        raise

                # Claude has read its mode off its own footer.
                poll_until(
                    lambda: session(claude)["labels"].get(_CLAUDE_MODE_LABEL_KEY), timeout_s=90
                )
                before = read_modes(
                    _READ_TASK,
                    {"own": {}, "codex": {"session_id": codex}, "claude": {"session_id": claude}},
                )
                # The reader described each session it asked about.
                assert {name: info["session_id"] for name, info in before.items()} == {
                    "own": reader,
                    "codex": codex,
                    "claude": claude,
                }

                # The web picker switches each session while it runs. Codex offers
                # Read Only on Linux only with a permission profile, so use a preset
                # every platform lists.
                switch(codex, {"approval_mode": "ask-for-approval"})
                switch(claude, {"permission_mode": "acceptEdits"})
                after = read_modes(
                    _REREAD_TASK,
                    {"codex": {"session_id": codex}, "claude": {"session_id": claude}},
                )
                # Codex's launch args still name the mode it started in; only its
                # reported mode moved.
                assert session(codex)["terminal_launch_args"] == codex_launch_args
                seen = {
                    f"{name} {phase}": info.get("permission_mode", "<absent>")
                    for phase, reads in (("before", before), ("after", after))
                    for name, info in reads.items()
                }
                assert seen == {
                    "own before": None,
                    "codex before": "approve-for-me",
                    "claude before": "default",
                    "codex after": "ask-for-approval",
                    "claude after": "acceptEdits",
                }
