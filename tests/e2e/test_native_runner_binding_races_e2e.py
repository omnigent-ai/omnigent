"""Real native sends must survive a binding change during runner resolution.

Two server processes share a database and artifact store. Real runner processes
and native CLIs use a local model endpoint. A scheduling barrier pauses the
unavailable-runner path after its real lookup; it never supplies a fake client,
conversation, liveness stamp, or response. Public API calls bind the live runner
before the send resumes, reproducing both initial binding and replacement races.

Run with: uv run --no-sync pytest tests/e2e/test_native_runner_binding_races_e2e.py -v
"""

from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from pathlib import Path

import httpx
import pytest

from dev.repro_env.runtime import write_model_config
from tests._helpers.live_server import terminate_process
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests.e2e.conftest import configure_mock_llm, get_mock_requests, set_fallback_mock_llm

_REPO_ROOT = Path(__file__).resolve().parents[2]
_SERVER_BOOTSTRAP = "from tests.e2e._runner_binding_race_server import main; main()"
_MODELS = json.loads((_REPO_ROOT / "tests/server/integration/repro_models.json").read_text())

pytestmark = [
    pytest.mark.timeout(300),
    pytest.mark.skipif(shutil.which("tmux") is None, reason="requires real native terminals"),
]


def _wait(check, description: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def _items(client: httpx.Client, base_url: str, session_id: str) -> list[dict]:
    response = client.get(
        f"{base_url}/v1/sessions/{session_id}/items", params={"limit": 100, "order": "asc"}
    )
    response.raise_for_status()
    return response.json()["data"]


@pytest.mark.parametrize("harness", ["codex", "claude"])
@pytest.mark.parametrize("race", ["first_binding_other_replica", "replacement_same_replica"])
def test_native_send_rechecks_binding_after_runner_miss(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
    harness: str,
    race: str,
) -> None:
    """A concurrent real binding must not persist a false failed-start turn."""
    if shutil.which(harness) is None:
        pytest.skip(f"requires the real {harness} CLI")
    mock_url = isolated_mock_llm_server_url
    gate_root = tmp_path / "gate"
    gate_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    config_root = tmp_path / "config"
    write_model_config(config_root, mock_url, _MODELS["claude-native"], _MODELS["codex-native"])
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')
    base_env = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"}
    }
    native_env = {
        "OMNIGENT_CONFIG_HOME": str(config_root),
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
        "CODEX_HOME": str(tmp_path / "codex-config"),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(tmp_path / "codex-state"),
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
        "OPENAI_BASE_URL": f"{mock_url}/v1",
        "OPENAI_API_KEY": "mock-key",
    }
    server_env = {
        **native_env,
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": None,
        "OMNIGENT_E2E_BINDING_GATE": str(gate_root),
    }
    common = {
        "base_env": base_env,
        "server_cwd": _REPO_ROOT,
        "server_env": server_env,
        "workspace": workspace,
        "database_uri": f"sqlite:///{tmp_path / 'shared.db'}",
        "artifact_location": tmp_path / "artifacts",
        "poll_interval": 0.1,
    }
    with ExitStack() as resources:
        client = resources.enter_context(
            httpx.Client(
                trust_env=False,
                timeout=60,
                headers={"x-omnigent-background-session-titles": "off"},
            )
        )
        reader = resources.enter_context(
            server_runner(tmp_path / "reader", server_bootstrap=_SERVER_BOOTSTRAP, **common)
        )
        sibling = resources.enter_context(server_runner(tmp_path / "sibling", **common))
        session_id = str(
            create_native_session(
                client,
                reader.base_url,
                harness=harness,
                metadata={"workspace": str(workspace)},
                model=_MODELS["codex-native"] if harness == "codex" else None,
            )["session_id"]
        )
        old_runner_id = None
        owner = sibling if race == "first_binding_other_replica" else reader

        if race == "replacement_same_replica":
            sibling.start_runner(
                env={**native_env, "RUNNER_SERVER_URL": reader.base_url}, wait_ready=False
            )
            old_runner_id = sibling.runner_id
            _wait(
                lambda: (
                    client.get(f"{reader.base_url}/v1/runners/{old_runner_id}/status")
                    .json()
                    .get("online")
                ),
                "original real runner to connect",
            )
            response = client.patch(
                f"{reader.base_url}/v1/sessions/{session_id}",
                json={"runner_id": old_runner_id},
            )
            response.raise_for_status()
            terminate_process(sibling.runner)
            _wait(
                lambda: (
                    not client.get(f"{reader.base_url}/v1/runners/{old_runner_id}/status")
                    .json()
                    .get("online")
                ),
                "original runner to disconnect",
            )

        prompt = f"Binding race probe {uuid.uuid4().hex}"
        answer = f"BINDING_RECOVERED_{uuid.uuid4().hex}"
        configure_mock_llm(mock_url, [{"text": answer}], key="binding-race", match=prompt)
        # Native CLIs can also request titles using the same user prompt.
        set_fallback_mock_llm(mock_url, key="binding-race", text=answer)
        body = {
            "type": "message",
            "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
        }
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(
                client.post,
                f"{reader.base_url}/v1/sessions/{session_id}/events",
                json=body,
                headers={"x-e2e-binding-race": "pause-after-runner-miss"},
                timeout=180,
            )
            try:
                _wait(lambda: (gate_root / "lookup-missed.json").exists(), "real lookup miss")
                observed = json.loads((gate_root / "lookup-missed.json").read_text())
                assert observed["runner_id"] == old_runner_id, observed
                assert observed["session_id"] == session_id, observed
                assert not pending.done(), "send must remain in flight during the binding change"

                owner.start_runner(env=native_env)
                response = client.patch(
                    f"{owner.base_url}/v1/sessions/{session_id}",
                    json={"runner_id": owner.runner_id},
                )
                response.raise_for_status()
                snapshot = client.get(f"{owner.base_url}/v1/sessions/{session_id}")
                snapshot.raise_for_status()
                assert snapshot.json()["runner_id"] == owner.runner_id, snapshot.text
                status = client.get(f"{owner.base_url}/v1/runners/{owner.runner_id}/status")
                assert status.json()["online"] is True, status.text
                before_release = _items(client, owner.base_url, session_id)
                assert not [
                    item for item in before_release if item.get("type") in {"message", "error"}
                ], before_release
            finally:
                (gate_root / "release").touch()
            response = pending.result(timeout=60)

        recorded = _items(client, owner.base_url, session_id)
        errors = [item for item in recorded if item.get("type") == "error"]
        print(
            json.dumps(
                {
                    "race": race,
                    "harness": harness,
                    "observed_binding": observed,
                    "replacement_runner_id": owner.runner_id,
                    "http_status": response.status_code,
                    "response": response.json(),
                    "errors": errors,
                }
            ),
            flush=True,
        )
        assert not errors, f"A real live replacement was incorrectly failed: {errors}"
        if owner is sibling:
            assert response.status_code == 400, response.text
            assert response.json()["error"]["code"] == "wrong_replica", response.text
            assert not [item for item in recorded if item.get("type") == "message"], (
                "a redirect must not persist the undelivered input"
            )
            response = client.post(f"{owner.base_url}/v1/sessions/{session_id}/events", json=body)
        assert response.status_code == 202, response.text

        result = _wait(
            lambda: (
                rows
                if answer in json.dumps(rows := _items(client, owner.base_url, session_id))
                else None
            ),
            "the native CLI's reply to the original message",
        )
        assert sum(answer in json.dumps(item) for item in result) == 1, result
        assert sum(prompt in json.dumps(item) for item in result) == 1, result
        assert not [item for item in result if item.get("type") == "error"], result
        assert any(prompt in json.dumps(request) for request in get_mock_requests(mock_url)), (
            "the real native CLI must have submitted the original prompt to the model endpoint"
        )
