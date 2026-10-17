"""Discovery tests for Codex app server."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

try:
    import tomllib
except ImportError:  # pragma: no cover - Python < 3.11
    import tomli as tomllib  # type: ignore[no-redef]
from omnigent.harnesses.codex_native.app_server import (
    CodexAppServerClient,
    CodexAppServerResponseError,
    NativeCodexLaunch,
    _model_discovery_cache,
    discover_codex_model_options,
)
from tests.harnesses.codex_native.app_server._support import (
    _disable_codex_startup_rpc,
    _test_app_server,
)


async def test_discover_codex_model_options_strips_secrets_and_stops_process(
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Pre-launch discovery uses an empty home, no credentials, and clean teardown."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    captured_env: dict[str, str] = {}

    class _FakeProcess:
        pid = None
        returncode: int | None = None
        terminated = False

        def terminate(self) -> None:
            self.terminated = True
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -1

        async def wait(self) -> int:
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    process = _FakeProcess()

    async def _fake_start(
        *,
        codex_path: str,
        listen_url: str,
        env: dict[str, str],
        cwd: Path,
    ) -> object:
        assert codex_path == "/test/codex"
        assert listen_url.startswith("ws://127.0.0.1:")
        assert cwd.is_dir()
        assert Path(env["CODEX_HOME"]).is_dir()
        captured_env.update(env)

        async def _empty_stderr() -> str:
            return ""

        return codex_native_app_server._CodexModelDiscoveryProcess(
            process=process,  # type: ignore[arg-type]
            stderr_tail=asyncio.create_task(_empty_stderr()),
        )

    async def _fake_wait(discovery: object, port: int) -> None:
        assert discovery is not None
        assert port > 0

    class _FakeClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            assert ws_url.startswith("ws://127.0.0.1:")
            assert client_name == "omnigent-codex-model-discovery"

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def request(
            self,
            method: str,
            params: dict[str, object],
        ) -> dict[str, object]:
            assert method == "model/list"
            assert params == {"includeHidden": False}
            return {
                "result": {
                    "data": [
                        {
                            "id": "coding-model",
                            "model": "coding-model",
                            "isDefault": True,
                        }
                    ],
                    "nextCursor": None,
                }
            }

    monkeypatch.setattr(
        codex_native_app_server,
        "_clean_codex_env",
        lambda: {
            "PATH": "/bin",
            "OPENAI_API_KEY": "openai-secret",
            "OPENAI_BASE_URL": "https://example.invalid/v1",
            "DATABRICKS_BEARER": "databricks-secret",
            "DATABRICKS_CODEX_TOKEN": "databricks-secret",
        },
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_start_codex_model_discovery_process",
        _fake_start,
    )
    monkeypatch.setattr(codex_native_app_server, "_wait_for_discovery_listener", _fake_wait)
    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _FakeClient)
    request.addfinalizer(_model_discovery_cache.clear)
    _model_discovery_cache.clear()

    options = await discover_codex_model_options(codex_path="/test/codex")

    assert options == [{"id": "coding-model", "model": "coding-model", "isDefault": True}]
    assert captured_env == {"PATH": "/bin", "CODEX_HOME": captured_env["CODEX_HOME"]}
    assert process.terminated is True


@pytest.mark.parametrize("gateway_rows", ["matching", "missing", "malformed", "current", None])
async def test_start_uses_fresh_gateway_catalog_before_debug_models(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    gateway_rows: str | None,
) -> None:
    """Only complete matching gateway rows replace the migration subprocess."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source_home = tmp_path / "source-codex-home"
    source_home.mkdir()
    (source_home / "config.toml").write_text("", encoding="utf-8")
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source_home))
    _disable_codex_startup_rpc(monkeypatch)
    probes: list[str] = []

    def _debug_catalog(codex_path: str, source: Path, *, timeout: float) -> dict[str, object]:
        del source, timeout
        probes.append(codex_path)
        return {"models": [{"slug": "gpt-5.4", "upgrade": {"model": "gpt-5.6-terra"}}]}

    monkeypatch.setattr(codex_native_app_server, "read_codex_model_catalog", _debug_catalog)
    server = _test_app_server(
        tmp_path,
        tmp_path / "codex-home",
        tmp_path / "bridge",
        workspace,
    )
    server.trust_project = True
    server.pinned_model = "gpt-5.4"
    if gateway_rows is not None:
        row: dict[str, object] = {
            "id": "gpt-5.4" if gateway_rows != "missing" else "gpt-5.6-terra",
            "model": "gpt-5.4" if gateway_rows != "missing" else "gpt-5.6-terra",
        }
        if gateway_rows == "malformed":
            row["upgradeInfo"] = {}
        elif gateway_rows == "current":
            row["upgrade"] = None
            row["upgradeInfo"] = None
        else:
            row["upgrade"] = "gpt-5.6-terra"
        server.model_catalog_rows = [row]

    await server.start()
    await server.close()

    assert probes == ([] if gateway_rows in {"matching", "current"} else [sys.executable])
    config = tomllib.loads((server.codex_home / "config.toml").read_text(encoding="utf-8"))
    if gateway_rows == "current":
        assert "model_migrations" not in config.get("notice", {})
    else:
        assert config["notice"]["model_migrations"] == {"gpt-5.4": "gpt-5.6-terra"}


async def test_probe_codex_model_options_uses_launch_config_and_marks_default(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The probe boots Codex with the session-launch materialization.

    Harness truth: the probe's rows are Codex's own ``model/list`` output
    under the same Databricks routing a session launch gets — provider
    overrides passed as ``-c`` args, ``DATABRICKS_HOST`` in env, a
    persistent probe home — reduced to a single default marker naming the
    launch-pinned model.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setattr(
        codex_native_app_server,
        "resolve_native_codex_launch",
        lambda *, model: codex_native_app_server.NativeCodexLaunch(
            config_overrides=[],
            model=None,
            profile="oss",
        ),
    )
    monkeypatch.setattr(
        codex_native_app_server,
        "_databricks_launch_materialization",
        lambda *, model, profile, codex_path: (
            codex_native_app_server._DatabricksLaunchMaterialization(
                config_overrides=[
                    'model="databricks-gpt-5-4"',
                    'model_provider="omnigent_databricks"',
                ],
                model="databricks-gpt-5-4",
                host="https://ws.example",
            )
        ),
    )
    monkeypatch.setattr(codex_native_app_server, "_clean_codex_env", lambda: {"PATH": "/bin"})

    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = None
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -1

        async def wait(self) -> int:
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    async def _fake_start(
        *,
        codex_path: str,
        listen_url: str,
        env: dict[str, str],
        cwd: Path,
        config_overrides: tuple[str, ...] = (),
    ) -> object:
        captured["codex_path"] = codex_path
        captured["env"] = dict(env)
        captured["cwd"] = cwd
        captured["config_overrides"] = list(config_overrides)

        async def _empty_stderr() -> str:
            return ""

        return codex_native_app_server._CodexModelDiscoveryProcess(
            process=_FakeProcess(),  # type: ignore[arg-type]
            stderr_tail=asyncio.create_task(_empty_stderr()),
        )

    async def _fake_wait(discovery: object, port: int) -> None:
        del discovery, port

    class _FakeClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            assert client_name == "omnigent-codex-model-probe"

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            assert method == "model/list"
            return {
                "result": {
                    "data": [
                        {"id": "gpt-5.6-sol", "displayName": "Sol", "isDefault": True},
                        {"id": "gpt-5.4", "displayName": "gpt-5.4"},
                    ],
                    "nextCursor": None,
                }
            }

    monkeypatch.setattr(
        codex_native_app_server, "_start_codex_model_discovery_process", _fake_start
    )
    monkeypatch.setattr(codex_native_app_server, "_wait_for_discovery_listener", _fake_wait)
    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _FakeClient)

    rows = await codex_native_app_server.probe_codex_model_options(codex_path="/test/codex")

    # The launch-pinned model wins the (single) default marker, in either
    # spelling; Codex's own flag on sol is dropped.
    assert rows == [
        {"id": "gpt-5.6-sol", "displayName": "Sol"},
        {"id": "gpt-5.4", "displayName": "gpt-5.4", "isDefault": True},
    ]
    assert captured["config_overrides"] == [
        'model="databricks-gpt-5-4"',
        'model_provider="omnigent_databricks"',
    ]
    env = captured["env"]
    assert isinstance(env, dict)
    assert env["DATABRICKS_HOST"] == "https://ws.example"
    assert str(tmp_path / ".omnigent" / "cache" / "codex-model-probe") in env["CODEX_HOME"]
    assert Path(env["CODEX_HOME"]).is_dir()


@pytest.mark.parametrize("reader", ["probe", "catalog", "reprobe"])
@pytest.mark.parametrize("supplied", [False, True], ids=["ambient", "supplied-provider"])
@pytest.mark.parametrize("config_model", [None, "gpt-5.5"])
async def test_probe_codex_model_options_probes_every_launch_shape(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    reader: str,
    supplied: bool,
    config_model: str | None,
) -> None:
    """A non-Databricks launch still probes, with its own overrides verbatim.

    The plain Codex-login shape carries no ``DATABRICKS_HOST`` and no
    provider overrides beyond what the launch resolved (here the dismissal
    pin). With no launch-pinned model, Omnigent's Sol default wins when it is
    visible; otherwise Codex's own default marker stands.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server
    from omnigent.models import model_catalog_store

    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    ambient = NativeCodexLaunch(
        config_overrides=['model_provider="openai"'], model=None, profile=None
    )
    spec_launch = NativeCodexLaunch(
        config_overrides=[
            'model_provider="spec_provider"',
            'model_providers.spec_provider.base_url="https://provider.example/v1"',
        ],
        model=None,
        profile=None,
    )
    resolutions: list[None] = []

    def _resolve(*, model: None) -> NativeCodexLaunch:
        resolutions.append(model)
        return ambient

    monkeypatch.setattr(codex_native_app_server, "resolve_native_codex_launch", _resolve)
    monkeypatch.setattr(codex_native_app_server, "_clean_codex_env", lambda: {"PATH": "/bin"})
    captured: dict[str, object] = {}

    class _FakeProcess:
        pid = None
        returncode: int | None = None

        def terminate(self) -> None:
            self.returncode = 0

        def kill(self) -> None:
            self.returncode = -1

        async def wait(self) -> int:
            self.returncode = 0 if self.returncode is None else self.returncode
            return self.returncode

    async def _fake_start(
        *,
        codex_path: str,
        listen_url: str,
        env: dict[str, str],
        cwd: Path,
        config_overrides: tuple[str, ...] = (),
    ) -> object:
        captured["env"] = dict(env)
        captured["config_overrides"] = list(config_overrides)

        async def _empty_stderr() -> str:
            return ""

        return codex_native_app_server._CodexModelDiscoveryProcess(
            process=_FakeProcess(),  # type: ignore[arg-type]
            stderr_tail=asyncio.create_task(_empty_stderr()),
        )

    async def _fake_wait(discovery: object, port: int) -> None:
        del discovery, port

    class _FakeClient:
        def __init__(self, *, ws_url: str, client_name: str) -> None:
            del ws_url, client_name

        async def connect(self) -> None:
            return None

        async def close(self) -> None:
            return None

        async def request(self, method: str, params: dict[str, object]) -> dict[str, object]:
            if method == "config/read":
                return {"result": {"config": {"model": config_model}}}
            assert method == "model/list" and params == {"includeHidden": False}
            return {
                "result": {
                    "data": [{"id": "gpt-5.6-sol", "isDefault": True}, {"id": "gpt-5.5"}],
                    "nextCursor": None,
                }
            }

    monkeypatch.setattr(
        codex_native_app_server, "_start_codex_model_discovery_process", _fake_start
    )
    monkeypatch.setattr(codex_native_app_server, "_wait_for_discovery_listener", _fake_wait)
    monkeypatch.setattr(codex_native_app_server, "CodexAppServerClient", _FakeClient)

    read = {
        "probe": codex_native_app_server.probe_codex_model_options,
        "catalog": codex_native_app_server.codex_launch_catalog,
        "reprobe": codex_native_app_server.codex_reprobed_launch_catalog,
    }[reader]
    rows = await read(codex_path="/test/codex", launch=spec_launch if supplied else None)

    expected_rows = [{"id": "gpt-5.6-sol"}, {"id": "gpt-5.5"}]
    expected_rows[0 if config_model is None else 1]["isDefault"] = True
    assert rows == expected_rows
    expected_launch = spec_launch if supplied else ambient
    assert captured["config_overrides"] == expected_launch.config_overrides
    assert resolutions == ([] if supplied else [None])
    env = captured["env"]
    assert isinstance(env, dict)
    assert "DATABRICKS_HOST" not in env
    fingerprint = codex_native_app_server.codex_catalog_fingerprint(
        expected_launch, codex_path="/test/codex"
    )
    if reader != "probe":
        assert model_catalog_store.read_catalog("codex-native", fingerprint) == rows
        assert await read(codex_path="/test/codex", launch=expected_launch) == rows
    if supplied:
        ambient_fingerprint = codex_native_app_server.codex_catalog_fingerprint(
            ambient, codex_path="/test/codex"
        )
        assert ambient_fingerprint != fingerprint
        assert model_catalog_store.read_catalog("codex-native", ambient_fingerprint) is None


def test_probe_codex_home_bridges_provider_tables_and_credential(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """The probe home carries the provider tables its overrides name.

    A launch shape resolved off the user's ``config.toml`` pins only a
    provider *name* (``-c model_provider="Databricks"``). Codex refuses to
    load a config that names an undefined provider, exiting before it binds
    the listener, so a probe home holding only a credential yields no
    catalog at all. Minimal keeps the user's MCP/hook/plugin config out.
    """
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    source = tmp_path / ".codex"
    source.mkdir()
    (source / "config.toml").write_text(
        'model_provider = "Databricks"\n'
        "\n"
        "[model_providers.Databricks]\n"
        'base_url = "https://ws.example/serving-endpoints"\n'
        "\n"
        "[mcp_servers.slow]\n"
        'command = "sleep"\n'
    )
    (source / ".credentials.json").write_text("{}")
    (source / "hooks.json").write_text("{}")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    home = codex_native_app_server._probe_codex_home(['model_provider="Databricks"'])

    config = (home / "config.toml").read_text()
    assert "[model_providers.Databricks]" in config
    assert "https://ws.example/serving-endpoints" in config
    # The credential the account's catalog is gated on, in either spelling.
    assert (home / ".credentials.json").samefile(source / ".credentials.json")
    # Minimal: no MCPs to boot and no hooks to fire during a probe.
    assert "mcp_servers" not in config
    assert not (home / "hooks.json").exists()

    # A persistent home must re-read an edited source config, not pin the
    # tables copied on first use.
    (source / "config.toml").write_text(
        'model_provider = "Other"\n\n[model_providers.Other]\nbase_url = "https://two.example"\n'
    )
    home = codex_native_app_server._probe_codex_home(['model_provider="Databricks"'])
    assert "https://two.example" in (home / "config.toml").read_text()


async def test_discovery_stderr_tail_is_bounded_and_redacted() -> None:
    """The probe retains one safe diagnostic without persisting raw stderr."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    stderr = asyncio.StreamReader()
    stderr.feed_data(
        b"discarded prefix "
        + (b"x" * 128)
        + b"\nError: Model provider `Databricks` not found; token=sk-abcdefghijklmnop\n"
    )
    stderr.feed_eof()

    detail = await codex_native_app_server._capture_codex_discovery_stderr_tail(
        stderr,
        byte_limit=96,
    )

    assert "Model provider `Databricks` not found" in detail
    assert "discarded prefix" not in detail
    assert "sk-abcdefghijklmnop" not in detail
    assert "[REDACTED]" in detail


async def test_discovery_early_exit_error_carries_redacted_codex_stderr() -> None:
    """A failed probe exposes Codex's redacted in-memory diagnostic."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    class _DeadProcess:
        returncode = 1

    async def _stderr_tail() -> str:
        return "Error: Model provider `Databricks` not found; token=[REDACTED]"

    discovery = codex_native_app_server._CodexModelDiscoveryProcess(
        process=_DeadProcess(),  # type: ignore[arg-type]
        stderr_tail=asyncio.create_task(_stderr_tail()),
    )
    with pytest.raises(RuntimeError) as excinfo:
        await codex_native_app_server._wait_for_discovery_listener(discovery, port=1)

    message = str(excinfo.value)
    assert "exited early (1)" in message
    assert "Model provider `Databricks` not found" in message
    assert "[REDACTED]" in message


async def test_discovery_process_captures_stderr_in_memory(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The probe drains stderr through a pipe rather than a filesystem log."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    captured_stderr: object = None

    async def _fake_create_subprocess_exec(*args: object, **kwargs: object) -> object:
        nonlocal captured_stderr
        del args
        captured_stderr = kwargs["stderr"]
        stderr = asyncio.StreamReader()
        stderr.feed_data(b"Error: provider unavailable\n")
        stderr.feed_eof()

        class _FakeProcess:
            pass

        process = _FakeProcess()
        process.stderr = stderr
        return process

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _fake_create_subprocess_exec)

    discovery = await codex_native_app_server._start_codex_model_discovery_process(
        codex_path="/test/codex",
        listen_url="ws://127.0.0.1:12345",
        env={},
        cwd=Path("/work"),
    )

    assert captured_stderr == asyncio.subprocess.PIPE
    assert await discovery.stderr_tail == "Error: provider unavailable"


async def test_discovery_early_exit_without_stderr_keeps_plain_error() -> None:
    """Empty captured stderr retains the existing error text."""
    from omnigent.harnesses.codex_native import app_server as codex_native_app_server

    class _DeadProcess:
        returncode = 1

    async def _empty_stderr() -> str:
        return ""

    discovery = codex_native_app_server._CodexModelDiscoveryProcess(
        process=_DeadProcess(),  # type: ignore[arg-type]
        stderr_tail=asyncio.create_task(_empty_stderr()),
    )
    with pytest.raises(RuntimeError, match=r"^Codex model discovery exited early \(1\)$"):
        await codex_native_app_server._wait_for_discovery_listener(discovery, port=1)


@pytest.mark.parametrize("relative", [False, True], ids=["absolute", "relative"])
def test_codex_probe_home_preserves_configured_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, relative: bool
) -> None:
    from omnigent.harnesses.codex_native import app_server

    source = tmp_path / "source"
    source.mkdir()
    catalog = source / "models.json"
    catalog.write_text('{"models": []}')
    catalog_setting = catalog.name if relative else str(catalog)
    (source / "config.toml").write_text(
        f'model = "gateway-model"\nmodel_catalog_json = {json.dumps(catalog_setting)}\n'
        'model_provider = "gateway"\n'
        '[model_providers.gateway]\nname = "Gateway"\n'
        '[mcp_servers.unrelated]\ncommand = "must-not-start"\n'
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(app_server, "_codex_home_config_source_from_env", lambda: source)

    home = app_server._probe_codex_home([])
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["model_catalog_json"] == str(catalog)
    assert config["model"] == "gateway-model"
    assert config["model_provider"] == "gateway"
    assert "mcp_servers" not in config


def test_codex_probe_home_activates_the_source_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The probe home keeps the active ``profile`` selector, not just its table.

    The minimal bridge copies ``[profiles.*]`` definitions without the
    top-level ``profile`` key, and an active profile's ``model`` decides the
    CLI's effective default. Dropping the selector would make the probe's
    ``config/read`` resolve a different default than the configured CLI.
    """
    from omnigent.harnesses.codex_native import app_server

    source = tmp_path / "source"
    source.mkdir()
    (source / "config.toml").write_text(
        'model = "catalog-default"\n'
        'profile = "work"\n'
        'model_provider = "gateway"\n'
        '[model_providers.gateway]\nname = "Gateway"\n'
        '[profiles.work]\nmodel = "profile-model"\n'
    )
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(app_server, "_codex_home_config_source_from_env", lambda: source)

    home = app_server._probe_codex_home([])
    config = tomllib.loads((home / "config.toml").read_text())
    assert config["profile"] == "work"
    assert config["profiles"]["work"]["model"] == "profile-model"
    assert config["model"] == "catalog-default"


def test_codex_probe_home_is_keyed_by_the_source_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Switching ``CODEX_HOME`` never reuses another source's bridged auth.

    The bridge skips files that already exist, so a probe home shared across
    source homes would keep the first source's ``auth.json`` symlink while
    reading the second source's configuration — mixing account credentials
    with another account's catalog.
    """
    from omnigent.harnesses.codex_native import app_server

    homes: dict[str, Path] = {}
    for name in ("account-a", "account-b"):
        source = tmp_path / name
        source.mkdir()
        (source / "config.toml").write_text(
            'model_provider = "gateway"\n[model_providers.gateway]\nname = "Gateway"\n'
        )
        (source / "auth.json").write_text(json.dumps({"account": name}))
        monkeypatch.setattr(app_server, "_codex_home_config_source_from_env", lambda s=source: s)
        monkeypatch.setattr(Path, "home", lambda: tmp_path)
        homes[name] = app_server._probe_codex_home([])

    assert homes["account-a"] != homes["account-b"]
    for name, home in homes.items():
        auth = home / "auth.json"
        assert auth.is_symlink()
        assert json.loads(auth.read_text()) == {"account": name}


@pytest.mark.parametrize(
    "change",
    ["catalog-content", "catalog-path", "default-model", "profile", "auth", "source-home"],
)
def test_codex_catalog_fingerprint_tracks_configured_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, change: str
) -> None:
    from omnigent.harnesses.codex_native import app_server

    catalog = tmp_path / "models.json"
    catalog.write_text('{"models": []}')
    config = tmp_path / "config.toml"
    config.write_text('model = "first"\nmodel_catalog_json = "models.json"\n')
    monkeypatch.setattr(app_server, "_codex_home_config_source_from_env", lambda: tmp_path)
    launch = NativeCodexLaunch([], None, None)
    before = app_server.codex_catalog_fingerprint(launch, codex_path=sys.executable)

    if change == "catalog-content":
        catalog.write_text('{"models": [{"slug": "newly-available"}]}')
    elif change == "catalog-path":
        config.write_text('model = "first"\nmodel_catalog_json = "another.json"\n')
    elif change == "default-model":
        config.write_text('model = "second"\nmodel_catalog_json = "models.json"\n')
    elif change == "profile":
        config.write_text(
            config.read_text() + 'profile = "work"\n[profiles.work]\nmodel = "profile-model"\n'
        )
    elif change == "auth":
        (tmp_path / "auth.json").write_text("{}")
    else:
        monkeypatch.setattr(
            app_server, "_codex_home_config_source_from_env", lambda: tmp_path / "other"
        )

    assert app_server.codex_catalog_fingerprint(launch, codex_path=sys.executable) != before


@pytest.mark.parametrize("code", [-32600, -32602])
@pytest.mark.parametrize("include_hidden", [False, True])
async def test_old_codex_model_list_retries_without_include_hidden_and_paginates(
    code: int,
    include_hidden: bool,
) -> None:
    from omnigent.harnesses.codex_native import app_server

    client = AsyncMock(spec=CodexAppServerClient)
    first = {"id": "first", "isDefault": True}
    second = {"id": "second"}
    client.request.side_effect = [
        CodexAppServerResponseError({"code": code, "message": "unknown field `includeHidden`"}),
        {"result": {"data": [first], "nextCursor": "page2"}},
        {"result": {"data": [second], "nextCursor": None}},
    ]

    assert await app_server.list_codex_model_options(client, include_hidden=include_hidden) == [
        first,
        second,
    ]
    assert [call.args for call in client.request.await_args_list] == [
        ("model/list", {"includeHidden": include_hidden}),
        ("model/list", {}),
        ("model/list", {"cursor": "page2"}),
    ]


@pytest.mark.parametrize("code", [-32600, -32601, -32602, -32603])
async def test_codex_model_list_does_not_hide_failures_or_retry_forever(code: int) -> None:
    from omnigent.harnesses.codex_native import app_server

    client = AsyncMock(spec=CodexAppServerClient)
    client.request.side_effect = CodexAppServerResponseError({"code": code})
    with pytest.raises(CodexAppServerResponseError):
        await app_server.list_codex_model_options(client)
    assert client.request.await_count == (2 if code == -32602 else 1)


@pytest.mark.parametrize("config_text", [None, "", 'model = "second"\n'])
@pytest.mark.parametrize("config_error", [-32600, -32601, -32602])
async def test_codex_without_custom_catalog_keeps_models_and_default(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    config_text: str | None,
    config_error: int,
) -> None:
    from omnigent.harnesses.codex_native import app_server

    source = tmp_path / "source"
    source.mkdir()
    if config_text is not None:
        (source / "config.toml").write_text(config_text)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setattr(app_server, "_codex_home_config_source_from_env", lambda: source)
    client = AsyncMock(spec=CodexAppServerClient)

    async def request(method: str, params: object) -> dict[str, object]:
        if method == "config/read":
            raise CodexAppServerResponseError(
                {"code": config_error, "message": "unknown variant `config/read`"}
            )
        assert method == "model/list"
        return {"result": {"data": [{"id": "first", "isDefault": True}, {"id": "second"}]}}

    client.request.side_effect = request
    monkeypatch.setattr(app_server, "CodexAppServerClient", lambda **kwargs: client)
    monkeypatch.setattr(app_server, "_start_codex_model_discovery_process", AsyncMock())
    monkeypatch.setattr(app_server, "_wait_for_discovery_listener", AsyncMock())
    stop = AsyncMock()
    monkeypatch.setattr(app_server, "_stop_codex_model_discovery_process", stop)

    rows = await app_server.probe_codex_model_options(
        codex_path="/test/codex", launch=NativeCodexLaunch([], None, None)
    )
    expected = [{"id": "first"}, {"id": "second"}]
    expected[0]["isDefault"] = True
    assert rows == expected
    config_path = source / "config.toml"
    config = tomllib.loads(config_path.read_text()) if config_path.exists() else {}
    assert "model_catalog_json" not in config
    client.close.assert_awaited_once()
    stop.assert_awaited_once()


@pytest.mark.parametrize("code", [-32600, -32602])
async def test_codex_config_read_retries_legacy_params(code: int) -> None:
    from omnigent.harnesses.codex_native import app_server

    client = AsyncMock(spec=CodexAppServerClient)
    client.request.side_effect = [
        CodexAppServerResponseError({"code": code, "message": "unknown field `includeLayers`"}),
        {"result": {"config": {"model": "configured"}}},
    ]
    assert await app_server._read_codex_probe_default(client) == "configured"
    assert [call.args for call in client.request.await_args_list] == [
        ("config/read", {"includeLayers": False}),
        ("config/read", {}),
    ]


async def test_codex_config_read_preserves_unrelated_errors() -> None:
    from omnigent.harnesses.codex_native import app_server

    client = AsyncMock(spec=CodexAppServerClient)
    client.request.side_effect = CodexAppServerResponseError({"code": -32603})
    with pytest.raises(CodexAppServerResponseError):
        await app_server._read_codex_probe_default(client)
    client.request.assert_awaited_once()
