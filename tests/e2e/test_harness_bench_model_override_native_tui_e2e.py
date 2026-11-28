"""Harness-bench ``model_override`` verdict on the native-TUI transport.

Runs the real ``python -m tests.harness_bench --harness pi-native --dimension
model_override --live`` command against the real Pi CLI, with Pi's omnigent
provider pointed at a local mock model endpoint that records every request. If
the caller-specified bench model never reached the endpoint, the probe must not
report ``SUPPORTED``. Skips when pi or tmux is unavailable or the basic turn
cannot complete here.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.live_server import find_free_port
from tests.e2e._harness_probes import cli_unavailable_reason
from tests.harness_bench.manifest import OFFICIAL_PROFILES

_REPO_ROOT = Path(__file__).resolve().parents[2]
_MOCK_SERVER = _REPO_ROOT / "tests" / "server" / "integration" / "mock_llm_server.py"

_HARNESS = "pi-native"
_PROFILE = OFFICIAL_PROFILES[_HARNESS]
# The model the configured provider serves by default; deliberately not the
# bench profile's model, so a request carrying it is unambiguous.
_PROVIDER_DEFAULT_MODEL = "claude-sonnet-4-20250514"

_BENCH_CMD = [
    sys.executable,
    "-m",
    "tests.harness_bench",
    "--harness",
    _HARNESS,
    "--dimension",
    "model_override",
    "--live",
    "--no-rich",
    "--no-color",
    "--json",
]
_BENCH_TIMEOUT_S = 300.0
_MOCK_READY_TIMEOUT_S = 10.0

pytestmark = [
    pytest.mark.skipif(shutil.which("tmux") is None, reason="requires tmux (native TUI pane)"),
    pytest.mark.timeout(420),
]


@pytest.fixture
def mock_model_endpoint(tmp_path: Path) -> Iterator[str]:
    """A mock model endpoint answering every turn with the bench marker."""
    port = find_free_port()
    log = (tmp_path / "mock_llm.log").open("w")
    proc = subprocess.Popen(
        [sys.executable, str(_MOCK_SERVER), str(port)],
        env={**os.environ, "PYTHONPATH": str(_REPO_ROOT)},
        stdout=log,
        stderr=subprocess.STDOUT,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        deadline = time.monotonic() + _MOCK_READY_TIMEOUT_S
        while True:
            try:
                if httpx.get(f"{base_url}/stats", timeout=1.0).status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.monotonic() > deadline:
                raise RuntimeError(f"mock model endpoint did not start; log at {log.name}")
            time.sleep(0.1)
        httpx.post(
            f"{base_url}/mock/set_fallback",
            json={"key": "default", "text": _PROFILE.marker},
            timeout=5.0,
        ).raise_for_status()
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)
        log.close()


def _provider_config(base_url: str) -> str:
    return yaml.safe_dump(
        {
            "providers": {
                "bench-mock": {
                    "kind": "key",
                    "default": ["anthropic"],
                    "anthropic": {
                        "base_url": base_url,
                        "api_key": "mock-key",
                        "models": {"default": _PROVIDER_DEFAULT_MODEL},
                    },
                }
            }
        }
    )


def _bench_env(config_home: Path) -> dict[str, str]:
    # A leaked runner/host identity would make the bench's own server and host
    # daemon take the zygote path instead of booting clean.
    stripped = ("OMNIGENT_RUNNER_", "OMNIGENT_HOST_", "RUNNER_SERVER_URL")
    env = {k: v for k, v in os.environ.items() if not k.startswith(stripped)}
    env["OMNIGENT_CONFIG_HOME"] = str(config_home)
    # Every bench child (server, host daemon, runner) must import this checkout,
    # whatever cwd it runs from.
    env["PYTHONPATH"] = str(_REPO_ROOT)
    return env


def _requested_models(base_url: str) -> set[str]:
    requests = httpx.get(f"{base_url}/mock/requests", timeout=5.0).json()["requests"]
    return {str(r["model"]) for r in requests if isinstance(r, dict) and r.get("model")}


def test_native_tui_model_override_is_not_supported_when_model_never_sent(
    tmp_path: Path, mock_model_endpoint: str
) -> None:
    reason = cli_unavailable_reason(_PROFILE.cli_binary or "pi")
    if reason is not None:
        pytest.skip(reason)

    config_home = tmp_path / "omnigent-config"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        _provider_config(mock_model_endpoint), encoding="utf-8"
    )

    proc = subprocess.run(
        _BENCH_CMD,
        cwd=_REPO_ROOT,
        env=_bench_env(config_home),
        capture_output=True,
        text=True,
        timeout=_BENCH_TIMEOUT_S,
        check=False,
    )
    output = f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    assert proc.stdout.strip(), output
    report = json.loads(proc.stdout)["harnesses"][0]
    cells = {cell["dimension"]: cell for cell in report["cells"]}

    if report["skipped_reason"] or cells["basic_turn"]["observed"] != "supported":
        pytest.skip(f"native-tui {_HARNESS} could not complete a basic turn here:\n{output}")

    requested = _requested_models(mock_model_endpoint)
    assert requested, f"the bench turns never reached the model endpoint\n{output}"
    if _PROFILE.model in requested:
        # The transport applied the override this time, so SUPPORTED is earned.
        return

    cell = cells["model_override"]
    assert cell["observed"] != "supported", (
        f"model_override reported SUPPORTED although the transport never sent "
        f"{_PROFILE.model!r} to the model (requests used {sorted(requested)}):\n"
        f"cell={cell}\n{output}"
    )
    assert "caller-specified model" not in cell["note"], cell
