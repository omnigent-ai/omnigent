"""E2E: the opt-in session file tools work on every path agents use.

The server, runner, harness subprocesses, native CLIs, host, MCP proxy and the
session file store are real. Only the model endpoint is scripted, so each
journey's tool calls are deterministic. An agent turns ``sys_files_upload``,
``sys_files_list`` and ``sys_files_download`` on with ``tool_groups.files:
true``; an agent that doesn't is offered none of them.

Run: uv run --no-sync pytest tests/e2e/test_session_file_tools_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from dev.repro_env.runtime import write_model_config
from omnigent.native.permission_modes import PERMISSION_LAUNCH_ARGS
from omnigent.onboarding.ambient import CLAUDE_CODE_MANAGED_SETTINGS_PATHS
from omnigent.runner.identity import OMNIGENT_INTERNAL_WS_ORIGIN
from tests._helpers.mock_llm import advertised_tool_names
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import (
    configure_mock_llm,
    get_mock_requests,
    poll_session_until_terminal,
    send_user_message_to_session,
    set_fallback_mock_llm,
)
from tests.e2e.routing._helpers import wait_for

_REPO = Path(__file__).resolve().parents[2]
_FILE_TOOLS = {"sys_files_upload", "sys_files_list", "sys_files_download"}
_OLD_NAMES = {"upload_file", "list_files", "download_file"}
_FILES_ON = {"files": True}
_OS_ENV = {"type": "caller_process", "cwd": ".", "sandbox": {"type": "none"}}
_NATIVE_MODELS = {"claude": "claude-sonnet-4-20250514", "codex": "gpt-4o"}
_SYSTEM_CODEX_CONFIG = Path("/etc/codex/managed_config.toml")


def _stack_base_env() -> dict[str, str]:
    """The ambient variables an isolated stack keeps; nothing else is inherited."""
    return {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }


def _session_files(client: httpx.Client, session_id: str) -> dict[str, str]:
    """Map each stored session file's name to its id."""
    response = client.get(f"/v1/sessions/{session_id}/resources/files")
    response.raise_for_status()
    return {entry["name"]: entry["id"] for entry in response.json()["data"]}


def _file_bytes(client: httpx.Client, session_id: str, file_id: str) -> bytes:
    """Download a stored session file's content."""
    response = client.get(f"/v1/sessions/{session_id}/resources/files/{file_id}/content")
    response.raise_for_status()
    return response.content


def _outputs_by_call_id(body: dict[str, Any]) -> dict[str, str]:
    """Map each function call's id to the tool output the turn recorded."""
    return {
        item["call_id"]: item.get("output", "")
        for item in body.get("output", [])
        if item.get("type") == "function_call_output"
    }


def _mcp_tool_call(
    client: httpx.Client, session_id: str, name: str, arguments: dict[str, Any]
) -> str:
    """
    Call a tool through the session's MCP proxy, the hop every native CLI call takes.

    :param client: Client for the server.
    :param session_id: Calling session, e.g. ``"conv_abc123"``.
    :param name: Tool name, e.g. ``"sys_files_upload"``.
    :param arguments: Tool arguments, e.g. ``{"path": "notes.txt"}``.
    :returns: The tool's text result, or the JSON-RPC error message.
    """
    response = client.post(
        f"/v1/sessions/{session_id}/mcp",
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments},
        },
    )
    response.raise_for_status()
    body = response.json()
    if "error" in body:
        return f"error: {body['error']['message']}"
    return "".join(block.get("text", "") for block in body["result"]["content"])


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
    """
    Script one mock model response that calls a single tool.

    :param call_id: The call's id, e.g. ``"upload"``.
    :param name: Tool name, e.g. ``"sys_files_upload"``.
    :param arguments: Tool arguments, e.g. ``{"path": "report.md"}``.
    :returns: A mock response entry for :func:`configure_mock_llm`.
    """
    return {"tool_calls": [{"call_id": call_id, "name": name, "arguments": json.dumps(arguments)}]}


def _model_session(
    client: httpx.Client,
    *,
    runner_id: str,
    mock_llm_server_url: str,
    model: str,
    spec_overrides: dict[str, Any],
    metadata: dict[str, Any] | None = None,
) -> str:
    """
    Create a runner-bound openai-agents session from a ``config.yaml`` bundle.

    The ``spec_version: 1`` bundle form is the one whose parser reads
    ``tools.builtins``; its model calls go to the mock endpoint.

    :param client: Client for the server.
    :param runner_id: The runner to bind, e.g. ``"runner_abc"``.
    :param mock_llm_server_url: Mock model server URL, without ``/v1``.
    :param model: Model name, also the mock queue key, e.g. ``"mock-files-1a2b"``.
    :param spec_overrides: Top-level spec keys to add, e.g. ``{"os_env": {...}}``.
    :param metadata: Session-create metadata, e.g. ``{"workspace": "/repo"}``.
    :returns: The session id.
    """
    config = {
        "spec_version": 1,
        "name": model,
        "prompt": "Share files with the user when asked.",
        "executor": {
            "type": "omnigent",
            "model": model,
            "config": {"harness": "openai-agents"},
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": f"{mock_llm_server_url}/v1",
            },
        },
        **spec_overrides,
    }
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({"config.yaml": yaml.safe_dump(config).encode()}),
        metadata=metadata,
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    created.raise_for_status()
    session_id = created.json()["session_id"]
    bind_session_runner(client.patch, "", session_id, runner_id)
    return session_id


def test_model_turn_shares_and_reads_session_files_with_the_file_tools_on(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
) -> None:
    """
    An agent that sets ``tool_groups.files: true`` is offered the three system
    tools. Its turn writes a report with ``sys_os_write``, uploads it and a file
    the user put in the session's filesystem, lists the session's files, and
    reads back the user's attachment as text. The shared runner has no
    workspace, so the two uploads come from the session's two default directories.
    """
    model = f"mock-files-{uuid.uuid4().hex[:8]}"
    session_id = _model_session(
        http_client,
        runner_id=live_runner_id,
        mock_llm_server_url=mock_llm_server_url,
        model=model,
        spec_overrides={"os_env": _OS_ENV, "tool_groups": _FILES_ON},
    )
    attachment = http_client.post(
        f"/v1/sessions/{session_id}/resources/files",
        files={"file": ("brief.txt", b"brief from the user", "text/plain")},
        headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
    )
    attachment.raise_for_status()
    brief_id = attachment.json()["id"]
    panel_write = http_client.put(
        f"/v1/sessions/{session_id}/resources/environments/default/filesystem/notes.txt",
        json={"content": "notes from the files panel", "encoding": "utf-8"},
    )
    panel_write.raise_for_status()
    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool_call(
                "write", "sys_os_write", {"path": "out/report.md", "content": "agent report"}
            ),
            _tool_call("upload_report", "sys_files_upload", {"path": "out/report.md"}),
            _tool_call("upload_notes", "sys_files_upload", {"path": "notes.txt"}),
            _tool_call("list", "sys_files_list", {}),
            _tool_call("download", "sys_files_download", {"file_id": brief_id}),
            {"text": "Shared your files."},
        ],
        key=model,
        required_tools=["sys_files_upload"],
    )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content="Share the report and the notes."
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=120
    )

    advertised = advertised_tool_names(get_mock_requests(mock_llm_server_url, key=model))
    assert advertised >= _FILE_TOOLS, sorted(advertised)
    assert not advertised & _OLD_NAMES, sorted(advertised)
    assert body["status"] == "completed", (body.get("status"), body.get("error"))
    outputs = _outputs_by_call_id(body)
    assert json.loads(outputs["upload_report"]).get("filename") == "report.md", outputs
    assert json.loads(outputs["upload_notes"]).get("filename") == "notes.txt", outputs
    assert {"report.md", "notes.txt", "brief.txt"} <= {
        entry["name"] for entry in json.loads(outputs["list"])["data"]
    }, outputs["list"]
    assert outputs["download"] == "brief from the user", outputs
    stored = _session_files(http_client, session_id)
    assert _file_bytes(http_client, session_id, stored["report.md"]) == b"agent report"
    assert _file_bytes(http_client, session_id, stored["notes.txt"]) == (
        b"notes from the files panel"
    )


@pytest.mark.parametrize(
    ("spec_overrides", "expected"),
    [
        ({}, set()),
        ({"tool_groups": _FILES_ON}, _FILE_TOOLS),
        ({"tool_groups": {"files": False}}, set()),
        ({"tools": {"builtins": sorted(_OLD_NAMES)}}, set()),
    ],
    ids=["default", "files-group-on", "files-group-off", "old-builtin-names"],
)
def test_spec_controls_which_file_tools_a_turn_is_offered(
    http_client: httpx.Client,
    live_runner_id: str,
    mock_llm_server_url: str,
    spec_overrides: dict[str, Any],
    expected: set[str],
) -> None:
    """
    The model is offered the three tools only when the spec sets
    ``tool_groups.files: true``: without a ``tool_groups`` block, or with
    ``files: false``, it gets none. An agent that still lists the removed names
    in ``tools.builtins`` gets none either, so a policy written for the old
    names is never bypassed by the new ones.
    """
    model = f"mock-files-spec-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Done."}],
        key=model,
        required_tools=["sys_session_list"],
    )
    session_id = _model_session(
        http_client,
        runner_id=live_runner_id,
        mock_llm_server_url=mock_llm_server_url,
        model=model,
        spec_overrides=spec_overrides,
    )
    response_id = send_user_message_to_session(
        http_client, session_id=session_id, content="Which tools do you have?"
    )
    body = poll_session_until_terminal(
        http_client, session_id=session_id, response_id=response_id, timeout=120
    )

    assert body["status"] == "completed", (body.get("status"), body.get("error"))
    advertised = advertised_tool_names(get_mock_requests(mock_llm_server_url, key=model))
    # A default system tool proves the captured request carried the tool list.
    assert "sys_session_list" in advertised, sorted(advertised)
    assert advertised & (_FILE_TOOLS | _OLD_NAMES) == expected, sorted(advertised)


def _omnigent_tool_names(requests: list[dict[str, Any]]) -> set[str]:
    """
    Name the Omnigent relay tools a native CLI offered its model.

    Claude Code prefixes them with ``mcp__omnigent__``; Codex groups them in an
    ``mcp__omnigent`` namespace.

    :param requests: Captured model requests.
    :returns: Bare tool names, e.g. ``{"sys_files_upload"}``.
    """
    names: set[str] = set()
    for request in requests:
        for tool in request.get("tools") or []:
            if not isinstance(tool, dict) or not isinstance(tool.get("name"), str):
                continue
            if tool["name"].startswith("mcp__omnigent__"):
                names.add(tool["name"].removeprefix("mcp__omnigent__"))
            elif tool["name"] == "mcp__omnigent":
                names.update(
                    nested["name"]
                    for nested in tool.get("tools") or []
                    if isinstance(nested, dict) and isinstance(nested.get("name"), str)
                )
    return names


@pytest.mark.timeout(360)
@pytest.mark.parametrize("harness", ["claude", "codex"])
def test_native_cli_uploads_a_workspace_file_through_the_omnigent_relay(
    tmp_path: Path, isolated_mock_llm_server_url: str, harness: str
) -> None:
    """
    A real Claude Code or Codex CLI, launched by a real host for an agent that
    sets ``tool_groups.files: true``, is offered the three tools by Omnigent's
    relay and uploads a file from its workspace with ``sys_files_upload``.
    """
    for binary in (harness, "tmux"):
        if shutil.which(binary) is None:
            pytest.skip(f"requires the real {binary} executable")
    if harness == "claude" and any(path.is_file() for path in CLAUDE_CODE_MANAGED_SETTINGS_PATHS):
        pytest.skip(
            "machine-managed Claude settings override mock auth; use an isolated container"
        )
    if (
        harness == "codex"
        and _SYSTEM_CODEX_CONFIG.exists()
        and _SYSTEM_CODEX_CONFIG.read_text().strip()
    ):
        pytest.skip("requires isolated Codex system config to keep model requests local")
    mock_url = isolated_mock_llm_server_url
    config = tmp_path / "config"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "report.md").write_text("native report")
    write_model_config(config, mock_url, _NATIVE_MODELS["claude"], _NATIVE_MODELS["codex"])
    base_env = _stack_base_env()
    if harness == "codex":
        # Set the app-server's MCP budget; --remote TUI overrides cannot change it.
        codex = shutil.which("codex")
        assert codex is not None
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        launcher = bin_dir / "codex"
        launcher.write_text(
            f"#!/bin/sh\nexec {shlex.quote(codex)} "
            '-c mcp_servers.omnigent.startup_timeout_sec=60 "$@"\n'
        )
        launcher.chmod(0o755)
        base_env["PATH"] = f"{bin_dir}{os.pathsep}{base_env['PATH']}"
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "CODEX_HOME": str(tmp_path / "codex-config"),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(tmp_path / "codex-state"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "OMNIGENT_SKIP_ONBOARD": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_SKIP_WEB_UI": "true",
    }
    upload_prompt = f"FILE_TOOLS_PROBE_{harness}: share report.md with me."
    probe = f"FILE_TOOLS_READY_{harness}: acknowledge."
    set_fallback_mock_llm(mock_url, key="default", text="Acknowledged.")
    set_fallback_mock_llm(
        mock_url,
        key="codex-auto-review",
        text='{"risk_level":"low","user_authorization":"high","outcome":"allow",'
        '"rationale":"The user asked to share this workspace file."}',
    )
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')

    with ExitStack() as resources:
        stack = resources.enter_context(
            server_runner(
                tmp_path,
                server_cwd=_REPO,
                base_env=base_env,
                server_env={**env, "OMNIGENT_RUNNER_TUNNEL_TOKEN": None},
                workspace=workspace,
                poll_interval=0.2,
            )
        )
        # The host launches the session's runner through the production API.
        stack.start_host(env=env, cwd=_REPO)
        client = resources.enter_context(
            httpx.Client(
                base_url=stack.base_url,
                trust_env=False,
                timeout=60,
                headers={
                    "Origin": OMNIGENT_INTERNAL_WS_ORIGIN,
                    "x-omnigent-background-session-titles": "off",
                },
            )
        )

        def online_host() -> dict[str, Any] | None:
            assert stack.host is not None and stack.host.poll() is None, stack.log_tail()
            response = client.get("/v1/hosts")
            response.raise_for_status()
            return next((h for h in response.json()["hosts"] if h["status"] == "online"), None)

        host = wait_for(online_host, timeout=90, what="real host registration", interval=0.2)
        spec = {
            "name": f"native-files-{harness}",
            "prompt": "Share files with the user when asked.",
            "executor": {
                "harness": f"{harness}-native",
                "model": _NATIVE_MODELS[harness],
                "yolo": True,
            },
            "os_env": _OS_ENV,
            "tool_groups": _FILES_ON,
        }
        created = post_session_bundle(
            client.post,
            "/v1/sessions",
            bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
            metadata={
                "host_id": host["host_id"],
                "workspace": str(workspace),
                # Codex asks before each Omnigent tool call by default; the
                # mocked automatic reviewer approves the upload instead.
                **(
                    {
                        "terminal_launch_args": PERMISSION_LAUNCH_ARGS["codex-native"][
                            "approve-for-me"
                        ]
                    }
                    if harness == "codex"
                    else {}
                ),
            },
        )
        created.raise_for_status()
        session_id = created.json()["session_id"]

        def send(text: str) -> None:
            response = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": text}]},
                },
            )
            assert response.status_code == 202, response.text

        # A native CLI can take input before its MCP discovery finishes.
        sent_probes = 0

        def relay_tools_offered() -> set[str] | None:
            nonlocal sent_probes
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            assert not snapshot.get("last_task_error"), snapshot["last_task_error"]
            requests = [r for r in get_mock_requests(mock_url) if probe in json.dumps(r)]
            offered = _omnigent_tool_names(requests)
            if "sys_files_upload" in offered:
                return offered
            if snapshot["status"] == "idle" and len(requests) >= sent_probes:
                sent_probes += 1
                send(probe)
            return None

        offered = wait_for(
            relay_tools_offered,
            timeout=180,
            what="the native CLI to offer the relay's tools",
            interval=0.2,
        )
        assert offered >= _FILE_TOOLS, sorted(offered)
        assert not offered & _OLD_NAMES, sorted(offered)

        upload_call: dict[str, Any] = {
            "call_id": "share_report",
            "name": "mcp__omnigent__sys_files_upload"
            if harness == "claude"
            else "sys_files_upload",
            **({"namespace": "mcp__omnigent"} if harness == "codex" else {}),
            "arguments": json.dumps({"path": "report.md"}),
        }
        configure_mock_llm(
            mock_url,
            [{"tool_calls": [upload_call]}, {"text": "Shared report.md."}],
            key=f"native-files-{harness}",
            match=upload_prompt,
            # Codex's approval review quotes the prompt but advertises no relay tools.
            required_tools=[
                "mcp__omnigent__sys_files_upload" if harness == "claude" else "mcp__omnigent"
            ],
        )
        send(upload_prompt)

        def uploaded_report() -> str | None:
            snapshot = client.get(f"/v1/sessions/{session_id}").json()
            assert not snapshot.get("last_task_error"), snapshot["last_task_error"]
            for elicitation in snapshot.get("pending_elicitations", []):
                assert elicitation["params"].get("tool_name", "").endswith("sys_files_upload"), (
                    elicitation
                )
                client.post(
                    f"/v1/sessions/{session_id}/elicitations/"
                    f"{elicitation['elicitation_id']}/resolve",
                    json={"action": "accept"},
                ).raise_for_status()
            return _session_files(client, session_id).get("report.md")

        file_id = wait_for(
            uploaded_report,
            timeout=180,
            what="the native CLI's upload to reach the session",
            interval=0.2,
        )
        assert _file_bytes(client, session_id, file_id) == b"native report"


def test_isolating_runner_keeps_uploads_inside_each_session(tmp_path: Path) -> None:
    """
    On a runner that isolates sessions, a session without its own workspace
    uploads from its own directory, while a sibling session's existing file is
    unreachable by relative, parent-traversal or absolute path.
    """
    base_env = _stack_base_env()
    with server_runner(tmp_path, base_env=base_env, poll_interval=0.2) as stack:
        stack.start_runner(env={"OMNIGENT_RUNNER_ISOLATE_SESSION": "1"})
        with httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as client:
            spec = {
                "name": "isolated-files",
                "prompt": "Share files with the user.",
                "executor": {"harness": "openai-agents", "model": "unused-model"},
                "os_env": _OS_ENV,
                "tool_groups": _FILES_ON,
            }

            def new_session() -> str:
                created = post_session_bundle(
                    client.post,
                    "/v1/sessions",
                    bundle_files({"agent.yaml": yaml.safe_dump(spec).encode()}),
                )
                created.raise_for_status()
                session_id = created.json()["session_id"]
                bind_session_runner(client.patch, "", session_id, stack.runner_id)
                return session_id

            def write(session_id: str, name: str, content: str) -> None:
                response = client.put(
                    f"/v1/sessions/{session_id}/resources/environments/default/filesystem/{name}",
                    json={"content": content, "encoding": "utf-8"},
                )
                response.raise_for_status()

            own_id, sibling_id = new_session(), new_session()
            write(own_id, "own.txt", "this session's file")
            write(sibling_id, "secret.txt", "SIBLING SECRET")
            secret = stack.workspace / sibling_id / "secret.txt"
            assert secret.read_text() == "SIBLING SECRET", "sibling's file is not where expected"

            own = _mcp_tool_call(client, own_id, "sys_files_upload", {"path": "own.txt"})
            refused = {
                path: _mcp_tool_call(client, own_id, "sys_files_upload", {"path": path})
                for path in (
                    f"{sibling_id}/secret.txt",
                    f"../{sibling_id}/secret.txt",
                    str(secret),
                )
            }
            stored = _session_files(client, own_id)

    assert json.loads(own).get("filename") == "own.txt", own
    for path, result in refused.items():
        assert result.startswith("Error: sys_files_upload failed:"), (path, result)
    assert set(stored) == {"own.txt"}, stored


@pytest.mark.parametrize("isolating", [True, False], ids=["cli-isolating-runner", "shared-runner"])
def test_model_turn_uploads_only_from_the_session_workspace(
    tmp_path: Path, mock_llm_server_url: str, isolating: bool
) -> None:
    """
    A model turn's ``sys_files_upload`` reads the session's own workspace: it shares
    the report ``sys_os_write`` just wrote there and refuses a file outside it.
    ``omnigent chat`` and ``omnigent run`` isolate sessions and use the launch
    directory as both workspaces; a shared runner may serve a session elsewhere.
    """
    runner_workspace = tmp_path / "runner-workspace"
    session_workspace = runner_workspace if isolating else tmp_path / "session-workspace"
    session_workspace.mkdir(parents=True, exist_ok=True)
    outside = (tmp_path if isolating else runner_workspace) / "outside.txt"
    outside.parent.mkdir(parents=True, exist_ok=True)
    outside.write_text("NOT THIS SESSION'S FILE")
    model = f"mock-files-cli-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(
        mock_llm_server_url,
        [
            _tool_call("write", "sys_os_write", {"path": "report.md", "content": "cli report"}),
            _tool_call("upload", "sys_files_upload", {"path": "report.md"}),
            _tool_call("outside", "sys_files_upload", {"path": str(outside)}),
            {"text": "Shared report.md."},
        ],
        key=model,
        required_tools=["sys_files_upload"],
    )
    with server_runner(
        tmp_path, base_env=_stack_base_env(), workspace=runner_workspace, poll_interval=0.2
    ) as stack:
        stack.start_runner(env={"OMNIGENT_RUNNER_ISOLATE_SESSION": "1" if isolating else "0"})
        with httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"Origin": OMNIGENT_INTERNAL_WS_ORIGIN},
        ) as client:
            session_id = _model_session(
                client,
                runner_id=stack.runner_id,
                mock_llm_server_url=mock_llm_server_url,
                model=model,
                spec_overrides={"os_env": _OS_ENV, "tool_groups": _FILES_ON},
                metadata={"workspace": str(session_workspace)},
            )
            response_id = send_user_message_to_session(
                client, session_id=session_id, content="Write the report and share it."
            )
            body = poll_session_until_terminal(
                client, session_id=session_id, response_id=response_id, timeout=120
            )
            stored = _session_files(client, session_id)
            shared = (
                _file_bytes(client, session_id, stored["report.md"])
                if "report.md" in stored
                else None
            )

    advertised = advertised_tool_names(get_mock_requests(mock_llm_server_url, key=model))
    assert "sys_files_upload" in advertised, sorted(advertised)
    assert body["status"] == "completed", (body.get("status"), body.get("error"))
    assert (session_workspace / "report.md").read_text() == "cli report"
    outputs = _outputs_by_call_id(body)
    assert not outputs["upload"].startswith("Error:"), outputs["upload"]
    assert json.loads(outputs["upload"])["filename"] == "report.md", outputs["upload"]
    assert outputs["outside"].startswith("Error: sys_files_upload failed:"), outputs["outside"]
    assert set(stored) == {"report.md"}, stored
    assert shared == b"cli report"
