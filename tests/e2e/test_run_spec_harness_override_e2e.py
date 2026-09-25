"""An agent spec's headless harness wins over a native terminal project default."""

from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path

import httpx
import yaml

_REPO_ROOT = Path(__file__).resolve().parents[2]

_MODEL = "e2e-spec-harness-model"
_PROVIDER = "e2e-spec-harness-gateway"
_SPEC_RELPATH = Path(".omnigent") / "agents" / "my-agent" / "config.yaml"
_NATIVE_REJECTION = "ignores an AGENT spec"
_RUN_TIMEOUT_S = 300


def _seed_config_home(config_home: Path, gateway_base_url: str) -> None:
    """Configure a mock gateway provider in the user-level config."""
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "providers": {
                    _PROVIDER: {
                        "kind": "gateway",
                        "openai": {
                            "api_key_ref": "dummy-key-spec-harness",
                            "base_url": gateway_base_url,
                            "wire_api": "chat",
                        },
                    }
                },
            }
        ),
        encoding="utf-8",
    )


def _seed_project(project: Path) -> None:
    """Pin the project default to claude-native and add a spec pinned to pi."""
    (project / ".omnigent").mkdir(parents=True, exist_ok=True)
    (project / ".omnigent" / "config.yaml").write_text(
        yaml.safe_dump({"harness": {"default": "claude-native"}}),
        encoding="utf-8",
    )
    spec_path = project / _SPEC_RELPATH
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        yaml.safe_dump(
            {
                "spec_version": 1,
                "name": "my-agent",
                "description": "headless agent pinned to pi",
                "executor": {
                    "type": "omnigent",
                    "auth": {"type": "provider", "name": _PROVIDER},
                    "model": _MODEL,
                    "config": {"harness": "pi"},
                },
                "prompt": "You are a terse test agent.\n",
                "os_env": {
                    "type": "caller_process",
                    "cwd": ".",
                    "sandbox": {"type": "none"},
                },
            }
        ),
        encoding="utf-8",
    )


def _cli_env(tmp_path: Path, config_home: Path) -> dict[str, str]:
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    session_data_root = Path(os.environ.get("OMNIGENT_DATA_DIR") or tmp_path)
    data_dir = session_data_root / f"spec-harness-{uuid.uuid4().hex[:8]}"

    inherited_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = (
        f"{_REPO_ROOT}{os.pathsep}{inherited_pythonpath}"
        if inherited_pythonpath
        else str(_REPO_ROOT)
    )

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home),
            "OMNIGENT_CONFIG_HOME": str(config_home),
            "OMNIGENT_DATA_DIR": str(data_dir),
            "PYTHONPATH": pythonpath,
        }
    )
    env.pop("OMNIGENT_MODEL", None)
    return env


def _run_agent(
    project: Path, env: dict[str, str], *extra_args: str
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "omnigent",
            "run",
            str(_SPEC_RELPATH),
            *extra_args,
            "-p",
            "say hi",
        ],
        capture_output=True,
        text=True,
        env=env,
        cwd=project,
        timeout=_RUN_TIMEOUT_S,
    )


def _gateway_request_models(mock_url: str) -> list[str]:
    resp = httpx.get(f"{mock_url}/mock/requests", timeout=10.0)
    resp.raise_for_status()
    return [r.get("model") for r in resp.json()["requests"] if isinstance(r, dict)]


def test_run_spec_harness_overrides_native_terminal_default(
    tmp_path: Path, isolated_mock_llm_server_url: str
) -> None:
    """``executor.config.harness: pi`` beats ``harness.default: claude-native``."""
    mock_url = isolated_mock_llm_server_url
    httpx.post(
        f"{mock_url}/mock/configure",
        json={"key": _MODEL, "responses": [{"text": "hi from mock"}] * 3},
        timeout=10.0,
    ).raise_for_status()
    httpx.post(
        f"{mock_url}/mock/set_fallback",
        json={"key": _MODEL, "response": {"text": "hi from mock"}},
        timeout=10.0,
    ).raise_for_status()

    config_home = tmp_path / "confighome"
    _seed_config_home(config_home, f"{mock_url}/v1")
    project = tmp_path / "project"
    _seed_project(project)
    env = _cli_env(tmp_path, config_home)

    try:
        result = _run_agent(project, env)
    finally:
        subprocess.run(
            [sys.executable, "-m", "omnigent", "stop"],
            capture_output=True,
            env=env,
            cwd=project,
            timeout=120,
        )

    combined = result.stdout + result.stderr

    assert _NATIVE_REJECTION not in combined, (
        "`omnigent run` applied the project `harness.default: claude-native` to the "
        "AGENT spec instead of the spec's `executor.config.harness: pi`:\n"
        f"{combined}"
    )
    assert result.returncode == 0, (
        f"`omnigent run` on the spec's pi harness exited {result.returncode}:\n{combined}"
    )
    models = _gateway_request_models(mock_url)
    assert _MODEL in models, (
        f"No request carrying model {_MODEL!r} reached the gateway "
        f"(captured request models: {models!r}):\n{combined}"
    )


def test_run_explicit_native_terminal_harness_flag_still_rejected(tmp_path: Path) -> None:
    """An explicit ``--harness claude-native`` with an AGENT path stays rejected."""
    config_home = tmp_path / "confighome"
    config_home.mkdir()
    project = tmp_path / "project"
    _seed_project(project)
    env = _cli_env(tmp_path, config_home)

    result = _run_agent(project, env, "--harness", "claude-native")

    combined = result.stdout + result.stderr
    assert result.returncode != 0, f"explicit native harness flag was accepted:\n{combined}"
    assert _NATIVE_REJECTION in combined, combined
