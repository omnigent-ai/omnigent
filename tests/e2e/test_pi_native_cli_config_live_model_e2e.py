"""E2E: pi-native ``cli-config`` launch model must come from the live workspace.

The user journey:

1. ``isaac configure codex`` writes a ``[model_providers.Databricks]`` AI
   Gateway table (base_url + token-printing auth command) into
   ``~/.codex/config.toml``.
2. ``omnigent setup`` adopts it as the default ``cli-config`` provider in
   ``~/.omnigent/config.yaml``.
3. The workspace's live Claude listing (Unity Catalog model-services) differs
   from the bundled MLflow catalog's ``databricks``/``claude`` default.
4. The user launches a pi-native session without an explicit model.

Observed bug: the launch model is resolved from the bundled catalog alone, so
Pi starts on an id the workspace does not serve, and ``to_models_config``
registers that phantom id in ``models.json`` next to the genuinely live
models — ``/model`` and routed dispatches can then select it and fail at
request time with a 404.

Expected (parity with the sibling ``databricks``-kind branch): the launch
model prefers the live workspace listing, falling back to the bundled catalog
only when that listing is empty.

The test drives the runner's REAL pi-native launch seam
(``_auto_create_pi_terminal`` — the code path a pi-native session-create
runs) against a fully staged user environment: real config files on disk
(codex gateway table, omnigent cli-config provider, ``~/.databrickscfg``), a
loopback HTTP stub playing the workspace's Unity Catalog model-services API,
a real subprocess auth command, and a staged bundled-catalog disk cache. Only
the terminal registry (captures the launch spec instead of opening a PTY) and
the Pi executable path are faked.

Self-contained: requires no server, no credentials, and no network.

Usage::

    pytest tests/e2e/test_pi_native_cli_config_live_model_e2e.py -v
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import httpx
import pytest

from omnigent.entities.session_resources import SessionResourceView
from omnigent.spec.types import AgentSpec, ExecutorSpec

# The live workspace's Claude listing — a generation the bundled catalog does
# not name. ``system.ai.*`` ids are what the Unity Catalog model-services API
# reports on current workspaces.
_LIVE_CLAUDE_MODELS = ("system.ai.claude-opus-4-6", "system.ai.claude-haiku-4-5")

# The bundled MLflow catalog's databricks/claude default — an endpoint this
# workspace does NOT serve (the report's ``databricks-claude-fable-5``).
_CATALOG_ONLY_MODEL = "databricks-claude-fable-5"

# The primary Pi provider id the Databricks builders emit.
_PRIMARY_PROVIDER_ID = "omnigent"


class _WorkspaceStub(ThreadingHTTPServer):
    """Loopback stand-in for the Databricks workspace API.

    Serves ``GET /api/2.1/unity-catalog/model-services`` with a configurable
    live Claude listing, mirroring the response shape
    ``fetch_databricks_model_service_entries`` parses.
    """

    def __init__(self, live_models: tuple[str, ...]) -> None:
        self.live_models = live_models
        super().__init__(("127.0.0.1", 0), _WorkspaceStubHandler)

    @property
    def host(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}"


class _WorkspaceStubHandler(BaseHTTPRequestHandler):
    server: _WorkspaceStub

    def do_GET(self) -> None:  # http.server handler naming
        if self.path.startswith("/api/2.1/unity-catalog/model-services"):
            body = json.dumps(
                {
                    "model_services": [
                        {
                            "name": f"model-services/{model_id}",
                            "supported_api_types": ["anthropic/v1/messages"],
                        }
                        for model_id in self.server.live_models
                    ]
                }
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args: object) -> None:  # keep pytest output clean
        pass


@dataclass
class _StagedEnvironment:
    """Paths of the staged user environment."""

    home: Path
    workspace: Path


def _stage_user_environment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    workspace_stub: _WorkspaceStub,
) -> _StagedEnvironment:
    """Stage the reported user setup on disk and in the environment.

    Writes the codex AI Gateway provider table, the omnigent ``cli-config``
    default provider, a ``~/.databrickscfg`` pointing the workspace API at
    the loopback stub, and a fresh bundled-catalog disk cache whose
    databricks/claude default is :data:`_CATALOG_ONLY_MODEL`.

    :param tmp_path: Per-test temp dir the staged files land under.
    :param monkeypatch: Pytest monkeypatch fixture (auto-undone).
    :param workspace_stub: The running loopback workspace stub.
    :returns: The staged environment paths.
    """
    home = tmp_path / "home"
    (home / ".codex").mkdir(parents=True)
    config_home = tmp_path / "omnigent-config"
    config_home.mkdir()
    cache_root = tmp_path / "cache"
    cache_root.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache_root))
    # tests/conftest.py disables the catalog globally; this journey NEEDS the
    # bundled catalog readable (from the staged disk cache — still no network).
    monkeypatch.delenv("OMNIGENT_DISABLE_CATALOG_LOOKUP", raising=False)
    # Ambient credentials/proxies would defeat the staged workspace state.
    for var in (
        "DATABRICKS_HOST",
        "DATABRICKS_TOKEN",
        "DATABRICKS_CONFIG_PROFILE",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "http_proxy",
        "https_proxy",
    ):
        monkeypatch.delenv(var, raising=False)

    # 1. The isaac-configured codex AI Gateway table (real Databricks gateway
    #    hostname shape — the cli-config path only translates genuine ones).
    (home / ".codex" / "config.toml").write_text(
        """
model_provider = "Databricks"

[model_providers.Databricks]
name = "Databricks AI Gateway"
base_url = "https://1965859176160743.ai-gateway.cloud.databricks.com/codex/v1"
wire_api = "responses"

[model_providers.Databricks.auth]
command = "echo"
args = ["gateway-bearer-token"]
timeout_ms = 5000
""",
        encoding="utf-8",
    )

    # 2. omnigent setup adopted it as the default cli-config provider.
    (config_home / "config.yaml").write_text(
        """
providers:
  codex-databricks:
    kind: cli-config
    default: true
    cli: codex
    model_provider: Databricks
    display_name: Databricks AI Gateway
""",
        encoding="utf-8",
    )

    # 3. ~/.databrickscfg resolves the workspace API to the loopback stub
    #    (the gateway hostname itself never serves workspace APIs).
    databrickscfg = tmp_path / "databrickscfg"
    databrickscfg.write_text(
        f"[DEFAULT]\nhost = {workspace_stub.host}\ntoken = workspace-pat\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("DATABRICKS_CONFIG_FILE", str(databrickscfg))

    # 4. Bundled MLflow catalog disk cache naming a model this workspace does
    #    not serve as the databricks/claude default.
    from omnigent.onboarding.providers import _catalog_source_url

    catalog = {
        "schema_version": "1.0.0",
        "models": {
            _CATALOG_ONLY_MODEL: {
                "mode": "chat",
                "capabilities": {
                    "function_calling": True,
                    "reasoning": True,
                    "vision": True,
                    "response_schema": True,
                },
                "context_window": {"max_input": 200000, "max_output": 64000},
                "pricing": {
                    "input_per_million_tokens": 5.0,
                    "output_per_million_tokens": 25.0,
                },
            }
        },
    }
    catalog_dir = cache_root / "omnigent" / "model-catalog"
    catalog_dir.mkdir(parents=True)
    (catalog_dir / "databricks.json").write_text(
        json.dumps(
            {
                "cache_schema_version": 1,
                "catalog_schema_version": catalog["schema_version"],
                "source_url": _catalog_source_url("databricks"),
                "fetched_at": time.time(),
                "catalog": catalog,
            }
        ),
        encoding="utf-8",
    )
    return _StagedEnvironment(home=home, workspace=workspace)


@pytest.fixture
def _fresh_catalog_caches() -> Iterator[None]:
    """Clear the in-memory catalog caches around the test.

    The bundled-catalog memory cache is process-global; without clearing, an
    earlier test's (empty, lookup-disabled) entry would shadow the staged disk
    cache — and the staged entry would leak into later tests.
    """
    import omnigent.model_catalog as model_catalog
    import omnigent.onboarding.providers as onboarding_providers

    onboarding_providers._catalog_cache.clear()
    model_catalog.clear_model_catalog_cache()
    try:
        yield
    finally:
        onboarding_providers._catalog_cache.clear()
        model_catalog.clear_model_catalog_cache()


async def _drive_pi_native_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    staged: _StagedEnvironment,
) -> tuple[list[str], dict[str, Any]]:
    """Run the runner's real pi-native terminal launch and capture its output.

    Drives ``_auto_create_pi_terminal`` — the seam a pi-native session-create
    executes — with no model override, capturing the Pi CLI args and the
    generated managed ``models.json``.

    :param tmp_path: Per-test temp dir backing the pi-native bridge root.
    :param monkeypatch: Pytest monkeypatch fixture.
    :param staged: The staged user environment.
    :returns: ``(pi_args, models_json)``.
    """
    import omnigent.pi_native_bridge as pi_bridge
    from omnigent.runner.native.orchestration import _auto_create_pi_terminal

    session_id = "conv_cli_config_live_model_e2e"
    monkeypatch.setattr(pi_bridge, "_BRIDGE_ROOT", tmp_path / "pi-native")
    monkeypatch.setenv("OMNIGENT_RUNNER_WORKSPACE", str(staged.workspace))
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://ap.example")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    # Resolve a Pi executable without requiring the real binary on PATH.
    monkeypatch.setattr("omnigent.pi_native.resolve_pi_executable", lambda: "/usr/bin/pi")

    class _SnapshotClient:
        """Fresh pi-native session snapshot (no launch args / external id)."""

        async def get(self, url: str, *, timeout: float) -> httpx.Response:
            del url, timeout
            return httpx.Response(
                200,
                json={
                    "workspace": str(staged.workspace),
                    "terminal_launch_args": None,
                    "external_session_id": None,
                },
                request=httpx.Request("GET", f"/v1/sessions/{session_id}"),
            )

    launched: dict[str, Any] = {}

    class _FakeResourceRegistry:
        """Captures the launched terminal spec (args + env)."""

        terminal_registry = None

        async def launch_required_terminal(
            self,
            session_id: str,
            terminal_name: str,
            session_key: str,
            spec: Any,
            *,
            resource_role: str | None = None,
            parent_os_env: Any = None,
        ) -> SessionResourceView:
            del terminal_name, session_key, resource_role, parent_os_env
            launched["args"] = list(spec.args)
            launched["env"] = dict(spec.env)
            return SessionResourceView(
                id="terminal_pi_main",
                type="terminal",
                session_id=session_id,
                name="pi",
            )

    # The user launches a pi-native session WITHOUT an explicit model.
    spec = AgentSpec(
        spec_version=1,
        name="pi-cli-config-live-model",
        executor=ExecutorSpec(type="omnigent", config={"harness": "pi-native"}, model=None),
    )

    await _auto_create_pi_terminal(
        session_id,
        _FakeResourceRegistry(),  # type: ignore[arg-type]
        lambda _sid, _event: None,
        server_client=_SnapshotClient(),  # type: ignore[arg-type]
        agent_spec=spec,
    )

    agent_dir = Path(launched["env"]["PI_CODING_AGENT_DIR"])
    models_json: dict[str, Any] = json.loads(
        (agent_dir / "models.json").read_text(encoding="utf-8")
    )
    return list(launched["args"]), models_json


def _selected_model(pi_args: list[str]) -> str:
    """Return the value of the ``--model`` arg the runner appended."""
    assert "--model" in pi_args, f"pi launch args carry no --model: {pi_args}"
    return pi_args[pi_args.index("--model") + 1]


@pytest.mark.asyncio
async def test_cli_config_databricks_prefers_live_models_over_catalog(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fresh_catalog_caches: None,
) -> None:
    """With a live workspace listing, the launch model must be a served one.

    The workspace serves ``system.ai.claude-opus-4-6`` / ``-haiku-4-5``; the
    bundled catalog names ``databricks-claude-fable-5``. The cli-config
    branch must prefer the live listing (parity with the ``databricks``-kind
    branch) — before the fix it launches on the catalog id and registers that
    phantom endpoint in ``models.json``, where ``/model`` and routed
    dispatches can select it and 404 at request time.
    """
    workspace_stub = _WorkspaceStub(_LIVE_CLAUDE_MODELS)
    threading.Thread(target=workspace_stub.serve_forever, daemon=True).start()
    try:
        staged = _stage_user_environment(tmp_path, monkeypatch, workspace_stub)
        pi_args, models_json = await _drive_pi_native_launch(tmp_path, monkeypatch, staged)
    finally:
        workspace_stub.shutdown()
        workspace_stub.server_close()

    primary = models_json["providers"][_PRIMARY_PROVIDER_ID]
    registered_ids = [model["id"] for model in primary["models"]]

    # Sanity: the live listing WAS fetched and rendered — the failure below is
    # the launch-model pick discarding it, not a broken listing.
    assert set(_LIVE_CLAUDE_MODELS) <= set(registered_ids), (
        f"live workspace models missing from models.json: {registered_ids}"
    )

    selected = _selected_model(pi_args)
    assert selected in _LIVE_CLAUDE_MODELS, (
        f"pi launched with {selected!r}, which the workspace does not serve; "
        f"expected a model from the live workspace listing {_LIVE_CLAUDE_MODELS} "
        "(the bundled-catalog default may be used only when the live listing is empty)"
    )
    assert _CATALOG_ONLY_MODEL not in registered_ids, (
        f"{_CATALOG_ONLY_MODEL!r} (bundled-catalog id the workspace does not serve) "
        f"was registered in models.json next to the live models: {registered_ids}; "
        "selecting it via /model or a routed dispatch fails at request time with a 404"
    )


@pytest.mark.asyncio
async def test_cli_config_databricks_falls_back_to_catalog_without_live_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    _fresh_catalog_caches: None,
) -> None:
    """With an EMPTY live listing, the bundled catalog default still applies.

    Pins the fallback half of the contract so the fix cannot overcorrect: when
    the workspace listing comes back empty (outage, no models), the launch
    model falls back to the bundled catalog's databricks/claude default —
    identical to the ``databricks``-kind branch.
    """
    workspace_stub = _WorkspaceStub(())
    threading.Thread(target=workspace_stub.serve_forever, daemon=True).start()
    try:
        staged = _stage_user_environment(tmp_path, monkeypatch, workspace_stub)
        pi_args, models_json = await _drive_pi_native_launch(tmp_path, monkeypatch, staged)
    finally:
        workspace_stub.shutdown()
        workspace_stub.server_close()

    assert _selected_model(pi_args) == _CATALOG_ONLY_MODEL, (
        "with no live workspace listing the launch model must fall back to the "
        "bundled catalog's databricks/claude default"
    )
    primary = models_json["providers"][_PRIMARY_PROVIDER_ID]
    registered_ids = [model["id"] for model in primary["models"]]
    assert _CATALOG_ONLY_MODEL in registered_ids
