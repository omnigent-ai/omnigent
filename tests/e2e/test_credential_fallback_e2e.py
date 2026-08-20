"""
End-to-end: the runner's first-available credential fallback (server → runner).

A web-UI / remote-host launch resolves credentials in the RUNNER, not the CLI or
the server. This proves that path end-to-end: with NO ambient OpenAI credential
and an openai provider that is configured but NOT marked ``default``, a real
``omnigent run`` (server → runner → openai-agents harness) credentials the head
via :func:`first_available_provider` and completes a turn. Before the fix the
head launched with no credential and failed with codex/openai's "Invalid API
key" — so a completed turn here is the regression guard for the runner fallback.

Mock-only: the assertion is that the turn routes through the *configured
provider* (pointed at the mock), which only happens if the fallback fired.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest
import yaml

from tests.e2e.conftest import (
    configure_mock_llm,
    find_free_port,
    reset_mock_llm,
    set_fallback_mock_llm,
    wait_for_server,
)

_REPO = Path(__file__).resolve().parents[2]
_SERVER_BOOT_TIMEOUT_SEC = 30.0
_RUN_TIMEOUT_SEC = 120.0

# Strip every ambient credential so the ONLY openai-family credential the runner
# can find is the configured-but-not-default provider — forcing the fallback.
_CREDENTIAL_VARS = (
    "DATABRICKS_TOKEN",
    "DATABRICKS_HOST",
    "DATABRICKS_CONFIG_PROFILE",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CODE",
    "CLAUDECODE",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENROUTER_API_KEY",
    "GEMINI_API_KEY",
    "CODEX",
)


@pytest.fixture
def local_server(tmp_path: Path, mock_llm_server_url: str) -> Iterator[str]:
    """
    Spawn a throwaway in-tree ``omnigent server`` (state only).

    A server ``llm:`` block points any server-side prompt-policy classifier at
    the mock with an ALLOW fallback, mirroring the session ``live_server`` so a
    classifier never reaches a real LLM.

    :param tmp_path: Per-test temp dir for the DB + artifacts.
    :param mock_llm_server_url: Mock LLM base URL (session fixture).
    :yields: The server base URL.
    """
    port = find_free_port()
    base_url = f"http://127.0.0.1:{port}"
    server_cfg = tmp_path / "server.yaml"
    server_cfg.write_text(
        yaml.safe_dump(
            {
                "llm": {
                    "model": "_policy_llm_",
                    "connection": {
                        "base_url": f"{mock_llm_server_url}/v1",
                        "api_key": "mock-key",
                    },
                }
            }
        )
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent",
            "server",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--database-uri",
            f"sqlite:///{tmp_path / 'cred_fallback_e2e.db'}",
            "--artifact-location",
            str(tmp_path / "artifacts"),
            "--config",
            str(server_cfg),
        ],
        cwd=str(_REPO),
        env={
            **os.environ,
            "OMNIGENT_SKIP_ONBOARD": "1",
            "OMNIGENT_NO_UPDATE_CHECK": "1",
        },
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        wait_for_server(base_url, timeout=_SERVER_BOOT_TIMEOUT_SEC)
        set_fallback_mock_llm(
            mock_llm_server_url, "_policy_llm_", '{"action": "allow", "reason": ""}'
        )
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            proc.kill()


def _fallback_run_env(mock_llm_server_url: str, config_home: Path) -> dict[str, str]:
    """
    Build the ``omnigent run`` env: no ambient credentials, and an isolated
    config whose only openai-family credential is a provider that is configured
    but NOT marked default.

    The runner (spawned by ``run``) inherits this env, so it can credential the
    head only through the first-available fallback.

    :param mock_llm_server_url: Mock LLM base URL.
    :param config_home: Isolated ``OMNIGENT_CONFIG_HOME`` (also used as HOME so
        ambient CLI-login detection finds nothing).
    :returns: The subprocess env.
    """
    env = dict(os.environ)
    env["OMNIGENT_SKIP_ONBOARD"] = "1"
    env["OMNIGENT_NO_UPDATE_CHECK"] = "1"
    env["HOME"] = str(config_home)
    for stale in _CREDENTIAL_VARS:
        env.pop(stale, None)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    "mock-openai": {  # configured, but NOT marked default
                        "kind": "key",
                        "openai": {
                            "base_url": f"{mock_llm_server_url}/v1",
                            "api_key": "mock-key",
                        },
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    return env


def _probe_agent_dir(tmp_path: Path) -> Path:
    """Write a minimal unpinned openai-agents agent (no model, no auth)."""
    agent_dir = tmp_path / "fallback-probe"
    agent_dir.mkdir()
    (agent_dir / "config.yaml").write_text(
        "spec_version: 1\n"
        "name: fallback-probe\n"
        "executor:\n"
        "  type: omnigent\n"
        "  config:\n"
        "    harness: openai-agents\n"
        'prompt: "You are a terse test agent. Reply concisely."\n',
        encoding="utf-8",
    )
    return agent_dir


def test_runner_fallback_credentials_head_with_nondefault_provider(
    local_server: str,
    mock_llm_server_url: str,
    using_mock_llm: bool,
    tmp_path: Path,
) -> None:
    """
    The runner credentials an unpinned head from a configured-but-not-default
    provider, with no ambient credential — end-to-end via ``omnigent run``
    (server → runner → openai-agents harness).

    The completed turn proves the first-available fallback fired in the real
    runner: there is no ambient OpenAI key and the provider is not a default, so
    the head is only routable through the fallback. Pre-fix, the head launched
    credential-less and the turn errored.
    """
    if not using_mock_llm:
        pytest.skip("fallback e2e is mock-only (asserts routing via the configured provider)")

    token = "fallback-probe-tr"  # unique content token routes the mock queue
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "pong from the fallback-credentialed head"}],
        match=token,
    )
    config_home = Path(tempfile.mkdtemp(prefix="omnigent-fallback-cfg-"))
    agent_dir = _probe_agent_dir(tmp_path)

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(agent_dir),
            "--server",
            local_server,
            "-p",
            f"{token} say pong",
        ],
        cwd=str(_REPO),
        env=_fallback_run_env(mock_llm_server_url, config_home),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    assert result.returncode == 0, (
        f"omnigent run failed (exit {result.returncode}) — the head was not "
        f"credentialed via the fallback.\nSTDOUT:\n{result.stdout[-3000:]}\n"
        f"STDERR:\n{result.stderr[-3000:]}"
    )
    # The reply came back → the head was credentialed via the fallback and
    # routed to the configured provider (the mock), not a phantom ambient key.
    assert "pong from the fallback-credentialed head" in result.stdout, (
        f"expected the mock reply in the run output.\nSTDOUT:\n{result.stdout[-3000:]}"
    )
