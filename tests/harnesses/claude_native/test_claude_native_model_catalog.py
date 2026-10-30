"""Claude picker availability and discovery compatibility with older CLIs."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import psutil
import pytest

from omnigent.harnesses.claude_native import main as claude_native
from omnigent.inner import _proc
from omnigent.models import model_catalog_store
from tests._helpers.polling import poll_until_async
from tests._helpers.providers import isolate_provider_resolution


@pytest.mark.parametrize("helper", [None, "", "printf test-token"])
def test_model_probe_uses_configured_auth_helper(
    monkeypatch: pytest.MonkeyPatch, helper: str | None
) -> None:
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
        monkeypatch.setenv(name, "ambient-credential")
    monkeypatch.setattr(
        "omnigent.claude_launcher.resolve_claude_launch", lambda command, args: (command, args)
    )
    config = (
        claude_native.ClaudeNativeUcodeConfig(
            env={"CLAUDE_CODE_USE_GATEWAY": "1"}, api_key_helper=helper
        )
        if helper is not None
        else None
    )

    _, args, env = claude_native._claude_model_probe_invocation(config)

    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        if helper:
            assert name not in env
        else:
            assert env[name] == "ambient-credential"
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "ambient-credential"
    if helper:
        assert json.loads(args[args.index("--settings") + 1])["apiKeyHelper"] == helper


@pytest.mark.posix_only
@pytest.mark.parametrize("previous_bare_catalog", [False, True], ids=["empty-store", "bare-cache"])
async def test_catalog_runs_wrapper_bootstrap_before_discovery(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, previous_bare_catalog: bool
) -> None:
    """A bootstrap wrapper initializes managed settings before any discovery subprocess."""
    config_home = isolate_provider_resolution(monkeypatch, tmp_path / "config")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    for name in (
        "OMNIGENT_CLAUDE_LAUNCHER",
        "OMNIGENT_CLAUDE_PATH",
        "HARNESS_CLAUDE_PATH",
        "ANTHROPIC_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)
    managed_settings = tmp_path / "managed-settings.json"
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.CLAUDE_CODE_MANAGED_SETTINGS_PATHS", (managed_settings,)
    )
    launches = tmp_path / "discovery.jsonl"
    gateway_model = "system.ai.claude-opus-5-5[1m]"
    settings = {
        "env": {
            "ANTHROPIC_BASE_URL": "https://gateway.example/anthropic",
            "CLAUDE_CODE_USE_GATEWAY": "1",
            "ANTHROPIC_MODEL": gateway_model,
        }
    }
    fake_claude = tmp_path / "claude"
    fake_claude.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(f"""\
            import json, sys
            from pathlib import Path
            settings_path = Path({str(managed_settings)!r})
            settings = json.loads(settings_path.read_text()) if settings_path.exists() else {{}}
            model = settings.get("env", {{}}).get("ANTHROPIC_MODEL", "claude-opus-5-5")
            with open({str(launches)!r}, "a") as output:
                output.write(json.dumps({{"model": model, "args": sys.argv[1:]}}) + "\\n")
            if "--input-format" in sys.argv:
                sys.stdin.read()
                print(json.dumps({{
                    "type": "control_response",
                    "response": {{"subtype": "success", "request_id": "model-catalog",
                        "response": {{"models": [{{"value": "opus", "displayName": "Opus"}}]}}}},
                }}))
            print(json.dumps({{"type": "system", "subtype": "init", "model": model}}))
            """)
    )
    fake_claude.chmod(0o755)
    wrapper = tmp_path / "bootstrap-claude"
    wrapper.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(f"""\
            import os, sys
            from pathlib import Path
            assert sys.argv[1] == "--bootstrap"
            Path({str(managed_settings)!r}).write_text({json.dumps(settings)!r})
            os.execv({str(fake_claude)!r}, [{str(fake_claude)!r}, *sys.argv[2:]])
            """)
    )
    wrapper.chmod(0o755)
    if previous_bare_catalog:
        # Legacy cache entries omit the configured wrapper.
        legacy_fingerprint = model_catalog_store.fingerprint_of(
            "claude-native",
            "control-picker-v3",
            None,
            None,
            None,
            None,
            model_catalog_store.binary_identity("claude"),
            None,
            (),
        )
        model_catalog_store.write_catalog(
            "claude-native",
            legacy_fingerprint,
            [{"id": "opus", "model": "claude-opus-5-5", "isDefault": True}],
        )
    (config_home / "config.yaml").write_text(
        json.dumps(
            {
                "harness": {
                    "claude-native": {
                        "command": str(wrapper),
                        "args": ["--bootstrap"],
                    }
                }
            }
        )
    )
    fingerprint = claude_native.claude_catalog_fingerprint(None)
    assert not managed_settings.exists()

    rows = await claude_native.claude_launch_catalog(None)

    assert rows is not None
    assert [row["model"] for row in rows if row.get("isDefault")] == [gateway_model]
    assert model_catalog_store.read_catalog("claude-native", fingerprint) == rows
    observed = [json.loads(line) for line in launches.read_text().splitlines()]
    assert len(observed) == 2
    assert all(launch["model"] == gateway_model for launch in observed)
    assert "--input-format" in observed[0]["args"]
    assert "--model" in observed[1]["args"]

    # Bootstrap changes ambient settings, so the next read needs a new catalog key.
    assert claude_native.claude_catalog_fingerprint(None) != fingerprint
    assert claude_native.stored_claude_catalog_rows(None) is None
    assert await claude_native.claude_launch_catalog(None) == rows
    assert claude_native.stored_claude_catalog_rows(None) == rows
    refreshed_log = launches.read_text()
    refreshed = [json.loads(line) for line in refreshed_log.splitlines()]
    assert len(refreshed) == 4
    assert all(launch["model"] == gateway_model for launch in refreshed)
    assert await claude_native.claude_launch_catalog(None) == rows
    assert launches.read_text() == refreshed_log


@pytest.mark.posix_only
@pytest.mark.parametrize("probe", ["enumeration", "alias"])
@pytest.mark.parametrize("cancel", [False, True], ids=["timeout", "cancel"])
@pytest.mark.parametrize("wrapper_exits", [False, True], ids=["wrapper-waits", "wrapper-exits"])
async def test_model_probe_interrupt_terminates_wrapper_children(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    probe: str,
    cancel: bool,
    wrapper_exits: bool,
) -> None:
    """Timeout and cancellation stop wrapper children holding the probe's pipes."""
    child_pid_path = tmp_path / "child.pid"
    registered_path = tmp_path / "registered"
    remember_process_group = _proc.remember_process_group
    processes: list[asyncio.subprocess.Process] = []

    def register(process: asyncio.subprocess.Process) -> None:
        remember_process_group(process)
        processes.append(process)
        registered_path.touch()

    monkeypatch.setattr(_proc, "remember_process_group", register)
    wrapper = textwrap.dedent("""\
        import os, subprocess, sys, time
        from pathlib import Path
        while not Path(sys.argv[2]).exists():
            time.sleep(0.01)
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        Path(sys.argv[1]).write_text(f"{child.pid} {os.getpid()}")
        if sys.argv[3] == "wait":
            child.wait()
        """)
    invocation = (
        sys.executable,
        [
            "-c",
            wrapper,
            str(child_pid_path),
            str(registered_path),
            "exit" if wrapper_exits else "wait",
        ],
        dict(os.environ),
    )
    monkeypatch.setattr(
        claude_native, "_claude_model_probe_invocation", lambda *_args, **_kwargs: invocation
    )
    monkeypatch.setattr(claude_native, "_CLAUDE_MODEL_PROBE_TIMEOUT_S", 60.0 if cancel else 1.0)
    task = asyncio.create_task(
        claude_native._run_claude_model_probe(None, stream_input=False)
        if probe == "enumeration"
        else claude_native._resolve_claude_model_alias(None, "opus")
    )
    child = None
    try:
        await poll_until_async(child_pid_path.exists, timeout_s=3)
        child_pid, wrapper_pid = map(int, child_pid_path.read_text().split())
        with contextlib.suppress(psutil.NoSuchProcess):
            child = psutil.Process(child_pid)
        if wrapper_exits:
            await poll_until_async(lambda: not _proc.process_alive(wrapper_pid), timeout_s=1)
        if cancel:
            task.cancel()
        await asyncio.wait({task}, timeout=3)
        assert task.done(), "interrupted probe left a wrapper child holding its pipes open"
        if cancel:
            assert task.cancelled()
        else:
            assert task.result() == (None if probe == "enumeration" else {})
        assert processes[0].returncode is not None, "probe supervisor was left running"
        if child is not None:
            await poll_until_async(lambda: not _proc.process_alive(child.pid), timeout_s=1)
    finally:
        # Release inherited pipes if only the wrapper was terminated.
        if child is not None:
            with contextlib.suppress(psutil.NoSuchProcess):
                child.kill()
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait_for(task, timeout=2)


@pytest.mark.parametrize("credential", ["ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"])
def test_stored_catalog_does_not_validate_launch_credentials(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, credential: str
) -> None:
    """Reading a catalog must not run the terminal's auth-environment validation."""
    isolate_provider_resolution(monkeypatch, tmp_path / "config")
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path / "data"))
    config = claude_native.ClaudeNativeUcodeConfig(
        env={credential: "test-credential"}, api_key_helper="printf test-token"
    )
    rows = [{"id": "opus", "model": "gateway-opus", "isDefault": True}]
    fingerprint = claude_native.claude_catalog_fingerprint(config)
    model_catalog_store.write_catalog("claude-native", fingerprint, rows)

    assert claude_native.stored_claude_catalog_rows(config) == rows
    assert claude_native.stored_claude_picker_values(config) == ["opus"]
    assert not claude_native.claude_launch_catalog_is_stale(config)
    with pytest.raises(RuntimeError, match="apiKeyHelper is configured"):
        claude_native._claude_model_probe_invocation(config)


def _stub_picker(
    monkeypatch: pytest.MonkeyPatch,
    models: list[dict[str, Any]] | None,
    *,
    default: str = "claude-opus-5",
    control_failure: str | None = None,
    legacy_failure: bool = False,
    help_text: str = "Usage: /model <name>. Available: opus, fable, best, fable[1m], default, "
    "or a full model ID.",
) -> list[tuple[str, ...]]:
    launches: list[tuple[str, ...]] = []
    events = [
        {
            "type": "control_response",
            "response": {
                "subtype": "success",
                "request_id": "model-catalog",
                "response": {"models": models},
            },
        },
        {"type": "system", "subtype": "init", "model": default},
        {
            "type": "result",
            "result": f"Current model: `Opus 5`\n{help_text}",
        },
    ]

    class Process:
        pid = None
        returncode = 0

        def __init__(self, alias: str | None) -> None:
            self.alias = alias

        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            if input is None:
                if self.alias is not None:
                    model = "claude-fable-5-1" if "fable" in self.alias else "claude-opus-5"
                    return json.dumps(
                        {"type": "system", "subtype": "init", "model": model}
                    ).encode(), b""
                if legacy_failure:
                    self.returncode = 1
                    return b"", b"probe failed"
                return "\n".join(json.dumps(event) for event in events[1:]).encode(), b""
            if control_failure == "timeout":
                self.returncode = None
                raise TimeoutError
            if control_failure == "cancelled":
                self.returncode = None
                raise asyncio.CancelledError
            if control_failure == "exit":
                self.returncode = 1
                return b"", b"unsupported control request"
            if control_failure == "unsupported":
                return json.dumps(
                    {
                        "type": "control_response",
                        "response": {"subtype": "error", "request_id": "model-catalog"},
                    }
                ).encode(), b""
            requests = [json.loads(line) for line in input.splitlines()]
            assert requests[0] == {
                "type": "control_request",
                "request_id": "model-catalog",
                "request": {"subtype": "initialize"},
            }
            assert requests[1]["message"] == {"role": "user", "content": "/model"}
            if control_failure == "after_picker":
                self.returncode = 1
            return "\n".join(json.dumps(event) for event in events).encode(), b""

        def kill(self) -> None:
            self.returncode = -9

        async def wait(self) -> int:
            assert self.returncode == -9
            return self.returncode

    async def spawn(command: str, *args: str, **kwargs: Any) -> Process:
        launches.append((command, *args))
        alias = args[args.index("--model") + 1] if "--model" in args else None
        if "--input-format" in args:
            assert kwargs["stdin"] == asyncio.subprocess.PIPE
        return Process(alias)

    monkeypatch.setattr(
        claude_native,
        "asyncio",
        SimpleNamespace(**{**vars(asyncio), "create_subprocess_exec": spawn}),
    )
    return launches


@pytest.mark.parametrize("control_failure", [None, "after_picker"])
@pytest.mark.parametrize("disabled_row", [False, True], ids=["hidden", "disabled"])
async def test_catalog_excludes_unavailable_fable(
    monkeypatch: pytest.MonkeyPatch, disabled_row: bool, control_failure: str | None
) -> None:
    models: list[dict[str, Any]] = [
        {"value": "default", "resolvedModel": "claude-opus-5", "displayName": "Default"},
        {"value": "opus", "resolvedModel": "claude-opus-5", "displayName": "Opus 5"},
    ]
    if disabled_row:
        models.append(
            {
                "value": "fable",
                "resolvedModel": "claude-fable-5-1",
                "displayName": "Fable (disabled)",
                "disabled": True,
                "description": "Requires usage credits",
            }
        )
    launches = _stub_picker(monkeypatch, models, control_failure=control_failure)

    assert await claude_native.claude_model_catalog(None) == [
        {"id": "opus", "model": "claude-opus-5", "displayName": "Opus 5", "isDefault": True}
    ]
    assert len(launches) == 1


@pytest.mark.parametrize("default", ["fable", "claude-fable-5-1"])
async def test_catalog_does_not_restore_a_disabled_default(
    monkeypatch: pytest.MonkeyPatch, default: str
) -> None:
    _stub_picker(
        monkeypatch,
        [{"value": "fable", "resolvedModel": "claude-fable-5-1", "disabled": True}],
        default=default,
    )
    assert await claude_native.claude_model_catalog(None) == []


@pytest.mark.parametrize("configured", [False, True])
async def test_catalog_uses_cli_managed_picker_for_every_launch_config(
    monkeypatch: pytest.MonkeyPatch, configured: bool
) -> None:
    _stub_picker(
        monkeypatch,
        [{"value": "gateway-opus", "resolvedModel": "gateway-opus", "displayName": "Opus"}],
        default="gateway-opus",
    )
    # Managed-file rows alone cannot describe the CLI's availability decisions.
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_model_picker",
        lambda: (("fable", "Fable"), ("gateway-opus", "Opus")),
    )
    config = (
        claude_native.ClaudeNativeUcodeConfig(env={}, model="gateway-opus") if configured else None
    )
    assert await claude_native.claude_model_catalog(config) == [
        {"id": "gateway-opus", "model": "gateway-opus", "displayName": "Opus", "isDefault": True}
    ]


@pytest.mark.parametrize("failure", [None, "unsupported", "exit", "timeout"])
async def test_catalog_falls_back_for_older_claude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failure: str | None
) -> None:
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        json.dumps({"harness": {"claude-native": {"command": "claude-wrapper", "args": ["--"]}}})
    )
    launches = _stub_picker(
        monkeypatch,
        None,
        control_failure=failure,
        help_text="Available: `opus`, default, or a full model ID.",
    )
    monkeypatch.setattr(
        "omnigent.onboarding.ambient.claude_managed_model_picker",
        lambda: (("fable", "Fable"),),
    )
    assert await claude_native.claude_model_catalog(None) == [
        {"id": "opus", "model": "claude-opus-5", "displayName": "opus", "isDefault": True}
    ]
    assert len(launches) == 3
    assert "--input-format" in launches[0]
    prefix = _proc.pipe_supervised_command("claude-wrapper", "--", "-p", "/model")
    assert launches[1][: len(prefix)] == prefix
    assert "--input-format" not in launches[1]
    assert "--model" in launches[2]


@pytest.mark.parametrize("configured", [False, True])
@pytest.mark.parametrize(
    "models",
    [[], [{"value": "fable", "resolvedModel": "claude-fable-5-1", "disabled": True}]],
    ids=["empty", "disabled-only"],
)
async def test_empty_structured_catalog_never_falls_back(
    monkeypatch: pytest.MonkeyPatch, configured: bool, models: list[dict[str, Any]]
) -> None:
    launches = _stub_picker(monkeypatch, models)
    config = (
        claude_native.ClaudeNativeUcodeConfig(env={}, model="gateway-default")
        if configured
        else None
    )
    assert await claude_native.claude_model_catalog(config) == []
    assert len(launches) == 1


async def test_default_only_structured_picker_keeps_its_enabled_default(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(monkeypatch, [{"value": "default", "resolvedModel": "claude-opus-5"}])
    assert await claude_native.claude_model_catalog(None) == [
        {
            "id": "claude-opus-5",
            "model": "claude-opus-5",
            "displayName": "Opus 5",
            "isDefault": True,
        }
    ]


async def test_disabled_only_refresh_replaces_the_cached_catalog(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("OMNIGENT_DATA_DIR", str(tmp_path))
    monkeypatch.setattr(claude_native, "claude_catalog_fingerprint", lambda _: "test-picker")
    model = {"value": "opus", "resolvedModel": "claude-opus-5", "displayName": "Opus 5"}
    _stub_picker(monkeypatch, [model])
    assert await claude_native.claude_launch_catalog(None)

    launches = _stub_picker(monkeypatch, [{**model, "disabled": True}])
    assert await claude_native.claude_reprobed_launch_catalog(None) == []
    assert await claude_native.claude_launch_catalog(None) == []
    assert len(launches) == 1


async def test_legacy_catalog_keeps_observed_default_without_help(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(monkeypatch, None, help_text="")
    assert await claude_native.claude_model_catalog(None) == [
        {
            "id": "claude-opus-5",
            "model": "claude-opus-5",
            "displayName": "Opus 5",
            "isDefault": True,
        }
    ]


async def test_legacy_catalog_failure_preserves_probe_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(monkeypatch, None, legacy_failure=True)
    assert await claude_native.claude_model_catalog(None) is None


async def test_cancelled_catalog_probe_never_starts_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    launches = _stub_picker(monkeypatch, None, control_failure="cancelled")
    with pytest.raises(asyncio.CancelledError):
        await claude_native.claude_model_catalog(None)
    assert len(launches) == 1


async def test_catalog_keeps_enabled_fable_and_future_picker_models(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_picker(
        monkeypatch,
        [
            {"value": "fable", "resolvedModel": "claude-fable-5-1", "displayName": "Fable 5.1"},
            {"value": "future", "resolvedModel": "vendor-future", "displayName": "Future model"},
        ],
        default="claude-fable-5-1",
    )
    assert await claude_native.claude_model_catalog(None) == [
        {
            "id": "fable",
            "model": "claude-fable-5-1",
            "displayName": "Fable 5.1",
            "isDefault": True,
        },
        {"id": "future", "model": "vendor-future", "displayName": "Future model"},
    ]
