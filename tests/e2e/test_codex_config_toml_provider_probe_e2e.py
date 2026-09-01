"""E2E regression test: config.toml-only codex providers must yield a catalog.

A host whose Codex CLI is configured through the user's ``~/.codex/config.toml``
only — a custom default ``[model_providers.X]`` table with self-contained auth
(the ``isaac configure codex`` / Databricks-gateway shape) — must still serve a
codex-native model catalog.

The bug: ``probe_codex_model_options()`` boots the discovery ``codex
app-server`` in an isolated probe ``CODEX_HOME`` that carries only a symlink to
the real ``auth.json`` — no ``config.toml`` — while passing
``-c model_provider="X"``. The provider table that gives that name meaning is
absent, so codex exits 1 (``Model provider `X` not found``), the probe raises
``Codex model discovery exited early (1)`` (with codex's stderr discarded), and
the host answers every pre-launch picker request with an empty model list.

The journey drives the product path a user hits: a real ``omnigent host``
daemon connects to a real server, and the test asks the same endpoint the
new-session model picker calls —
``GET /v1/hosts/{id}/harnesses/codex-native/model-options``.

Run::

    .venv/bin/python -m pytest tests/e2e/test_codex_config_toml_provider_probe_e2e.py -v

Requires ``codex`` on PATH (skipped otherwise). No real credentials are needed:
codex's ``model/list`` is served locally by the CLI, so a syntactically valid
provider table with a fake bearer command is enough for a healthy probe.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

import httpx
import pytest
import yaml

from tests._helpers.compat import apply_runner_env, compat_runner_cwd, runner_executable
from tests.e2e.conftest import POLL_INTERVAL_S

_CODEX_CONFIG_TOML = """\
model = "gpt-5.2"
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://workspace.ai-gateway.cloud.databricks.com/codex/v1"
wire_api = "responses"

[model_providers.Databricks.auth]
command = "printf"
args = ["%s", "e2e-fake-bearer-token"]
"""


def _spawn_config_toml_host_daemon(
    *,
    tmp_path: Path,
    live_server: str,
) -> tuple[subprocess.Popen[bytes], str, Path]:
    """Spawn an ``omnigent host`` daemon on a HOME configured like the bug report.

    The daemon's ``$HOME`` carries a ``~/.codex/config.toml`` whose effective
    default provider is a custom ``[model_providers.Databricks]`` table with
    self-contained auth, and an ``~/.omnigent/config.yaml`` with NO providers —
    so codex-native routing resolves the cli-config detection
    (``-c model_provider="Databricks"``), exactly the reported machine state.

    :param tmp_path: Per-test temp dir used as the daemon's ``HOME``.
    :param live_server: Server URL the daemon registers with.
    :returns: ``(daemon process, host_id, daemon log path)``.
    """
    home_dir = tmp_path / "home"
    codex_dir = home_dir / ".codex"
    omni_dir = home_dir / ".omnigent"
    codex_dir.mkdir(parents=True)
    omni_dir.mkdir(parents=True)
    (codex_dir / "config.toml").write_text(_CODEX_CONFIG_TOML, encoding="utf-8")

    # Bare 32-char hex — host_id is a Uuid16 column, and the API returns the
    # bare form, so the online poll's comparison must see the same.
    host_id = uuid.uuid4().hex
    host_name = f"e2e-codex-toml-provider-{uuid.uuid4().hex[:12]}"
    (omni_dir / "config.yaml").write_text(
        yaml.safe_dump(
            {"host": {"host_id": host_id, "name": host_name}},
            default_flow_style=False,
            sort_keys=True,
        )
    )

    repo_root = Path(__file__).resolve().parents[2]
    env = os.environ.copy()
    env["PYTHONPATH"] = f"{repo_root}{os.pathsep}{env.get('PYTHONPATH', '')}"
    env["HOME"] = str(home_dir)
    # The provider/onboarding layer honors OMNIGENT_CONFIG_HOME over HOME, so
    # pin it to the same isolated dir (the outer test env may set its own).
    env["OMNIGENT_CONFIG_HOME"] = str(omni_dir)
    # A leaked CODEX_HOME would shadow the staged ~/.codex as the bridged
    # config source.
    env.pop("CODEX_HOME", None)
    # Ambient provider credentials are detected BEFORE the config.toml
    # provider, so a leaked key (OPENAI_API_KEY, OMNIGENT_*_API_KEY, a Vertex
    # trio, ...) would route the probe through a different provider shape and
    # let the test pass without exercising the config.toml bridge. Clear every
    # env-key lane the ambient sweep reads.
    for name in tuple(env):
        if name.endswith("_API_KEY"):
            env.pop(name)
    env.pop("CLAUDE_CODE_USE_VERTEX", None)

    daemon_log = tmp_path / "host-daemon.log"
    with open(daemon_log, "w") as log_fh:
        proc = subprocess.Popen(
            [
                runner_executable(),
                "-m",
                "omnigent.host._daemon_entry",
                "--server",
                live_server,
            ],
            env=apply_runner_env(env),
            cwd=compat_runner_cwd(),
            stdout=subprocess.DEVNULL,
            stderr=log_fh,
        )
    return proc, host_id, daemon_log


def _wait_for_host_online(client: httpx.Client, host_id: str, timeout: float = 60.0) -> None:
    """Poll ``GET /v1/hosts`` until *host_id* shows online.

    :param client: HTTP client pointed at the test server.
    :param host_id: The pre-seeded host id to wait for.
    :param timeout: Max seconds to wait.
    :raises AssertionError: If the host never appears online.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            resp = client.get("/v1/hosts")
            if resp.status_code == 200:
                for host in resp.json().get("hosts", []):
                    if host["host_id"] == host_id and host["status"] == "online":
                        return
        except httpx.ConnectError:
            pass
        time.sleep(POLL_INTERVAL_S)
    raise AssertionError(f"Host {host_id!r} did not appear online within {timeout}s")


@pytest.mark.skipif(
    shutil.which("codex") is None,
    reason="codex CLI is required for the codex model-discovery probe",
)
def test_codex_model_options_with_config_toml_provider(
    tmp_path: Path,
    live_server: str,
    http_client: httpx.Client,
) -> None:
    """A config.toml-provider host serves a non-empty codex model catalog.

    Regression scenario: on the buggy build the discovery probe's isolated
    ``CODEX_HOME`` lacks the ``config.toml`` that defines the pinned
    ``model_provider``, codex exits 1 at startup, and the picker endpoint
    yields no catalog. Fixed behavior: the probe boots with the provider
    definition available and the endpoint returns codex's model rows.
    """
    proc, host_id, daemon_log = _spawn_config_toml_host_daemon(
        tmp_path=tmp_path, live_server=live_server
    )
    home_dir = tmp_path / "home"
    try:
        _wait_for_host_online(http_client, host_id, timeout=60.0)

        resp = http_client.get(
            f"/v1/hosts/{host_id}/harnesses/codex-native/model-options",
            timeout=120.0,
        )
        log_tail = daemon_log.read_text(errors="replace")[-4000:] if daemon_log.exists() else ""

        # On the buggy build this request never yields a catalog: the host's
        # probe dies at codex startup and the empty answer surfaces either as
        # models=[] with the generic probe error, or as a non-200 (observed:
        # HTTP 500 — the route's response model rejects the empty answer's
        # string ``error`` field).
        assert resp.status_code == 200, (
            "the pre-launch codex model-options request failed for a "
            "config.toml-defined provider (the model-discovery "
            "probe's CODEX_HOME carries no config.toml, so codex exits 1 and "
            f"no catalog is served): HTTP {resp.status_code}: {resp.text}\n"
            f"host daemon log tail:\n{log_tail}"
        )
        payload = resp.json()
        assert payload.get("models"), (
            "the host served an empty codex model catalog for a "
            "config.toml-defined provider (the model-discovery "
            "probe's CODEX_HOME carries no config.toml, so codex cannot "
            f"resolve the pinned model_provider). payload={payload!r}\n"
            f"host daemon log tail:\n{log_tail}"
        )
        assert "error" not in payload, f"the picker endpoint carried a probe error: {payload!r}"

        # The catalog must have come from the config.toml provider bridge,
        # not from some other ambient provider shape that happens to be
        # reachable: the probe home under the daemon's HOME must carry the
        # bridged provider table.
        probe_root = home_dir / ".omnigent" / "cache" / "codex-model-probe"
        bridged_configs = list(probe_root.glob("*/config.toml"))
        probe_contents = list(probe_root.glob("*/*")) if probe_root.is_dir() else "missing"
        assert bridged_configs, (
            "no bridged config.toml found in the probe home — the catalog was "
            "served without exercising the config.toml provider bridge. "
            f"probe root contents: {probe_contents}"
        )
        assert any(
            "[model_providers.Databricks]" in path.read_text(encoding="utf-8")
            for path in bridged_configs
        ), "the bridged probe config does not carry the config.toml provider table"
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)
