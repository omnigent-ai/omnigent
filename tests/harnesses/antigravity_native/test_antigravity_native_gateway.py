"""Databricks Gemini auth, model translation, and streaming boundaries."""

from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from omnigent.harnesses.antigravity_native.credentials import databricks_token_source
from omnigent.harnesses.antigravity_native.gateway import (
    GeminiDatabricksGateway,
    resolve_model,
    run,
    wrap_agy_gateway_launch,
)
from omnigent.inner.credential_proxy import DatabricksProfileTokenProvider
from omnigent.onboarding.provider_config import (
    default_provider_for_harness,
    load_providers,
    provider_families,
    set_default_provider,
)
from tests._helpers.https_server import enable_https


def test_databricks_gemini_requires_opt_in_and_preserves_existing_defaults() -> None:
    config = {
        "providers": {"existing": {"kind": "databricks", "profile": "test", "default": True}}
    }
    assert default_provider_for_harness(config, "antigravity-native") is None
    before = load_providers(config)["existing"]
    config["providers"]["agy"] = {"kind": "databricks", "profile": "test", "native_gemini": True}
    config["providers"] = set_default_provider(config["providers"], "agy", "gemini")
    assert default_provider_for_harness(config, "antigravity-native").name == "agy"
    assert default_provider_for_harness(config, "native-codex").name == "existing"
    assert load_providers(config)["existing"].default_families == before.default_families
    assert provider_families(load_providers(config)["agy"]) == {"gemini"}


def test_model_translation_keeps_version_and_role() -> None:
    models = ("system.ai.gemini-3-1-pro", "system.ai.gemini-3-1-flash-lite")
    assert resolve_model("gemini-3.1-pro-preview", models) == models[0]
    assert resolve_model("gemini-3.1-flash-lite-preview", models) == models[1]
    with pytest.raises(ValueError, match="unambiguous"):
        resolve_model("gemini-2.5-pro", models)
    with pytest.raises(ValueError, match="unambiguous"):
        resolve_model("gemini-3.1-pro-preview", (*models, "databricks-gemini-3-1-pro"))


def test_wrapper_is_scoped_to_resolved_launch() -> None:
    args = ["/test/agy", "--conversation", "existing"]
    assert wrap_agy_gateway_launch(args, {}) == args
    env = {"OMNIGENT_AGY_DATABRICKS_PROFILE": "gateway-test"}
    wrapped = wrap_agy_gateway_launch(args, env)
    assert wrapped[-len(args) :] == args
    assert wrapped[wrapped.index("--profile") + 1] == "gateway-test"
    assert "OMNIGENT_AGY_DATABRICKS_PROFILE" not in env


@pytest.mark.parametrize("shadow", ["omnigent", "httpx"])
def test_supervisor_uses_installed_packages_from_workspace(tmp_path: Path, shadow: str) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    marker = workspace / "workspace-package-imported"
    shadow_file = workspace / f"{shadow}.py"
    if shadow == "omnigent":
        shadow_file = workspace / "omnigent/__init__.py"
        shadow_file.parent.mkdir()
    shadow_file.write_text(
        "from pathlib import Path\nPath('workspace-package-imported').touch()\n"
    )

    class ModelsHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_GET(self) -> None:
            if not self.path.startswith("/api/2.1/unity-catalog/model-services?") or (
                self.headers.get("Authorization") != "Bearer supervisor-test-token"
            ):
                self.send_error(403)
                return
            body = json.dumps(
                {"model_services": [{"name": "model-services/system.ai.gemini-3-1-pro"}]}
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    child = tmp_path / "child.py"
    child.write_text(
        "import os, sys, urllib.request, urllib.error\n"
        "from pathlib import Path\n"
        "assert Path.cwd() == Path(sys.argv[1])\n"
        "request = urllib.request.Request(\n"
        "    os.environ['GOOGLE_GEMINI_BASE_URL'] + '/unsupported', data=b'{}',\n"
        "    headers={'x-goog-api-key': os.environ['GEMINI_API_KEY']})\n"
        "try:\n"
        "    urllib.request.urlopen(request, timeout=5)\n"
        "except urllib.error.HTTPError as exc:\n"
        "    assert exc.code == 404\n"
        "else:\n"
        "    raise AssertionError('Expected the supervisor to reject this operation')\n"
        "sys.exit(23)\n"
    )
    # Use the environment's installed package, without PYTHONPATH or a safe-path override.
    env = {key: value for key, value in os.environ.items() if key in {"PATH", "HOME", "LANG"}}
    config_file = tmp_path / "databrickscfg"
    env["DATABRICKS_CONFIG_FILE"] = str(config_file)
    env["NO_PROXY"] = "127.0.0.1,localhost"
    env["DATABRICKS_TOKEN"] = "ambient-wrong-token"
    env["DATABRICKS_HOST"] = "http://127.0.0.1:1"
    env["DATABRICKS_CONFIG_PROFILE"] = "wrong-profile"
    argv = wrap_agy_gateway_launch(
        [sys.executable, str(child), str(workspace)],
        {"OMNIGENT_AGY_DATABRICKS_PROFILE": "supervisor-test"},
    )
    with ThreadingHTTPServer(("127.0.0.1", 0), ModelsHandler) as upstream:
        env.update(enable_https(upstream, tmp_path / "tls"))
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            config_file.write_text(
                f"[supervisor-test]\nhost = https://localhost:{upstream.server_port}\n"
                "token = supervisor-test-token\nauth_type = pat\n"
            )
            result = subprocess.run(
                argv, cwd=workspace, env=env, capture_output=True, text=True, timeout=30
            )
        finally:
            upstream.shutdown()
            thread.join(timeout=5)
    assert not marker.exists(), "Supervisor imported a workspace package"
    assert result.returncode == 23, result.stderr


@pytest.fixture
def profile_workspace(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Real SDK auth against a local metadata / OAuth server, with fake credentials."""
    minted: list[str] = []

    class AuthHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def reply(self, payload: dict) -> None:
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/.well-known/databricks-config":
                self.reply({})
            elif self.path == "/oidc/.well-known/oauth-authorization-server":
                self.reply(
                    {"token_endpoint": f"https://localhost:{self.server.server_port}/token"}
                )
            else:
                self.send_error(404)

        def do_POST(self) -> None:
            expected = base64.b64encode(b"profile-client:profile-secret").decode()
            if self.path != "/token" or self.headers.get("Authorization") != f"Basic {expected}":
                self.send_error(403)
                return
            self.rfile.read(int(self.headers["Content-Length"]))
            token = f"profile-oauth-token-{len(minted)}"
            minted.append(token)
            self.reply({"access_token": token, "token_type": "Bearer", "expires_in": 3600})

    cfg = tmp_path / "databrickscfg"
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    with ThreadingHTTPServer(("127.0.0.1", 0), AuthHandler) as upstream:
        for name, value in enable_https(upstream, tmp_path / "tls").items():
            monkeypatch.setenv(name, value)
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            yield cfg, f"https://localhost:{upstream.server_port}", minted
        finally:
            upstream.shutdown()
            thread.join(timeout=5)


@pytest.mark.parametrize("auth_type", ["pat", "oauth-m2m"])
@pytest.mark.parametrize("ambient_auth", ["pat", "oauth-m2m"])
def test_selected_databricks_profile_owns_host_and_identity(
    monkeypatch: pytest.MonkeyPatch, profile_workspace, auth_type: str, ambient_auth: str
) -> None:
    cfg, host, minted = profile_workspace
    credentials = (
        "token = selected-token\n"
        if auth_type == "pat"
        else "client_id = profile-client\nclient_secret = profile-secret\n"
    )
    cfg.write_text(
        f"[selected]\nhost = {host}\nauth_type = {auth_type}\n{credentials}"
        f"discovery_url = {host}/oidc/.well-known/oauth-authorization-server\n"
    )
    monkeypatch.setenv("DATABRICKS_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "wrong-profile")
    monkeypatch.setenv("DATABRICKS_AUTH_TYPE", ambient_auth)
    monkeypatch.setenv("DATABRICKS_TOKEN", "ambient-token")
    monkeypatch.setenv("DATABRICKS_CLIENT_ID", "ambient-client")
    monkeypatch.setenv("DATABRICKS_CLIENT_SECRET", "ambient-secret")
    monkeypatch.setenv("DATABRICKS_DISCOVERY_URL", f"{host}/wrong-discovery")
    before = dict(os.environ)
    source = databricks_token_source("selected")
    now = [0.0]
    source._clock = lambda: now[0]
    assert source.workspace_url == host
    assert source.resolve() == (
        "selected-token" if auth_type == "pat" else "profile-oauth-token-0"
    )
    cfg.write_text(cfg.read_text().replace("selected-token", "refreshed-token"))
    assert source.resolve() == (
        "selected-token" if auth_type == "pat" else "profile-oauth-token-0"
    )
    now[0] = 61.0
    assert source.resolve() == (
        "refreshed-token" if auth_type == "pat" else "profile-oauth-token-1"
    )
    assert minted == (
        [] if auth_type == "pat" else ["profile-oauth-token-0", "profile-oauth-token-1"]
    )
    assert dict(os.environ) == before


def test_selected_cli_oauth_profile_reaches_cli_with_isolated_environment(
    monkeypatch: pytest.MonkeyPatch, profile_workspace, tmp_path: Path
) -> None:
    cfg, host, _ = profile_workspace
    cli = tmp_path / "databricks"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, os, sys\n"
        "if sys.argv[1] == 'version':\n"
        "    print(json.dumps({'Major': 0, 'Minor': 296, 'Patch': 0}))\n"
        "else:\n"
        "    assert sys.argv[1:3] == ['auth', 'token']\n"
        "    assert sys.argv[sys.argv.index('--profile') + 1] == 'selected'\n"
        "    assert not any(key in os.environ for key in [\n"
        "        'DATABRICKS_TOKEN', 'DATABRICKS_CLIENT_ID', 'DATABRICKS_CLIENT_SECRET',\n"
        "        'DATABRICKS_HOST', 'DATABRICKS_CONFIG_PROFILE', 'DATABRICKS_AUTH_TYPE'])\n"
        "    assert os.environ['DATABRICKS_CONFIG_FILE'] == sys.argv[0] + 'cfg'\n"
        "    print(json.dumps({'access_token': 'cli-profile-token',\n"
        "                      'token_type': 'Bearer', 'expiry': '2100-01-01T00:00:00Z'}))\n"
    )
    cli.chmod(0o755)
    cfg.write_text(
        f"[selected]\nhost = {host}\nauth_type = databricks-cli\ndatabricks_cli_path = {cli}\n"
    )
    monkeypatch.setenv("DATABRICKS_HOST", "http://127.0.0.1:1")
    monkeypatch.setenv("DATABRICKS_TOKEN", "ambient-token")
    monkeypatch.setenv("DATABRICKS_AUTH_TYPE", "pat")
    monkeypatch.setenv("DATABRICKS_CONFIG_PROFILE", "wrong-profile")
    before = dict(os.environ)
    assert databricks_token_source("selected").resolve() == "cli-profile-token"
    assert dict(os.environ) == before


@pytest.mark.parametrize("invalid", ["missing", "hostless", "tokenless", "changed-host"])
def test_selected_databricks_profile_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, profile_workspace, invalid: str
) -> None:
    from omnigent.errors import OmnigentError

    cfg, host, _ = profile_workspace
    monkeypatch.setenv("DATABRICKS_HOST", host)
    monkeypatch.setenv("DATABRICKS_TOKEN", "ambient-token")
    monkeypatch.setenv("DATABRICKS_AUTH_TYPE", "pat")
    section = "other" if invalid == "missing" else "selected"
    cfg.write_text(
        f"[{section}]\nauth_type = pat\n"
        + (f"host = {host}\n" if invalid != "hostless" else "")
        + ("token = selected-token\n" if invalid != "tokenless" else "")
    )
    with pytest.raises(OmnigentError):
        source = databricks_token_source("selected")
        if invalid == "changed-host":
            cfg.write_text(cfg.read_text().replace(host, host + "/other-workspace"))
        source.resolve()


@pytest.mark.asyncio
async def test_supervisor_closes_gateway_when_real_child_exits(monkeypatch, tmp_path) -> None:
    import omnigent.harnesses.antigravity_native.gateway as gateway_module

    source = SimpleNamespace(workspace_url="https://workspace.example", resolve=lambda: "token")
    monkeypatch.setattr(gateway_module, "databricks_token_source", lambda _: source)
    monkeypatch.setattr(gateway_module, "discover_databricks_gemini_models", lambda *a: ("model",))
    endpoint_file = tmp_path / "endpoint"
    child_code = """
import os, sys, urllib.request, urllib.error
from pathlib import Path
url = os.environ['GOOGLE_GEMINI_BASE_URL']
Path(sys.argv[1]).write_text(url)
request = urllib.request.Request(url + '/unsupported', data=b'{}', headers={
    'x-goog-api-key': os.environ['GEMINI_API_KEY'],
})
try:
    urllib.request.urlopen(request, timeout=5)
except urllib.error.HTTPError as exc:
    assert exc.code == 404
else:
    raise AssertionError('gateway should reject this operation')
sys.exit(7)
"""
    assert await run("test", [sys.executable, "-c", child_code, str(endpoint_file)]) == 7
    async with httpx.AsyncClient(trust_env=False, timeout=1) as client:
        with pytest.raises(httpx.ConnectError):
            await client.get(endpoint_file.read_text())


@pytest.mark.asyncio
async def test_proxy_preserves_tool_payload_stream_and_refreshes_token() -> None:
    now = [0.0]
    tokens = iter(["first-token", "refreshed-token"])
    source = DatabricksProfileTokenProvider(
        "test",
        config_factory=lambda _: SimpleNamespace(host="https://workspace.example"),
        authenticate=lambda _: next(tokens),
        clock=lambda: now[0],
        refresh_interval=10,
    )
    requests: list[httpx.Request] = []
    stream = (
        b'data: {"candidates":[{"content":{"parts":'
        b'[{"text":"ok","thoughtSignature":"opaque"}]}}]}\n\n'
    )

    class Chunks(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield stream[:20]
            yield stream[20:]

    async def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Chunks())

    payload = json.dumps(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {
                            "functionResponse": {
                                "name": "view_file",
                                "response": {"result": "file content"},
                            }
                        }
                    ],
                }
            ],
            "tools": [
                {"functionDeclarations": [{"name": "view_file", "parametersJsonSchema": {}}]}
            ],
        }
    ).encode()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as upstream:
        gateway = GeminiDatabricksGateway(source, ("system.ai.gemini-3-1-pro",), upstream)
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway.app), base_url="http://gateway"
        ) as client:
            for tick in (0.0, 11.0):
                now[0] = tick
                response = await client.post(
                    "/v1beta/models/gemini-3.1-pro-preview:streamGenerateContent?alt=sse",
                    headers={"x-goog-api-key": gateway.key},
                    content=payload,
                )
                assert response.status_code == 200
                assert response.content == stream
            for path, headers in [
                ("/v1beta/models/gemini-3.1-pro-preview:generateContent", {}),
                ("/v1beta/models/missing:generateContent", {"x-goog-api-key": gateway.key}),
                ("/api/2.0/token/create", {"x-goog-api-key": gateway.key}),
            ]:
                response = await client.post(path, headers=headers)
                assert response.status_code in (401, 404)
    assert len(requests) == 2
    assert [r.headers["Authorization"] for r in requests] == [
        "Bearer first-token",
        "Bearer refreshed-token",
    ]
    for request in requests:
        assert request.url == (
            "https://workspace.example/ai-gateway/gemini/v1beta/models/"
            "system.ai.gemini-3-1-pro:streamGenerateContent?alt=sse"
        )
        assert "x-goog-api-key" not in request.headers
        assert request.content == payload


@pytest.mark.parametrize(
    "host",
    [
        "http://workspace.example.internal",
        "http://127.0.0.1:1234",
        "https://user:secret@workspace.example",
        "https://workspace.example?token=secret",
    ],
)
def test_databricks_rejects_unsafe_workspace_before_authentication(monkeypatch, tmp_path, host):
    from unittest.mock import Mock

    from omnigent.errors import OmnigentError

    cfg = tmp_path / "databrickscfg"
    cfg.write_text(f"[selected]\nhost = {host}\ntoken = fake-token\nauth_type = pat\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    authenticate = Mock()
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.databricks_auth.ProfileAuthConfig", authenticate
    )
    with pytest.raises(OmnigentError, match="HTTPS"):
        databricks_token_source("selected")
    authenticate.assert_not_called()


def test_auth_process_rejects_profile_changed_to_http_before_sdk(monkeypatch, tmp_path):
    from unittest.mock import Mock

    from omnigent.harnesses.antigravity_native.databricks_auth import main

    cfg = tmp_path / "databrickscfg"
    cfg.write_text("[selected]\nhost = http://workspace.example\ntoken = fake-token\n")
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(cfg))
    monkeypatch.setattr(
        sys, "argv", ["auth", "--profile", "selected", "--host", "https://workspace.example"]
    )
    authenticate = Mock(return_value=("http://workspace.example", "fake-token"))
    monkeypatch.setattr("omnigent.inner.databricks_token._sdk_bearer", authenticate)
    assert main() == 1
    authenticate.assert_not_called()
