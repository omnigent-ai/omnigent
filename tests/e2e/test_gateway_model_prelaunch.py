"""Gateway preview -> managed provision -> real Codex's first model request."""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

import httpx
import pytest

from dev.repro_env.runtime import write_model_config
from omnigent.models.codex_model_vocabulary import comparable_model_id
from tests._helpers.native_session import create_native_session
from tests._helpers.server_runner import server_runner
from tests.e2e._gateway_preview_server import MODEL
from tests.e2e.conftest import get_mock_requests, set_fallback_mock_llm

_SYSTEM_CODEX_CONFIG = Path("/etc/codex/managed_config.toml")
_PASSTHROUGH_ENV = frozenset({"PATH", "LANG", "LC_ALL", "TMPDIR", "SSL_CERT_FILE", "SSL_CERT_DIR"})


def _system_codex_config_present() -> bool:
    try:
        return bool(_SYSTEM_CODEX_CONFIG.read_text().strip())
    except OSError:
        return _SYSTEM_CODEX_CONFIG.exists()


pytestmark = [
    pytest.mark.posix_only,
    pytest.mark.timeout(300),
    pytest.mark.skipif(
        not shutil.which("codex") or not shutil.which("tmux"), reason="requires Codex and tmux"
    ),
    pytest.mark.skipif(
        _system_codex_config_present(),
        reason="requires isolated Codex system config to keep model requests local",
    ),
]


def _wait(check, description: str, timeout: float = 90):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = check()
        if value:
            return value
        time.sleep(0.1)
    raise AssertionError(f"Timed out waiting for {description}")


def _bundled_codex_slugs(codex_home: Path, home: Path) -> set[str]:
    """Model slugs the installed Codex bundles; other requests fall back to its default."""
    env = {key: value for key, value in os.environ.items() if key in _PASSTHROUGH_ENV}
    listing = subprocess.run(
        ["codex", "debug", "models", "--bundled"],
        env={**env, "CODEX_HOME": str(codex_home), "HOME": str(home)},
        capture_output=True,
        text=True,
        check=True,
        timeout=15,
    ).stdout
    return {comparable_model_id(row["slug"]) for row in json.loads(listing)["models"]}


def _check_response(response: httpx.Response) -> None:
    if response.is_error:
        response.read()
        raise AssertionError(f"HTTP {response.status_code}: {response.text}")


def test_gateway_choice_reaches_managed_codex_first_turn(
    tmp_path: Path,
    isolated_mock_llm_server_url: str,
) -> None:
    """No host exists at discovery; the selection survives actual local provisioning."""
    mock_url = isolated_mock_llm_server_url
    config = tmp_path / "config"
    source = tmp_path / "codex-config"
    source.mkdir()
    if comparable_model_id(MODEL) not in _bundled_codex_slugs(source, tmp_path):
        pytest.skip(f"installed Codex does not advertise {MODEL}")
    # Deliberately different source default: the launch selection must override it.
    write_model_config(config, mock_url, "claude-test", "gpt-5.4")
    set_fallback_mock_llm(mock_url, key="_policy_llm_", text='{"action":"allow","reason":""}')
    answer = "ASTRA_MAX_LAUNCH_VERIFIED"
    for model in (MODEL, "gpt-6-astra"):
        set_fallback_mock_llm(mock_url, key=model, text=answer)
    base_env = {key: value for key, value in os.environ.items() if key in _PASSTHROUGH_ENV}
    env = {
        "OMNIGENT_CONFIG_HOME": str(config),
        "CODEX_HOME": str(source),
        "OMNIGENT_CODEX_NATIVE_STATE_DIR": str(tmp_path / "codex-state"),
        "OMNIGENT_E2E_GATEWAY_ROOT": str(tmp_path),
        "OMNIGENT_RUNNER_TUNNEL_TOKEN": None,
        "OPENAI_API_KEY": "mock-key",
        "OPENAI_BASE_URL": f"{mock_url}/v1",
    }
    with (
        server_runner(
            tmp_path / "stack",
            server_bootstrap="from tests.e2e._gateway_preview_server import main; main()",
            server_env=env,
            base_env=base_env,
            server_cwd=Path(__file__).resolve().parents[2],
        ) as stack,
        httpx.Client(
            base_url=stack.base_url,
            trust_env=False,
            timeout=60,
            headers={"x-omnigent-background-session-titles": "off"},
            event_hooks={"response": [_check_response]},
        ) as client,
    ):
        info = client.get("/v1/info")
        assert info.json()["sandbox_provider_capabilities"]["test-gateway"]["gateway_models"]
        assert client.get("/v1/hosts").json()["hosts"] == []
        preview = client.get(
            "/v1/sandbox-providers/test-gateway/harnesses/codex-native/model-options"
        )
        preview_body = preview.json()
        assert preview_body["configured"] is False
        assert preview_body["models"][0]["id"] == MODEL
        assert not (tmp_path / "provisioned").exists()
        # Install the real native wrapper, then use the same JSON create path
        # as the landing page. The seed session has no host or runner.
        seed = create_native_session(client, stack.base_url, harness="codex")
        assert not (tmp_path / "provisioned").exists()
        response = client.post(
            "/v1/sessions",
            json={
                "agent_id": seed["agent_id"],
                "host_type": "managed",
                "sandbox_provider": "test-gateway",
                "model_override": MODEL,
                "reasoning_effort": "max",
            },
        )
        assert response.status_code == 201, response.text
        session_id = response.json()["id"]

        def snapshot():
            response = client.get(f"/v1/sessions/{session_id}")
            response.raise_for_status()
            return response.json()

        saved = snapshot()
        assert saved["model_override"] == MODEL
        assert saved["reasoning_effort"] == "max"
        try:
            bound = _wait(
                lambda: row if (row := snapshot()).get("runner_id") else None,
                "managed host and runner registration",
            )
            assert (tmp_path / "provisioned").exists()
            assert bound["host_id"]
            prompt = "Reply with the launch verification marker."
            response = client.post(
                f"/v1/sessions/{session_id}/events",
                json={
                    "type": "message",
                    "data": {"role": "user", "content": [{"type": "input_text", "text": prompt}]},
                },
            )
            assert response.status_code == 202, response.text
            requests = _wait(
                lambda: [row for row in get_mock_requests(mock_url) if prompt in json.dumps(row)],
                "Codex's first model request",
            )
            assert comparable_model_id(requests[0]["model"]) == comparable_model_id(MODEL)
            assert requests[0]["reasoning"]["effort"] == "max"
            _wait(
                lambda: answer in client.get(f"/v1/sessions/{session_id}/items").text,
                "Codex's reply",
            )
            saved = snapshot()
            assert comparable_model_id(saved["model_override"]) == comparable_model_id(MODEL)
            assert saved["reasoning_effort"] == "max"
        except Exception as exc:
            host_log = tmp_path / "host.log"
            host_tail = host_log.read_text(errors="replace")[-5000:] if host_log.exists() else ""
            raise AssertionError(f"{exc}\n{stack.log_tail()}\n{host_tail}") from exc
        finally:
            # Cleanup must not displace the diagnostic raised above.
            for sid in (session_id, seed["session_id"]):
                with contextlib.suppress(Exception):
                    client.delete(f"/v1/sessions/{sid}")
