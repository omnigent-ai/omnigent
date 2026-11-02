"""Real Codex consumes regenerated ucode auth after a synthetic archive eviction."""

from __future__ import annotations

import json
import shlex
import shutil
import sys
import threading
import tomllib
from pathlib import Path

import pytest

from tests._helpers.codex_auth import run_codex_turn, run_recorded, token_helper_environment
from tests.e2e.test_codex_gateway_stale_bearer_e2e import FRESH_TOKEN, _FakeGateway

_REPO = Path(__file__).resolve().parents[2]
_RESOLVE_CONFIG = """
import json, sys
from pathlib import Path
from omnigent.inner.codex_executor import _databricks_codex_config_overrides
from omnigent.onboarding.ucode_state import read_ucode_state
agent = read_ucode_state(sys.argv[1]).agent('codex')
overrides = _databricks_codex_config_overrides(
    model='mock-model', base_url=agent.base_url, auth_command=agent.auth_command,
    auth_refresh_interval_ms=agent.auth_refresh_interval_ms, auth_config=agent.auth)
Path(sys.argv[3]).write_text(json.dumps(overrides))
"""


@pytest.mark.posix_only
@pytest.mark.timeout(180)
@pytest.mark.parametrize("launcher_name", ["ucode", "ug"])
@pytest.mark.parametrize("auth_form", ["direct", "shell"])
def test_structured_auth_survives_ucode_archive_eviction(
    tmp_path: Path, launcher_name: str, auth_form: str
) -> None:
    """A fresh launch uses the archive; a later launch preserves auth arguments."""
    codex = shutil.which("codex")
    if codex is None:
        pytest.skip("requires a real Codex CLI")
    home = tmp_path / "home"
    tools = tmp_path / "installed tools"
    helper_log = tmp_path / "helper.jsonl"
    archived = home / ".cache" / "uv" / "archive-v0" / "first" / "bin" / launcher_name
    installed = tools / launcher_name
    environment = token_helper_environment(
        home=home,
        tools=tools,
        source_root=_REPO,
        helper_paths=(archived, installed),
        argument_log=helper_log,
        token=FRESH_TOKEN,
    )
    gateway = _FakeGateway()
    thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    thread.start()
    try:
        profile = 'synthetic gateway "profile"'
        arguments = ["auth-token", "--host", gateway.host, "--profile", profile, "--use-pat"]
        command = shlex.join([str(archived), *arguments])
        auth = {
            "command": str(archived) if auth_form == "direct" else "/bin/sh",
            "args": arguments if auth_form == "direct" else ["-c", command],
            "timeout_ms": 180000,
            "refresh_interval_ms": 60000,
        }
        state_file = home / ".ucode" / "state.json"
        state_file.parent.mkdir()
        state_file.write_text(
            json.dumps(
                {
                    "workspaces": {
                        gateway.host: {
                            "agents": {
                                "codex": {
                                    "base_url": f"{gateway.host}/v1",
                                    "auth_command": command,
                                    "auth": auth,
                                }
                            }
                        }
                    }
                }
            )
        )
        original_state = state_file.read_bytes()
        for phase in ("fresh", "evicted"):
            if phase == "evicted":
                archived.unlink()
            codex_home = home / f"codex-{phase}"
            codex_home.mkdir()
            environment["CODEX_HOME"] = str(codex_home)
            config_path = tmp_path / f"{phase}-overrides.json"
            resolved = run_recorded(
                [sys.executable, "-c", _RESOLVE_CONFIG, gateway.host, profile, str(config_path)],
                environment=environment,
                cwd=home,
                log=tmp_path / f"{phase}-resolver.log",
                timeout=30,
            )
            assert resolved.returncode == 0, resolved.stderr
            overrides = json.loads(config_path.read_text())
            provider = tomllib.loads("\n".join(overrides))["model_providers"][
                "omnigent_databricks"
            ]
            assert provider["base_url"] == f"{gateway.host}/v1"
            assert provider["auth"]["timeout_ms"] == 180000
            assert provider["auth"]["refresh_interval_ms"] == 60000
            helper_log.write_text("")
            gateway.auth_headers_seen.clear()
            result = run_codex_turn(
                executable=codex,
                overrides=[
                    *overrides,
                    'web_search="disabled"',
                    "analytics.enabled=false",
                    "feedback.enabled=false",
                ],
                prompt="Say hello without tools.",
                cwd=home,
                environment=environment,
                log=tmp_path / f"{phase}-codex.log",
            )
            assert result.returncode == 0, result.stdout + result.stderr
            assert "gateway says hello" in result.stdout
            assert gateway.auth_headers_seen
            assert set(gateway.auth_headers_seen) == {f"Bearer {FRESH_TOKEN}"}
            invocations = [json.loads(line) for line in helper_log.read_text().splitlines()]
            (tmp_path / f"{phase}-helper.json").write_text(json.dumps(invocations))
            expected = str(archived if phase == "fresh" else installed)
            assert invocations and all(call == [expected, *arguments] for call in invocations)
            assert state_file.read_bytes() == original_state
    finally:
        gateway.shutdown()
        gateway.server_close()
        thread.join(timeout=5)
