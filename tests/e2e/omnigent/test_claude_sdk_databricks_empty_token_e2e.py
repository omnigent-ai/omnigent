"""E2E — a Databricks login that mints no token must fail ``omnigent run -p``.

Runs the real ``omnigent run --harness claude-sdk --model databricks-… -p "hi"``
with the default ``~/.databrickscfg`` profile pointed at a loopback workspace that
has no cached login, so the generated ``apiKeyHelper`` prints an empty token and
every request is rejected with 401. The Claude CLI then reports "Your apiKeyHelper
script is failing" as assistant text; the run must exit non-zero with a re-login
hint instead of printing that text as the reply. Needs ``claude`` on PATH.
"""

from __future__ import annotations

import json
import subprocess
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests._helpers.live_server import isolated_process_env
from tests.e2e._harness_probes import skip_if_harness_cli_missing

_MODEL = "databricks-claude-sonnet-5-5"
# claude-sdk boots a local server, runner and the Claude CLI; the observed run
# takes ~30 s, so this leaves headroom for a cold CI box.
_RUN_TIMEOUT_SEC = 180

# Ambient credentials and harness state that would bypass the empty login.
_ENV_TO_CLEAR = (
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_MODEL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDECODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL",
    "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND",
    "HARNESS_CLAUDE_SDK_GATEWAY_HOST",
    "HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE",
    "ALL_PROXY",
    "all_proxy",
)


@contextmanager
def _rejecting_workspace() -> Iterator[tuple[str, list[tuple[str, str | None]]]]:
    """Serve a loopback workspace that answers every request with 401.

    :returns: The workspace URL and a journal of ``(path, Authorization)`` pairs.
    """
    seen: list[tuple[str, str | None]] = []

    class _Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _reject(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            self.rfile.read(length)
            seen.append((self.path, self.headers.get("Authorization")))
            body = json.dumps(
                {"type": "error", "error": {"type": "authentication_error", "message": "x"}}
            ).encode()
            self.send_response(401)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        do_GET = _reject
        do_POST = _reject

        def log_message(self, *args: object) -> None:  # keep pytest output quiet
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", seen
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.timeout(_RUN_TIMEOUT_SEC + 60)
def test_databricks_run_with_empty_helper_token_exits_non_zero(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """An auth failure must not exit 0 with the CLI's error text as the reply."""
    skip_if_harness_cli_missing("claude-sdk")

    home = tmp_path / "home"
    config_home = home / ".omnigent"
    config_home.mkdir(parents=True)
    with _rejecting_workspace() as (workspace_url, seen):
        # A browser login profile with no token cache: ``databricks auth token``
        # and the databricks-sdk fallback both come back empty.
        databrickscfg = home / ".databrickscfg"
        databrickscfg.write_text(
            f"[DEFAULT]\nhost = {workspace_url}\nauth_type = databricks-cli\n"
        )
        env = isolated_process_env(
            home,
            {
                **dict.fromkeys(_ENV_TO_CLEAR),
                "OMNIGENT_CONFIG_HOME": str(config_home),
                "OMNIGENT_SKIP_ONBOARD": "1",
                "OMNIGENT_NO_UPDATE_CHECK": "1",
                "DATABRICKS_CONFIG_FILE": str(databrickscfg),
                # CLI 1.x reads the OS keyring by default; keep it on the empty file cache.
                "DATABRICKS_AUTH_STORAGE": "plaintext",
                "TERM": "dumb",
            },
            base_env=None,
        )
        try:
            proc = subprocess.run(
                [
                    str(omnigent_python),
                    "-m",
                    "omnigent.cli",
                    "run",
                    "--harness",
                    "claude-sdk",
                    "--model",
                    _MODEL,
                    "-p",
                    "hi",
                ],
                env=env,
                cwd=str(omnigent_repo_root),
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT_SEC,
            )
        finally:
            _, survivors = reap_leaked_omnigent_processes(config_home)
            assert not survivors, f"Test runtime processes survived cleanup: {survivors}"

    model_auth = [auth for path, auth in seen if urlsplit(path).path.endswith("/v1/messages")]
    detail = (
        f"\nmodel request auth headers: {model_auth}"
        f"\nexit={proc.returncode}"
        f"\nstdout:\n{proc.stdout[-2000:]}"
        f"\nstderr:\n{proc.stderr[-2000:]}"
    )
    assert model_auth, "the run never reached the workspace's model route" + detail
    assert all((auth or "").strip() in {"", "Bearer"} for auth in model_auth), (
        "the helper minted a token, so this run did not exercise the empty-token path" + detail
    )
    assert "apiKeyHelper script is failing" not in proc.stdout, (
        "the CLI's auth error was printed as the reply" + detail
    )
    assert proc.returncode != 0, "the auth failure exited 0" + detail
    assert "Claude SDK provider authentication failed" in proc.stderr, detail
    assert "databricks auth login" in proc.stderr, detail
