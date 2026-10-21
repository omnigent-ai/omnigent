"""Bundled Pi resources survive upload and a separate server/runner workspace."""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent._pi_mock_gateway import write_pi_gateway_config, write_pi_resource_bundle
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_EXT_MARKER = "OMNI_PI_BUNDLE_EXT_ACTIVE"
_AGENTSMD_MARKER = "OMNI_PI_BUNDLE_AGENTSMD_MARKER"
_SKILL_MARKER = "OMNI_PI_BUNDLE_SKILL_MARKER"
_AGENT_PROMPT = "You are the bundle test agent."
_TURN_TIMEOUT = 180.0

_pytest_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(
    _pytest_pi_unavailable is not None,
    reason=(
        "pi bundle-extensions e2e requires a runnable 'pi' CLI; "
        f"{_pytest_pi_unavailable}. Install/fix Pi to run this test."
    ),
)


def _create_session(client: httpx.Client, bundle: Path, runner_id: str) -> str:
    resp = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files(
            {
                str(path.relative_to(bundle)): path.read_bytes()
                for path in bundle.rglob("*")
                if path.is_file()
            }
        ),
    )
    assert resp.status_code == 201, resp.text
    session_id = resp.json()["session_id"]
    bind_session_runner(client.patch, "", session_id, runner_id)
    return session_id


def _drive_turn_to_terminal(client: httpx.Client, session_id: str, prompt: str) -> dict[str, Any]:
    body = {
        "type": "message",
        "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
    }
    client.post(f"/v1/sessions/{session_id}/events", json=body).raise_for_status()
    seen_running = False
    last: dict[str, Any] = {}

    def terminal() -> bool:
        nonlocal seen_running, last
        last = client.get(f"/v1/sessions/{session_id}").json()
        status = last.get("status")
        if status in ("running", "waiting"):
            seen_running = True
        turn_items = [
            item
            for item in last.get("items", [])
            if item.get("type") not in ("resource_event",)
            and not (item.get("type") == "message" and item.get("data", {}).get("role") == "user")
        ]
        return status == "failed" or (status == "idle" and (seen_running or bool(turn_items)))

    poll_until(terminal, timeout_s=_TURN_TIMEOUT, interval_s=1)
    return last


def _captured_system_prompts(mock_url: str, model: str) -> list[str]:
    prompts: list[str] = []
    for req in get_mock_requests(mock_url, key=model):
        if not isinstance(req, dict):
            continue
        for msg in req.get("messages", []):
            if isinstance(msg, dict) and msg.get("role") == "system":
                content = msg.get("content")
                if isinstance(content, str):
                    prompts.append(content)
        instructions = req.get("system") or req.get("instructions")
        if isinstance(instructions, str):
            prompts.append(instructions)
    return prompts


def test_pi_bundle_extensions_reach_session_cwd(
    omnigent_repo_root: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """All bundled markers reach the model alongside the authored prompt."""
    model = f"mock-pi-bundle-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello from the mock model."}] * 4,
        key=model,
    )

    bundle = tmp_path / "myagent"
    write_pi_resource_bundle(
        bundle,
        model=model,
        markers=(_AGENT_PROMPT, _EXT_MARKER, _SKILL_MARKER, _AGENTSMD_MARKER),
        extension_suffix="js",
        sandbox={"type": "none"},
    )

    config_home = tmp_path / "omnigent-config"
    write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)

    env = {
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_PI_PATH": os.environ.get("OMNIGENT_PI_PATH"),
    }
    bootstrap = (
        "import sys\nfrom omnigent.cli import main\n"
        f"sys.argv.extend(['--agent', {str(bundle)!r}])\nmain()\n"
    )
    with server_runner(
        tmp_path / "stack",
        base_env=mock_credentials_env,
        server_env=env,
        server_bootstrap=bootstrap,
        server_cwd=omnigent_repo_root,
    ) as stack:
        stack.start_runner(env=env, cwd=omnigent_repo_root)
        with httpx.Client(base_url=stack.base_url, timeout=30) as client:
            session_id = _create_session(client, bundle, stack.runner_id)
            snapshot = _drive_turn_to_terminal(
                client, session_id, "Introduce yourself in one sentence."
            )

    assert snapshot.get("status") == "idle", snapshot
    system_prompts = _captured_system_prompts(mock_llm_server_url, model)
    assert system_prompts, "Pi never sent a system prompt to the provider"
    joined = "\n".join(system_prompts)
    for marker in (_EXT_MARKER, _AGENTSMD_MARKER, _SKILL_MARKER, _AGENT_PROMPT):
        assert marker in joined, f"bundle resource missing from provider request: {marker}"
