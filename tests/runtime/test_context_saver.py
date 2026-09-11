"""Tests for Context Saver configuration, classification, and Focused Read."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from omnigent.llms.types import MessageOutput, OutputText, Response, Usage
from omnigent.runtime.context_saver import (
    ContextFile,
    ContextReadRequest,
    ContextSaverAction,
    FocusedReadSettings,
    FocusedReadWorkerResult,
    classify_file_read,
    classify_native_tool_call,
    context_saver_has_filesystem_access,
    harness_supports_context_saver,
    load_context_saver_settings,
    native_redirect_hook_output,
    parse_context_saver_settings,
    shell_read_candidates,
    validate_focused_read_worker_model,
)
from omnigent.runtime.focused_read import (
    ConfiguredFocusedReadWorker,
    FocusedReadTechnique,
    resolve_configured_focused_read_connection,
)
from omnigent.tools.manager import ToolManager


def _enabled_settings(min_lines: int = 5):
    return parse_context_saver_settings(
        {
            "enabled": True,
            "techniques": {"focused_read": {"min_lines": min_lines}},
        }
    )


def test_context_saver_defaults_disabled() -> None:
    settings = parse_context_saver_settings(None)

    assert settings.enabled is False
    assert settings.focused_read.enabled is True
    assert settings.focused_read.min_lines == 350


@pytest.mark.parametrize("value", [True, 0, -1, "350"])
def test_context_saver_rejects_invalid_min_lines(value: object) -> None:
    with pytest.raises(ValueError, match="min_lines"):
        parse_context_saver_settings({"techniques": {"focused_read": {"min_lines": value}}})


def test_project_context_saver_settings_override_user_fields(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "context_saver:\n"
        "  enabled: true\n"
        "  techniques:\n"
        "    focused_read:\n"
        "      min_lines: 500\n"
        "      worker_model: databricks/custom-worker\n"
    )
    workspace = tmp_path / "workspace"
    (workspace / ".omnigent").mkdir(parents=True)
    (workspace / ".omnigent" / "config.yaml").write_text(
        "context_saver:\n  techniques:\n    focused_read:\n      min_lines: 200\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    settings = load_context_saver_settings(workspace)

    assert settings.enabled is True
    assert settings.focused_read.min_lines == 200
    assert settings.focused_read.worker_model == "databricks/custom-worker"


@pytest.mark.parametrize(
    "worker_model",
    ["gpt-4o-mini", "openai/gpt-4o-mini", "anthropic/claude-haiku"],
)
def test_external_worker_requires_explicit_source_upload_consent(worker_model: str) -> None:
    with pytest.raises(ValueError, match=r"provider prefix|allow_source_upload"):
        parse_context_saver_settings(
            {
                "enabled": True,
                "techniques": {"focused_read": {"worker_model": worker_model}},
            }
        )


def test_user_can_configure_external_worker_with_source_upload_consent() -> None:
    settings = parse_context_saver_settings(
        {
            "enabled": True,
            "techniques": {
                "focused_read": {
                    "worker_model": "openai/gpt-4o-mini",
                    "worker_provider": "openai",
                    "allow_source_upload": True,
                }
            },
        }
    )

    assert settings.focused_read.worker_model == "openai/gpt-4o-mini"
    assert settings.focused_read.worker_provider == "openai"
    assert settings.focused_read.allow_source_upload is True


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("worker_model", "anthropic/claude-haiku"),
        ("worker_provider", "vendor-worker"),
        ("allow_source_upload", "false"),
    ],
)
def test_project_config_cannot_change_worker_destination(
    field: str,
    value: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "context_saver:\n"
        "  enabled: true\n"
        "  techniques:\n"
        "    focused_read:\n"
        "      worker_model: openai/gpt-4o-mini\n"
        "      allow_source_upload: true\n"
    )
    workspace = tmp_path / "workspace"
    (workspace / ".omnigent").mkdir(parents=True)
    (workspace / ".omnigent" / "config.yaml").write_text(
        f"context_saver:\n  techniques:\n    focused_read:\n      {field}: {value}\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    with pytest.raises(ValueError, match="project Context Saver configuration"):
        load_context_saver_settings(workspace)


@pytest.mark.parametrize(
    "local_context_saver",
    [
        "  techniques: null\n",
        "  techniques:\n    focused_read: null\n",
    ],
)
def test_project_config_cannot_reset_worker_destination_with_null(
    local_context_saver: str,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "context_saver:\n"
        "  enabled: true\n"
        "  techniques:\n"
        "    focused_read:\n"
        "      worker_model: openai/gpt-4o-mini\n"
        "      allow_source_upload: true\n"
    )
    workspace = tmp_path / "workspace"
    (workspace / ".omnigent").mkdir(parents=True)
    (workspace / ".omnigent" / "config.yaml").write_text("context_saver:\n" + local_context_saver)
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))

    with pytest.raises(ValueError, match=r"project context_saver\.techniques"):
        load_context_saver_settings(workspace)


@pytest.mark.parametrize(
    ("provider_name", "worker_provider"),
    [("cheap-worker", "cheap-worker"), ("openai", None)],
)
def test_external_worker_resolves_user_provider_connection(
    provider_name: str,
    worker_provider: str | None,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config_home = tmp_path / "config-home"
    config_home.mkdir()
    (config_home / "config.yaml").write_text(
        "providers:\n"
        f"  {provider_name}:\n"
        "    kind: key\n"
        "    openai:\n"
        "      base_url: https://worker.example.com/v1\n"
        "      api_key_ref: env:CONTEXT_SAVER_WORKER_KEY\n"
    )
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(config_home))
    monkeypatch.setenv("CONTEXT_SAVER_WORKER_KEY", "worker-secret")

    connection = resolve_configured_focused_read_connection(
        "openai/gpt-4o-mini",
        worker_provider=worker_provider,
    )

    assert connection == {
        "api_key": "worker-secret",
        "base_url": "https://worker.example.com/v1",
    }


def test_databricks_worker_uses_session_profile() -> None:
    assert resolve_configured_focused_read_connection(
        "databricks/context-saver-cheap",
        databricks_profile="team-profile",
    ) == {"profile": "team-profile"}


def test_worker_model_validation_accepts_supported_external_provider_with_consent() -> None:
    assert (
        validate_focused_read_worker_model(
            "ollama/qwen3",
            allow_source_upload=True,
        )
        == "ollama/qwen3"
    )


@pytest.mark.asyncio
async def test_configured_worker_rejects_external_route_before_client_call() -> None:
    calls: list[dict[str, object]] = []

    class _Responses:
        async def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            raise AssertionError("client must not be called")

    worker = ConfiguredFocusedReadWorker(
        SimpleNamespace(responses=_Responses())  # type: ignore[arg-type]
    )

    with pytest.raises(ValueError, match="allow_source_upload"):
        await worker.focus(
            files=[ContextFile("source.py", "secret\n", 1, 7)],
            question="What is here?",
            model="openai/gpt-4o-mini",
            allow_source_upload=False,
            timeout_seconds=30,
            max_excerpt_lines=10,
            output_budget=256,
        )

    assert calls == []


@pytest.mark.asyncio
async def test_configured_worker_passes_resolved_connection_to_client() -> None:
    calls: list[dict[str, object]] = []

    class _Responses:
        async def create(self, **kwargs: object) -> object:
            calls.append(kwargs)
            raise RuntimeError("stop after capturing request")

    worker = ConfiguredFocusedReadWorker(
        SimpleNamespace(responses=_Responses()),  # type: ignore[arg-type]
        connection_params={
            "api_key": "worker-secret",
            "base_url": "https://worker.example.com/v1",
        },
    )

    with pytest.raises(RuntimeError, match="stop after capturing request"):
        await worker.focus(
            files=[ContextFile("source.py", "secret\n", 1, 7)],
            question="What is here?",
            model="openai/gpt-4o-mini",
            allow_source_upload=True,
            timeout_seconds=30,
            max_excerpt_lines=10,
            output_budget=256,
        )

    assert calls[0]["connection_params"] == {
        "api_key": "worker-secret",
        "base_url": "https://worker.example.com/v1",
    }


@pytest.mark.asyncio
async def test_configured_worker_returns_the_model_reported_by_the_provider() -> None:
    class _Responses:
        async def create(self, **kwargs: object) -> Response:
            del kwargs
            return Response(
                output=[MessageOutput(content=[OutputText(text="worker result")])],
                model="databricks-glm-5-2",
                usage=Usage(input_tokens=12, output_tokens=4),
            )

    worker = ConfiguredFocusedReadWorker(
        SimpleNamespace(responses=_Responses())  # type: ignore[arg-type]
    )

    result = await worker.focus(
        files=[ContextFile("source.py", "value = 1\n", 1, 10)],
        question="What is here?",
        model="databricks/context-saver-cheap",
        allow_source_upload=False,
        timeout_seconds=30,
        max_excerpt_lines=10,
        output_budget=256,
    )

    assert result.content == "worker result"
    assert result.reported_model == "databricks-glm-5-2"


def test_tool_manager_accepts_runner_context_saver_override() -> None:
    manager = ToolManager.__new__(ToolManager)
    manager._tools = {}
    manager._context_saver_enabled = True
    manager._os_env = object()
    manager._spec = SimpleNamespace(
        executor=SimpleNamespace(harness_kind="codex"),
    )

    manager._register_context_saver_tools()

    assert "sys_context_read" in manager._tools


def test_tool_manager_requires_filesystem_capability_for_context_saver() -> None:
    manager = ToolManager.__new__(ToolManager)
    manager._tools = {}
    manager._context_saver_enabled = True
    manager._os_env = None
    manager._spec = SimpleNamespace(
        executor=SimpleNamespace(harness_kind="codex"),
    )

    manager._register_context_saver_tools()

    assert "sys_context_read" not in manager._tools


def test_tool_manager_runtime_availability_overrides_workspace_enablement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from omnigent.runtime import _globals
    from omnigent.runtime.caps import RuntimeCaps

    monkeypatch.setattr(
        _globals,
        "_caps",
        RuntimeCaps(context_saver_available=False),
    )
    manager = ToolManager.__new__(ToolManager)
    manager._tools = {}
    manager._context_saver_enabled = True
    manager._os_env = object()
    manager._spec = SimpleNamespace(
        executor=SimpleNamespace(harness_kind="codex"),
    )

    manager._register_context_saver_tools()

    assert "sys_context_read" not in manager._tools


def test_context_saver_only_claims_supported_native_harnesses() -> None:
    assert harness_supports_context_saver("codex-native") is True
    assert harness_supports_context_saver("claude-native") is True
    assert harness_supports_context_saver("antigravity-native") is False


@pytest.mark.parametrize("harness", ["cursor", "copilot", "github-copilot"])
def test_context_saver_excludes_unsupported_sdk_harnesses(harness: str) -> None:
    assert harness_supports_context_saver(harness) is False
    assert (
        context_saver_has_filesystem_access(
            harness=harness,
            os_env_available=True,
        )
        is False
    )


def test_context_saver_requires_os_env_for_non_native_harnesses() -> None:
    assert (
        context_saver_has_filesystem_access(
            harness="codex",
            os_env_available=False,
        )
        is False
    )
    assert (
        context_saver_has_filesystem_access(
            harness="codex",
            os_env_available=True,
        )
        is True
    )
    assert (
        context_saver_has_filesystem_access(
            harness="codex-native",
            os_env_available=False,
        )
        is True
    )


def test_large_broad_read_redirects_but_targeted_read_passes() -> None:
    settings = _enabled_settings()

    broad = classify_file_read(
        path="large.py", offset=None, limit=None, total_lines=20, settings=settings
    )
    targeted = classify_file_read(
        path="large.py", offset=8, limit=3, total_lines=20, settings=settings
    )

    assert broad.action is ContextSaverAction.REDIRECT
    assert targeted.action is ContextSaverAction.ALLOW
    assert targeted.reason == "explicit_bounded_range"


def test_shell_classifier_finds_broad_pipeline_and_skips_bounded_reads() -> None:
    assert shell_read_candidates("cat large.py | grep token").paths == ("large.py",)
    assert shell_read_candidates("head -n 80 large.py").paths == ()
    assert shell_read_candidates("head -n80 large.py").paths == ()
    assert shell_read_candidates("head -n 800 large.py").paths == ("large.py",)
    assert shell_read_candidates("head -800 large.py").paths == ("large.py",)
    assert shell_read_candidates("sed -n '120,190p' large.py").paths == ()


@pytest.mark.parametrize(
    "command",
    [
        "head -n -5 large.py",
        "head --lines=-5 large.py",
        "head -c 1000000 large.py",
        "tail -n +5 large.py",
        "tail -c +1 large.py",
    ],
)
def test_shell_classifier_detects_broad_head_and_tail_forms(command: str) -> None:
    assert shell_read_candidates(command).paths == ("large.py",)


def test_shell_classifier_keeps_negative_tail_count_bounded() -> None:
    assert shell_read_candidates("tail -n -5 large.py").paths == ()


def test_shell_classifier_tracks_literal_directory_changes() -> None:
    candidates = shell_read_candidates("cd sub && cd nested && cat large.py")

    assert candidates.paths == ("sub/nested/large.py",)


@pytest.mark.parametrize(
    "command",
    [
        "cat small.py > /tmp/copy.py",
        "cat small.py >/tmp/copy.py",
        "cat small.py 2> /tmp/error.log",
        "cat small.py 2>/tmp/error.log",
        "> /tmp/copy.py cat small.py",
    ],
)
def test_shell_classifier_ignores_output_redirection_targets(command: str) -> None:
    assert shell_read_candidates(command).paths == ("small.py",)


@pytest.mark.parametrize(
    "command",
    [
        "cat < ../outside.py",
        "cat <../outside.py",
        "cat 0<../outside.py",
        "<../outside.py cat",
    ],
)
def test_shell_classifier_keeps_input_redirection_sources(command: str) -> None:
    assert shell_read_candidates(command).paths == ("../outside.py",)


@pytest.mark.parametrize(
    "command",
    [
        "head -z large.py",
        "tail -z large.py",
        "sed -z -n '1p' large.py",
        "sed -nz '1p' large.py",
        "sed --null-data -n '1p' large.py",
    ],
)
def test_shell_classifier_treats_zero_delimited_reads_as_broad(command: str) -> None:
    assert shell_read_candidates(command).paths == ("large.py",)


def test_native_read_redirects_before_execution(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("\n".join(f"line {i}" for i in range(10)))

    decision = classify_native_tool_call(
        "Read",
        {"file_path": "large.py"},
        workspace=tmp_path,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.REDIRECT
    assert decision.paths == ("large.py",)


def test_native_unbounded_read_outside_workspace_redirects(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n")

    decision = classify_native_tool_call(
        "Read",
        {"file_path": str(outside)},
        workspace=workspace,
        settings=_enabled_settings(),
    )
    output = native_redirect_hook_output(decision)

    assert decision.action is ContextSaverAction.REDIRECT
    assert decision.reason == "outside_workspace_broad_read"
    assert decision.paths == (str(outside),)
    reason = output["hookSpecificOutput"]["permissionDecisionReason"]
    assert isinstance(reason, str)
    assert "explicit line range" in reason
    assert "sys_context_read" not in reason


@pytest.mark.parametrize(
    ("tool_name", "tool_input"),
    [
        ("Read", {"file_path": "../outside.py", "limit": 5}),
        ("commandExecution", {"command": "head -n 5 ../outside.py"}),
        ("commandExecution", {"command": "tail -n 5 ../outside.py"}),
        ("commandExecution", {"command": "sed -n '1,5p' ../outside.py"}),
    ],
)
def test_native_bounded_reads_outside_workspace_are_allowed(
    tool_name: str,
    tool_input: dict[str, object],
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.py").write_text("value = 1\n" * 10)

    decision = classify_native_tool_call(
        tool_name,
        tool_input,
        workspace=workspace,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.ALLOW


@pytest.mark.parametrize(
    "tool_input",
    [
        {"cmd": "cat ../outside.py"},
        {"cmd": "cat <../outside.py"},
        {"cmd": "cat outside.py", "workdir": ".."},
        {"cmd": "cd .. && cat outside.py"},
        {"cmd": "head -z ../outside.py"},
        {"cmd": "tail -z ../outside.py"},
        {"cmd": "sed -z -n '1p' ../outside.py"},
    ],
)
def test_native_shell_read_outside_workspace_redirects(
    tool_input: dict[str, str],
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (tmp_path / "outside.py").write_text("value = 1\n")

    decision = classify_native_tool_call(
        "exec_command",
        tool_input,
        workspace=workspace,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.REDIRECT
    assert decision.reason == "outside_workspace_broad_read"


def test_native_shell_output_redirection_is_not_treated_as_read(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "small.py").write_text("value = 1\n")

    decision = classify_native_tool_call(
        "exec_command",
        {"cmd": "cat small.py > ../copy.py"},
        workspace=workspace,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.ALLOW


def test_native_read_through_symlink_outside_workspace_redirects(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("value = 1\n")
    (workspace / "linked.py").symlink_to(outside)

    decision = classify_native_tool_call(
        "Read",
        {"file_path": "linked.py"},
        workspace=workspace,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.REDIRECT
    assert decision.reason == "outside_workspace_broad_read"


@pytest.mark.parametrize("path", ["missing.py", "binary.dat"])
def test_native_uninspectable_workspace_read_remains_unknown(
    path: str,
    tmp_path: Path,
) -> None:
    if path == "binary.dat":
        (tmp_path / path).write_bytes(b"binary\x00content")

    decision = classify_native_tool_call(
        "Read",
        {"file_path": path},
        workspace=tmp_path,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.UNKNOWN
    assert decision.reason == "unreadable_or_binary"


def test_native_symlink_loop_remains_unknown(tmp_path: Path) -> None:
    (tmp_path / "loop.py").symlink_to("loop.py")

    decision = classify_native_tool_call(
        "Read",
        {"file_path": "loop.py"},
        workspace=tmp_path,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.UNKNOWN
    assert decision.reason == "unreadable_or_binary"


def test_native_expanduser_failure_remains_unknown(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    def fail_expanduser(_path: Path) -> Path:
        raise RuntimeError("unknown home")

    monkeypatch.setattr(Path, "expanduser", fail_expanduser)

    decision = classify_native_tool_call(
        "Read",
        {"file_path": "~unknown/source.py"},
        workspace=tmp_path,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.UNKNOWN
    assert decision.reason == "unreadable_or_binary"


def test_native_exec_command_resolves_file_from_workdir(tmp_path: Path) -> None:
    workdir = tmp_path / "sub"
    workdir.mkdir()
    (workdir / "large.py").write_text("\n".join(f"line {i}" for i in range(10)))

    decision = classify_native_tool_call(
        "exec_command",
        {"cmd": "cat large.py", "workdir": "sub"},
        workspace=tmp_path,
        settings=_enabled_settings(),
    )

    assert decision.action is ContextSaverAction.REDIRECT
    assert decision.paths == ("sub/large.py",)


def test_native_exec_command_redirect_uses_pre_tool_denial_contract(tmp_path: Path) -> None:
    (tmp_path / "large.py").write_text("\n".join(f"line {i}" for i in range(10)))

    decision = classify_native_tool_call(
        "exec_command",
        {"cmd": "cat large.py"},
        workspace=tmp_path,
        settings=_enabled_settings(),
    )
    output = native_redirect_hook_output(decision)

    assert decision.action is ContextSaverAction.REDIRECT
    assert output["hookSpecificOutput"]["hookEventName"] == "PreToolUse"
    assert output["hookSpecificOutput"]["permissionDecision"] == "deny"


class _Reader:
    async def read(self, path: str) -> ContextFile:
        content = "alpha\nbeta\ngamma\n"
        return ContextFile(path=path, content=content, total_lines=3, total_bytes=len(content))


class _Worker:
    async def focus(self, **kwargs: object) -> FocusedReadWorkerResult:
        return FocusedReadWorkerResult(
            content=(
                '{"answer":"Beta is the relevant value.","sources":['
                '{"path":"source.py","ranges":['
                '{"start":2,"end":2,"excerpt":"beta"}]}]}'
            ),
            reported_model="  databricks-glm-5-2  ",
        )


@pytest.mark.asyncio
async def test_focused_read_validates_and_renders_worker_ranges() -> None:
    technique = FocusedReadTechnique(FocusedReadSettings(max_excerpt_lines=2), _Worker())

    result = await technique.render(
        ContextReadRequest(
            paths=("source.py",),
            question="Which value matters?",
            reader=_Reader(),
            requested_output_budget=256,
        )
    )

    assert result.failure is None
    assert "source.py:2-2" in result.content
    assert result.relevant_line_ranges == {"source.py": ((2, 2),)}
    assert result.worker_model_reported == "databricks-glm-5-2"


class _HallucinatingWorker:
    async def focus(self, **kwargs: object) -> FocusedReadWorkerResult:
        return FocusedReadWorkerResult(
            content=(
                '{"answer":"Wrong.","sources":['
                '{"path":"source.py","ranges":['
                '{"start":2,"end":2,"excerpt":"delta"}]}]}'
            ),
            input_tokens=12,
            output_tokens=4,
            reported_model="  databricks-glm-5-2  ",
        )


@pytest.mark.asyncio
async def test_focused_read_fails_closed_on_hallucinated_excerpt() -> None:
    technique = FocusedReadTechnique(FocusedReadSettings(), _HallucinatingWorker())

    result = await technique.render(
        ContextReadRequest(
            paths=("source.py",),
            question="Which value matters?",
            reader=_Reader(),
            requested_output_budget=256,
        )
    )

    assert result.failure == "worker_failed:ValueError"
    assert "explicit line ranges" in result.content
    assert result.worker_input_tokens == 12
    assert result.worker_output_tokens == 4
    assert result.worker_model_reported == "databricks-glm-5-2"


class _MalformedWorker:
    async def focus(self, **kwargs: object) -> FocusedReadWorkerResult:
        return object()  # type: ignore[return-value]


@pytest.mark.asyncio
async def test_focused_read_fails_closed_on_malformed_worker_result() -> None:
    technique = FocusedReadTechnique(FocusedReadSettings(), _MalformedWorker())

    result = await technique.render(
        ContextReadRequest(
            paths=("source.py",),
            question="Which value matters?",
            reader=_Reader(),
            requested_output_budget=256,
        )
    )

    assert result.failure == "worker_failed:AttributeError"
    assert "explicit line ranges" in result.content
