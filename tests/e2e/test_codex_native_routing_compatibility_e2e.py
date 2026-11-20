"""A bad automatic model verdict cannot break a real native Codex turn."""

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

from tests._helpers.server_runner import server_runner
from tests._helpers.session import (
    bind_session_runner,
    bundle_files,
    post_session_bundle,
    send_user_text,
)
from tests.e2e.conftest import get_mock_requests, set_fallback_mock_llm

_SERVER_BOOTSTRAP = """
import json, os
from pathlib import Path
import omnigent.server.routing_backend as backend
from omnigent.server.smart_routing import RoutingResult

class Router:
    async def route(self, message, available_models):
        Path(os.environ['KPI3_ROUTING_OFFER_FILE']).write_text(json.dumps(available_models))
        model = os.environ.get('ROUTING_TEST_MODEL', 'databricks-gpt-5-4-nano')
        return RoutingResult(model=model, rationale='short task')

backend.backends_from_caps = lambda caps: backend.RoutingBackends(local=Router())
from omnigent.cli import main
main()
"""


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires native Codex and tmux",
)
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
                                        "base_url": f"{isolated_mock_llm_server_url}/v1",
                                    },
                                },
                            }
                        ).encode(),
                    }
                ),
            )
            created.raise_for_status()
            session = created.json()["session_id"]
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
            sent = client.post(
                f"/v1/sessions/{session}/events",
                json={
                    "type": "message",
                    "data": {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "Reply once."},
                        ],
                    },
                },
            )
            sent.raise_for_status()
            snapshot = _wait_for_answer(client, session, answer, stack.log_tail)
            requests = get_mock_requests(isolated_mock_llm_server_url)
            assert requests, stack.log_tail()
            assert all("nano" not in str(request.get("model", "")) for request in requests)
            assert snapshot["last_task_error"] is None
            assert snapshot["pending_inputs"] == []


def _wait_for_answer(
    client: httpx.Client, session: str, answer: str, log_tail: Any
) -> dict[str, Any]:
    deadline = time.monotonic() + 90
    while True:
        response = client.get(f"/v1/sessions/{session}")
        response.raise_for_status()
        snapshot = response.json()
        assert snapshot["status"] != "failed", (snapshot["last_task_error"], log_tail())
        answered = any(
            item["type"] == "message"
            and item["data"].get("role") == "assistant"
            and answer in json.dumps(item["data"].get("content", []))
            for item in snapshot["items"]
        )
        if snapshot["status"] == "idle" and answered:
            return snapshot
        assert time.monotonic() < deadline, log_tail()
        time.sleep(0.1)


def _codex_with_managed_model(root: Path, model: str) -> Path:
    """Return a codex launcher that sees a system managed config pinning *model*."""
    root.mkdir()
    managed = root / "managed_config.toml"
    # The acknowledged upgrade notice keeps the TUI from stopping on a migration prompt.
    managed.write_text(f'model = "{model}"\n[notice.model_migrations]\n"{model}" = "gpt-6-sol"\n')
    mounts = ["--die-with-parent", "--dev-bind", "/", "/", "--tmpfs", "/etc"]
    for entry in sorted(os.listdir("/etc")):
        if entry != "codex":
            mounts += ["--ro-bind-try", f"/etc/{entry}", f"/etc/{entry}"]
    mounts += ["--dir", "/etc/codex", "--ro-bind", str(managed), "/etc/codex/managed_config.toml"]
    probe = subprocess.run(
        ["bwrap", *mounts, "--", "cat", "/etc/codex/managed_config.toml"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    if probe.returncode != 0:
        pytest.skip(f"bwrap cannot mount a stand-in /etc/codex: {probe.stderr.strip()}")
    launcher = root / "codex"
    command = shlex.join(["bwrap", *mounts, "--", str(shutil.which("codex"))])
    launcher.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
    launcher.chmod(0o755)
    return launcher


def _seed_codex_model_catalog(home: Path, slugs: tuple[str, ...]) -> None:
    """List *slugs* in Codex's catalog; routing only applies picks the host can run."""
    bundled = json.loads(
        subprocess.run(
            [str(shutil.which("codex")), "debug", "models", "--bundled"],
            capture_output=True,
            check=True,
            env={**os.environ, "CODEX_HOME": str(home)},
            timeout=15,
        ).stdout
    )
    template = next(model for model in bundled["models"] if model["visibility"] == "list")
    models = [
        {
            **template,
            "slug": slug,
            "display_name": slug,
            "supported_in_api": True,
            "availability_nux": None,
            "upgrade": None,
        }
        for slug in slugs
    ]
    catalog = home / "models.json"
    catalog.write_text(json.dumps({**bundled, "models": models}))
    (home / "config.toml").write_text(f"model_catalog_json = {json.dumps(str(catalog))}\n")


def _last_user_text(request: dict[str, Any]) -> str:
    texts = [
        block.get("text", "")
        for item in request.get("input") or []
        if isinstance(item, dict) and item.get("role") == "user"
        for block in (item.get("content") if isinstance(item.get("content"), list) else [])
        if isinstance(block, dict)
    ]
    return texts[-1].strip() if texts else ""


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None or shutil.which("bwrap") is None,
    reason="requires native Codex, tmux and bwrap",
)
@pytest.mark.timeout(240)
def test_routed_model_runs_the_turn_under_a_managed_codex_model(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    routed, managed, prompt = "gpt-5.6-luna", "gpt-5.6-sol", "hi there"
    answer = "MANAGED_PIN_ROUTED_TURN"
    set_fallback_mock_llm(isolated_mock_llm_server_url, "default", answer)
    codex = _codex_with_managed_model(tmp_path / "managed", managed)
    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        json.dumps(
            {
                "providers": {
                    "routing-test": {
                        "kind": "key",
                        "default": ["openai"],
                        "openai": {
                            "base_url": f"{isolated_mock_llm_server_url}/v1",
                            "api_key": "mock-key",
                            "wire_api": "responses",
                        },
                    }
                }
            }
        )
    )
    source = tmp_path / "source-home"
    source.mkdir()
    _seed_codex_model_catalog(source, (routed, managed))
    offered = tmp_path / "offered.json"
    common = {
        "OMNIGENT_CONFIG_HOME": str(config_home),
        "OMNIGENT_HARNESS_TMP_PARENT": os.environ.get("OMNIGENT_HARNESS_TMP_PARENT"),
    }
    with server_runner(
        tmp_path / "stack",
        server_bootstrap=_SERVER_BOOTSTRAP,
        server_env={
            **common,
            "KPI3_ROUTING_OFFER_FILE": str(offered),
            "ROUTING_TEST_MODEL": routed,
        },
    ) as stack:
        stack.start_host(
            env={
                **common,
                "CODEX_HOME": str(source),
                "OMNIGENT_CODEX_PATH": str(codex),
                "PATH": f"{codex.parent}{os.pathsep}{os.environ['PATH']}",
            },
            cwd=stack.workspace,
        )
        with httpx.Client(base_url=stack.base_url, trust_env=False, timeout=30) as client:
            agents = client.get("/v1/agents")
            agents.raise_for_status()
            agent_id = next(
                agent["id"]
                for agent in agents.json()["data"]
                if agent["name"] == "codex-native-ui"
            )
            created = client.post(
                "/v1/sessions",
                json={
                    "agent_id": agent_id,
                    "host_id": stack.host_id,
                    "workspace": str(stack.workspace),
                    "cost_control_mode_override": "on",
                    "smart_routing_message": prompt,
                },
                timeout=120,
            )
            created.raise_for_status()
            session = created.json()["id"]
            send_user_text(client, session_id=session, text=prompt)
            snapshot = _wait_for_answer(client, session, answer, stack.log_tail)
            assert offered.exists(), stack.log_tail()
            assert snapshot["model_override"] == routed, snapshot["model_override"]
            requests = get_mock_requests(isolated_mock_llm_server_url)
            turn_models = [
                request.get("model") for request in requests if _last_user_text(request) == prompt
            ]
            assert turn_models, [request.get("model") for request in requests]
            assert set(turn_models) == {routed}, turn_models


@pytest.mark.skipif(
    shutil.which("codex") is None or shutil.which("tmux") is None,
    reason="requires native Codex and tmux",
)
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
        # Server title requests echo the prompt on the session model, not Codex's turn model.
        with httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=30,
            headers={"x-omnigent-background-session-titles": "off"},
        ) as client:
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
                                    "model": "gpt-5.6-luna",
                                    "auth": {
                                        "type": "api_key",
                                        "api_key": "mock-key",
                                        "base_url": f"{isolated_mock_llm_server_url}/v1",
                                    },
                                },
                            }
                        ).encode(),
                    }
                ),
            )
            created.raise_for_status()
            session = created.json()["session_id"]
            bind_session_runner(client.patch, stack.base_url, session, stack.runner_id)
            send_user_text(client, session_id=session, text=prompt)
            _wait_for_answer(client, session, answer, stack.log_tail)
            turn_models = {
                request.get("model")
                for request in get_mock_requests(isolated_mock_llm_server_url)
                if prompt in json.dumps(request)
            }
            assert turn_models == {"gpt-5.4"}, (turn_models, stack.log_tail())
