"""Automatic model verdicts reach a real native Codex turn intact."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from tests._helpers.server_runner import ServerRunner, server_runner
from tests._helpers.session import bind_session_runner, bundle_files, post_session_bundle
from tests.e2e.conftest import get_mock_requests, set_fallback_mock_llm

_SERVER_BOOTSTRAP = """
import json, os
from pathlib import Path
import omnigent.server.routing_backend as backend
from omnigent.server.smart_routing import RoutingResult

class Router:
    async def route(self, message, available_models):
        Path(os.environ['KPI3_ROUTING_OFFER_FILE']).write_text(json.dumps(available_models))
        return RoutingResult(model='databricks-gpt-5-4-nano', rationale='short task')

backend.backends_from_caps = lambda caps: backend.RoutingBackends(local=Router())
from omnigent.cli import main
main()
"""

# Server title requests echo the prompt on the session model, not Codex's turn model.
_TITLES_OFF = {"x-omnigent-background-session-titles": "off"}

pytestmark = pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires native Codex and tmux",
)


def _create_native_codex_session(client: httpx.Client, model: str, mock_url: str) -> str:
    created = post_session_bundle(
        client.post,
        "/v1/sessions",
        bundle_files(
            {
                "native.yaml": yaml.safe_dump(
                    {
                        "name": "routing-test",
                        "prompt": "Reply briefly.",
                        "executor": {
                            "harness": "codex-native",
                            "model": model,
                            "auth": {
                                "type": "api_key",
                                "api_key": "mock-key",
                                "base_url": f"{mock_url}/v1",
                            },
                        },
                    }
                ).encode(),
            }
        ),
    )
    created.raise_for_status()
    return created.json()["session_id"]


def _send_and_wait_for_answer(
    client: httpx.Client, stack: ServerRunner, session: str, prompt: str, answer: str
) -> dict[str, Any]:
    sent = client.post(
        f"/v1/sessions/{session}/events",
        json={
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        },
    )
    sent.raise_for_status()
    deadline = time.monotonic() + 90
    while True:
        response = client.get(f"/v1/sessions/{session}")
        response.raise_for_status()
        snapshot = response.json()
        assert snapshot["status"] != "failed", (snapshot["last_task_error"], stack.log_tail())
        answered = any(
            item["type"] == "message"
            and item["data"].get("role") == "assistant"
            and answer in json.dumps(item["data"].get("content", []))
            for item in snapshot["items"]
        )
        if snapshot["status"] == "idle" and answered:
            return snapshot
        assert time.monotonic() < deadline, stack.log_tail()
        time.sleep(0.1)


@pytest.mark.timeout(150)
def test_native_turn_survives_an_unsupported_automatic_model_verdict(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    model, answer = "gpt-5.4", "COMPATIBLE_ROUTED_TURN"
    # The native turn and background title calls share this isolated gateway.
    set_fallback_mock_llm(isolated_mock_llm_server_url, "default", answer)
    offered = tmp_path / "offered.json"
    source = tmp_path / "source-home"
    source.mkdir()
    with server_runner(
        tmp_path / "stack",
        server_bootstrap=_SERVER_BOOTSTRAP,
        server_env={
            "KPI3_ROUTING_OFFER_FILE": str(offered),
            "OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT"),
        },
    ) as stack:
        stack.start_runner(
            env={
                "CODEX_HOME": str(source),
                "OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT"),
            }
        )
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            session = _create_native_codex_session(client, model, isolated_mock_llm_server_url)
            bind_session_runner(client.patch, stack.base_url, session, stack.runner_id)
            enabled = client.patch(
                f"/v1/sessions/{session}",
                json={
                    "cost_control_mode_override": "on",
                    "silent": True,
                },
            )
            enabled.raise_for_status()
            routed = client.post(
                f"/v1/sessions/{session}/hooks/route-turn",
                json={
                    "harness": "codex-native",
                    "prompt": "Reply once.",
                    "model": model,
                },
            )
            routed.raise_for_status()
            decision = routed.json()
            assert offered.exists(), (decision, stack.log_tail())
            assert "nano" not in json.dumps(json.loads(offered.read_text()))
            assert "nano" not in str(decision.get("model", "")), decision
            assert decision["action"] in {"allow", "route"}, decision
            snapshot = _send_and_wait_for_answer(client, stack, session, "Reply once.", answer)
            requests = get_mock_requests(isolated_mock_llm_server_url)
            assert requests, stack.log_tail()
            assert all("nano" not in str(request.get("model", "")) for request in requests)
            assert snapshot["last_task_error"] is None
            assert snapshot["pending_inputs"] == []


def _codex_under_managed_model_pin(
    root: Path, pinned_model: str, upgrade_target: str
) -> Path | None:
    """Return a PATH dir whose ``codex`` sees a managed config, or ``None`` if bwrap cannot."""
    if shutil.which("bwrap") is None:
        return None
    managed = root / "managed_config.toml"
    # Acknowledge the bundled upgrade notice so it cannot hold the TUI before the turn.
    managed.write_text(
        f'model = "{pinned_model}"\n'
        f'[notice.model_migrations]\n"{pinned_model}" = "{upgrade_target}"\n'
    )
    etc_entries = sorted(entry for entry in os.listdir("/etc") if entry != "codex")
    mounts = ["--die-with-parent", "--dev-bind", "/", "/", "--tmpfs", "/etc"]
    for entry in etc_entries:
        mounts += ["--ro-bind-try", f"/etc/{entry}", f"/etc/{entry}"]
    mounts += ["--dir", "/etc/codex", "--ro-bind", str(managed), "/etc/codex/managed_config.toml"]
    probe = subprocess.run(["bwrap", *mounts, "--", "true"], capture_output=True, timeout=10)
    if probe.returncode != 0:
        return None
    shim_dir = root / "bin"
    shim_dir.mkdir()
    shim = shim_dir / "codex"
    real_codex = shutil.which("codex")
    shim.write_text(f'#!/bin/sh\nexec {shlex.join(["bwrap", *mounts, "--", real_codex])} "$@"\n')
    shim.chmod(0o755)
    return shim_dir


@pytest.mark.timeout(150)
def test_native_turn_uses_the_session_model_over_a_managed_config_pin(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    session_model, managed_model = "gpt-5.6-luna", "gpt-5.6-sol"
    shim_dir = _codex_under_managed_model_pin(tmp_path, managed_model, "gpt-6-sol")
    if shim_dir is None:
        pytest.skip("requires bwrap mount namespaces to install a system Codex config")
    prompt, answer = "Reply once under the managed pin.", "SESSION_MODEL_TURN"
    set_fallback_mock_llm(isolated_mock_llm_server_url, "default", answer)
    source = tmp_path / "source-home"
    source.mkdir()
    with server_runner(
        tmp_path / "stack",
        server_env={"OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT")},
    ) as stack:
        stack.start_runner(
            env={
                "CODEX_HOME": str(source),
                "PATH": f"{shim_dir}{os.pathsep}{os.environ['PATH']}",
                "OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT"),
            }
        )
        with httpx.Client(
            base_url=stack.base_url, trust_env=False, timeout=30, headers=_TITLES_OFF
        ) as client:
            session = _create_native_codex_session(
                client, session_model, isolated_mock_llm_server_url
            )
            bind_session_runner(client.patch, stack.base_url, session, stack.runner_id)
            _send_and_wait_for_answer(client, stack, session, prompt, answer)
            turn_models = {
                request.get("model")
                for request in get_mock_requests(isolated_mock_llm_server_url)
                if prompt in json.dumps(request)
            }
            assert turn_models == {session_model}, (turn_models, stack.log_tail())


@pytest.mark.timeout(150)
def test_configured_model_launch_args_keep_choosing_the_model(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    prompt, answer = "Reply once with configured args.", "CONFIGURED_ARGS_TURN"
    set_fallback_mock_llm(isolated_mock_llm_server_url, "default", answer)
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    # Codex exits on a repeated --model, so the session model must not be added after this.
    (config_home / "config.yaml").write_text(
        yaml.safe_dump({"harness": {"codex-native": {"args": ["--model", "gpt-5.4"]}}})
    )
    source = tmp_path / "source-home"
    source.mkdir()
    with server_runner(
        tmp_path / "stack",
        server_env={"OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT")},
    ) as stack:
        stack.start_runner(
            env={
                "CODEX_HOME": str(source),
                "OMNIGENT_CONFIG_HOME": str(config_home),
                "OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT"),
            }
        )
        with httpx.Client(
            base_url=stack.base_url, trust_env=False, timeout=30, headers=_TITLES_OFF
        ) as client:
            session = _create_native_codex_session(
                client, "gpt-5.6-luna", isolated_mock_llm_server_url
            )
            bind_session_runner(client.patch, stack.base_url, session, stack.runner_id)
            _send_and_wait_for_answer(client, stack, session, prompt, answer)
            turn_models = {
                request.get("model")
                for request in get_mock_requests(isolated_mock_llm_server_url)
                if prompt in json.dumps(request)
            }
            assert turn_models == {"gpt-5.4"}, (turn_models, stack.log_tail())
