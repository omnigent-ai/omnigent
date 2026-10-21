"""E2E — a databricks-* model must honor an exported same-workspace ANTHROPIC_BASE_URL.

Runs the real ``omnigent run --harness claude-sdk --model databricks-… -p "hi"``
with ``ANTHROPIC_BASE_URL=<workspace>/serving-endpoints/anthropic`` exported, against
:mod:`tests.e2e._stub_databricks_workspace` (serving endpoints answer, the AI Gateway
route 403s). The default profile and Databricks CLI token cache point at the stub so
the generated ``apiKeyHelper`` mints offline. Needs ``claude``, ``databricks`` and
``jq`` on PATH.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path
from urllib.parse import urlsplit

import pytest

from omnigent.testing.process_reaper import reap_leaked_omnigent_processes
from tests.e2e._harness_probes import skip_if_harness_cli_missing
from tests.e2e._stub_databricks_workspace import (
    AI_GATEWAY_PATH,
    REPLY_TEXT,
    SERVING_ENDPOINTS_PATH,
    stub_workspace,
)
from tests.e2e.omnigent._pexpect_harness import ensure_repl_test_theme_env

_MODEL = "databricks-claude-sonnet-5-5"
_STAGED_TOKEN = "stub-oauth-access-token"
# claude-sdk boots a local server, runner and the Claude CLI; the observed run
# takes ~30 s, so this leaves headroom for a cold CI box.
_RUN_TIMEOUT_SEC = 180

# Ambient credentials, harness state and proxies that would defeat the staged
# profile or route the loopback stub through a proxy.
_ENV_TO_CLEAR = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_MODEL",
    "OMNIGENT_ANTHROPIC_API_KEY",
    "OMNIGENT_ANTHROPIC_AUTH_TOKEN",
    "OMNIGENT_ANTHROPIC_BASE_URL",
    "OMNIGENT_ANTHROPIC_MODEL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "HARNESS_CLAUDE_SDK_GATEWAY_BASE_URL",
    "HARNESS_CLAUDE_SDK_GATEWAY_AUTH_COMMAND",
    "HARNESS_CLAUDE_SDK_GATEWAY_HOST",
    "HARNESS_CLAUDE_SDK_DATABRICKS_PROFILE",
    "DATABRICKS_HOST",
    "DATABRICKS_TOKEN",
    "DATABRICKS_CONFIG_PROFILE",
    "DATABRICKS_CLIENT_ID",
    "DATABRICKS_CLIENT_SECRET",
    "DATABRICKS_AUTH_TYPE",
    "CLAUDECODE",
    "CLAUDE_CODE",
    "CLAUDE_CODE_ENTRYPOINT",
    "CLAUDE_CODE_EXECPATH",
    "OMNIGENT_RUNNER_ID",
    "OMNIGENT_HOST_ID",
    "OMNIGENT_HOST_TOKEN",
    "OMNIGENT_REMOTE_AUTH_TOKEN",
    "OMNIGENT_DATABASE_URI",
    "OMNIGENT_RUNNER_TUNNEL_TOKEN",
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def _stage_workspace_login(home: Path, workspace_url: str) -> None:
    """Point the default Databricks profile at *workspace_url* with a cached OAuth token."""
    (home / ".databrickscfg").write_text(
        f"[DEFAULT]\nhost = {workspace_url}\nauth_type = databricks-cli\n"
    )
    cache_dir = home / ".databricks"
    cache_dir.mkdir()
    entry = {
        "access_token": _STAGED_TOKEN,
        "token_type": "Bearer",
        "refresh_token": "stub-refresh-token",
        "expiry": (datetime.now(UTC) + timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    # CLI 0.x keys the cache by host; 1.x by profile name.
    (cache_dir / "token-cache.json").write_text(
        json.dumps({"version": 1, "tokens": {workspace_url: entry, "DEFAULT": entry}})
    )


def _journey_env(home: Path, exported_base_url: str) -> dict[str, str]:
    """Build the shell environment the user runs ``omnigent run`` from."""
    env = {key: value for key, value in os.environ.items() if key not in _ENV_TO_CLEAR}
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_DATA_DIR": str(home / ".omnigent"),
            "OMNIGENT_CONFIG_HOME": str(home / ".omnigent"),
            "DATABRICKS_CONFIG_FILE": str(home / ".databrickscfg"),
            # CLI 1.x reads the OS keyring by default and ignores the staged file cache.
            "DATABRICKS_AUTH_STORAGE": "plaintext",
            "ANTHROPIC_BASE_URL": exported_base_url,
            "NO_PROXY": "127.0.0.1,localhost",
            "no_proxy": "127.0.0.1,localhost",
            "TERM": "dumb",
        }
    )
    return ensure_repl_test_theme_env(env)


@pytest.mark.timeout(_RUN_TIMEOUT_SEC + 60)
def test_databricks_model_run_uses_exported_same_workspace_base_url(
    omnigent_python: Path,
    omnigent_repo_root: Path,
    tmp_path: Path,
) -> None:
    """``omnigent run --harness claude-sdk --model databricks-* -p`` must call the exported URL."""
    skip_if_harness_cli_missing("claude-sdk")
    for binary in ("databricks", "jq"):
        if shutil.which(binary) is None:
            pytest.skip(f"{binary!r} CLI is not on PATH (the generated apiKeyHelper needs it)")

    home = tmp_path / "home"
    home.mkdir()
    with stub_workspace() as workspace:
        _stage_workspace_login(home, workspace.base_url)
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
                env=_journey_env(home, workspace.serving_endpoints_url),
                cwd=str(omnigent_repo_root),
                capture_output=True,
                text=True,
                timeout=_RUN_TIMEOUT_SEC,
            )
        finally:
            # The session reaper only attributes the shared test data dir, so the daemon,
            # local server (port 6767) and runner spawned under this HOME are reaped here.
            _, survivors = reap_leaked_omnigent_processes(home / ".omnigent")
            assert not survivors, f"Test runtime processes survived cleanup: {survivors}"

    # Read the journal after the run and the stub server shut down; only POST /v1/messages
    # entries are asserted, so a straggling handler from the CLI's keep-alive socket cannot
    # produce a false pass.
    model_requests = [
        request
        for request in workspace.seen
        if request.method == "POST" and urlsplit(request.path).path.endswith("/v1/messages")
    ]
    message_posts = [urlsplit(request.path).path for request in model_requests]
    detail = (
        f"\nmodel requests seen by the workspace: {message_posts}"
        f"\nexit={proc.returncode}"
        f"\nstdout:\n{proc.stdout[-2000:]}"
        f"\nstderr:\n{proc.stderr[-2000:]}"
    )
    assert message_posts, "the run never sent a model request to the workspace" + detail
    gateway_posts = [path for path in message_posts if path.startswith(AI_GATEWAY_PATH)]
    assert not gateway_posts, (
        f"claude-sdk sent model requests to {AI_GATEWAY_PATH} instead of the exported "
        f"{SERVING_ENDPOINTS_PATH} URL" + detail
    )
    assert all(request.authorization == f"Bearer {_STAGED_TOKEN}" for request in model_requests), (
        "requests on the exported URL must carry the bearer minted from the staged profile"
        + detail
    )
    assert proc.returncode == 0, "the run exited non-zero" + detail
    assert REPLY_TEXT in proc.stdout, (
        "the reply served on the exported URL was not printed" + detail
    )
