"""A claude-sdk session sees its host's Claude MCP servers and their sign-in state."""

from __future__ import annotations

import contextlib
import json
import shutil
import threading
import time
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.compat import runner_executable
from tests._helpers.host import isolated_host
from tests._helpers.live_server import reserve_port
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e_ui.conftest import configure_mock_llm

_MANAGED_SERVER = "linear"
_USER_SERVER = "notes"
_SIGN_IN_URL = "https://example.test/sign-in/linear"
_MODEL = "claude-sonnet-4-20250514"
_NOTE_TEXT = "the shared note says hello"
_NOTE_TOOL = f"mcp__{_USER_SERVER}__read_note"
_SESSION_TOOL = "mcp__omnigent__sys_session_get_info"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'


def _host_config(mock_url: str) -> dict[str, object]:
    """Route the host's Claude provider to the mock model server."""
    anthropic = {"base_url": mock_url, "api_key": "mock-key", "models": {"default": _MODEL}}
    return {
        "providers": {
            "e2e-claude": {"kind": "key", "default": ["anthropic"], "anthropic": anthropic}
        }
    }


def _host_command(base_url: str, managed_path: Path) -> list[str]:
    """Start the daemon with the host inventory reading managed-mcp.json at a writable path."""
    # The real managed-mcp.json lives under /etc; only the host inventory reads this copy.
    bootstrap = (
        "import runpy, sys\n"
        "from pathlib import Path\n"
        "import omnigent.host.mcp_inventory as inventory\n"
        f"inventory._managed_mcp_paths = lambda: (Path({str(managed_path)!r}),)\n"
        f"sys.argv = ['omnigent.host._daemon_entry', '--server', {base_url!r}]\n"
        "runpy.run_module('omnigent.host._daemon_entry', run_name='__main__')\n"
    )
    return [runner_executable(), "-c", bootstrap]


def _write_auth_status(home: Path, name: str, status: str) -> None:
    entry = {"harness": "claude", "name": name, "status": status}
    if status == "sign_in_required":
        entry["sign_in_url"] = _SIGN_IN_URL
    path = home / ".omnigent" / "mcp-auth-status.json"
    staged = path.with_suffix(".tmp")
    staged.write_text(json.dumps({"servers": [entry]}))
    staged.replace(path)


@contextlib.contextmanager
def _claude_sdk_session(
    client: httpx.Client, mock_url: str, name: str, host_id: str, workspace: Path
) -> Iterator[str]:
    """Create a claude-sdk session on the host and delete it afterwards."""
    yaml_text = (
        f"name: {name}\n"
        "prompt: You are a terse assistant.\n"
        "executor:\n"
        "  harness: claude-sdk\n"
        f"  model: {_MODEL}\n"
        "  context_window: 200000\n"
        "  auth:\n"
        "    type: api_key\n"
        "    api_key: mock-key\n"
        f"    base_url: {mock_url}\n"
    )
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files({f"{name}.yaml": yaml_text.encode()}),
        metadata={"workspace": str(workspace), "host_id": host_id},
        timeout=120.0,
    )
    created.raise_for_status()
    session_id = str(created.json()["session_id"])
    try:
        yield session_id
    finally:
        with contextlib.suppress(httpx.HTTPError):
            client.delete(f"/v1/sessions/{session_id}")


def _skip_imports_review(page: Page, host_id: str) -> None:
    reviewed_key = json.dumps(f"omnigent:imports-reviewed:{host_id}")
    page.add_init_script(f"localStorage.setItem({reviewed_key}, 'true')")


@pytest.mark.timeout(600)
def test_claude_sdk_mcp_panel_lists_host_managed_server_sign_in_state(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    if shutil.which("claude") is None:
        pytest.skip("needs the claude CLI")
    managed = tmp_path / "managed" / "managed-mcp.json"
    managed.parent.mkdir(parents=True)
    managed.write_text(
        json.dumps(
            {
                "mcpServers": {
                    _MANAGED_SERVER: {"type": "http", "url": "https://mcp.linear.app/mcp"}
                }
            }
        )
    )
    home = tmp_path / "host" / "home"
    (home / ".omnigent").mkdir(parents=True)
    _write_auth_status(home, _MANAGED_SERVER, "sign_in_required")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with (
        httpx.Client(base_url=live_server, timeout=30) as client,
        isolated_host(
            tmp_path / "host",
            live_server,
            env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
            config=_host_config(mock_llm_server_url),
            command=_host_command(live_server, managed),
            ready=lambda started: started.wait_online(client, timeout=120),
        ) as host,
        _claude_sdk_session(
            client, mock_llm_server_url, f"sdk-mcp-{host.host_id[:8]}", host.host_id, workspace
        ) as session_id,
    ):
        page = request.getfixturevalue("page")
        _skip_imports_review(page, host.host_id)
        page.goto(f"{live_server}/c/{session_id}")
        composer = page.get_by_label("Message the agent")
        expect(composer).to_be_visible(timeout=60_000)
        composer.fill("/mcp")
        row = page.get_by_test_id("mcp-server-menu").get_by_test_id(
            f"mcp-server-row-{_MANAGED_SERVER}"
        )
        expect(row).to_contain_text("Sign-in required", timeout=15_000)
        expect(row.get_by_role("link", name="Sign in")).to_have_attribute("href", _SIGN_IN_URL)

        _write_auth_status(home, _MANAGED_SERVER, "connected")
        # The panel re-reads the host every 5 s while a row awaits sign-in.
        expect(row).to_contain_text("Connected", timeout=20_000)


class _SignInGate:
    """An HTTP MCP server that answers 401 until its sign-in completes."""

    def __init__(self) -> None:
        self.signed_in = False
        self.port = 0
        self._cleanup = contextlib.ExitStack()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}/mcp"

    def _app(self):
        from mcp.server.fastmcp import FastMCP

        server = FastMCP(_USER_SERVER, stateless_http=True)

        @server.tool(name="read_note")
        def read_note() -> str:
            """Read the shared note."""
            return _NOTE_TEXT

        inner = server.streamable_http_app()

        async def app(scope, receive, send):
            if scope["type"] == "http" and not self.signed_in:
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"error":"sign_in_required"}'})
                return
            await inner(scope, receive, send)

        return app

    def __enter__(self) -> _SignInGate:
        import uvicorn

        with contextlib.ExitStack() as stack:
            sock = stack.enter_context(reserve_port())
            self.port = sock.getsockname()[1]
            server = uvicorn.Server(uvicorn.Config(self._app(), log_level="warning"))
            thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
            thread.start()
            stack.callback(self._stop, server, thread)
            deadline = time.monotonic() + 15
            while not server.started and thread.is_alive() and time.monotonic() < deadline:
                time.sleep(0.05)
            assert server.started, "gated MCP server did not start"
            self._cleanup = stack.pop_all()
        return self

    def __exit__(self, *exc: object) -> None:
        self._cleanup.close()

    @staticmethod
    def _stop(server, thread: threading.Thread) -> None:
        server.should_exit = True
        thread.join(timeout=10)
        assert not thread.is_alive(), "gated MCP server did not stop"


def _send(page: Page, text: str) -> None:
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=60_000)
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _wait_reply(page: Page, reply: str) -> None:
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        approve = page.get_by_role("button", name="Approve")
        if approve.count():
            approve.first.click()
        if page.locator(_ASSISTANT).filter(has_text=reply).count():
            break
        page.wait_for_timeout(500)
    expect(page.locator(_ASSISTANT).filter(has_text=reply)).to_be_visible(timeout=5_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)


@pytest.mark.timeout(900)
def test_claude_sdk_session_reaches_mcp_server_signed_in_mid_session(
    request: pytest.FixtureRequest,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    if shutil.which("claude") is None:
        pytest.skip("needs the claude CLI")
    uid = uuid.uuid4().hex[:6]
    first, second = f"TURNONE-{uid}", f"TURNTWO-{uid}"
    # Only the session's main model advertises Omnigent tools, so CLI side requests
    # cannot drain these scripts.
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "ack one"}] * 4,
        key=first,
        match=first,
        required_tools=[_SESSION_TOOL],
    )
    configure_mock_llm(
        mock_llm_server_url,
        [
            {"tool_calls": [{"call_id": f"read-{uid}", "name": _NOTE_TOOL, "arguments": "{}"}]},
            *[{"text": "turn two finished"}] * 4,
        ],
        key=second,
        match=second,
        required_tools=[_SESSION_TOOL],
    )
    managed = tmp_path / "managed" / "managed-mcp.json"
    managed.parent.mkdir(parents=True)
    home = tmp_path / "host" / "home"
    (home / ".omnigent").mkdir(parents=True)
    with _SignInGate() as gate:
        (home / ".claude.json").write_text(
            json.dumps({"mcpServers": {_USER_SERVER: {"type": "http", "url": gate.url}}})
        )
        _write_auth_status(home, _USER_SERVER, "sign_in_required")
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        with (
            httpx.Client(base_url=live_server, timeout=30) as client,
            isolated_host(
                tmp_path / "host",
                live_server,
                env={"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1"},
                config=_host_config(mock_llm_server_url),
                command=_host_command(live_server, managed),
                ready=lambda started: started.wait_online(client, timeout=120),
            ) as host,
            _claude_sdk_session(
                client, mock_llm_server_url, f"sdk-reconnect-{uid}", host.host_id, workspace
            ) as session_id,
        ):
            page = request.getfixturevalue("page")
            _skip_imports_review(page, host.host_id)
            page.goto(f"{live_server}/c/{session_id}")
            _send(page, f"Say ack. {first}")
            _wait_reply(page, "ack one")

            gate.signed_in = True
            _write_auth_status(home, _USER_SERVER, "connected")
            _send(page, f"Read the note. {second}")
            _wait_reply(page, "turn two finished")
            deadline = time.monotonic() + 15
            items = ""
            while _NOTE_TEXT not in items and time.monotonic() < deadline:
                items = json.dumps(client.get(f"/v1/sessions/{session_id}/items").json())
                time.sleep(0.5)

    assert _NOTE_TEXT in items, (
        "the session never reached the MCP server signed in after it started"
    )
