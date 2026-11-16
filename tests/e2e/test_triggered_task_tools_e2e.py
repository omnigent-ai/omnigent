"""An agent sets up a Jira trigger with the sys_triggered_task_* tools.

Real local host, server and harness processes (an SDK harness, and Claude Code
and Codex through the native tool relay) with a scripted model, no cloud
sandbox. The agent's create and update wait for the owner's approval, the saved
automation is the one the web form's trigger template saves, and Run now sends
its poller the rendered prompt without the form's header.
"""

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
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.tools.builtins.trigger_prompts import decode_trigger_prompt
from tests._helpers.mock_llm import advertised_tool_names
from tests._helpers.native_session import create_native_session
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle, send_user_text
from tests.e2e._native_resume_helpers import poll_for_pending_elicitation, resolve_elicitation
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_TRIGGER_OPENING = "This automation watches Jira and starts one Omnigent session"
# The mock serves the longest marker found in the conversation, even an exhausted one,
# so each step's marker extends the previous step's.
_CREATE = "TRIGGER_CREATE"
_LIST = f"{_CREATE}_LIST"
_PAUSE = f"{_LIST}_PAUSE"
_DELETE = f"{_PAUSE}_DELETE"
_PROBE = "RELAY_TOOLS_PROBE: acknowledge readiness."
_MODELS = {
    "openai-agents": "gpt-4o",
    "claude-native": "claude-sonnet-4-20250514",
    "codex-native": "gpt-4o",
}
_BINARIES = {"claude-native": "claude", "codex-native": "codex"}
_TOOLS = (
    "sys_triggered_task_create",
    "sys_triggered_task_list",
    "sys_triggered_task_update",
    "sys_triggered_task_delete",
)
# Nobody watches an automation's turn, so Codex's own default would park on its
# first Omnigent tool call; in approve-for-me the mocked reviewer approves it.
_TASK_PERMISSION_MODES = {
    "openai-agents": None,
    "claude-native": None,
    "codex-native": "approve-for-me",
}
# A tool every poller turn advertises, so other requests can't take its scripted reply.
_POLLER_TOOLS = {
    "openai-agents": "sys_session_create",
    "claude-native": "Bash",
    "codex-native": "mcp__omnigent",
}


def _tool_output(request: dict, call_id: str) -> str | None:
    """The tool result a model request carries for *call_id*, in any provider's shape."""
    for entry in request.get("input", request.get("messages", [])):
        if not isinstance(entry, dict):
            continue
        if (entry.get("type") == "function_call_output" and entry.get("call_id") == call_id) or (
            entry.get("role") == "tool" and entry.get("tool_call_id") == call_id
        ):
            output = entry.get("output", entry.get("content"))
            return output if isinstance(output, str) else json.dumps(output)
        content = entry.get("content")
        for block in content if isinstance(content, list) else []:
            if (
                isinstance(block, dict)
                and block.get("type") == "tool_result"
                and block.get("tool_use_id") == call_id
            ):
                output = block.get("content")
                return output if isinstance(output, str) else json.dumps(output)
    return None


@pytest.mark.timeout(360)
@pytest.mark.parametrize("harness", ["openai-agents", "claude-native", "codex-native"])
def test_agent_creates_runs_changes_and_deletes_a_jira_trigger(
    tmp_path, isolated_mock_llm_server_url, harness
):
    binary = _BINARIES.get(harness)
    if binary is not None and any(shutil.which(cmd) is None for cmd in (binary, "tmux")):
        pytest.skip(f"requires the real {binary} executable and tmux")
    if harness == "claude-native" and any(p.is_file() for p in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip(
            "machine-managed Claude settings override mock auth; use an isolated container"
        )
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    write_model_config(config, mock, _MODELS["claude-native"], _MODELS["codex-native"])
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    with tempfile.TemporaryDirectory(prefix="trigger-native-", dir="/tmp") as native_state:
        env = {
            "OMNIGENT_CONFIG_HOME": str(config),
            "OMNIGENT_CODEX_NATIVE_STATE_DIR": native_state,
            "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
        }
        with server_runner(
            tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
        ) as stack:
            stack.start_host(env=env, cwd=_REPO)
            with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
                assert client.get("/v1/info").json()["triggered_task_tools"] is True
                poll_until(
                    lambda: [
                        h
                        for h in client.get("/v1/hosts").json().get("hosts", [])
                        if h.get("status") == "online"
                    ],
                    timeout_s=45,
                )
                host_id = client.get("/v1/hosts").json()["hosts"][0]["host_id"]
                set_fallback_mock_llm(mock, key="default", text="Acknowledged.")
                set_fallback_mock_llm(
                    mock, key="_policy_llm_", text='{"action":"allow","reason":""}'
                )
                set_fallback_mock_llm(
                    mock,
                    key="codex-auto-review",
                    text=(
                        '{"risk_level":"low","user_authorization":"high",'
                        '"outcome":"allow","rationale":"Isolated test"}'
                    ),
                )

                def required_tool(name):
                    # Native CLIs see Omnigent's tools through the relay's MCP server.
                    return {
                        "claude-native": f"mcp__omnigent__{name}",
                        "codex-native": "mcp__omnigent",
                    }.get(harness, name)

                def tool(name, args, call_id):
                    if harness == "claude-native":
                        name = f"mcp__omnigent__{name}"
                    result = {"name": name, "arguments": json.dumps(args), "call_id": call_id}
                    if harness == "codex-native":
                        result["namespace"] = "mcp__omnigent"
                    return result

                def script(marker, name, args, call_id, reply):
                    configure_mock_llm(
                        mock,
                        [{"tool_calls": [tool(name, args, call_id)]}, {"text": reply}],
                        match=marker,
                        required_tools=[required_tool(name)],
                    )

                def result_of(call_id):
                    def output():
                        return next(
                            (
                                found
                                for request in get_mock_requests(mock)
                                if (found := _tool_output(request, call_id)) is not None
                            ),
                            None,
                        )

                    poll_until(output, timeout_s=90)
                    # Native relays wrap the tool's JSON in their own content blocks,
                    # so callers check for values rather than parse it.
                    return output()

                def items(session):
                    response = client.get(f"/v1/sessions/{session}/items", params={"limit": 100})
                    response.raise_for_status()
                    return response.json()["data"]

                def wait_text(session, text):
                    poll_until(lambda: text in json.dumps(items(session)), timeout_s=90)

                def send(session, text):
                    send_user_text(client, session_id=session, text=text)

                def wait_for_relay_tools(session, name):
                    # Native CLIs can take input before their MCP connection finishes discovery.
                    send(session, _PROBE)
                    seen = 0

                    def ready():
                        nonlocal seen
                        snapshot = client.get(f"/v1/sessions/{session}").json()
                        if snapshot["status"] != "idle" or snapshot.get("pending_inputs"):
                            return False
                        requests = [r for r in get_mock_requests(mock) if _PROBE in json.dumps(r)]
                        if name in advertised_tool_names(requests):
                            return True
                        if len(requests) > seen:
                            seen = len(requests)
                            send(session, _PROBE)
                        return False

                    poll_until(ready, timeout_s=90)

                def approval_card(session, needle):
                    return poll_for_pending_elicitation(
                        client, conversation_id=session, timeout=90, needle=needle
                    )

                def approve(session, card):
                    resolve_elicitation(
                        client, conversation_id=session, elicitation_id=str(card["elicitation_id"])
                    )

                metadata = {
                    "title": "Owner",
                    "host_id": host_id,
                    "workspace": str(stack.workspace),
                }
                # The owner's CLI mustn't stop for its own tool prompts, which a person
                # would answer in its terminal; Omnigent's approval of the call still applies.
                if harness == "codex-native":
                    metadata["terminal_launch_args"] = PERMISSION_LAUNCH_ARGS["codex-native"][
                        "approve-for-me"
                    ]
                if harness == "claude-native":
                    claude_config = Path(env["CLAUDE_CONFIG_DIR"])
                    claude_config.mkdir(parents=True, exist_ok=True)
                    allow = [f"mcp__omnigent__{name}" for name in _TOOLS]
                    claude_config.joinpath("settings.json").write_text(
                        json.dumps({"permissions": {"allow": allow}})
                    )
                if harness in _BINARIES:
                    created = create_native_session(
                        client,
                        stack.base_url,
                        harness=_BINARIES[harness],
                        model=_MODELS[harness] if harness == "codex-native" else None,
                        metadata=metadata,
                    )
                else:
                    spec = {
                        "name": "owner",
                        "prompt": "Manage the owner's automations.",
                        # The trigger's poller runs this agent and starts sessions for matches.
                        "spawn": True,
                        "executor": {
                            "harness": harness,
                            "model": _MODELS[harness],
                            "auth": {
                                "type": "api_key",
                                "api_key": "mock-key",
                                "base_url": f"{mock}/v1",
                            },
                        },
                        "skills_filter": "none",
                    }
                    response = post_session_bundle(
                        client.post,
                        "/v1/sessions",
                        bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
                        metadata=metadata,
                    )
                    response.raise_for_status()
                    created = response.json()
                owner = created["session_id"]
                agent = client.get(f"/v1/sessions/{owner}").json()["agent_id"]
                permission_mode = _TASK_PERMISSION_MODES[harness]
                if harness != "openai-agents":
                    wait_for_relay_tools(owner, required_tool("sys_triggered_task_create"))

                trigger = {
                    "name": "Triage ES",
                    "provider": "jira",
                    "scope": ["ES"],
                    "conditions": [{"field": "label", "operator": "is", "value": "bug"}],
                    "instructions": "Investigate the ticket and propose a fix.",
                    "agent_id": agent,
                    "permission_mode": permission_mode,
                    "host_id": host_id,
                    "workspace": str(stack.workspace),
                }
                script(_CREATE, "sys_triggered_task_create", trigger, "create", "TRIGGER_CREATED")
                send(owner, _CREATE)
                # The owner sees the whole trigger, and nothing is saved until they approve.
                card = approval_card(owner, "triggered task 'Triage ES'")
                assert 'provider: \\"jira\\"' in json.dumps(card)
                assert "Investigate the ticket and propose a fix." in json.dumps(card)
                assert client.get("/v1/scheduled-tasks").json()["scheduled_tasks"] == []
                approve(owner, card)
                wait_text(owner, "TRIGGER_CREATED")
                [task] = client.get("/v1/scheduled-tasks").json()["scheduled_tasks"]
                task_id = task["id"]
                assert task_id in result_of("create")

                assert task["session_mode"] == "continue"
                assert task["rrule"] in ("FREQ=MINUTELY;INTERVAL=10", "FREQ=HOURLY;BYMINUTE=0")
                saved = decode_trigger_prompt(task["prompt"])
                assert saved is not None
                assert (saved["source"], saved["scope"]) == ("jira", "ES")
                assert saved["conditions"] == trigger["conditions"]

                # Run now: the poller gets the rendered prompt; the header stays out.
                configure_mock_llm(
                    mock,
                    [{"text": "TRIGGER_POLLED"}],
                    match=_TRIGGER_OPENING,
                    required_tools=[_POLLER_TOOLS[harness]],
                )
                response = client.post(f"/v1/scheduled-tasks/{task_id}/run")
                assert response.status_code == 202, response.text

                def last_run_session():
                    return (
                        client.get(f"/v1/scheduled-tasks/{task_id}")
                        .json()
                        .get("last_run_conversation_id")
                    )

                poll_until(last_run_session, timeout_s=90)
                wait_text(last_run_session(), "TRIGGER_POLLED")
                polled = [
                    json.dumps(request)
                    for request in get_mock_requests(mock)
                    if _TRIGGER_OPENING in json.dumps(request)
                ]
                assert polled
                assert all("omnigent-trigger v1" not in request for request in polled)

                script(_LIST, "sys_triggered_task_list", {}, "list", "TRIGGERS_LISTED")
                send(owner, _LIST)
                wait_text(owner, "TRIGGERS_LISTED")
                listed = result_of("list")
                assert task_id in listed and trigger["instructions"] in listed
                # The list reports the trigger, not the prompt or schedule it renders to.
                assert _TRIGGER_OPENING not in listed and "rrule" not in listed

                change = {
                    "triggered_task_id": task_id,
                    "instructions": trigger["instructions"],
                    "permission_mode": permission_mode,
                    "conditions": [{"field": "priority", "operator": "is", "value": "High"}],
                    "state": "paused",
                }
                script(_PAUSE, "sys_triggered_task_update", change, "update", "TRIGGER_PAUSED")
                send(owner, _PAUSE)
                approve(owner, approval_card(owner, f"triggered task {task_id}"))
                wait_text(owner, "TRIGGER_PAUSED")
                task = client.get(f"/v1/scheduled-tasks/{task_id}").json()
                assert task["state"] == "paused"
                saved = decode_trigger_prompt(task["prompt"])
                assert saved is not None
                assert saved["conditions"] == change["conditions"]

                remove = {"triggered_task_id": task_id}
                script(_DELETE, "sys_triggered_task_delete", remove, "delete", "TRIGGER_DELETED")
                send(owner, _DELETE)
                wait_text(owner, "TRIGGER_DELETED")
                assert "deleted" in result_of("delete")
                assert client.get(f"/v1/scheduled-tasks/{task_id}").status_code == 404
