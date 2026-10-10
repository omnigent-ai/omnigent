"""Browser journey: an antigravity-native worker keeps the user's own MCP servers.

Runs the real agy in a runner-owned session against a local mock Gemini that
answers each turn with the user's MCP tool names agy offered it::

    OMNIGENT_E2E_ANTIGRAVITY=mock uv run --no-sync pytest \\
        tests/e2e_ui/shells/test_antigravity_native_mcp_inheritance.py -v --ui-skip-build

Mock mode only: the fixture registers servers with ``agy mcp add`` in the
journey's isolated HOME and must never touch a real ``~/.gemini``.
"""

from __future__ import annotations

import functools
import json
import os
import re
import subprocess
import threading
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Browser, expect

from omnigent.harnesses.antigravity_native.bridge import (
    agy_gemini_dir,
    agy_home_dir,
    read_tmux_info,
)
from tests.e2e_ui.shells.test_antigravity_tmux_recovery import _antigravity_stack, _wait_until
from tests.e2e_ui.shells.test_terminal_direct_attach import _BLOCK_LOOPBACK_DIALS

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_ANTIGRAVITY") != "mock",
        reason="set OMNIGENT_E2E_ANTIGRAVITY=mock (registers MCP servers in an isolated HOME)",
    ),
    pytest.mark.timeout(600),
]

_USER_SERVERS = ("graft", "graphify")
# Each stub server exposes one ``<server>_find_code`` tool; agy names connected
# servers' tools in its system prompt, so a request-wide scan finds them.
_USER_TOOL_RE = re.compile("|".join(f"{server}_find_code" for server in _USER_SERVERS))
_PROMPT = "Which MCP tools do you have? List their names."
_REPLY_PREFIX = "User MCP tools: "

_MCP_STUB = r"""#!/bin/sh
init='{"protocolVersion":"2024-11-05","capabilities":{"tools":{}},'
init="$init"'"serverInfo":{"name":"@NAME@","version":"0.0.1"}}'
tools='{"tools":[{"name":"@NAME@_find_code","description":"stand-in",'
tools="$tools"'"inputSchema":{"type":"object","properties":{}}}]}'
while IFS= read -r line; do
  id=$(printf '%s' "$line" | sed -n 's/.*"id":\([0-9]*\).*/\1/p')
  case "$line" in
    *'"method":"initialize"'*) result="$init" ;;
    *'"method":"tools/list"'*) result="$tools" ;;
    *'"id":'*) result='{}' ;;
    *) continue ;;
  esac
  printf '{"jsonrpc":"2.0","id":%s,"result":%s}\n' "$id" "$result"
done
"""


def _agy_mcp_list(home: Path) -> str:
    """``agy mcp list`` output for ``<home>/.gemini/config/mcp_config.json``."""
    result = subprocess.run(
        ["agy", "mcp", "list"],
        env={**os.environ, "HOME": str(home)},
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    return result.stdout


def _listed_names(agy_mcp_list: str) -> set[str]:
    rows = agy_mcp_list.strip().splitlines()
    return {row.split()[0] for row in rows[1:] if row.strip()}


def _mcp_connected(mcp_dir: Path, server: str) -> bool:
    """agy writes ``antigravity-cli/mcp/<server>/<tool>.json`` once a server connects."""
    return (mcp_dir / server / f"{server}_find_code.json").is_file()


@pytest.fixture
def tool_listing_model(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, built_spa: None
) -> Iterator[list[bytes]]:
    """Isolate HOME and serve a mock Gemini whose reply names the user's MCP tools agy offered."""
    requests_seen: list[bytes] = []

    class GeminiHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: object) -> None:
            pass

        def do_POST(self) -> None:
            streaming = ":streamGenerateContent" in self.path
            if not streaming and ":generateContent" not in self.path:
                self.send_error(404)
                return
            raw = self.rfile.read(int(self.headers["Content-Length"]))
            requests_seen.append(raw)
            offered = sorted(set(_USER_TOOL_RE.findall(raw.decode("utf-8", "replace"))))
            reply = _REPLY_PREFIX + (", ".join(offered) or "none")
            response = {
                "candidates": [
                    {
                        "content": {"role": "model", "parts": [{"text": reply}]},
                        "finishReason": "STOP",
                        "index": 0,
                    }
                ],
                "usageMetadata": {"promptTokenCount": 10, "candidatesTokenCount": 5},
            }
            payload = json.dumps(response)
            body = (f"data: {payload}\n\n" if streaming else payload).encode()
            self.send_response(200)
            self.send_header(
                "Content-Type", "text/event-stream" if streaming else "application/json"
            )
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    home = tmp_path / "model-home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.bridge._BRIDGE_ROOT",
        home / ".omnigent" / "antigravity-native",
    )
    monkeypatch.setenv("GEMINI_API_KEY", "mock-gemini-key")
    with ThreadingHTTPServer(("127.0.0.1", 0), GeminiHandler) as server:
        monkeypatch.setenv("GOOGLE_GEMINI_BASE_URL", f"http://127.0.0.1:{server.server_port}/")
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield requests_seen
        finally:
            server.shutdown()
            thread.join(timeout=5)


@pytest.fixture
def user_home(tool_listing_model: list[bytes], tmp_path: Path) -> Path:
    """Register the user's own stdio MCP servers with ``agy mcp add`` in the journey HOME."""
    home = Path(os.environ["HOME"])
    for name in _USER_SERVERS:
        stub = tmp_path / "mcp-stubs" / name
        stub.parent.mkdir(exist_ok=True)
        stub.write_text(_MCP_STUB.replace("@NAME@", name), encoding="utf-8")
        stub.chmod(0o700)
        subprocess.run(
            ["agy", "mcp", "add", name, str(stub), "mcp"],
            capture_output=True,
            text=True,
            timeout=30,
            check=True,
        )
    assert _listed_names(_agy_mcp_list(home)) == set(_USER_SERVERS), (
        "interactive agy does not list the fixture servers"
    )
    return home


def _retain_evidence(files: dict[str, str | bytes]) -> None:
    """Keep the journey's raw evidence beside the recording when one is being made."""
    record_dir = os.environ.get("OMNIGENT_E2E_RECORD_DIR")
    if not record_dir:
        return
    for name, content in files.items():
        target = Path(record_dir) / name
        target.parent.mkdir(parents=True, exist_ok=True)
        if isinstance(content, bytes):
            target.write_bytes(content)
        else:
            target.write_text(content, encoding="utf-8")


def test_dispatched_agy_worker_keeps_user_mcp_servers(
    browser: Browser, tmp_path: Path, user_home: Path, tool_listing_model: list[bytes]
) -> None:
    user_config = user_home / ".gemini" / "config" / "mcp_config.json"
    user_config_before = user_config.read_bytes()
    evidence: dict[str, str | bytes] = {
        "user-mcp-config.json": user_config_before,
        "user-agy-mcp-list.txt": _agy_mcp_list(user_home),
    }

    with _antigravity_stack(tmp_path) as session:
        iso_gemini = agy_gemini_dir(session.bridge_dir)
        worker_config = iso_gemini / "config" / "mcp_config.json"
        mcp_dir = iso_gemini / "antigravity-cli" / "mcp"
        reply_text = ""

        def pane_text() -> str:
            if read_tmux_info(session.bridge_dir) is None:
                return ""
            target = session.pane()["tmux_target"]
            return session.tmux_command("capture-pane", "-p", "-t", target).stdout

        context = browser.new_context(viewport={"width": 1280, "height": 800})
        try:
            page = context.new_page()
            page.add_init_script(_BLOCK_LOOPBACK_DIALS)
            page.goto(f"{session.base_url}/c/{session.session_id}?view=terminal")
            terminal = page.get_by_test_id("main-terminal-view").get_by_test_id("terminal-view")
            expect(terminal).to_have_attribute("data-state", "connected", timeout=120_000)

            # The worker's isolated --gemini_dir is the file agy loads: it must carry
            # the user's servers alongside the omnigent relay.
            _wait_until(worker_config.is_file, "runner never wrote the worker's mcp_config.json")
            evidence["worker-mcp-config.json"] = worker_config.read_text(encoding="utf-8")
            worker_servers = set(json.loads(evidence["worker-mcp-config.json"])["mcpServers"])
            assert "omnigent" in worker_servers
            assert set(_USER_SERVERS) <= worker_servers, (
                f"worker mcp_config.json lists {sorted(worker_servers)}"
            )

            _wait_until(
                lambda: "? for shortcuts" in pane_text(), "agy prompt never became ready", 120
            )
            for server in _USER_SERVERS:
                _wait_until(
                    functools.partial(_mcp_connected, mcp_dir, server),
                    f"agy never connected the inherited {server} server",
                    90,
                )

            # Ask the worker which MCP tools it has; the mock model answers with the
            # user's tool names agy offered in the request, so the chat shows them.
            page.get_by_test_id("view-mode-chat").click()
            page.get_by_placeholder("Send a message…").fill(_PROMPT)
            page.get_by_role("button", name="Send", exact=True).click()
            reply = page.locator('[data-testid="message-bubble"][data-role="assistant"]').filter(
                has_text=_REPLY_PREFIX
            )
            expect(reply).to_have_count(1, timeout=180_000)
            reply.scroll_into_view_if_needed()
            expect(reply).to_be_visible()
            reply_text = reply.inner_text()
            page.wait_for_timeout(3_000)
        finally:
            evidence["worker-pane.txt"] = pane_text()
            context.close()

        worker_list = _agy_mcp_list(agy_home_dir(session.bridge_dir))
        evidence.update(
            {
                "worker-agy-mcp-list.txt": worker_list,
                "assistant-reply.txt": reply_text,
                **{f"model-request-{i}.json": raw for i, raw in enumerate(tool_listing_model)},
            }
        )
        _retain_evidence(evidence)

    # The worker launch must not touch the user's real config, and interactive agy
    # still sees the user's servers (the asymmetry the bug is about).
    assert user_config.read_bytes() == user_config_before
    assert _listed_names(_agy_mcp_list(user_home)) == set(_USER_SERVERS)

    # `agy mcp list` on the worker's agy-home reports the inherited servers, and the
    # worker's model was offered their tools.
    assert set(_USER_SERVERS) <= _listed_names(worker_list), (
        f"`agy mcp list` on the worker's agy-home lists {sorted(_listed_names(worker_list))}"
    )
    for server in _USER_SERVERS:
        assert f"{server}_find_code" in reply_text, (
            f"the worker's model was offered no {server} tool; reply {reply_text!r}"
        )
