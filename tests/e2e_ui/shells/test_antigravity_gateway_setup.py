"""Real setup -> local daemon -> runner -> agy, using a fake Gemini gateway.

Run with OMNIGENT_E2E_ANTIGRAVITY=mock. The UI CI job installs pinned agy
1.5.2; this journey checks the backend used by a host-bound native chat.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import psutil
import pytest
import yaml

from omnigent.harnesses.antigravity_native.bridge import bridge_dir_for_bridge_id, read_tmux_info
from tests._helpers.https_server import enable_https
from tests._helpers.native_session import create_native_session

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") != "mock",
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock; requires agy and tmux",
    ),
    pytest.mark.timeout(240),
]
_ROOT = Path(__file__).resolve().parents[3]


def _wait(check, message: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        result = check()
        if result:
            return result
        threading.Event().wait(0.2)
    raise AssertionError(message)


@pytest.mark.timeout(360)
@pytest.mark.parametrize("databricks", [False, True], ids=["direct", "databricks"])
def test_setup_gateway_reaches_real_agy_through_fresh_local_daemon(
    tmp_path: Path, databricks: bool, page, built_spa, request: pytest.FixtureRequest
) -> None:
    """Read a file, reload/recall, then automatically recover the owned terminal.

    Earlier message/tool IDs stay unchanged and only the new turn is appended.
    """
    from playwright.sync_api import expect

    assert shutil.which("agy") and shutil.which("tmux"), "install agy and tmux"
    captured: list[dict] = []
    tool_requested = threading.Event()
    tool_completed = threading.Event()
    control_calls: dict[str, str] = {}
    control_results: dict[str, list[dict]] = {}
    control_result_offsets: dict[str, int] = {}
    missing_tools: dict[str, list[str]] = {}
    agent_id: str | None = None
    control_tools = {
        "AGY_RELAY_REVIEW": "sys_scheduled_task_create",
        "AGY_NATIVE_ASK": "sys_terminal_list",
        "AGY_NATIVE_DENY": "sys_session_get_info",
    }

    def tool_declarations(body: dict) -> list[dict]:
        return [
            declaration
            for tool in body.get("tools", [])
            for declaration in tool.get("functionDeclarations", [])
        ]

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    if databricks:
        shadow_package = workspace / "omnigent"
        shadow_package.mkdir()
        (shadow_package / "__init__.py").write_text(
            "raise RuntimeError('The gateway supervisor imported workspace code')\n"
        )
    tool_file = workspace / "gateway-tool-check.txt"
    tool_file.write_text("GATEWAY_TOOL_RESULT\n")

    class GeminiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if (
                not self.path.startswith("/api/2.1/unity-catalog/model-services?")
                or self.headers.get("Authorization") != "Bearer gateway-test-key"
            ):
                self.send_error(403)
                return
            payload = json.dumps(
                {
                    "model_services": [
                        {"name": "model-services/system.ai.gemini-3-1-pro"},
                        {"name": "model-services/system.ai.gemini-3-1-flash-lite"},
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_POST(self) -> None:
            prefix = (
                "/ai-gateway/gemini/v1beta/models/system.ai."
                if databricks
                else "/gemini/v1beta/models/"
            )
            authenticated = (
                self.headers.get("Authorization") == "Bearer gateway-test-key"
                and "x-goog-api-key" not in self.headers
                if databricks
                else self.headers.get("x-goog-api-key") == "gateway-test-key"
            )
            if (
                not self.path.startswith(prefix)
                or ":streamGenerateContent" not in self.path
                or not authenticated
            ):
                self.send_error(403)
                return
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            captured.append(request)
            parts: list[dict] = [{"text": "GATEWAY_SETUP_OK"}]
            has_tool_result = any(
                "GATEWAY_TOOL_RESULT" in json.dumps(part.get("functionResponse", {}))
                for content in request.get("contents", [])
                for part in content.get("parts", [])
            )
            if request.get("tools"):
                if not tool_requested.is_set():
                    file_tool = next(
                        declaration
                        for declaration in tool_declarations(request)
                        if declaration["name"] in {"read_file", "view_file"}
                    )
                    path_argument = "Path" if file_tool["name"] == "read_file" else "AbsolutePath"
                    arguments = {path_argument: str(tool_file)}
                    properties = file_tool.get(
                        "parametersJsonSchema", file_tool.get("parameters", {})
                    ).get("properties", {})
                    for name, value in {
                        "toolSummary": "Gateway file check",
                        "toolAction": "Reading test file",
                    }.items():
                        if name in properties:
                            arguments[name] = value
                    tool_requested.set()
                    parts = [
                        {
                            "functionCall": {
                                "name": file_tool["name"],
                                "args": arguments,
                            }
                        }
                    ]
                elif has_tool_result:
                    tool_completed.set()
            if any(
                "GATEWAY_RECALL_OK" in part.get("text", "")
                for content in request.get("contents", [])
                if content.get("role") == "user"
                for part in content.get("parts", [])
            ):
                parts = [
                    {
                        "text": (
                            "GATEWAY_RECALL_OK: GATEWAY_TOOL_RESULT"
                            if has_tool_result
                            else "GATEWAY_HISTORY_MISSING"
                        )
                    }
                ]
            user_texts = [
                part.get("text", "")
                for content in request.get("contents", [])
                if content.get("role") == "user"
                for part in content.get("parts", [])
            ]
            stage = next(
                (
                    marker
                    for text in reversed(user_texts)
                    for marker in control_tools
                    if marker in text
                ),
                None,
            )
            if stage is not None:
                declarations = tool_declarations(request)
                if stage not in control_calls:
                    declaration = next(
                        (d for d in declarations if d["name"].endswith(control_tools[stage])),
                        None,
                    )
                    generic = next((d for d in declarations if d["name"] == "call_mcp_tool"), None)
                    if declaration is None:
                        declaration = generic
                    if declaration is None:
                        missing_tools[stage] = sorted(d["name"] for d in declarations)
                        parts = [{"text": f"{stage}_TOOL_MISSING"}]
                    else:
                        name = declaration["name"]
                        arguments = (
                            {
                                "name": "AGY relay demo task",
                                "prompt": "Reply with AGY-RELAY only.",
                                "rrule": "FREQ=DAILY;BYHOUR=12;BYMINUTE=0",
                                "timezone": "UTC",
                                "agent_id": agent_id,
                                "permission_mode": None,
                            }
                            if stage == "AGY_RELAY_REVIEW"
                            else {}
                        )
                        if declaration is generic:
                            schema = (
                                declaration.get("parameters")
                                or declaration["parametersJsonSchema"]
                            )
                            properties = schema["properties"]
                            fields = {key.replace("_", "").lower(): key for key in properties}
                            assert {"servername", "toolname", "arguments"} <= fields.keys(), (
                                f"Unexpected native MCP call schema: {properties}"
                            )
                            argument_key = fields["arguments"]
                            arguments = {
                                fields["servername"]: "omnigent",
                                fields["toolname"]: control_tools[stage],
                                argument_key: (
                                    json.dumps(arguments)
                                    if properties[argument_key].get("type", "").lower() == "string"
                                    else arguments
                                ),
                            }
                        control_result_offsets[stage] = sum(
                            part.get("functionResponse", {}).get("name") == name
                            for content in request.get("contents", [])
                            for part in content.get("parts", [])
                        )
                        control_calls[stage] = name
                        parts = [{"functionCall": {"name": name, "args": arguments}}]
                else:
                    results = [
                        part["functionResponse"]
                        for content in request.get("contents", [])
                        for part in content.get("parts", [])
                        if part.get("functionResponse", {}).get("name") == control_calls[stage]
                    ][control_result_offsets[stage] :]
                    if results:
                        control_results[stage] = results
                    parts = [{"text": f"{stage}_DONE" if results else f"{stage}_RESULT_MISSING"}]
            authored_context = {
                "systemInstruction": request.get("systemInstruction"),
                "contents": [c for c in request.get("contents", []) if c.get("role") != "model"],
            }
            if "AGY-STARTUP" in json.dumps(authored_context):
                for part in parts:
                    if "text" in part:
                        part["text"] = "AGY-STARTUP " + part["text"]
            response = {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": parts},
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            }
            body = f"data: {json.dumps(response)}\n\n".encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "USER", "SHELL", "TMPDIR", "LANG"}
    }
    env.update(
        {
            "PYTHONPATH": str(_ROOT),
            "OMNIGENT_CONFIG_HOME": str(tmp_path / "config"),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_DISABLE_KEYRING": "1",
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    native_settings: Path | None = None
    native_settings_bytes: bytes | None = None
    if not databricks:
        native_home = tmp_path / "native-home"
        native_settings = native_home / ".gemini/antigravity-cli/settings.json"
        native_settings.parent.mkdir(parents=True)
        native_settings_bytes = json.dumps(
            {
                "permissions": {
                    "ask": ["mcp(omnigent/sys_terminal_list)"],
                    "deny": ["mcp(omnigent/sys_session_get_info)"],
                },
            }
        ).encode()
        native_settings.write_bytes(native_settings_bytes)
        env["HOME"] = str(native_home)
    command = [sys.executable, "-m", "omnigent"]
    session_id: str | None = None
    client: httpx.Client | None = None
    processes: list[psutil.Process] = []

    def cli(*args: str, stdin: str | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*command, *args],
            env=env,
            cwd=tmp_path,
            input=stdin,
            capture_output=True,
            text=True,
            timeout=60,
        )

    with ThreadingHTTPServer(("127.0.0.1", 0), GeminiHandler) as gateway:
        if databricks:
            env.update(enable_https(gateway, tmp_path / "tls"))
        thread = threading.Thread(target=gateway.serve_forever, daemon=True)
        thread.start()
        try:
            endpoint = f"http://127.0.0.1:{gateway.server_port}/gemini"
            if databricks:
                profile_file = tmp_path / "databrickscfg"
                profile_file.write_text(
                    "[gateway-test]\n"
                    f"host = https://localhost:{gateway.server_port}\n"
                    "token = gateway-test-key\nauth_type = pat\n"
                )
                profile_file.chmod(0o600)
                env.update(
                    {
                        "DATABRICKS_CONFIG_FILE": str(profile_file),
                        "DATABRICKS_HOST": f"https://localhost:{gateway.server_port}/ambient",
                        "DATABRICKS_TOKEN": "ambient-wrong-token",
                        "DATABRICKS_CLIENT_ID": "ambient-client",
                        "DATABRICKS_CLIENT_SECRET": "ambient-secret",
                        "DATABRICKS_AUTH_TYPE": "oauth-m2m",
                        "DATABRICKS_CONFIG_PROFILE": "wrong-profile",
                    }
                )
            result = cli(
                "setup",
                "--no-internal-beta",
                stdin="\n".join(
                    ["7", "3", "1", "3", "gateway-test", "q", "q", "q"]
                    if databricks
                    else [
                        "7",
                        "3",
                        "1",
                        "2",
                        "test-gemini",
                        endpoint,
                        "gateway-test-key",
                        "",
                        "q",
                        "q",
                        "q",
                    ]
                )
                + "\n",
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "gateway-test-key" not in result.stdout
            config_text = (tmp_path / "config/config.yaml").read_text()
            assert "gateway-test-key" not in config_text
            config = yaml.safe_load(config_text)
            if databricks:
                from omnigent.onboarding.provider_config import load_providers

                provider = config["providers"]["databricks-gemini-gateway-test"]
                assert provider["profile"] == "gateway-test"
                assert load_providers(config)[
                    "databricks-gemini-gateway-test"
                ].default_families == {"gemini"}
            else:
                assert config["providers"]["test-gemini"]["gemini"]["base_url"] == endpoint
            # The new daemon must read the Gemini credentials saved by setup.
            assert "GEMINI_API_KEY" not in env and "GOOGLE_GEMINI_BASE_URL" not in env
            result = cli("host", "--background", "--non-interactive")
            assert result.returncode == 0, result.stdout + result.stderr
            pidfile = tmp_path / "data/local_server.pid"
            _wait(pidfile.exists, "local daemon did not start its server")
            pid, port = pidfile.read_text().splitlines()[:2]
            processes.append(psutil.Process(int(pid)))
            for record in (tmp_path / "data/daemons").glob("*.json"):
                processes.append(psutil.Process(json.loads(record.read_text())["pid"]))
            client = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15)

            def online_host():
                try:
                    response = client.get("/v1/hosts")
                    if response.status_code == 200:
                        return next(
                            (h for h in response.json()["hosts"] if h["status"] == "online"),
                            None,
                        )
                except httpx.HTTPError:
                    # Connection failures are expected while the server starts.
                    return None
                return None

            host = _wait(online_host, "fresh host did not connect")
            created = create_native_session(
                client,
                str(client.base_url).rstrip("/"),
                harness="antigravity",
                instructions="Begin every reply with AGY-STARTUP.",
                metadata={"host_id": host["host_id"], "workspace": str(workspace)},
            )
            session_id = created["session_id"]
            agent_id = created["agent_id"]

            def terminals():
                response = client.get(f"/v1/sessions/{session_id}/resources/terminals")
                if response.status_code == 503:
                    return None
                response.raise_for_status()
                return response.json()["data"]

            _wait(terminals, "host did not start native agy")
            page.goto(f"{str(client.base_url).rstrip('/')}/c/{session_id}")
            composer = page.get_by_role("textbox", name="Message the agent")
            composer.fill("Read gateway-tool-check.txt, then reply GATEWAY_SETUP_OK.")
            page.get_by_role("button", name="Send", exact=True).click()
            replies = page.locator('[data-testid="message-bubble"][data-role="assistant"]')
            expect(replies.filter(has_text="GATEWAY_SETUP_OK")).to_have_count(1, timeout=90_000)
            expect(replies.filter(has_text="AGY-STARTUP GATEWAY_SETUP_OK")).to_have_count(1)
            expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)
            assert tool_completed.is_set(), "gateway did not receive the real file tool result"
            assert any("GATEWAY_SETUP_OK" in json.dumps(r.get("contents")) for r in captured)
            expect(page.get_by_test_id("error-pill")).to_have_count(0)

            page.reload()
            expect(replies.filter(has_text="GATEWAY_SETUP_OK")).to_have_count(1)
            composer.fill(
                "Without tools, recall the file's contents from earlier. "
                "Prefix your reply with GATEWAY_RECALL_OK."
            )
            page.get_by_role("button", name="Send", exact=True).click()
            expect(
                replies.filter(has_text="GATEWAY_RECALL_OK: GATEWAY_TOOL_RESULT")
            ).to_have_count(1, timeout=90_000)
            expect(replies.filter(has_text="GATEWAY_SETUP_OK")).to_have_count(1)
            expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)
            expect(page.get_by_test_id("error-pill")).to_have_count(0)
            response = client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            assert response.json()["status"] == "idle"

            def transcript():
                response = client.get(f"/v1/sessions/{session_id}/items")
                response.raise_for_status()
                return [
                    item
                    for item in response.json()["data"]
                    if item["type"] in {"message", "function_call", "function_call_output"}
                ]

            before_resume = transcript()
            page.get_by_test_id("view-mode-terminal").click()
            terminal_view = page.get_by_test_id("main-terminal-view")
            terminal = terminal_view.get_by_test_id("terminal-view")
            expect(terminal).to_have_attribute("data-state", "connected", timeout=30_000)
            tmux = read_tmux_info(
                tmp_path
                / "data"
                / "antigravity-native"
                / bridge_dir_for_bridge_id(session_id).name
            )
            assert tmux is not None
            subprocess.run(
                ["tmux", "-S", tmux["socket_path"], "kill-session", "-t", tmux["tmux_target"]],
                check=True,
                capture_output=True,
            )
            expect(
                terminal_view.get_by_role("button", name="Resume session", exact=True)
            ).to_be_enabled(timeout=30_000)
            page.get_by_test_id("view-mode-chat").click()
            with page.expect_response(
                lambda response: (
                    response.url.endswith(f"/v1/sessions/{session_id}/events")
                    and response.request.method == "POST"
                    and response.request.post_data_json.get("type") == "retry_session"
                ),
                timeout=60_000,
            ) as recovery:
                page.get_by_test_id("view-mode-terminal").click()
            assert recovery.value.ok
            assert recovery.value.json()["recovered"] is True
            expect(terminal).to_have_attribute("data-state", "connected", timeout=60_000)
            page.get_by_test_id("view-mode-chat").click()
            composer.fill("Without tools, recall the file again. Prefix with GATEWAY_RECALL_OK.")
            page.get_by_role("button", name="Send", exact=True).click()
            expect(
                replies.filter(has_text="GATEWAY_RECALL_OK: GATEWAY_TOOL_RESULT")
            ).to_have_count(2, timeout=90_000)
            expect(page.get_by_test_id("working-indicator")).to_have_count(0, timeout=30_000)
            expect(page.get_by_test_id("error-pill")).to_have_count(0)
            expect(replies).to_have_count(3)
            expect(
                replies.filter(has_text="AGY-STARTUP GATEWAY_RECALL_OK: GATEWAY_TOOL_RESULT")
            ).to_have_count(2)
            after_resume = transcript()
            assert after_resume[: len(before_resume)] == before_resume
            assert [item["type"] for item in after_resume[len(before_resume) :]] == [
                "message",
                "message",
            ]
            response = client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            assert response.json()["status"] == "idle"
            if not databricks:
                pending = page.locator('[data-testid="approval-card"][data-state="pending"]')
                for marker in control_tools:
                    composer.fill(f"Exercise {marker} once, then report the result.")
                    page.get_by_role("button", name="Send", exact=True).click()
                    _wait(
                        lambda marker=marker: marker in control_calls or marker in missing_tools,
                        f"Native model never attempted {marker}",
                        timeout=60,
                    )
                    assert marker in control_calls, (
                        f"Missing {control_tools[marker]}; "
                        f"native declarations: {missing_tools[marker]}"
                    )
                    if marker != "AGY_NATIVE_DENY":
                        card = pending.first
                        expect(card).to_be_visible(timeout=60_000)
                        expect(card).to_contain_text(
                            "create scheduled task"
                            if marker == "AGY_RELAY_REVIEW"
                            else "Antigravity"
                        )
                        if marker == "AGY_RELAY_REVIEW":
                            tasks = client.get("/v1/scheduled-tasks")
                            tasks.raise_for_status()
                            assert not tasks.json()["scheduled_tasks"]
                        with page.expect_response(
                            lambda response: (
                                f"/v1/sessions/{session_id}/elicitations/" in response.url
                                and response.url.endswith("/resolve")
                                and response.request.method == "POST"
                                and response.request.post_data_json.get("action") == "decline"
                            )
                        ) as rejection:
                            card.get_by_role("button", name="Reject", exact=True).click()
                        assert rejection.value.status == 202
                        assert rejection.value.json() == {"queued": False}
                    expect(replies.filter(has_text=f"{marker}_DONE")).to_have_count(
                        1, timeout=60_000
                    )
                    expect(page.get_by_test_id("working-indicator")).to_have_count(
                        0, timeout=30_000
                    )
                    expect(pending).to_have_count(0)
                    result = json.dumps(control_results[marker]).lower()
                    declined = any(
                        word in result
                        for word in (
                            "denied",
                            "declined",
                            "rejected",
                            "cancelled",
                            "canceled",
                            "not allowed",
                        )
                    )
                    if marker == "AGY_RELAY_REVIEW":
                        tasks = client.get("/v1/scheduled-tasks")
                        tasks.raise_for_status()
                        assert not tasks.json()["scheduled_tasks"]
                        # agy can poll after synchronous resolution removed the elicitation.
                        declined = declined or (
                            "error" in result
                            and "elicitation not found or already resolved" in result
                        )
                    assert declined, f"{marker} did not retain its rejection"
                tasks = client.get("/v1/scheduled-tasks")
                tasks.raise_for_status()
                assert not tasks.json()["scheduled_tasks"]
                assert native_settings is not None
                # agy normalizes JSON formatting; the complete user configuration must survive.
                assert json.loads(native_settings.read_bytes()) == json.loads(
                    native_settings_bytes
                )
        finally:
            try:
                with contextlib.suppress(Exception):
                    from omnigent.process_logging import redact_log_text

                    diagnostics = Path(
                        request.config.getoption("output", default="test-results")
                    ) / ("agy-databricks" if databricks else "agy-direct")
                    diagnostics.mkdir(parents=True, exist_ok=True)
                    declarations = [d for body in captured for d in tool_declarations(body)]
                    discovered = {
                        "native_tool_names": sorted({d["name"] for d in declarations}),
                        "control_calls": control_calls,
                        "control_results": {
                            marker: redact_log_text(
                                json.dumps(results), include_whitespace_credentials=True
                            )
                            for marker, results in control_results.items()
                        },
                        "missing_tools": missing_tools,
                        "native_mcp_call_schemas": [
                            d for d in declarations if d["name"] == "call_mcp_tool"
                        ][:1],
                    }
                    if session_id is not None:
                        relay_file = (
                            tmp_path
                            / "data/antigravity-native"
                            / (bridge_dir_for_bridge_id(session_id).name)
                            / "tool_relay.json"
                        )
                        with contextlib.suppress(OSError, ValueError):
                            relay = json.loads(relay_file.read_text())
                            discovered["relay_tool_names"] = sorted(
                                t["name"] for t in relay.get("tools", [])
                            )
                    errors: list[str] = []
                    for log in (tmp_path / "data").rglob("*.log"):
                        with contextlib.suppress(OSError):
                            with log.open("rb") as handle:
                                handle.seek(max(0, log.stat().st_size - 64_000))
                                lines = handle.read().decode(errors="replace").splitlines()
                            for line in lines:
                                if any(
                                    word in line.lower() for word in ("mcp", "tool_relay", "error")
                                ):
                                    errors.append(
                                        redact_log_text(line, include_whitespace_credentials=True)[
                                            :1200
                                        ]
                                    )
                    discovered["service_errors"] = errors[-60:]
                    (diagnostics / "relay-discovery.json").write_text(
                        json.dumps(discovered, indent=2)
                    )
            finally:
                # Finish the recording before deleting the session and its native stream.
                with contextlib.suppress(Exception):
                    page.context.close()
                with contextlib.suppress(OSError):
                    (tmp_path / "gateway-requests.json").write_text(json.dumps(captured, indent=2))
                if client is not None:
                    if session_id is not None:
                        with contextlib.suppress(httpx.HTTPError):
                            client.delete(f"/v1/sessions/{session_id}")
                    client.close()
                with contextlib.suppress(subprocess.TimeoutExpired):
                    cli("host", "stop", "--all", "--force")
                for process in reversed(processes):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        descendants = process.children(recursive=True)
                        for child in reversed(descendants):
                            with contextlib.suppress(psutil.NoSuchProcess):
                                child.terminate()
                        process.terminate()
                        _, alive = psutil.wait_procs([*descendants, process], timeout=5)
                        for child in alive:
                            with contextlib.suppress(psutil.NoSuchProcess):
                                child.kill()
                if session_id is not None:
                    shutil.rmtree(
                        tmp_path
                        / "data"
                        / "antigravity-native"
                        / bridge_dir_for_bridge_id(session_id).name,
                        ignore_errors=True,
                    )
                gateway.shutdown()
                thread.join(timeout=5)


@pytest.mark.parametrize("failure", ["missing-secret", "incompatible-url", "malformed-profile"])
def test_saved_credential_error_visible_through_fresh_daemon(tmp_path, page, built_spa, failure):
    """A fresh host reports credentials invalidated after its readiness handshake."""
    from playwright.sync_api import expect

    config_dir = tmp_path / "config"
    config_dir.mkdir()
    profile_file = tmp_path / "databrickscfg"
    profile_file.write_text("")
    env = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "PATH",
            "HOME",
            "USER",
            "SHELL",
            "TMPDIR",
            "LANG",
        }
    }
    env.update(
        {
            "PYTHONPATH": str(_ROOT),
            "OMNIGENT_CONFIG_HOME": str(config_dir),
            "OMNIGENT_DATA_DIR": str(tmp_path / "data"),
            "OMNIGENT_DISABLE_KEYRING": "1",
            "OMNIGENT_AUTH_PROVIDER": "header",
            "OMNIGENT_LOCAL_SINGLE_USER": "1",
            "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
            "AGY_CLI_DISABLE_AUTO_UPDATE": "true",
            "DATABRICKS_CONFIG_FILE": str(profile_file),
        }
    )
    entry = {
        "kind": "gateway",
        "default": ["gemini"],
        "gemini": {
            "base_url": "https://gateway.example/gemini",
            "api_key_ref": "keychain:missing",
        },
    }
    expected = "no stored secret"
    if failure == "incompatible-url":
        entry["gemini"] = {"base_url": "https://api.openai.com/v1", "api_key": "fake"}
        expected = "OpenAI Responses"
    if failure == "malformed-profile":
        entry = {
            "kind": "databricks",
            "profile": "broken",
            "native_gemini": True,
            "default": ["gemini"],
        }
        expected = "Repair ~/.databrickscfg"
    config_path = config_dir / "config.yaml"
    config_path.write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "selected": {
                        "kind": "key",
                        "default": ["gemini"],
                        "gemini": {
                            "base_url": "https://generativelanguage.googleapis.com",
                            "api_key": "fake",
                        },
                    }
                }
            }
        )
    )
    session_id = None
    processes = []

    def cli(*args):
        return subprocess.run(
            [sys.executable, "-m", "omnigent", *args],
            cwd=tmp_path,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
        )

    try:
        result = cli("host", "--background", "--non-interactive")
        assert result.returncode == 0, result.stdout + result.stderr
        pidfile = tmp_path / "data/local_server.pid"
        _wait(pidfile.exists, "local daemon did not start")
        pid, port = pidfile.read_text().splitlines()[:2]
        processes.append(psutil.Process(int(pid)))
        for record in (tmp_path / "data/daemons").glob("*.json"):
            processes.append(psutil.Process(json.loads(record.read_text())["pid"]))
        base_url = f"http://127.0.0.1:{port}"
        with httpx.Client(base_url=base_url, timeout=30) as client:

            def online_host():
                response = client.get("/v1/hosts")
                response.raise_for_status()
                return next((h for h in response.json()["hosts"] if h["status"] == "online"), None)

            host = _wait(online_host, "fresh host did not connect")
            # Readiness is cached; launch must revalidate changed saved credentials.
            config_path.write_text(yaml.safe_dump({"providers": {"selected": entry}}))
            if failure == "malformed-profile":
                profile_file.write_text("token = private-malformed-secret\n")
            created = create_native_session(
                client,
                str(client.base_url).rstrip("/"),
                harness="antigravity",
                metadata={"host_id": host["host_id"], "workspace": str(tmp_path)},
            )
            session_id = created["session_id"]
            page.goto(f"{base_url}/c/{session_id}")
            page.get_by_role("textbox", name="Message the agent").fill("Hello")
            page.get_by_role("button", name="Send", exact=True).click()
            error_pill = page.get_by_test_id("error-pill").first
            expect(error_pill).to_be_visible(timeout=60_000)
            error_pill.click()
            expect(page.get_by_text(expected, exact=False).first).to_be_visible(timeout=60_000)
            expect(
                page.get_by_text("Run omni setup on the host", exact=False).first
            ).to_be_visible()
            body = page.locator("body").inner_text()
            assert "see the runner log" not in body
            assert "private-malformed-secret" not in body
            assert not list((tmp_path / "data/crashes").glob("crash-*.md"))
            for log in (tmp_path / "data/logs").rglob("*.log"):
                assert "private-malformed-secret" not in log.read_text()
    finally:
        # Capture children even if background-host registration failed.
        pidfile = tmp_path / "data/local_server.pid"
        if pidfile.exists():
            with contextlib.suppress(psutil.NoSuchProcess):
                processes.append(psutil.Process(int(pidfile.read_text().splitlines()[0])))
        for record in (tmp_path / "data/daemons").glob("*.json"):
            with contextlib.suppress(psutil.NoSuchProcess):
                processes.append(psutil.Process(json.loads(record.read_text())["pid"]))
        with contextlib.suppress(subprocess.TimeoutExpired):
            cli("host", "stop", "--all", "--force")
        for process in reversed(processes):
            with contextlib.suppress(psutil.NoSuchProcess):
                descendants = process.children(recursive=True)
                for child in reversed(descendants):
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.terminate()
                process.terminate()
                _, alive = psutil.wait_procs([*descendants, process], timeout=5)
                for child in alive:
                    with contextlib.suppress(psutil.NoSuchProcess):
                        child.kill()
        if session_id is not None:
            shutil.rmtree(
                tmp_path
                / "data"
                / "antigravity-native"
                / bridge_dir_for_bridge_id(session_id).name,
                ignore_errors=True,
            )
