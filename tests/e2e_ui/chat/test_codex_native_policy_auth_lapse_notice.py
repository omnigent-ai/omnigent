"""Codex chat must say why a prompt was blocked when the runner's login lapses.

The runner authenticates to the server through a Databricks CLI profile. Once
the CLI can no longer refresh that login, the prompt-submit policy check fails
closed; the Chat view must report that instead of waiting silently.
"""

from __future__ import annotations

import os
import re
import shutil
import sys
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests._helpers.server_runner import server_runner
from tests.e2e_ui.conftest import (
    _BUILD_OUTPUT,
    _create_native_codex_session,
    _temp_omnigent_mock_config,
    configure_mock_llm,
    reset_mock_llm,
    set_fallback_mock_llm,
)
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _USER,
    _WORKING,
    _ensure_chat_view,
    _send,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _open_terminal_view,
    _wait_terminal_connected,
)

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="codex-native e2e needs the `codex` CLI and `tmux` on PATH.",
)

_TOKEN_TTL_S = 20
_NOTICE_TIMEOUT_MS = 90_000
_ERROR_PILL = '[data-testid="error-pill"]'
_FAILURE_TEXT = re.compile(r"Blocked by policy|policy evaluation unavailable", re.IGNORECASE)

# The SDK's databricks-cli provider skips executables under 1 MB as the legacy CLI.
_CLI_PADDING = ("# " + "x" * 1022 + "\n") * 1100


@dataclass(frozen=True)
class DatabricksCliCodexSession:
    base_url: str
    session_id: str
    login_lapsed: Path
    cli_calls: Path


class _WorkspaceHostHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:
        self.send_error(404)

    def log_message(self, format: str, *args: object) -> None:
        pass


@contextmanager
def _local_workspace_host() -> Iterator[str]:
    """Serve a workspace host whose metadata probe 404s, which the SDK does not retry."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), _WorkspaceHostHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _write_lapsing_databricks_cli(root: Path, host: str) -> tuple[Path, Path]:
    """Write a CLI that issues short-lived tokens until ``login-lapsed`` exists."""
    bin_dir = root / "bin"
    bin_dir.mkdir(parents=True)
    cli = bin_dir / "databricks"
    cli.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        "from datetime import datetime, timedelta\n"
        "from pathlib import Path\n"
        f"root = Path({str(root)!r})\n"
        "with open(root / 'cli-calls.log', 'a') as log:\n"
        "    log.write(datetime.now().isoformat() + ' ' + ' '.join(sys.argv[1:]) + '\\n')\n"
        "if (root / 'login-lapsed').exists():\n"
        "    sys.stderr.write('Error: invalid_grant: refresh token expired; '\n"
        "                     'run databricks auth login\\n')\n"
        "    sys.exit(1)\n"
        f"expiry = datetime.now() + timedelta(seconds={_TOKEN_TTL_S})\n"
        "print(json.dumps({'access_token': 'cli-token', 'token_type': 'Bearer',\n"
        "                  'expiry': expiry.strftime('%Y-%m-%dT%H:%M:%S')}))\n" + _CLI_PADDING,
        encoding="utf-8",
    )
    cli.chmod(0o755)
    profile = root / "databrickscfg"
    profile.write_text(
        f"[DEFAULT]\nhost = {host}\nauth_type = databricks-cli\n",
        encoding="utf-8",
    )
    return bin_dir, profile


@pytest.fixture
def databricks_cli_codex_session(
    built_spa: None,
    mock_llm_server_url: str,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> Iterator[DatabricksCliCodexSession]:
    if request.config.getoption("--ui-base-url"):
        pytest.skip("needs a dedicated runner authenticated through a Databricks CLI profile")
    for var in ("NO_PROXY", "no_proxy"):
        loopback = ",".join(filter(None, [os.environ.get(var, ""), "127.0.0.1,localhost"]))
        monkeypatch.setenv(var, loopback)
    config_home = tmp_path / "config-home"
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    cli_root = tmp_path / "databricks"
    shared_env = {"OMNIGENT_CONFIG_HOME": str(config_home)}
    with (
        _local_workspace_host() as workspace_host,
        _temp_omnigent_mock_config(mock_llm_server_url, "codex"),
        server_runner(
            tmp_path / "stack",
            server_env={**shared_env, "OMNIGENT_WEB_UI_DIST": str(_BUILD_OUTPUT)},
        ) as stack,
    ):
        cli_bin, profile = _write_lapsing_databricks_cli(cli_root, workspace_host)
        stack.start_runner(
            env={
                **shared_env,
                "DATABRICKS_CONFIG_FILE": str(profile),
                "PATH": f"{cli_bin}{os.pathsep}{os.environ['PATH']}",
            }
        )
        session_id = _create_native_codex_session(stack.base_url, stack.runner_id)
        yield DatabricksCliCodexSession(
            stack.base_url, session_id, cli_root / "login-lapsed", cli_root / "cli-calls.log"
        )


@pytest.mark.timeout(480)
def test_codex_chat_reports_prompt_blocked_after_runner_login_lapses(
    request: pytest.FixtureRequest,
    databricks_cli_codex_session: DatabricksCliCodexSession,
    mock_llm_server_url: str,
) -> None:
    session = databricks_cli_codex_session
    nonce = uuid.uuid4().hex[:8]
    control_prompt, control_reply = f"control-{nonce}", f"control-reply-{nonce}"
    trigger_prompt = f"are you still there? {nonce}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(mock_llm_server_url, [{"text": control_reply}], match=control_prompt)
    configure_mock_llm(
        mock_llm_server_url, [{"text": f"still-here-{nonce}"}], match=trigger_prompt
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page: Page = request.getfixturevalue("page")
    page.goto(f"{session.base_url}/c/{session.session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)

    _send(page, control_prompt)
    expect(page.locator(_ASSISTANT).filter(has_text=control_reply)).to_be_visible(timeout=90_000)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=60_000)
    assert "auth token" in session.cli_calls.read_text(encoding="utf-8")

    failure_notice = page.locator(_ERROR_PILL).or_(page.get_by_text(_FAILURE_TEXT))
    expect(failure_notice).to_have_count(0)

    session.login_lapsed.touch()
    # Let the cached CLI token expire so the next server call must refresh it.
    time.sleep(_TOKEN_TTL_S + 5)

    _send(page, trigger_prompt)
    expect(page.locator(_USER).filter(has_text=trigger_prompt)).to_be_visible(timeout=30_000)
    error_pill = page.locator(_ERROR_PILL)
    expect(error_pill).to_be_visible(timeout=_NOTICE_TIMEOUT_MS)
    expect(page.locator(_WORKING)).to_have_count(0, timeout=30_000)
    error_pill.click()
    expect(error_pill.get_by_test_id("error-message-content")).to_contain_text(
        "policy evaluation unavailable", ignore_case=True
    )
