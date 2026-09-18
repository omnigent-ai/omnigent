"""Real setup -> local daemon -> runner -> agy, using a fake Gemini gateway.

Run with OMNIGENT_E2E_ANTIGRAVITY=mock. The UI CI job installs pinned agy
1.2.4; this journey checks the backend used by a host-bound native chat.
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

from omnigent.harnesses.antigravity_native.bridge import bridge_dir_for_bridge_id
from tests._helpers.https_server import enable_https

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


@pytest.mark.parametrize("databricks", [False, True], ids=["direct", "databricks"])
def test_setup_gateway_reaches_real_agy_through_fresh_local_daemon(
    tmp_path: Path, databricks: bool
) -> None:
    assert shutil.which("agy") and shutil.which("tmux"), "install agy and tmux"
    captured: list[dict] = []
    tool_requested = threading.Event()
    tool_completed = threading.Event()
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
            if request.get("tools"):
                if not tool_requested.is_set():
                    tool_requested.set()
                    parts = [
                        {
                            "functionCall": {
                                "name": "view_file",
                                "args": {
                                    "AbsolutePath": str(tool_file),
                                    "toolSummary": "Gateway file check",
                                    "toolAction": "Reading test file",
                                },
                            }
                        }
                    ]
                elif any(
                    "GATEWAY_TOOL_RESULT" in json.dumps(part.get("functionResponse", {}))
                    for content in request.get("contents", [])
                    for part in content.get("parts", [])
                ):
                    tool_completed.set()
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
                    pass

            host = _wait(online_host, "fresh host did not connect")
            agents = client.get("/v1/agents")
            agents.raise_for_status()
            agent = next(a for a in agents.json()["data"] if a["name"] == "antigravity-native-ui")
            response = client.post(
                "/v1/sessions",
                json={
                    "agent_id": agent["id"],
                    "host_id": host["host_id"],
                    "workspace": str(workspace),
                },
                timeout=60,
            )
            response.raise_for_status()
            session_id = response.json()["id"]

            def terminals():
                response = client.get(f"/v1/sessions/{session_id}/resources/terminals")
                if response.status_code == 503:
                    return None
                response.raise_for_status()
                return response.json()["data"]

            _wait(terminals, "host did not start native agy")
            response = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {
                                "type": "input_text",
                                "text": (
                                    "Read gateway-tool-check.txt, then reply GATEWAY_SETUP_OK."
                                ),
                            }
                        ],
                    },
                },
            )
            response.raise_for_status()

            def reply():
                response = client.get(f"/v1/sessions/{session_id}/items")
                response.raise_for_status()
                return any(
                    item.get("role") == "assistant" and "GATEWAY_SETUP_OK" in json.dumps(item)
                    for item in response.json()["data"]
                )

            _wait(reply, "no native reply through the configured gateway", timeout=90)
            assert tool_completed.is_set(), "gateway did not receive the real file tool result"
            assert any("GATEWAY_SETUP_OK" in json.dumps(r.get("contents")) for r in captured)
        finally:
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
                shutil.rmtree(bridge_dir_for_bridge_id(session_id), ignore_errors=True)
            gateway.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("failure", ["missing-secret", "incompatible-url", "malformed-profile"])
def test_saved_credential_error_visible_through_fresh_daemon(tmp_path, page, built_spa, failure):
    """A real host rejects invalid saved credentials with visible recovery guidance."""
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
        profile_file.write_text("token = private-malformed-secret\n")
        expected = "Repair ~/.databrickscfg"
    (config_dir / "config.yaml").write_text(yaml.safe_dump({"providers": {"selected": entry}}))
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
            agents = client.get("/v1/agents")
            agents.raise_for_status()
            agent = next(a for a in agents.json()["data"] if a["name"] == "antigravity-native-ui")
            response = client.post(
                "/v1/sessions",
                json={
                    "agent_id": agent["id"],
                    "host_id": host["host_id"],
                    "workspace": str(tmp_path),
                },
                timeout=60,
            )
            response.raise_for_status()
            session_id = response.json()["id"]
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
            shutil.rmtree(bridge_dir_for_bridge_id(session_id), ignore_errors=True)
