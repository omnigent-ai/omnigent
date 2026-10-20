"""An own-login model survives a real Pi terminal restart beside a default gateway."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import time
from pathlib import Path

import httpx

from omnigent.harnesses.pi_native.bridge import bridge_dir_for_session_id
from tests._helpers.host import isolated_host
from tests._helpers.pi_native import seed_pi_own_login_catalog
from tests._helpers.session import bundle_files, post_session_bundle

_SELECTION = "openai-codex/gpt-5.6-sol"


def _pane_command(metadata: dict) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            "tmux",
            "-S",
            metadata["tmux_socket"],
            "display-message",
            "-p",
            "-t",
            metadata["tmux_target"],
            "#{pane_start_command}",
        ],
        capture_output=True,
        text=True,
        timeout=10,
    )


def _wait_for_catalog(client: httpx.Client, session_id: str) -> dict:
    deadline = time.monotonic() + 150
    snapshot = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session_id}", timeout=10)
        response.raise_for_status()
        snapshot = response.json()
        if snapshot.get("external_session_id") and any(
            option["id"] == _SELECTION for option in snapshot.get("model_options") or []
        ):
            return snapshot
        time.sleep(0.5)
    raise AssertionError(f"Pi never published its session ID and catalog: {snapshot}")


def test_cold_resume_keeps_explicit_openai_codex_selection(
    live_server: str,
    http_client: httpx.Client,
    tmp_path: Path,
) -> None:
    agent = tmp_path / "home" / ".pi" / "agent"
    seed_pi_own_login_catalog(agent, "openai-codex", ["gpt-5.6-sol", "gpt-5.5"])
    (agent / "auth.json").write_text(
        json.dumps(
            {
                "openai-codex": {"type": "oauth", "access": "test-only"},
            }
        )
    )
    config = {
        "providers": {
            "openrouter": {
                "kind": "gateway",
                "default": "pi",
                "openai": {
                    "base_url": "https://openrouter.ai/api/v1",
                    "api_key": "test-only",
                    "models": {"pi": "gpt-4o-mini"},
                },
            }
        }
    }
    with isolated_host(
        tmp_path,
        live_server,
        config=config,
        env={"OMNIGENT_PI_PATH": os.environ.get("OMNIGENT_PI_PATH")},
    ) as host:
        host.wait_online(http_client)
        workspace = host.home / "workspace"
        workspace.mkdir()
        bundle = bundle_files(
            {
                "pi-native-ui.yaml": f"""
name: pi-native-ui
prompt: Pi is running in the session terminal.
executor:
  harness: pi-native
  model: {_SELECTION}
spawn: true
os_env:
  type: caller_process
  cwd: .
  sandbox:
    type: none
""".encode()
            }
        )
        create = post_session_bundle(
            http_client.post,
            "/v1/sessions",
            bundle,
            metadata={
                "host_id": host.host_id,
                "workspace": str(workspace),
                "labels": {"omnigent.ui": "terminal", "omnigent.wrapper": "pi-native-ui"},
            },
            timeout=60,
        )
        create.raise_for_status()
        session_id = create.json()["session_id"]
        terminal_url = f"/v1/sessions/{session_id}/resources/terminals"
        try:
            _wait_for_catalog(http_client, session_id)
            terminal = http_client.get(f"{terminal_url}/terminal_pi_main", timeout=30)
            terminal.raise_for_status()
            old_pane = terminal.json()["metadata"]
            http_client.delete(f"{terminal_url}/terminal_pi_main", timeout=30).raise_for_status()
            deadline = time.monotonic() + 30
            while _pane_command(old_pane).returncode == 0:
                assert time.monotonic() < deadline, "initial Pi terminal did not stop"
                time.sleep(0.5)

            # Hiding a saved model must preserve its launch identity.
            (agent / "settings.json").write_text(
                json.dumps(
                    {
                        "enabledModels": ["openai-codex/gpt-5.5"],
                    }
                )
            )

            # A stale catalog must be replaced by the new process's publication.
            http_client.post(
                f"/v1/sessions/{session_id}/events",
                timeout=30,
                json={
                    "type": "external_model_options",
                    "data": {"models": [{"id": "test/stale"}]},
                },
            ).raise_for_status()

            ensured = http_client.post(
                terminal_url,
                timeout=60,
                json={
                    "terminal": "pi",
                    "session_key": "main",
                    "ensure_native_terminal": True,
                },
            )
            ensured.raise_for_status()
            terminal = http_client.get(f"{terminal_url}/terminal_pi_main", timeout=30)
            terminal.raise_for_status()
            command = _pane_command(terminal.json()["metadata"])
            assert command.returncode == 0, command.stderr
            assert f"--model {_SELECTION}" in command.stdout
            assert f"omnigent/{_SELECTION}" not in command.stdout
            bridge = host.home / ".omnigent/pi-native" / bridge_dir_for_session_id(session_id).name
            assert not (bridge / "pi-agent/models.json").exists()
            options = _wait_for_catalog(http_client, session_id)["model_options"]
            ids = {option["id"] for option in options}
            assert ids == {_SELECTION, "openai-codex/gpt-5.5"}
            assert not any(model.startswith("omnigent/") for model in ids)
        finally:
            with contextlib.suppress(httpx.HTTPError):
                http_client.delete(f"/v1/sessions/{session_id}", timeout=10)
