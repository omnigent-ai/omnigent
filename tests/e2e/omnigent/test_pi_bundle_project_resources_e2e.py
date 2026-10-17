"""A ``harness: pi`` bundle's project-local ``.pi`` resources must reach Pi.

``PiExecutor`` spawns ``pi`` in the session workspace, so Pi's cwd-relative
discovery never sees resources shipped inside the agent bundle: the bundle's
``.pi/extensions/<name>``, ``.pi/skills/<name>`` and an un-declared
bundle-root ``AGENTS.md`` silently fail to load, and the model answers as Pi's
stock assistant with no error. The executor has to pass them explicitly, and
under an ``os_env`` sandbox it also has to expose them: the runner extracts
bundles under the host temp dir, which ``linux_bwrap`` replaces with a private
tmpfs, and Pi aborts on an ``--extension`` path it cannot see.

Both tests run ``omnigent run <bundle>`` from a sibling cwd (``workspace``),
matching a real dispatch where the session workspace is not the bundle
directory, and assert that each marker reaches the captured model request.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from pathlib import Path

import pytest
import yaml

from tests.e2e._harness_probes import bwrap_namespace_unavailable_reason, cli_unavailable_reason
from tests.e2e.conftest import get_mock_requests
from tests.e2e.omnigent.conftest import configure_mock_llm, set_fallback_mock_llm

_PI_UNAVAILABLE = cli_unavailable_reason("pi")
pytestmark = pytest.mark.skipif(_PI_UNAVAILABLE is not None, reason=str(_PI_UNAVAILABLE))

_EXTENSION_INDEX_TS = """\
export default function (pi: any) {
  pi.on("before_agent_start", async (event: any) => {
    return {
      systemPrompt:
        event.systemPrompt +
        "\\n\\nEXT_MARKER_7C3A: bundle extension loaded.",
    };
  });
}
"""

_SKILL_MD = """\
---
name: grilling
description: SKILL_MARKER_9B2F project skill shipped under the bundle's .pi/skills directory.
---

# Grilling (SKILL_MARKER_9B2F)
"""

_BUNDLE_AGENTS_MD = "BUNDLE_CONTEXT_MARKER_5E1D: bundle-root project context."
_MARKERS = (
    "AUTHORED_PROMPT_42D9",
    "EXT_MARKER_7C3A",
    "SKILL_MARKER_9B2F",
    "BUNDLE_CONTEXT_MARKER_5E1D",
)

# A checkout-wide grant masks its .venv; grant only the Python runtime and source.
_RUNTIME_READ_PATHS = [str(Path(__file__).resolve().parents[3] / "omnigent"), sys.prefix]


def _write_bundle(source: Path, model: str, *, sandbox: dict[str, object] | None) -> None:
    """Write a ``harness: pi`` bundle whose resources use Pi's project layout only."""
    source.mkdir()
    spec: dict[str, object] = {
        "spec_version": 1,
        "name": "pi-bundle-probe",
        # An authored prompt keeps Omnigent's spec loader from auto-injecting the
        # bundle AGENTS.md, isolating Pi's own context-file loading of the bundle root.
        "prompt": "AUTHORED_PROMPT_42D9",
        "skills": "all",
        "executor": {"type": "omnigent", "model": model, "config": {"harness": "pi"}},
    }
    if sandbox is not None:
        spec["os_env"] = {"type": "caller_process", "cwd": ".", "sandbox": sandbox}
    (source / "config.yaml").write_text(yaml.safe_dump(spec))
    ext_dir = source / ".pi" / "extensions" / "bundle-ext"
    ext_dir.mkdir(parents=True)
    (ext_dir / "index.ts").write_text(_EXTENSION_INDEX_TS)
    skill_dir = source / ".pi" / "skills" / "grilling"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_SKILL_MD)
    (source / "AGENTS.md").write_text(_BUNDLE_AGENTS_MD)


def _run_bundle_from_sibling_cwd(
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
    *,
    sandbox: dict[str, object] | None,
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run ``omnigent run <bundle>`` from a sibling cwd.

    :returns: The completed process and the captured system/developer text.
    """
    model = f"mock-pi-bundle-{uuid.uuid4().hex[:8]}"
    configure_mock_llm(mock_llm_server_url, [{"text": "ok"}], key=model)
    set_fallback_mock_llm(mock_llm_server_url, model, "ok")

    config_home = tmp_path / "config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        yaml.safe_dump(
            {
                "auth": {"type": "api_key"},
                "providers": {
                    "mock-oai": {
                        "kind": "key",
                        "default": True,
                        "openai": {
                            "base_url": f"{mock_llm_server_url}/v1",
                            "api_key": "mock-key",
                            "models": {"default": model},
                        },
                    }
                },
            }
        )
    )

    # Run cwd is a sibling of the bundle, so Pi's cwd-relative discovery
    # cannot reach the bundle resources on its own.
    workspace = tmp_path / "project" / "workspace"
    workspace.mkdir(parents=True)
    source = tmp_path / "agent"
    _write_bundle(source, model, sandbox=sandbox)

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
    return result, system


def test_pi_bundle_project_resources_reach_model(
    omnigent_python: Path,
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    tmp_path: Path,
) -> None:
    result, system = _run_bundle_from_sibling_cwd(
        omnigent_python, mock_credentials_env, mock_llm_server_url, tmp_path, sandbox=None
    )
    assert result.returncode == 0, f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    assert system, f"No Pi model requests captured. stdout:\n{result.stdout}"
    assert "AUTHORED_PROMPT_42D9" in system, "authored prompt should always reach the model"
    assert "EXT_MARKER_7C3A" in system, "bundle .pi/extensions/<name> never loaded"
    assert "SKILL_MARKER_9B2F" in system, "bundle .pi/skills/<name> never loaded"
    assert "BUNDLE_CONTEXT_MARKER_5E1D" in system, (
        "bundle-root AGENTS.md never loaded as project context"
    )


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
    """The same bundle under a real ``linux_bwrap`` sandbox.

    The sandbox replaces the host temp dir with a private tmpfs, so the executor
    must grant the bundle's resource directories; otherwise Pi aborts with
    ``Extension path does not exist`` before its first model request.
    """
    result, system = _run_bundle_from_sibling_cwd(
        omnigent_python,
        mock_credentials_env,
        mock_llm_server_url,
        tmp_path,
        # The sandbox shares the host network so Pi can reach the mock model.
        sandbox={"type": "linux_bwrap", "allow_network": True, "read_paths": _RUNTIME_READ_PATHS},
    )
    assert result.returncode == 0, (
        "sandboxed pi did not complete the turn.\n"
        f"stdout:\n{result.stdout}\nstderr:\n{result.stderr}"
    )
    missing = [marker for marker in _MARKERS if marker not in system]
    assert not missing, f"markers absent from the sandboxed Pi request: {missing}"
