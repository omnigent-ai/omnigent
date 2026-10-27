"""Pi loads bundle resources from a sibling workspace, including under Bubblewrap."""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest

from tests.e2e._harness_probes import bwrap_namespace_unavailable_reason, cli_unavailable_reason
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent._pi_mock_gateway import write_pi_gateway_config, write_pi_resource_bundle
from tests.e2e.omnigent.conftest import configure_mock_llm, set_fallback_mock_llm

_PI_UNAVAILABLE = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(_PI_UNAVAILABLE is not None, reason=str(_PI_UNAVAILABLE))

_MARKERS = (
    "AUTHORED_PROMPT_42D9",
    "EXT_MARKER_7C3A",
    "SKILL_MARKER_9B2F",
    "BUNDLE_CONTEXT_MARKER_5E1D",
)

# A checkout-wide grant masks its .venv; grant only the Python runtime and source.
_RUNTIME_READ_PATHS = [str(Path(__file__).resolve().parents[3] / "omnigent"), sys.prefix]


@pytest.mark.skipif(
    bwrap_namespace_unavailable_reason() is not None,
    reason=f"cannot execute bwrap namespaces here: {bwrap_namespace_unavailable_reason()}",
)
def test_pi_bundle_project_resources_reach_sandboxed_pi(
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    """Bubblewrap must expose resources hidden by its private temporary directory."""
    model = f"mock-pi-bundle-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}], key=model)
    set_fallback_mock_llm(mock_llm_server_url, model, "ok")

    config_home = tmp_path / "config"
    write_pi_gateway_config(config_home, mock_url=mock_llm_server_url, model=model)

    # Run cwd is a sibling of the bundle, so Pi's cwd-relative discovery
    # cannot reach the bundle resources on its own.
    workspace = tmp_path / "project" / "workspace"
    workspace.mkdir(parents=True)
    source = tmp_path / "agent"
    write_pi_resource_bundle(
        source,
        model=model,
        markers=_MARKERS,
        extension_suffix="ts",
        sandbox={"type": "linux_bwrap", "allow_network": True, "read_paths": _RUNTIME_READ_PATHS},
    )

    result = subprocess.run(
        [
            str(omnigent_python),
            "-m",
            "omnigent",
            "run",
            str(source),
            "-p",
            "Hello",
            "--no-log",
            "--no-session",
        ],
        env={**mock_credentials_env, "OMNIGENT_CONFIG_HOME": str(config_home)},
        cwd=workspace,
        capture_output=True,
        text=True,
        timeout=120,
    )
    system = "\n".join(
        json.dumps(message["content"])
        for request in get_mock_requests(mock_llm_server_url, key=model)
        for message in request["messages"]
        if message["role"] in ("system", "developer")
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    missing = [marker for marker in _MARKERS if marker not in system]
    assert not missing, f"markers absent from the sandboxed Pi request: {missing}"
