"""A bad automatic model verdict cannot break a real native Codex turn."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.server_runner import server_runner
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
            deadline = time.monotonic() + 90
            while True:
                response = client.get(f"/v1/sessions/{session}")
                response.raise_for_status()
                snapshot = response.json()
                assert snapshot["status"] != "failed", (
                    snapshot["last_task_error"],
                    stack.log_tail(),
                )
                answered = any(
                    item["type"] == "message"
                    and item["data"].get("role") == "assistant"
                    and answer in json.dumps(item["data"].get("content", []))
                    for item in snapshot["items"]
                )
                if snapshot["status"] == "idle" and answered:
                    break
                assert time.monotonic() < deadline, stack.log_tail()
                time.sleep(0.1)
            requests = get_mock_requests(isolated_mock_llm_server_url)
            assert requests, stack.log_tail()
            assert all("nano" not in str(request.get("model", "")) for request in requests)
            assert snapshot["last_task_error"] is None
            assert snapshot["pending_inputs"] == []
