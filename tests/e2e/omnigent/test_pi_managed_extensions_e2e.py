"""E2E: pi gateway harness loads user extensions from the managed agent dir.

Gateway mode sets ``PI_CODING_AGENT_DIR`` to a per-session temp directory for
``models.json``. Without seeding that dir from ``~/.pi/agent``, extensions
installed via ``pi install`` (or listed in global settings) never load (#1423).

This test drives a real ``omnigent run --harness pi`` subprocess with:

- an isolated ``HOME`` carrying ``~/.pi/agent/settings.json`` + a marker
  extension;
- a mock OpenAI provider in ``OMNIGENT_CONFIG_HOME`` so pi enters gateway mode
  while still routing LLM calls to the session mock server;
- a marker file the extension writes on ``session_start``.

**Serial execution:** uses the session-scoped mock LLM server like the other
``tests/e2e/omnigent/`` pi rows — do not run under xdist against a shared mock.
"""

from __future__ import annotations

import json
import subprocess
import uuid
from pathlib import Path

import pytest
import yaml

from tests.e2e._harness_probes import cli_unavailable_reason
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm

_EXTENSION_PATH = (
    Path(__file__).resolve().parents[2] / "resources" / "pi_extensions" / "e2e_marker_extension.js"
)
_MARKER_NAME = "omnigent-pi-ext-marker"
_PROMPT = "say hi in 5 words"
_RUN_TIMEOUT_SEC = 180

_pytest_pi_unavailable = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(
    _pytest_pi_unavailable is not None,
    reason=(
        "pi managed-extensions e2e requires a runnable 'pi' CLI; "
        f"{_pytest_pi_unavailable}. Install/fix Pi to run this test."
    ),
)


def _write_pi_gateway_config(config_home: Path, *, mock_url: str, model: str) -> None:
    """Write a provider config that puts pi in gateway mode against the mock LLM."""
    config = {
        "auth": {"type": "api_key"},
        "providers": {
            "mock-oai": {
                "kind": "key",
                "default": True,
                "openai": {
                    "base_url": f"{mock_url}/v1",
                    "api_key": "mock-key",
                    "models": {"default": model},
                },
            },
        },
    }
    config_home.mkdir(parents=True, exist_ok=True)
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(config, sort_keys=False),
        encoding="utf-8",
    )


def _seed_pi_extension_home(home: Path) -> Path:
    """
    Install a global Pi settings file pointing at the marker extension.

    :returns: Path where the extension should write its marker file.
    """
    agent_dir = home / ".pi" / "agent"
    agent_dir.mkdir(parents=True, exist_ok=True)
    (agent_dir / "settings.json").write_text(
        json.dumps({"extensions": [str(_EXTENSION_PATH.resolve())]}),
        encoding="utf-8",
    )
    return home / _MARKER_NAME


def test_pi_gateway_run_loads_global_extensions(
    omnigent_repo_root: Path,
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """
    ``omnigent run --harness pi`` in gateway mode loads extensions from the
    user's global Pi agent settings.

    The marker extension writes ``~/omnigent-pi-ext-marker`` on
    ``session_start``. Presence of that file after a successful run proves the
    managed ``PI_CODING_AGENT_DIR`` was seeded from ``~/.pi/agent``.
    """
    model = f"mock-pi-ext-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": "Hello from the mock model."}],
        key=model,
    )

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    marker_path = _seed_pi_extension_home(fake_home)
    assert not marker_path.exists()

    config_home = tmp_path / "omnigent-config"
    _write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)

    env = dict(mock_credentials_env)
    env["HOME"] = str(fake_home)
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)

    yaml_path = omnigent_repo_root / "tests" / "resources" / "examples" / "hello_world.yaml"
    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(yaml_path),
            "--model",
            model,
            "--harness",
            "pi",
            "-p",
            _PROMPT,
            "--no-log",
            "--no-session",
        ],
        env=env,
        cwd=str(omnigent_repo_root),
        capture_output=True,
        text=True,
        timeout=_RUN_TIMEOUT_SEC,
    )

    assert result.returncode == 0, (
        f"pi gateway run failed; stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert marker_path.is_file(), (
        "Pi extension marker missing — gateway managed agent dir was not seeded "
        f"from {fake_home / '.pi' / 'agent' / 'settings.json'!s}.\n"
        f"stdout:\n{result.stdout!r}\n\nstderr:\n{result.stderr!r}"
    )
    assert marker_path.read_text(encoding="utf-8") == "loaded"
