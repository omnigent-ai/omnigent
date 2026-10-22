"""Opt-in routing and model switching against an isolated two-gateway sandbox lab.

Set OMNIGENT_INFERENCE_E2E=1 and OMNIGENT_INFERENCE_SERVER_URL, KUBECONFIG,
CONTEXT, NAMESPACE, BIFROST_GATEWAY_URL, and SECOND_GATEWAY_URL (all with the
OMNIGENT_INFERENCE_ prefix). Start two mock_llm_server processes and configure
their served models and fallback responses before running this test.

The server must offer kubernetes and agent_sandbox, bind claude-native to the
provider named second, and codex-native to bifrost. Each binding must expose at
least two models from its fake gateway. The test creates and removes its own
sessions; all endpoints and the Kubernetes namespace must be dedicated to it.
No real model credentials or LLM-key pytest flags are needed.
"""

from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
import pytest

pytestmark = [
    pytest.mark.skipif(
        os.environ.get("OMNIGENT_INFERENCE_E2E") != "1",
        reason="requires an explicitly configured, isolated synthetic inference lab",
    ),
    pytest.mark.timeout(600),
]


def _setting(name: str) -> str:
    key = f"OMNIGENT_INFERENCE_{name}"
    value = os.environ.get(key, "").strip()
    assert value, f"Set {key} to select the isolated test deployment"
    return value


def _wait_ready(client: httpx.Client, session_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 240
    while time.monotonic() < deadline:
        response = client.get(f"/v1/sessions/{session_id}")
        response.raise_for_status()
        session = response.json()
        assert (session.get("sandbox_status") or {}).get("stage") != "failed"
        if session.get("host_online") and session.get("runner_online"):
            return session
        time.sleep(1)
    pytest.fail("The managed host and runner did not become ready")


def _assert_pod_configuration(host_id: str, harness: str, provider: str) -> None:
    kubeconfig = Path(_setting("KUBECONFIG")).expanduser().resolve()
    assert kubeconfig.is_file()
    kubectl = [
        "kubectl",
        "--kubeconfig",
        str(kubeconfig),
        "--context",
        _setting("CONTEXT"),
        "--namespace",
        _setting("NAMESPACE"),
    ]
    output = subprocess.run(
        [*kubectl, "get", "pods", "-o", "json"],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    ).stdout
    pods = json.loads(output)["items"]
    matching = [
        pod
        for pod in pods
        if any(
            env.get("name") == "OMNIGENT_HOST_ID" and env.get("value") == host_id
            for container in pod["spec"]["containers"]
            for env in container.get("env", [])
        )
    ]
    assert len(matching) == 1, "Host must run inside the explicitly selected lab namespace"
    check = (
        "from pathlib import Path; import json,sys,yaml; "
        "c=yaml.safe_load((Path.home()/'.omnigent/config.yaml').read_text()); "
        "assert c['inference']['harnesses'][sys.argv[1]]['provider']==sys.argv[2]; "
        "assert 'model_discovery' not in c; "
        "assert 'CATALOG_KEY' not in json.dumps(c); "
        "print('runtime configuration verified')"
    )
    result = subprocess.run(
        [
            *kubectl,
            "exec",
            matching[0]["metadata"]["name"],
            "-c",
            "host",
            "--",
            "python",
            "-c",
            check,
            harness,
            provider,
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert result.stdout.strip() == "runtime configuration verified"


def _send_turn_and_check_route(
    client: httpx.Client,
    gateway: httpx.Client,
    other_gateway: httpx.Client,
    session_id: str,
    model: str,
) -> None:
    marker = f"inference-lab-{uuid.uuid4().hex}"
    response = client.post(
        f"/v1/sessions/{session_id}/events",
        json={
            "type": "message",
            "data": {
                "role": "user",
                "content": [{"type": "input_text", "text": f"Greet {marker}. Do not use tools."}],
            },
        },
    )
    response.raise_for_status()
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        requests = gateway.get("/mock/requests")
        requests.raise_for_status()
        routed = [row for row in requests.json()["requests"] if marker in json.dumps(row)]
        session_response = client.get(f"/v1/sessions/{session_id}")
        session_response.raise_for_status()
        session = session_response.json()
        if (
            any(row.get("model") == model for row in routed)
            and session.get("status") == "idle"
            and session.get("llm_model") == model
        ):
            items_response = client.get(f"/v1/sessions/{session_id}/items", params={"limit": 100})
            items_response.raise_for_status()
            items = items_response.json()["data"]
            prompt_position = next(
                (i for i, item in enumerate(items) if marker in json.dumps(item)), None
            )
            if prompt_position is not None and any(
                item.get("type") == "message" and item.get("role") == "assistant"
                for item in items[prompt_position + 1 :]
            ):
                break
        time.sleep(1)
    else:
        pytest.fail("The native harness did not complete a turn on the selected gateway/model")
    other_requests = other_gateway.get("/mock/requests")
    other_requests.raise_for_status()
    assert all(marker not in json.dumps(row) for row in other_requests.json()["requests"])


@pytest.mark.parametrize(
    ("sandbox", "harness", "provider", "gateway_setting", "other_gateway_setting"),
    [
        ("agent_sandbox", "claude-native", "second", "SECOND_GATEWAY_URL", "BIFROST_GATEWAY_URL"),
        ("kubernetes", "codex-native", "bifrost", "BIFROST_GATEWAY_URL", "SECOND_GATEWAY_URL"),
    ],
)
def test_managed_native_session_routes_and_switches_only_allowed_models(
    sandbox: str,
    harness: str,
    provider: str,
    gateway_setting: str,
    other_gateway_setting: str,
) -> None:
    with (
        httpx.Client(base_url=_setting("SERVER_URL"), timeout=45) as client,
        httpx.Client(base_url=_setting(gateway_setting), timeout=10) as gateway,
        httpx.Client(base_url=_setting(other_gateway_setting), timeout=10) as other_gateway,
    ):
        agents_response = client.get("/v1/agents")
        agents_response.raise_for_status()
        agent = next(
            row for row in agents_response.json()["data"] if row["name"] == f"{harness}-ui"
        )
        preview = client.get(
            f"/v1/sandbox-providers/{sandbox}/harnesses/{harness}/model-options",
            params={"agent_id": agent["id"]},
        )
        preview.raise_for_status()
        catalog = preview.json()
        assert catalog["status"] == "ready"
        assert catalog["provider_label"] == provider
        default = catalog["default_model"]
        allowed = [row["id"] for row in catalog["models"]]
        assert default in allowed and len(allowed) >= 2
        alternate = next(model for model in allowed if model != default)
        served = gateway.get("/v1/models")
        served.raise_for_status()
        unlisted = next(row["id"] for row in served.json()["data"] if row["id"] not in allowed)
        response = client.post(
            "/v1/sessions",
            json={
                "agent_id": agent["id"],
                "host_type": "managed",
                "sandbox_provider": sandbox,
                "title": f"Synthetic inference profile test {uuid.uuid4().hex[:8]}",
                "model_override": default,
                "inference_configuration_revision": catalog["configuration_revision"],
            },
        )
        response.raise_for_status()
        session_id = response.json()["id"]
        try:
            session = _wait_ready(client, session_id)
            _assert_pod_configuration(session["host_id"], harness, provider)
            assert [row["id"] for row in session["model_options"]] == allowed
            _send_turn_and_check_route(client, gateway, other_gateway, session_id, default)
            switched = client.patch(
                f"/v1/sessions/{session_id}", json={"model_override": alternate}
            )
            switched.raise_for_status()
            rejected = client.patch(
                f"/v1/sessions/{session_id}", json={"model_override": unlisted}
            )
            assert rejected.status_code in (400, 422)
            session = client.get(f"/v1/sessions/{session_id}").json()
            assert session["model_override"] == alternate
            _send_turn_and_check_route(client, gateway, other_gateway, session_id, alternate)
        finally:
            deleted = client.delete(f"/v1/sessions/{session_id}")
            deleted.raise_for_status()
