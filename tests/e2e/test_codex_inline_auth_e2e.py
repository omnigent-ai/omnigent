"""Real Codex requests authenticate to the endpoint declared by inline auth."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
import tomllib

from omnigent.harnesses.codex_native.app_server import resolve_native_codex_launch
from omnigent.inner.codex_harness import _build_codex_executor
from omnigent.inner.executor import ExecutorError, TurnComplete
from omnigent.runtime.workflow import _build_codex_spawn_env
from omnigent.spec.types import AgentSpec, ApiKeyAuth, ExecutorSpec
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.test_codex_gateway_stale_bearer_e2e import FRESH_TOKEN, _FakeGateway


@pytest.mark.posix_only
@pytest.mark.timeout(90)
@pytest.mark.parametrize("harness", ["codex", "codex-native"])
async def test_inline_auth_reaches_endpoint_with_expected_bearer(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    harness: str,
) -> None:
    """Both launch paths complete a turn using only the spec's key and endpoint."""
    reason = cli_unavailable_reason("codex")
    if reason is not None:
        pytest.skip(f"requires a runnable 'codex' CLI; {reason}")
    codex_path = shutil.which("codex")
    assert codex_path is not None

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")

    gateway = _FakeGateway()
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        (tmp_path / "config.yaml").write_text(
            json.dumps(
                {
                    "providers": {
                        "other": {
                            "kind": "key",
                            "default": True,
                            "openai": {
                                "base_url": f"{gateway.host}/other",
                                "api_key": "wrong-provider-key",
                            },
                        }
                    }
                }
            )
        )
        spec = AgentSpec(
            spec_version=1,
            name="inline-auth-probe",
            instructions="Say hello.",
            skills_filter="none",
            executor=ExecutorSpec(
                type="omnigent",
                config={"harness": harness},
                model="mock-model",
                auth=ApiKeyAuth(api_key=FRESH_TOKEN, base_url=f"{gateway.host}/v1"),
            ),
        )
        if harness == "codex":
            env = _build_codex_spawn_env(spec, cwd=tmp_path)
            # Never let a regression send this probe to a real provider.
            assert env["HARNESS_CODEX_GATEWAY_BASE_URL"] == f"{gateway.host}/v1"
            for name, value in env.items():
                monkeypatch.setenv(name, value)
            monkeypatch.setenv("HARNESS_CODEX_ENABLE_WEB_SEARCH", "false")
            executor = _build_codex_executor()
            try:
                events = [
                    event
                    async for event in executor.run_turn(
                        [{"role": "user", "content": "hello?", "session_id": "inline-auth"}],
                        [],
                        spec.instructions,
                    )
                ]
            finally:
                await executor.close()
            assert not [event for event in events if isinstance(event, ExecutorError)]
            completions = [event for event in events if isinstance(event, TurnComplete)]
            assert len(completions) == 1
            assert completions[0].response == "gateway says hello"
        else:
            launch = resolve_native_codex_launch(model=spec.executor.model, spec=spec)
            config = tomllib.loads("\n".join(launch.config_overrides))
            provider = config["model_providers"][config["model_provider"]]
            assert provider["base_url"] == f"{gateway.host}/v1"
            args = [codex_path, "exec", "--skip-git-repo-check", "--ephemeral", "--json"]
            for override in launch.config_overrides:
                args.extend(["-c", override])
            args.append("Say hello.")
            result = await asyncio.to_thread(
                subprocess.run,
                args,
                cwd=tmp_path,
                env={
                    "PATH": os.environ["PATH"],
                    "CODEX_HOME": str(codex_home),
                    "NO_PROXY": "127.0.0.1,localhost",
                },
                capture_output=True,
                text=True,
                timeout=45,
            )
            assert result.returncode == 0, result.stderr
            assert "gateway says hello" in result.stdout

        assert gateway.auth_headers_seen
        assert set(gateway.auth_headers_seen) == {f"Bearer {FRESH_TOKEN}"}
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=5)
