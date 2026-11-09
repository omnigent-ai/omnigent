"""Tests for GenieExecutor and the genie harness registration.

Genie is Databricks' fork of the codex CLI; :class:`GenieExecutor` is a thin
:class:`CodexExecutor` subclass that swaps the binary (``genie``), the on-disk
home (``GENIE_HOME`` / ``~/.genie``), moves ``-c`` overrides into the private
``config.toml`` (genie denies ``-c``), and maps Databricks endpoint names to
genie catalog slugs. These tests pin those seams, the gateway-config
inheritance, the brokered-auth differences, plus the ``genie`` harness
registry entry.
"""

from __future__ import annotations

import asyncio
import tomllib
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from packaging.version import Version

from omnigent.inner.codex_executor import (
    CodexExecutor,
    _CodexAppServerSession,
    _databricks_codex_config_overrides,
    _require_brokered_codex_version,
    write_codex_config_overrides,
)
from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec
from omnigent.inner.executor import ExecutorConfig, ExecutorError
from omnigent.inner.genie_executor import (
    GENIE_HOME_DIR_NAME,
    GENIE_HOME_ENV_VAR,
    GenieExecutor,
    _find_genie_cli,
    genie_model_id,
)
from omnigent.inner.model_egress import FrozenModelRoute
from omnigent.inner.model_signer import SignerLaunchConfig
from omnigent.models.genie_catalog import GENIE_CATALOG_MODELS
from omnigent.util.reasoning_effort import CODEX_EFFORTS, GENIE_EFFORTS

_GATEWAY_BASE_URL = "https://example.databricks.com/ai-gateway/codex/v1"


def _gateway_kwargs(base_url: str) -> dict[str, object]:
    """Explicit gateway routing to *base_url*, independent of ~/.databrickscfg."""
    return {
        "gateway": True,
        "gateway_host": "https://example.databricks.com",
        "base_url_override": base_url,
        "gateway_auth_command": "printf token",
        "model": "gpt-5.5",
    }


def _genie_executor(**overrides: object) -> GenieExecutor:
    """Build a GenieExecutor with the genie binary resolution stubbed."""
    with patch(
        "omnigent.inner.genie_executor._find_genie_cli",
        return_value="/usr/bin/genie",
    ):
        return GenieExecutor(**overrides)  # type: ignore[arg-type]


class TestGenieExecutor:
    def test_is_codex_executor_subclass(self) -> None:
        # The whole design: reuse the codex app-server driver, don't fork it.
        assert issubclass(GenieExecutor, CodexExecutor)

    def test_forces_genie_home(self) -> None:
        ex = _genie_executor()
        assert ex._home_env_var == GENIE_HOME_ENV_VAR == "GENIE_HOME"
        assert ex._home_dir_name == GENIE_HOME_DIR_NAME == ".genie"

    def test_home_override_cannot_be_smuggled_in(self) -> None:
        # Even if a caller passes codex's home, genie forces its own.
        ex = _genie_executor(home_env_var="CODEX_HOME", home_dir_name=".codex")
        assert ex._home_env_var == "GENIE_HOME"
        assert ex._home_dir_name == ".genie"

    def test_resolves_genie_binary(self) -> None:
        with patch(
            "omnigent.inner.genie_executor._find_genie_cli",
            return_value="/opt/genie",
        ):
            ex = GenieExecutor()
        assert ex._codex_path == "/opt/genie"

    def test_missing_binary_raises_with_genie_hint(self) -> None:
        with patch("omnigent.inner.genie_executor._find_genie_cli", return_value=None):
            with pytest.raises(ImportError, match="OMNIGENT_GENIE_PATH"):
                GenieExecutor()

    def test_find_genie_cli_uses_genie_name_and_env(self) -> None:
        with patch("omnigent.inner.genie_executor.resolve_cli_binary") as resolve:
            resolve.return_value = "/usr/bin/genie"
            assert _find_genie_cli() == "/usr/bin/genie"
        resolve.assert_called_once_with("genie", env_var="OMNIGENT_GENIE_PATH")

    def test_inherits_databricks_gateway_config(self) -> None:
        # Genie routes through the same Databricks Codex Responses gateway
        # (/ai-gateway/codex/v1) as codex; the gateway -c overrides come from
        # the shared CodexExecutor path unchanged.
        # An explicit gateway host skips the ~/.databrickscfg profile lookup,
        # so the test does not depend on the machine's Databricks config.
        ex = _genie_executor(
            gateway=True,
            gateway_host="https://example.databricks.com",
            base_url_override="https://example.databricks.com/ai-gateway/codex/v1",
            gateway_auth_command="printf token",
            model="databricks-gpt-5-4-mini",
        )
        joined = "\n".join(ex._codex_config_overrides)
        assert "/ai-gateway/codex/v1" in joined
        # Endpoint name is mapped to genie's catalog slug.
        assert 'model="gpt-5.4-mini"' in joined

    def test_gateway_without_profile_or_host_fails_loud(self) -> None:
        # Codex would derive the workspace from the default ~/.databrickscfg
        # profile, routing genie through whichever workspace that is.
        with pytest.raises(OSError, match="requires a Databricks profile or gateway host"):
            _genie_executor(gateway=True, model="databricks-gpt-5-4-mini")

    def test_brokered_session_skips_bundled_catalog_probe(self) -> None:
        # Genie has no ``debug models`` command to write a brokered catalog.
        genie = _genie_executor()
        codex = CodexExecutor(codex_path="/usr/bin/codex")
        assert genie._app_session_factory.keywords["brokered_catalog_probe"] is False
        assert codex._app_session_factory.keywords["brokered_catalog_probe"] is True

    @pytest.mark.parametrize(
        ("banner", "accepted"),
        [
            ("genie 0.2.0", True),
            ("genie 0.3.1", True),
            ("genie 0.1.9", False),
            ("genie 0.2.0-alpha.8", False),
            ("codex-cli 0.163.0", False),
        ],
    )
    async def test_brokered_version_floor_reads_the_genie_banner(
        self, tmp_path: Path, banner: str, accepted: bool
    ) -> None:
        cli = tmp_path / "genie"
        cli.write_text(f"#!/bin/sh\necho '{banner}'\n")
        cli.chmod(0o755)
        check = _require_brokered_codex_version(
            str(cli),
            banner=GenieExecutor._brokered_version_banner,
            minimum=GenieExecutor._brokered_min_version,
            cli_name=GenieExecutor._brokered_cli_name,
        )
        if accepted:
            await check
        else:
            with pytest.raises(RuntimeError, match="Genie wire version"):
                await check
        assert GenieExecutor._brokered_min_version == Version("0.2.0")

    def test_writes_overrides_to_config_instead_of_argv(self) -> None:
        ex = _genie_executor()
        assert ex._config_overrides_in_file is True

    @pytest.mark.parametrize(
        ("model", "expected"),
        [
            ("databricks-gpt-5-4-mini", "gpt-5.4-mini"),
            ("databricks-gpt-5-6-luna", "gpt-5.6-luna"),
            ("databricks-gpt-5-5", "gpt-5.5"),
            ("databricks-gpt-5-2", "gpt-5.2"),
            ("gpt-5.4-mini", "gpt-5.4-mini"),
            # Genie's catalog lists these two under their endpoint names.
            ("databricks-gpt-5-4-nano", "databricks-gpt-5-4-nano"),
            ("databricks-gpt-5-2-codex", "databricks-gpt-5-2-codex"),
            ("databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-6"),
            ("gpt-x", "gpt-x"),
        ],
    )
    def test_genie_model_id(self, model: str, expected: str) -> None:
        # Mirrors genie's databricks_codex_model_id; genie silently swaps a
        # model missing from its catalog for the provider default.
        assert genie_model_id(model) == expected

    def test_genie_model_id_keeps_every_catalog_slug(self) -> None:
        # The pinned listing offers these ids, so the launch must not remap them.
        for model in GENIE_CATALOG_MODELS:
            assert genie_model_id(model.slug) == model.slug

    @pytest.mark.parametrize(
        ("gateway_url", "strict"),
        [
            (_GATEWAY_BASE_URL, True),
            (f"{_GATEWAY_BASE_URL}/", True),
            ("https://llm.test/v1", False),
        ],
    )
    def test_catalog_is_enforced_only_on_the_databricks_gateway(
        self, gateway_url: str, strict: bool
    ) -> None:
        # Off the gateway genie forwards an unknown model to the provider
        # instead of swapping it, so only the gateway route is held to it.
        # Not ``base_url``: that name is pytest-base-url's session fixture.
        gateway = _gateway_kwargs(gateway_url)
        genie = _genie_executor(**gateway)
        assert genie._app_session_factory.keywords["strict_model_catalog"] is strict
        assert genie._app_session_factory.keywords["effort_vocabulary"] == GENIE_EFFORTS
        codex = CodexExecutor(codex_path="/usr/bin/codex", **gateway)  # type: ignore[arg-type]
        assert codex._app_session_factory.keywords["strict_model_catalog"] is False
        assert codex._app_session_factory.keywords["effort_vocabulary"] == CODEX_EFFORTS

    def test_brokered_gateway_session_enforces_the_catalog(self) -> None:
        # The signer endpoint is the ucode gateway URL, which genie also swaps on.
        genie = _genie_executor(
            os_env=OSEnvSpec(sandbox=OSEnvSandboxSpec(type="darwin_seatbelt")),
            model="gpt-5.5",
            signer_launch_config=SignerLaunchConfig(
                binding_id="test-fake-provider-v1",
                endpoint=_GATEWAY_BASE_URL,
                routes=(
                    FrozenModelRoute(
                        method="POST",
                        host="example.databricks.com",
                        path="/ai-gateway/codex/v1/responses",
                    ),
                ),
            ),
        )
        assert genie._app_session_factory.keywords["strict_model_catalog"] is True

    @pytest.mark.parametrize(("effort", "accepted"), [("ultra", True), ("minimal", False)])
    async def test_run_validates_effort_against_genie_vocabulary(
        self, effort: str, accepted: bool
    ) -> None:
        # Codex folds ultra to xhigh; genie runs ultra and never takes minimal.
        calls: list[dict[str, Any]] = []

        class _RecordingSession:
            async def run_turn(self, **kwargs: Any) -> Any:
                calls.append(kwargs)
                return
                yield

        genie = _genie_executor(
            app_session_factory=lambda **_: _RecordingSession(),
            **_gateway_kwargs(_GATEWAY_BASE_URL),
        )
        events = [
            event
            async for event in genie.run_turn(
                [{"role": "user", "content": "hi", "session_id": "s1"}],
                [],
                "",
                config=ExecutorConfig(extra={"reasoning_effort": effort}),
            )
        ]
        if accepted:
            assert [call["reasoning_effort"] for call in calls] == [effort]
        else:
            assert calls == []
            assert isinstance(events[0], ExecutorError)
            assert "minimal" in events[0].message


class _FakePipe:
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        return None

    def close(self) -> None:
        pass

    async def wait_closed(self) -> None:
        return None

    async def readline(self) -> bytes:
        return b""

    async def read(self, n: int) -> bytes:
        return b""


class _FakeProcess:
    def __init__(self) -> None:
        self.stdin = _FakePipe()
        self.stdout = _FakePipe()
        self.stderr = _FakePipe()
        self.returncode: int | None = None
        self.pid = 12345

    def terminate(self) -> None:
        self.returncode = 0

    def kill(self) -> None:
        self.returncode = -9

    async def wait(self) -> int:
        return self.returncode or 0


async def _start_session(
    tmp_path: Path, *, source_config: str | None = None, **session_kwargs: Any
) -> tuple[list[str], dict[str, Any]]:
    """Start an app-server session against a fake process; return argv + config.

    :param source_config: ``config.toml`` text for the user's real home, e.g.
        ``'model_reasoning_effort = "max"'``; ``None`` leaves the home empty.
    """
    captured_argv: list[str] = []
    captured_env: dict[str, str] = {}

    async def _fake_create_subprocess_exec(*args: str, **kwargs: Any) -> _FakeProcess:
        captured_argv.extend(args)
        captured_env.update(kwargs["env"])
        return _FakeProcess()

    source_home = tmp_path / "source-home"
    source_home.mkdir()
    if source_config is not None:
        (source_home / "config.toml").write_text(source_config, encoding="utf-8")
    session = _CodexAppServerSession(
        cwd=str(tmp_path), env={}, tool_executor=None, **session_kwargs
    )
    session._request = AsyncMock(return_value={"result": {}})
    with (
        patch(
            "omnigent.inner.codex_executor._create_subprocess_exec",
            new=_fake_create_subprocess_exec,
        ),
        patch(
            "omnigent.inner.codex_executor._codex_home_config_source_from_env",
            return_value=source_home,
        ),
    ):
        await session.start()
        home = Path(captured_env[session._home_env_var])
        config_path = home / "config.toml"
        config = (
            tomllib.loads(config_path.read_text(encoding="utf-8")) if config_path.exists() else {}
        )
        await session.close()
    return captured_argv, config


class TestConfigOverridesInFile:
    async def test_genie_session_launches_bare_app_server(self, tmp_path: Path) -> None:
        overrides = _databricks_codex_config_overrides(
            model="gpt-5.4-mini",
            base_url="https://example.databricks.com/ai-gateway/codex/v1",
            auth_command="printf token",
        )
        argv, config = await _start_session(
            tmp_path,
            codex_path="/usr/bin/genie",
            codex_config_overrides=[*overrides, 'web_search="disabled"'],
            home_env_var=GENIE_HOME_ENV_VAR,
            home_dir_name=GENIE_HOME_DIR_NAME,
            config_overrides_in_file=True,
        )
        # genie's CLI allowlist denies every app-server flag, including -c.
        assert argv == ["/usr/bin/genie", "app-server"]
        assert config["model"] == "gpt-5.4-mini"
        assert config["model_provider"] == "omnigent_databricks"
        assert config["model_supports_reasoning_summaries"] is True
        assert config["web_search"] == "disabled"
        assert (
            "/ai-gateway/codex/v1" in config["model_providers"]["omnigent_databricks"]["base_url"]
        )

    async def test_codex_session_still_passes_c_flags(self, tmp_path: Path) -> None:
        argv, config = await _start_session(
            tmp_path,
            codex_path="/usr/bin/codex",
            codex_config_overrides=['model="gpt-5.4-mini"'],
        )
        assert argv == ["/usr/bin/codex", "app-server", "-c", 'model="gpt-5.4-mini"']
        assert "model" not in config

    @pytest.mark.parametrize(
        ("vocabulary", "expected"), [(GENIE_EFFORTS, "max"), (CODEX_EFFORTS, "xhigh")]
    )
    async def test_copied_config_effort_follows_the_cli_vocabulary(
        self, tmp_path: Path, vocabulary: frozenset[str], expected: str
    ) -> None:
        # A genie user's max is real on its 5.6 models; codex's SDK wire caps at xhigh.
        _, config = await _start_session(
            tmp_path,
            source_config='model_reasoning_effort = "max"\n',
            codex_path="/usr/bin/genie",
            effort_vocabulary=vocabulary,
        )
        assert config["model_reasoning_effort"] == expected


_STRICT_CATALOG = [
    {
        "model": "gpt-5.6-sol",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort}
            for effort in ("low", "medium", "high", "xhigh", "max", "ultra")
        ],
    },
    {
        "model": "gpt-5.5",
        "defaultReasoningEffort": "medium",
        "supportedReasoningEfforts": [
            {"reasoningEffort": effort} for effort in ("low", "medium", "high", "xhigh")
        ],
    },
]


def _strict_session(
    *, served_model: str | None = None, inherited_effort: str | None = None
) -> tuple[_CodexAppServerSession, list[tuple[str, dict[str, Any]]]]:
    """A strict genie app session whose scripted app-server lists ``_STRICT_CATALOG``.

    :param served_model: Model ``thread/start`` reports, e.g. ``"gpt-5.6-sol"``
        to mimic genie's silent swap; ``None`` echoes the requested model.
    :param inherited_effort: Effort ``thread/start`` reports from the copied
        config, e.g. ``"max"``; ``None`` reports none.
    :returns: The session and the ``(method, params)`` requests it sends.
    """
    session = _CodexAppServerSession(
        codex_path="/usr/bin/genie",
        cwd="/tmp/workspace",
        env={},
        tool_executor=None,
        strict_model_catalog=True,
    )
    session.start = AsyncMock()  # type: ignore[method-assign]
    session._proc = _FakeProcess()  # type: ignore[assignment]
    requests: list[tuple[str, dict[str, Any]]] = []

    async def _request(method: str, params: dict[str, Any]) -> dict[str, Any]:
        requests.append((method, params))
        if method == "model/list":
            return {"result": {"data": _STRICT_CATALOG, "nextCursor": None}}
        if method == "thread/start":
            model = served_model or params["model"]
            result = {"thread": {"id": "thread-1"}, "model": model}
            if inherited_effort is not None:
                result["reasoningEffort"] = inherited_effort
            return {"result": result}
        if method == "turn/start":
            turn = {"id": f"turn-{len(requests)}"}
            asyncio.get_running_loop().call_soon(
                session._events.put_nowait, {"method": "turn/completed", "params": {"turn": turn}}
            )
            return {"result": {"turn": turn}}
        return {"result": {}}

    session._request = AsyncMock(side_effect=_request)  # type: ignore[method-assign]
    return session, requests


async def _strict_turn(
    session: _CodexAppServerSession, *, model: str, effort: str | None
) -> list[object]:
    """Run one turn and collect its events."""
    return [
        event
        async for event in session.run_turn(
            messages=[{"role": "user", "content": "hi"}],
            tools=[],
            system_prompt="",
            model=model,
            cwd="/tmp/workspace",
            sandbox="read-only",
            reasoning_effort=effort,
        )
    ]


class TestStrictModelCatalog:
    """Genie on the Databricks gateway: no silent model swap, no unlisted effort."""

    @pytest.mark.parametrize(
        ("model", "effort", "error"),
        [
            (
                "databricks-gpt-5-4-mini",
                None,
                "genie does not offer model 'databricks-gpt-5-4-mini'",
            ),
            ("gpt-5.5", "max", "genie model 'gpt-5.5' does not support reasoning effort 'max'"),
        ],
    )
    async def test_rejects_before_thread_start(
        self, model: str, effort: str | None, error: str
    ) -> None:
        session, requests = _strict_session()
        events = await _strict_turn(session, model=model, effort=effort)
        assert len(events) == 1
        assert isinstance(events[0], ExecutorError)
        assert error in events[0].message
        assert events[0].retryable is False
        assert [method for method, _ in requests] == ["model/list"]

    async def test_runs_a_catalog_model_at_an_effort_it_lists(self) -> None:
        session, requests = _strict_session()
        events = await _strict_turn(session, model="gpt-5.6-sol", effort="ultra")
        assert not any(isinstance(event, ExecutorError) for event in events)
        sent = dict(requests)
        assert sent["thread/start"]["model"] == "gpt-5.6-sol"
        assert sent["thread/settings/update"]["effort"] == "ultra"

    async def test_reports_a_substituted_model_and_drops_the_thread(self) -> None:
        session, requests = _strict_session(served_model="gpt-5.6-sol")
        events = await _strict_turn(session, model="gpt-5.5", effort=None)
        assert isinstance(events[-1], ExecutorError)
        assert "replaced the requested model 'gpt-5.5' with 'gpt-5.6-sol'" in events[-1].message
        assert session.thread_id is None
        assert "turn/start" not in [method for method, _ in requests]

    @pytest.mark.parametrize(("inherited", "sent"), [("max", ["medium"]), ("high", [])])
    async def test_default_effort_replaces_only_an_inherited_effort_the_model_lacks(
        self, inherited: str, sent: list[str]
    ) -> None:
        """A copied ``max`` would fail on gpt-5.5, so Default runs its own default effort."""
        session, requests = _strict_session(inherited_effort=inherited)
        for _ in range(2):
            events = await _strict_turn(session, model="gpt-5.5", effort=None)
            assert not any(isinstance(event, ExecutorError) for event in events)
        efforts = {
            params["effort"] for method, params in requests if method == "thread/settings/update"
        }
        assert sorted(efforts) == sent

    async def test_checks_a_later_effort_against_the_thread_model(self) -> None:
        session, requests = _strict_session()
        await _strict_turn(session, model="gpt-5.5", effort="high")
        events = await _strict_turn(session, model="gpt-5.5", effort="ultra")
        assert isinstance(events[0], ExecutorError)
        assert "does not support reasoning effort 'ultra'" in events[0].message
        methods = [method for method, _ in requests]
        assert methods.count("model/list") == 1
        assert methods.count("thread/settings/update") == 1


class TestWriteCodexConfigOverrides:
    def test_parses_toml_values_and_dotted_keys(self, tmp_path: Path) -> None:
        (tmp_path / "config.toml").write_text('model = "old"\n[features]\nkeep = true\n')
        write_codex_config_overrides(
            tmp_path,
            ['model="new"', "features.unified_exec=false", "web_search=disabled"],
        )
        config = tomllib.loads((tmp_path / "config.toml").read_text())
        assert config["model"] == "new"
        assert config["features"] == {"keep": True, "unified_exec": False}
        # Unparseable TOML falls back to a literal string, like ``-c``.
        assert config["web_search"] == "disabled"

    def test_model_id_toml_breakout_stays_inert(self, tmp_path: Path) -> None:
        payload = 'x",auth={command="sh",args=["-c","touch /tmp/pwned"]},wire_api="responses"}'
        overrides = _databricks_codex_config_overrides(
            model=payload,
            base_url="https://example.databricks.com/ai-gateway/codex/v1",
            auth_command="printf token",
        )
        write_codex_config_overrides(tmp_path, [o for o in overrides if o.startswith("model=")])
        config = tomllib.loads((tmp_path / "config.toml").read_text())
        assert config == {"model": payload}

    def test_rejects_override_without_equals(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="key=value"):
            write_codex_config_overrides(tmp_path, ["model"])


class TestGenieRegistration:
    def test_registered_as_valid_harness(self) -> None:
        from omnigent import harness_plugins as hp

        assert "genie" in hp.valid_harnesses()
        assert hp.harness_modules()["genie"] == "omnigent.inner.genie_harness"
        assert hp.model_env_keys()["genie"] == "HARNESS_GENIE_MODEL"

    def test_consumes_openai_family(self) -> None:
        from omnigent.onboarding.provider_config import _HARNESS_FAMILY, OPENAI_FAMILY

        assert _HARNESS_FAMILY["genie"] == OPENAI_FAMILY

    def test_workflow_gateway_and_profile_maps(self) -> None:
        from omnigent.runtime import workflow as w

        assert w._HARNESS_GATEWAY_FLAG["genie"] == "HARNESS_GENIE_GATEWAY"
        assert w._HARNESS_DATABRICKS_PROFILE["genie"] == "HARNESS_GENIE_DATABRICKS_PROFILE"
        assert w._PROVIDER_HARNESS_FAMILY["genie"] == "openai"
        assert "genie" in w._UCODE_HARNESS_CONFIGS
