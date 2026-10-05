"""Configured Codex command invocation parity tests."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from omnigent.harnesses.codex_native.app_server import (
    NativeCodexLaunch,
    _build_native_codex_app_server_argv,
    _codex_startup_timeout_seconds,
    _isaac_model_catalog_identity,
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
                    "args": ["ISAAC_ENABLE_UG=0", "isaac", "codex", "--"],
                }
            }
        }
    )
    assert invocation.executable == "env"
    assert invocation.argv_prefix == ("ISAAC_ENABLE_UG=0", "isaac", "codex", "--")
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
            "config",
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
    ids=["config-only", "env-only", "config-beats-env", "explicit-differs", "explicit-equal"],
)
def test_resolve_invocation_command_precedence(
    monkeypatch: pytest.MonkeyPatch,
    cfg: dict[str, object],
    env_command: str | None,
    explicit: str | None,
    expected_command: str,
    expected_prefix: tuple[str, ...],
) -> None:
    """Codex config wins over env, while explicit commands select their own args."""
    if env_command is None:
        monkeypatch.delenv("OMNIGENT_CODEX_PATH", raising=False)
    else:
        monkeypatch.setenv("OMNIGENT_CODEX_PATH", env_command)
    invocation = resolve_codex_invocation(explicit=explicit, cfg=cfg)
    assert invocation.executable == expected_command
    assert invocation.argv_prefix == expected_prefix


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


def test_app_server_argv_keeps_default_bare_and_prepends_configured_prefix() -> None:
    """A configured wrapper is prepended exactly once; bare argv is unchanged."""
    assert _build_native_codex_app_server_argv(
        tagged_argv0="codex",
        listen_url="ws://127.0.0.1:1234",
        config_overrides=(),
    ) == ["codex", "app-server", "--listen", "ws://127.0.0.1:1234"]
    assert _build_native_codex_app_server_argv(
        tagged_argv0="env",
        invocation_prefix=("ISAAC_ENABLE_UG=0", "isaac", "codex", "--"),
        listen_url="ws://127.0.0.1:1234",
        config_overrides=(),
    ) == [
        "env",
        "ISAAC_ENABLE_UG=0",
        "isaac",
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
        codex_invocation=CodexInvocation("env", ("isaac", "codex", "--")),
    )
    assert bare != wrapped


def test_catalog_fingerprint_tracks_isaac_catalog_path_and_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An Isaac catalog path or in-place edit selects a new disk cache entry."""
    first = tmp_path / "catalog-a.json"
    second = tmp_path / "catalog-b.json"
    first.write_text("catalog-a")
    second.write_text("catalog-b")
    launch = NativeCodexLaunch([], None, None)
    invocation = CodexInvocation("env", ("isaac", "codex", "--"))

    monkeypatch.setenv("ISAAC_CODEX_MODEL_CATALOG_PATH", str(first))
    path_a = codex_catalog_fingerprint(launch, codex_invocation=invocation)
    monkeypatch.setenv("ISAAC_CODEX_MODEL_CATALOG_PATH", str(second))
    path_b = codex_catalog_fingerprint(launch, codex_invocation=invocation)
    assert path_a != path_b

    second.write_text("catalog-b-updated-with-new-content")
    path_b_updated = codex_catalog_fingerprint(launch, codex_invocation=invocation)
    assert path_b_updated != path_b


def test_discovery_cache_key_tracks_isaac_catalog_path_and_contents(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Short-lived discovery does not replay rows after Isaac catalog edits."""
    catalog = tmp_path / "catalog.json"
    catalog.write_text("catalog-v1")
    invocation = CodexInvocation("env", ("isaac", "codex", "--"))

    monkeypatch.setenv("ISAAC_CODEX_MODEL_CATALOG_PATH", str(catalog))
    first = _model_discovery_cache_key(invocation)
    catalog.write_text("catalog-v2-with-new-content")
    second = _model_discovery_cache_key(invocation)
    assert second != first

    other = tmp_path / "other-catalog.json"
    other.write_text("catalog-v2-with-new-content")
    monkeypatch.setenv("ISAAC_CODEX_MODEL_CATALOG_PATH", str(other))
    assert _model_discovery_cache_key(invocation) != second


def test_discovery_cache_key_tracks_in_place_wrapper_replacement(tmp_path: Path) -> None:
    """A wrapper update invalidates short-lived model discovery rows."""
    wrapper = tmp_path / "isaac"
    wrapper.write_text("wrapper-v1")
    invocation = CodexInvocation(str(wrapper))
    first = _model_discovery_cache_key(invocation)
    wrapper.write_text("wrapper-v2-with-new-content")
    assert _model_discovery_cache_key(invocation) != first


def test_isaac_catalog_identity_fails_soft_and_keeps_raw_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing and looping paths never abort fingerprint calculation."""
    missing = "relative/missing-catalog.json"
    monkeypatch.chdir(tmp_path)
    invocation = CodexInvocation("env", (f"ISAAC_CODEX_MODEL_CATALOG_PATH={missing}",))
    missing_identity = _isaac_model_catalog_identity(invocation)
    assert missing_identity == (missing, str(tmp_path / missing), None, None)

    loop_a = tmp_path / "loop-a"
    loop_b = tmp_path / "loop-b"
    loop_a.symlink_to(loop_b)
    loop_b.symlink_to(loop_a)
    monkeypatch.setenv("ISAAC_CODEX_MODEL_CATALOG_PATH", str(loop_a))
    loop_identity = _isaac_model_catalog_identity(CodexInvocation("env"))
    assert loop_identity is not None
    assert loop_identity[0] == str(loop_a)
    assert loop_identity[1:] == (None, None, None)


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
            codex_invocation=CodexInvocation("env", ("isaac", "codex", "--")),
        )
        is not None
    )
    assert captured == ["env", "isaac", "codex", "--", "debug", "models"]


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
        invocation=CodexInvocation("env", ("isaac", "codex", "--")),
        listen_url="ws://127.0.0.1:1234",
        env={},
        cwd=Path("/tmp"),
    )
    await discovery.stderr_tail
    assert captured[:6] == ["env", "isaac", "codex", "--", "app-server", "--listen"]


@pytest.mark.parametrize("command", ["--version", "debug", "models"])
def test_invocation_builds_immutable_prefix(command: str) -> None:
    """The invocation value composes wrapper args before every Codex command."""
    invocation = CodexInvocation("env", ("isaac", "codex", "--"))
    if command == "--version":
        assert invocation.argv(command) == ("env", "isaac", "codex", "--", "--version")
    else:
        assert invocation.argv(command, "models") == (
            "env",
            "isaac",
            "codex",
            "--",
            command,
            "models",
        )
