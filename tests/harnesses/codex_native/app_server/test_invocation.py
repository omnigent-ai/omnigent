"""Configured Codex command invocation parity tests."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import (
    NativeCodexLaunch,
    _build_native_codex_app_server_argv,
    _codex_startup_timeout_seconds,
    _model_discovery_cache_key,
    _resolve_native_codex_invocation,
    codex_catalog_fingerprint,
)
from omnigent.harnesses.codex_native.invocation import (
    CodexInvocation,
    resolve_codex_invocation,
)


def test_resolve_invocation_freezes_configured_command_and_args() -> None:
    """The effective native config becomes one reusable immutable value."""
    invocation = resolve_codex_invocation(
        cfg={
            "harness": {
                "codex-native": {
                    "command": "env",
                    "args": ["MANAGED_CODEX_MODE=0", "managed-codex", "codex", "--"],
                }
            }
        }
    )
    assert invocation.executable == "env"
    assert invocation.argv_prefix == ("MANAGED_CODEX_MODE=0", "managed-codex", "codex", "--")
    assert invocation.configured is True


@pytest.mark.parametrize(
    ("cfg", "env_command", "explicit", "expected_command", "expected_prefix"),
    [
        (
            {"harness": {"codex-native": {"command": "config", "args": ["--"]}}},
            None,
            None,
            "config",
            ("--",),
        ),
        ({}, "env", None, "env", ()),
        (
            {"harness": {"codex-native": {"command": "config", "args": ["--"]}}},
            "env",
            None,
            "env",
            (),
        ),
        (
            {"harness": {"codex-native": {"command": "env", "args": ["--"]}}},
            "env",
            None,
            "env",
            ("--",),
        ),
        (
            {"harness": {"codex-native": {"command": "config", "args": ["--"]}}},
            None,
            "explicit",
            "explicit",
            (),
        ),
        (
            {"harness": {"codex-native": {"command": "config", "args": ["--"]}}},
            None,
            "config",
            "config",
            ("--",),
        ),
    ],
    ids=[
        "config-only",
        "env-only",
        "env-beats-config",
        "env-matches-config",
        "explicit-differs",
        "explicit-equal",
    ],
)
def test_resolve_invocation_command_precedence(
    monkeypatch: pytest.MonkeyPatch,
    cfg: dict[str, object],
    env_command: str | None,
    explicit: str | None,
    expected_command: str,
    expected_prefix: tuple[str, ...],
) -> None:
    """Shared precedence selects the command before configured args inheritance."""
    if env_command is None:
        monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_CODEX_PATH", env_command)
    invocation = resolve_codex_invocation(explicit=explicit, cfg=cfg)
    assert invocation.executable == expected_command
    assert invocation.argv_prefix == expected_prefix


def test_env_command_overrides_config_without_app_server_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A differing env command does not inherit config args or app-server status."""
    monkeypatch.setenv("OMNIGENT_CODEX_PATH", "managed-codex-env")
    invocation = resolve_codex_invocation(
        cfg={
            "harness": {
                "codex-native": {
                    "command": "managed-codex-config",
                    "args": ["--managed-config"],
                }
            }
        }
    )
    assert invocation.executable == "managed-codex-env"
    assert invocation.argv_prefix == ()
    assert invocation.terminal_prefix == ()
    assert invocation.app_server_configured is False


def test_args_only_config_is_terminal_only() -> None:
    """Args without a command configure the TUI, not app-server startup."""
    invocation = resolve_codex_invocation(
        cfg={"harness": {"codex-native": {"args": ["--remote-config"]}}}
    )
    assert invocation.argv_prefix == ()
    assert invocation.terminal_prefix == ("--remote-config",)
    assert invocation.argv("app-server") == ("codex", "app-server")


def test_args_only_config_survives_bare_executable_resolution(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Making bare Codex absolute must retain its terminal-only arguments."""
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server.resolve_codex_invocation",
        lambda: CodexInvocation(
            "codex",
            terminal_prefix=("--terminal-only",),
            configured=True,
            app_server_configured=False,
        ),
    )
    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.app_server._find_codex_cli",
        lambda: "/opt/codex/bin/codex",
    )

    invocation = _resolve_native_codex_invocation()

    assert invocation.executable == "/opt/codex/bin/codex"
    assert invocation.argv_prefix == ()
    assert invocation.terminal_prefix == ("--terminal-only",)
    assert invocation.app_server_configured is False


def test_configured_app_server_timeout_is_distinct_from_args_only() -> None:
    """Only a command-wrapped app-server gets the configured bootstrap budget."""
    configured = CodexInvocation("wrapper", configured=True, app_server_configured=True)
    args_only = CodexInvocation(
        "codex", configured=True, terminal_prefix=("--remote-config",), app_server_configured=False
    )
    assert _codex_startup_timeout_seconds(configured, 60.0) == 120.0
    assert _codex_startup_timeout_seconds(args_only, 60.0) == 60.0
    assert _codex_startup_timeout_seconds(CodexInvocation("codex"), 60.0) == 60.0


async def test_command_only_wrapper_reaches_extended_catalog_plumbing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Command-only wrappers stay on the resolved invocation for startup probes."""
    from omnigent.harnesses.codex_native import app_server
    from omnigent.inner.codex_executor import CODEX_EXTENDED_CATALOG_ENV_VAR
    from tests.harnesses.codex_native.app_server._support import (
        _disable_codex_startup_rpc,
        _test_app_server,
    )

    source = tmp_path / "source"
    source.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(source))
    _disable_codex_startup_rpc(monkeypatch)
    invocation = CodexInvocation(
        sys.executable,
        configured=True,
        app_server_configured=True,
    )
    server = _test_app_server(
        tmp_path,
        tmp_path / "private",
        tmp_path / "bridge",
        tmp_path,
        env={CODEX_EXTENDED_CATALOG_ENV_VAR: "1"},
    )
    server.codex_invocation = invocation
    server.reconcile_process_registry = False

    version_targets: list[object] = []

    async def _version(target: object) -> tuple[int, int, int]:
        version_targets.append(target)
        return (0, 154, 0)

    populate_kwargs: dict[str, object] = {}

    def _populate(*_args: object, **kwargs: object) -> None:
        populate_kwargs.update(kwargs)

    monkeypatch.setattr(app_server, "_codex_cli_version", _version)
    monkeypatch.setattr(app_server, "_populate_codex_home_config", _populate)

    await server.start()
    await server.close()

    assert version_targets == [invocation]
    assert populate_kwargs["extend_model_catalog"] is True
    assert populate_kwargs["codex_invocation"] is invocation


def test_app_server_argv_keeps_default_bare_and_prepends_configured_prefix() -> None:
    """A configured wrapper is prepended exactly once; bare argv is unchanged."""
    assert _build_native_codex_app_server_argv(
        tagged_argv0="codex",
        listen_url="ws://127.0.0.1:1234",
        config_overrides=(),
    ) == ["codex", "app-server", "--listen", "ws://127.0.0.1:1234"]
    assert _build_native_codex_app_server_argv(
        tagged_argv0="env",
        invocation_prefix=("MANAGED_CODEX_MODE=0", "managed-codex", "codex", "--"),
        listen_url="ws://127.0.0.1:1234",
        config_overrides=(),
    ) == [
        "env",
        "MANAGED_CODEX_MODE=0",
        "managed-codex",
        "codex",
        "--",
        "app-server",
        "--listen",
        "ws://127.0.0.1:1234",
    ]


def test_catalog_fingerprint_includes_configured_prefix() -> None:
    """Bare and wrapped Codex catalogs cannot share a store entry."""
    launch = NativeCodexLaunch([], None, None)
    bare = codex_catalog_fingerprint(
        launch,
        codex_invocation=CodexInvocation("/bin/codex"),
    )
    wrapped = codex_catalog_fingerprint(
        launch,
        codex_invocation=CodexInvocation("env", ("managed-codex", "codex", "--")),
    )
    assert bare != wrapped


def test_catalog_fingerprint_tracks_changed_wrapper_prefix() -> None:
    """Changing a configured wrapper prefix selects a new disk cache entry."""
    launch = NativeCodexLaunch([], None, None)
    first = codex_catalog_fingerprint(
        launch,
        codex_invocation=CodexInvocation("env", ("managed-codex", "codex", "--")),
    )
    second = codex_catalog_fingerprint(
        launch,
        codex_invocation=CodexInvocation(
            "env", ("managed-codex", "codex", "--profile", "enterprise")
        ),
    )
    assert first != second


def test_discovery_cache_key_tracks_in_place_wrapper_replacement(tmp_path: Path) -> None:
    """A wrapper update invalidates short-lived model discovery rows."""
    wrapper = tmp_path / "managed-codex"
    wrapper.write_text("wrapper-v1")
    invocation = CodexInvocation(str(wrapper))
    first = _model_discovery_cache_key(invocation)
    wrapper.write_text("wrapper-v2-with-new-content")
    assert _model_discovery_cache_key(invocation) != first


def test_invocation_cache_identity_does_not_retain_prefix_values(tmp_path: Path) -> None:
    """Cache identities do not expose assignments or arbitrary wrapper args."""
    from omnigent.harnesses.codex_native import app_server
    from omnigent.inner import codex_executor

    secret_assignment = "MANAGED_CODEX_TOKEN=do-not-retain-this-value"
    secret_argument = "private-profile-value"
    invocation = CodexInvocation(
        "env",
        (secret_assignment, "managed-codex", "--profile", secret_argument),
    )
    launch = NativeCodexLaunch([], None, None)
    identities = (
        _model_discovery_cache_key(invocation),
        repr(app_server._codex_invocation_identity(invocation)),
        repr(
            codex_executor._model_catalog_cache_key("env", tmp_path, codex_invocation=invocation)
        ),
        codex_catalog_fingerprint(launch, codex_invocation=invocation),
    )
    assert all(
        secret not in identity
        for identity in identities
        for secret in (secret_assignment, secret_argument)
    )


def test_debug_models_probe_uses_configured_prefix(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The catalog subprocess receives the same wrapper as the app-server."""
    from omnigent.inner import codex_executor

    captured: list[str] = []

    class _Completed:
        returncode = 0
        stderr = ""
        stdout = '{"models": [{"slug": "gpt-5.6-test"}]}'

    def _run(argv: list[str], **_kwargs: object) -> _Completed:
        captured.extend(argv)
        return _Completed()

    monkeypatch.setattr(codex_executor.subprocess, "run", _run)
    assert (
        codex_executor.read_codex_model_catalog(
            "env",
            tmp_path,
            codex_invocation=CodexInvocation("env", ("managed-codex", "codex", "--")),
        )
        is not None
    )
    assert captured == ["env", "managed-codex", "codex", "--", "debug", "models"]


def test_catalog_cache_key_tracks_in_place_wrapper_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Updating a configured wrapper invalidates the in-process probe cache."""
    from omnigent.inner import codex_executor

    wrapper = tmp_path / "wrapper"
    wrapper.write_text("v1")
    calls: list[int] = []

    def _probe(
        _codex_path: str,
        _source_home: Path,
        *,
        timeout: float,
        codex_invocation: CodexInvocation | None = None,
    ) -> dict[str, object]:
        del timeout, codex_invocation
        calls.append(1)
        return {"models": [{"slug": "gpt-5.6-test"}]}

    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_CACHE", {})
    monkeypatch.setattr(codex_executor, "_MODEL_CATALOG_FAILURES", {})
    monkeypatch.setattr(codex_executor, "_probe_codex_model_catalog", _probe)
    invocation = CodexInvocation("env", (str(wrapper),))

    assert (
        codex_executor.read_codex_model_catalog("env", tmp_path, codex_invocation=invocation)
        is not None
    )
    assert (
        codex_executor.read_codex_model_catalog("env", tmp_path, codex_invocation=invocation)
        is not None
    )
    assert calls == [1]

    wrapper.write_text("v2-with-a-different-size")
    assert (
        codex_executor.read_codex_model_catalog("env", tmp_path, codex_invocation=invocation)
        is not None
    )
    assert calls == [1, 1]


async def test_discovery_process_uses_configured_prefix(monkeypatch: pytest.MonkeyPatch) -> None:
    """The short-lived model discovery server receives the configured wrapper."""
    from omnigent.harnesses.codex_native import app_server

    captured: list[object] = []

    async def _create(*args: object, **kwargs: object) -> object:
        captured.extend(args)
        stderr = asyncio.StreamReader()
        stderr.feed_eof()

        class _Process:
            returncode = None
            pid = None
            self_stderr = stderr

            def __init__(self) -> None:
                self.stderr = self.self_stderr

        return _Process()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", _create)
    discovery = await app_server._start_codex_model_discovery_process(
        codex_path="env",
        invocation=CodexInvocation("env", ("managed-codex", "codex", "--")),
        listen_url="ws://127.0.0.1:1234",
        env={},
        cwd=Path("/tmp"),
    )
    await discovery.stderr_tail
    assert captured[:6] == ["env", "managed-codex", "codex", "--", "app-server", "--listen"]


@pytest.mark.parametrize("command", ["--version", "debug", "models"])
def test_invocation_builds_immutable_prefix(command: str) -> None:
    """The invocation value composes wrapper args before every Codex command."""
    invocation = CodexInvocation("env", ("managed-codex", "codex", "--"))
    if command == "--version":
        assert invocation.argv(command) == ("env", "managed-codex", "codex", "--", "--version")
    else:
        assert invocation.argv(command, "models") == (
            "env",
            "managed-codex",
            "codex",
            "--",
            command,
            "models",
        )
