"""Switch a real Claude terminal to a managed picker model through the runner."""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.claude_native import bridge
from omnigent.harnesses.claude_native import main as claude_native
from omnigent.models import model_catalog_store
from omnigent.runner import create_runner_app
from omnigent.spec.types import AgentSpec, ExecutorSpec
from tests.runner.conftest import _FakeProcessManager, _runner_client, _ScriptedHarnessClient
from tests.runner.helpers import NullServerClient

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_E2E_CLAUDE_NATIVE") != "1"
        or shutil.which("claude") is None
        or shutil.which("tmux") is None,
        reason="requires Claude Code, tmux, and OMNIGENT_E2E_CLAUDE_NATIVE=1",
    ),
    pytest.mark.timeout(180),
]

_INITIAL_MODEL = "system.ai.claude-opus-4-8[1m]"
_TARGET_MODEL = "system.ai.glm-5-3"


async def _wait_for_model(bridge_dir: Path, expected: str, socket_path: Path) -> None:
    deadline = asyncio.get_running_loop().time() + 30
    while asyncio.get_running_loop().time() < deadline:
        if bridge.read_claude_status_model(bridge_dir) == expected:
            return
        await asyncio.sleep(0.1)
    pane = subprocess.run(
        ["tmux", "-S", str(socket_path), "capture-pane", "-p", "-t", "model-switch:0.0"],
        capture_output=True,
        text=True,
        timeout=5,
    ).stdout
    pytest.fail(f"Claude did not report {expected!r}; terminal contents:\n{pane[-3000:]}")


async def test_runner_switches_managed_glm_in_a_real_claude_terminal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Picker discovery, runner dispatch, tmux injection, and status confirmation run live."""
    config_dir = tmp_path / "claude-config"
    config_dir.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(config_dir))
    for key in tuple(os.environ):
        if key.startswith(("ANTHROPIC_", "CLAUDE_CODE_")) or key == "CLAUDECODE":
            monkeypatch.delenv(key)
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "local-test-token")
    monkeypatch.setenv("ANTHROPIC_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1")
    monkeypatch.setenv("DISABLE_AUTOUPDATER", "1")
    monkeypatch.setenv("DISABLE_TELEMETRY", "1")
    monkeypatch.setenv("DISABLE_ERROR_REPORTING", "1")
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).resolve().parents[2]))
    monkeypatch.setattr(claude_native, "resolve_native_claude_config", lambda *, spec: None)

    session_id = uuid.uuid4().hex
    bridge_dir = bridge.prepare_bridge_dir(
        session_id, workspace=workspace, launch_model=_INITIAL_MODEL
    )
    # This models an existing terminal whose launch predates picker metadata.
    assert bridge.read_model_picker_values(bridge_dir) == []
    (config_dir / ".claude.json").write_text(
        json.dumps(
            {
                "hasCompletedOnboarding": True,
                "theme": "dark",
                "projects": {str(workspace): {"hasTrustDialogAccepted": True}},
            }
        )
    )
    settings_path = config_dir / "settings.json"
    settings_path.write_text(
        json.dumps(
            {
                "model": _INITIAL_MODEL,
                "modelPicker": {
                    "replaceBuiltInOptions": True,
                    "options": [
                        {"model": _INITIAL_MODEL, "label": "Opus"},
                        {"model": _TARGET_MODEL, "label": "GLM 5.3"},
                    ],
                },
                "statusLine": {
                    "type": "command",
                    "command": shlex.join(
                        [
                            sys.executable,
                            "-m",
                            "omnigent.harnesses.claude_native.status",
                            "--bridge-dir",
                            str(bridge_dir),
                        ]
                    ),
                },
            }
        )
    )
    rows = await claude_native.claude_model_catalog(None)
    assert rows is not None
    assert {_INITIAL_MODEL, _TARGET_MODEL} <= {row["id"] for row in rows}
    model_catalog_store.write_catalog(
        "claude-native", claude_native.claude_catalog_fingerprint(None), rows
    )

    spec = AgentSpec(
        spec_version=1,
        name="managed-model-switch",
        executor=ExecutorSpec(type="omnigent", config={"harness": "claude-native"}),
    )

    async def resolve_spec(agent_id: str, session_id: str | None = None) -> AgentSpec:
        return spec

    async def existing_terminal(
        session_id: str, resource_registry: Any, publish_event: Any, **kwargs: Any
    ) -> SessionResourceView:
        return SessionResourceView(
            id="terminal_claude_main",
            type="terminal",
            session_id=session_id,
            name="claude:main",
            metadata={"terminal_name": "claude", "session_key": "main", "running": True},
        )

    monkeypatch.setattr(
        "omnigent.runner.native.orchestration._auto_create_claude_terminal", existing_terminal
    )
    socket_path = tmp_path / "tmux.sock"
    bridge.write_tmux_target(bridge_dir, socket_path=socket_path, tmux_target="model-switch:0.0")
    command = [
        "claude",
        "--model",
        _INITIAL_MODEL,
        "--settings",
        str(settings_path),
        "--strict-mcp-config",
        "--mcp-config",
        '{"mcpServers":{}}',
    ]
    subprocess.run(
        [
            "tmux",
            "-f",
            "/dev/null",
            "-S",
            str(socket_path),
            "new-session",
            "-d",
            "-s",
            "model-switch",
            "-x",
            "120",
            "-y",
            "40",
            "-c",
            str(workspace),
            shlex.join(command),
        ],
        check=True,
        timeout=10,
    )
    try:
        await _wait_for_model(bridge_dir, _INITIAL_MODEL, socket_path)
        app = create_runner_app(
            process_manager=_FakeProcessManager(_ScriptedHarnessClient([])),  # type: ignore[arg-type]
            spec_resolver=resolve_spec,
            server_client=NullServerClient(),  # type: ignore[arg-type]
        )
        async with _runner_client(app) as client:
            created = await client.post(
                "/v1/sessions", json={"session_id": session_id, "agent_id": "managed-switch"}
            )
            assert created.status_code == 201, created.text
            catalog = await client.get(f"/v1/sessions/{session_id}/claude-model-options")
            assert catalog.status_code == 200, catalog.text
            assert _TARGET_MODEL in {row["id"] for row in catalog.json()["models"]}
            for model in (_TARGET_MODEL, _INITIAL_MODEL):
                response = await client.post(
                    f"/v1/sessions/{session_id}/events",
                    json={"type": "model_change", "model": model},
                )
                assert response.status_code == 204, response.text
                await _wait_for_model(bridge_dir, model, socket_path)
    finally:
        subprocess.run(
            ["tmux", "-S", str(socket_path), "kill-server"], capture_output=True, timeout=10
        )
