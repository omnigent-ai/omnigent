"""Real local host/server/SDK processes, scripted model: a config_path child gets its model."""

import json
import os
from pathlib import Path

import httpx
import pytest
import yaml

from dev.repro_env.runtime import write_model_config
from tests._helpers.polling import poll_until
from tests._helpers.server_runner import server_runner
from tests._helpers.session import bundle_files, post_session_bundle
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO = Path(__file__).resolve().parents[2]
_PARENT_MODEL = "mock-parent"
_BUNDLE_MODEL = "mock-bundle-default"
_REQUESTED_MODEL = "mock-requested"


def _spec(name: str, model: str, mock: str) -> dict[str, object]:
    """An ``openai-agents`` spec whose LLM calls go to the mock, keyed by ``model``."""
    return {
        "name": name,
        "prompt": "Follow the scripted turn.",
        "spawn": True,
        "executor": {
            "harness": "openai-agents",
            "model": model,
            "auth": {"type": "api_key", "api_key": "mock-key", "base_url": f"{mock}/v1"},
        },
        "skills_filter": "none",
    }


# Older servers reject a multipart model_override, and older runners drop the
# model and refuse the effort.
pytestmark = [
    pytest.mark.min_server_version("0.20.0"),
    pytest.mark.min_runner_version("0.20.0"),
]


@pytest.mark.timeout(300)
def test_config_path_child_runs_requested_model_and_effort(tmp_path, isolated_mock_llm_server_url):
    """
    ``sys_session_create(config_path=..., model=..., reasoning_effort=...)``:
    the child's LLM request names the requested model and effort, not the
    uploaded bundle's ``executor.model``.
    """
    effort = "low"
    mock = isolated_mock_llm_server_url
    config = tmp_path / "config"
    write_model_config(config, mock, "claude-sonnet-4-20250514", "gpt-4o")
    base_env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "TMPDIR"}}
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "OMNIGENT_DISABLE_CATALOG_LOOKUP": "1",
        "OMNIGENT_NO_UPDATE_CHECK": "1",
        "OMNIGENT_AUTH_PROVIDER": "header",
        "OMNIGENT_LOCAL_SINGLE_USER": "1",
    }
    with server_runner(
        tmp_path / "stack", server_cwd=_REPO, server_env=env, base_env=base_env
    ) as stack:
        stack.start_host(env=env, cwd=_REPO)
        with httpx.Client(base_url=stack.base_url, timeout=60, trust_env=False) as client:
            poll_until(
                lambda: [
                    h
                    for h in client.get("/v1/hosts").json().get("hosts", [])
                    if h.get("status") == "online"
                ],
                timeout_s=45,
            )
            host_id = client.get("/v1/hosts").json()["hosts"][0]["host_id"]
            set_fallback_mock_llm(mock, key="_policy_llm_", text='{"action":"allow","reason":""}')

            (stack.workspace / "child.yaml").write_text(
                yaml.safe_dump(_spec("bundle-child", _BUNDLE_MODEL, mock))
            )
            configure_mock_llm(mock, [{"text": "CHILD_RAN_REQUESTED_MODEL"}], key=_REQUESTED_MODEL)
            configure_mock_llm(mock, [{"text": "CHILD_RAN_BUNDLE_MODEL"}], key=_BUNDLE_MODEL)
            create_args = {
                "config_path": "child.yaml",
                "title": "Bundle child",
                "model": _REQUESTED_MODEL,
                "reasoning_effort": effort,
                "message": "CHILD_TASK",
            }
            configure_mock_llm(
                mock,
                [
                    {
                        "tool_calls": [
                            {
                                "name": "sys_session_create",
                                "arguments": json.dumps(create_args),
                                "call_id": "create",
                            }
                        ]
                    },
                    {"text": "PARENT_DONE"},
                ],
                key=_PARENT_MODEL,
            )

            created = post_session_bundle(
                client.post,
                "/v1/sessions",
                bundle_files(
                    {"agent.yaml": yaml.safe_dump(_spec("parent", _PARENT_MODEL, mock)).encode()}
                ),
                metadata={
                    "title": "Parent",
                    "host_id": host_id,
                    "workspace": str(stack.workspace),
                },
            )
            created.raise_for_status()
            parent = created.json()["session_id"]

            def items(session: str) -> str:
                response = client.get(f"/v1/sessions/{session}/items", params={"limit": 100})
                response.raise_for_status()
                return json.dumps(response.json()["data"])

            sent = client.post(
                f"/v1/sessions/{parent}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": "GO"}]},
                },
            )
            assert sent.status_code == 202, sent.text
            poll_until(lambda: "PARENT_DONE" in items(parent), timeout_s=90)
            assert "supported only" not in items(parent), "the create was refused"

            outputs = [
                item
                for item in json.loads(items(parent))
                if item.get("type") == "function_call_output"
            ]
            assert outputs, items(parent)[-1200:]
            handle = json.loads(outputs[-1]["output"])
            child = handle.get("conversation_id")
            assert child, handle
            poll_until(lambda: "CHILD_RAN_" in items(child), timeout_s=90)
            assert not get_mock_requests(mock, key=_BUNDLE_MODEL), "child ran the bundle's model"
            requested = get_mock_requests(mock, key=_REQUESTED_MODEL)
            assert requested, "child never asked for the model"
            assert all(r.get("reasoning") == {"effort": effort} for r in requested), requested
            assert "CHILD_RAN_REQUESTED_MODEL" in items(child)

            snapshot = client.get(f"/v1/sessions/{child}").json()
            assert snapshot["parent_session_id"] == parent
            assert snapshot["model_override"] == _REQUESTED_MODEL
            assert snapshot["reasoning_effort"] == effort
