"""OMP lifecycle wiring, isolated state and Pi-compatible session protocol."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import yaml

from omnigent.entities.session_resources import SessionResourceView
from omnigent.harnesses.omp_native import bridge, credentials, main
from omnigent.harnesses.pi_native import bridge as pi_bridge
from omnigent.inner.executor import ExecutorConfig, TurnComplete
from omnigent.inner.omp_native_executor import OmpNativeExecutor
from omnigent.runner.native import orchestration
from omnigent.spec.types import AgentSpec, ExecutorSpec


def test_omp_wrapper_spec_is_first_class(tmp_path: Path) -> None:
    path = main._materialize_omp_agent_spec(tmp_path)
    spec = yaml.safe_load(path.read_text())
    assert path.name == "omp-native-ui.yaml"
    assert spec["name"] == "omp-native-ui"
    assert spec["executor"]["harness"] == "omp-native"
    assert "OMP" in spec["prompt"]
    assert "Pi" not in spec["prompt"]


def test_executable_uses_omp_override_only() -> None:
    seen: list[str] = []

    def which(command: str) -> str:
        seen.append(command)
        return "/bin/omp"

    assert (
        main.resolve_omp_executable(
            env={"OMNIGENT_OMP_PATH": "my-omp", "OMNIGENT_PI_PATH": "wrong"}, which=which
        )
        == "/bin/omp"
    )
    assert seen == ["my-omp"]


def test_bridge_resources_and_state_are_isolated(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(bridge, "bridge_root", lambda: tmp_path / "omp-native")
    directory = bridge.prepare_bridge_dir("conv_shared")
    assert directory != pi_bridge.bridge_dir_for_session_id("conv_shared")
    assert directory.parent.name == "omp-native"
    (directory / "input_ready").touch()
    extension, config = bridge.write_extension_files(
        directory,
        session_id="conv_shared",
        server_url="http://server/",
        conversation_url="http://server/c/conv_shared",
        auth_headers={"Authorization": "Bearer test"},
    )
    payload = json.loads(config.read_text())
    assert payload["harness"] == "omp-native"
    assert payload["displayName"] == "OMP"
    assert payload["serverUrl"] == "http://server"
    assert extension.name == "omnigent_omp_native_extension.mjs"
    assert extension.is_file()
    assert (directory / "omnigent_pi_native_extension.cjs").is_file()
    assert not (directory / "omnigent_pi_native_extension.js").exists()
    assert not (directory / "input_ready").exists()
    assert directory.stat().st_mode & 0o777 == 0o700
    env = bridge.build_omp_native_spawn_env("conv_shared")
    assert env == {
        "HARNESS_OMP_NATIVE_BRIDGE_DIR": str(directory),
        "HARNESS_OMP_NATIVE_REQUEST_SESSION_ID": "conv_shared",
    }
    assert bridge.refresh_config_auth_headers(directory, {"X-Route": "test"})
    assert json.loads(config.read_text())["authHeaders"]["Authorization"] == "Bearer test"


async def test_executor_uses_omp_environment_and_orders_model_effort_before_input(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("HARNESS_OMP_NATIVE_BRIDGE_DIR", str(tmp_path))
    monkeypatch.setenv("HARNESS_PI_NATIVE_BRIDGE_DIR", str(tmp_path / "wrong"))
    executor = OmpNativeExecutor()
    monkeypatch.setattr(executor, "_refresh_auth_headers", lambda: None)
    config = ExecutorConfig(model="openai/test-model", extra={"reasoning_effort": "high"})
    events = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello"}], [], "", config
        )
    ]
    assert len(events) == 1 and isinstance(events[0], TurnComplete)
    queued = [json.loads(path.read_text()) for path in sorted((tmp_path / "inbox").glob("*.json"))]
    assert [item["type"] for item in queued] == ["model_change", "user_message"]
    assert queued[0]["thinkingLevel"] == "high"
    assert queued[0]["model"] == "openai/test-model"
    assert queued[1]["content"] == "hello"
    assert not (tmp_path / "wrong").exists()


async def test_executor_effort_only_change_precedes_message(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    executor = OmpNativeExecutor(tmp_path)
    monkeypatch.setattr(executor, "_refresh_auth_headers", lambda: None)
    config = ExecutorConfig(extra={"reasoning_effort": "none"})
    _ = [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello"}], [], "", config
        )
    ]
    queued = [json.loads(path.read_text()) for path in sorted((tmp_path / "inbox").glob("*.json"))]
    assert queued[0]["type"] == "thinking_level_change"
    assert queued[0]["thinkingLevel"] == "off"


@pytest.mark.parametrize("managed", [False, True])
@pytest.mark.parametrize("prompt_mode", ["append", "replace"])
@pytest.mark.parametrize("fresh", [False, True])
async def test_runner_launches_omp_with_authored_context_and_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, managed: bool, prompt_mode: str, fresh: bool
) -> None:
    import omnigent.inference_config as inference
    import omnigent.runner._entry as entry

    monkeypatch.setattr(bridge, "bridge_root", lambda: tmp_path / "omp-native")
    monkeypatch.setattr(main, "resolve_omp_executable", lambda: "/bin/omp")
    monkeypatch.setattr(entry, "_make_auth_token_factory", lambda: None)
    monkeypatch.setattr(inference, "load_runtime_inference_config", dict)
    monkeypatch.setattr(inference, "binding_for_harness", lambda *_: None)
    own_root = tmp_path / "own-omp"
    own_root.mkdir()
    (own_root / "config.yml").write_text("own: untouched\n")
    monkeypatch.setattr(credentials, "global_omp_agent_dir", lambda **_: own_root)
    provider = (
        SimpleNamespace(
            credential_warning=None, model_notice=None, unroutable_model_warning=lambda: None
        )
        if managed
        else None
    )
    monkeypatch.setattr(credentials, "resolve_omp_native_provider", lambda **_: provider)
    monkeypatch.setattr(credentials, "omp_own_login_model_arg", lambda selection: selection)
    launches: list[dict[str, Any]] = []

    def launch(agent_dir: Path, provider: Any, effort: str, **kwargs: Any) -> Any:
        launches.append({"agent_dir": agent_dir, "effort": effort, **kwargs})
        return SimpleNamespace(
            env={"PI_CODING_AGENT_DIR": str(agent_dir)},
            args=["--provider", "omnigent", "--model", "test-model", "--thinking", "high"],
            effort_warning=None,
        )

    monkeypatch.setattr(credentials, "omp_native_provider_launch", launch)

    async def launch_config(**_: Any) -> orchestration._PiNativeLaunchConfig:
        return orchestration._PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://server",
            terminal_launch_args=None,
            external_session_id="019efdb8-54c8-7c02-be27-875eb2620635" if fresh else None,
            model_override="openai/test-model",
            reasoning_effort="high",
        )

    monkeypatch.setattr(orchestration, "_pi_native_launch_config", launch_config)
    captured: dict[str, Any] = {}

    class Registry:
        def client_surface(self, _: str) -> str:
            return "web"

        async def launch_required_terminal(self, **kwargs: Any) -> SessionResourceView:
            captured.update(kwargs)
            return SessionResourceView(
                id="terminal_omp_main",
                type="terminal",
                session_id=kwargs["session_id"],
                name="omp:main",
                metadata={"running": True},
            )

    spec = AgentSpec(
        spec_version=1,
        name="authored",
        instructions="Follow this authored role.",
        executor=ExecutorSpec(config={"context_files": False, "system_prompt_mode": prompt_mode}),
    )
    await orchestration._auto_create_omp_terminal(
        "conv_launch",
        Registry(),
        lambda *_: None,
        server_client=None,
        agent_spec=spec,
        fresh=fresh,
    )
    terminal = captured["spec"]
    assert captured["terminal_name"] == "omp"
    assert captured["resource_role"] == "omp-native"
    assert terminal.command == "/bin/omp"
    if fresh:
        config = json.loads(Path(terminal.env["OMNIGENT_OMP_NATIVE_CONFIG"]).read_text())
        assert config["previousExternalSessionId"] == "019efdb8-54c8-7c02-be27-875eb2620635"
        assert "--session" not in terminal.args
    assert "--approve" not in terminal.args
    assert "--no-context-files" in terminal.args
    assert terminal.env["OMP_SKIP_SETUP"] == "1"
    assert "OMNIGENT_PI_NATIVE_CONFIG" not in terminal.env
    assert terminal.env["OMNIGENT_NATIVE_CONFIG"] == terminal.env["OMNIGENT_OMP_NATIVE_CONFIG"]
    assert "OMNIGENT_PI_NATIVE_CONFIG" in terminal.env_unset
    assert terminal.args[terminal.args.index("--thinking") + 1] == "high"
    flag = "--system-prompt" if prompt_mode == "replace" else "--append-system-prompt"
    instructions = Path(terminal.args[terminal.args.index(flag) + 1])
    assert "Follow this authored role." in instructions.read_text()
    overlay = Path(terminal.args[terminal.args.index("--config") + 1])
    assert (
        yaml.safe_load(overlay.read_text())["extensionHandlers"]["toolCallTimeoutMs"] == 86410000
    )
    assert (own_root / "config.yml").read_text() == "own: untouched\n"
    if managed:
        assert len(launches) == 1
        assert launches[0]["agent_dir"].name == "omp-agent"
        assert launches[0]["selection"] == "openai/test-model"
        assert "omp_env" in launches[0]
    else:
        assert not launches
        assert "PI_CODING_AGENT_DIR" not in terminal.env
        assert "--provider" not in terminal.args
        assert terminal.args[terminal.args.index("--model") + 1] == "openai/test-model"


async def test_omp_resume_and_fork_reuse_shared_jsonl_without_touching_parent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(credentials, "resolve_omp_native_provider", lambda: None)
    external_id = "019efdb8-54c8-7c02-be27-875eb2620635"
    source = tmp_path / "source"
    source.mkdir()
    captured: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.method == "PATCH":
            captured.append(json.loads(request.content))
            return httpx.Response(200, json={})
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "u1",
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "carried history"}],
                    }
                ],
                "has_more": False,
            },
        )

    async with httpx.AsyncClient(
        base_url="http://server", transport=httpx.MockTransport(handler)
    ) as client:
        config = orchestration._PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://server",
            terminal_launch_args=None,
            external_session_id=external_id,
        )
        resumed = await orchestration._resolve_pi_resume_session(
            session_id="conv_source",
            launch_config=config,
            session_dir=source,
            workspace=tmp_path,
            server_client=client,
            harness="omp-native",
        )
        assert resumed == external_id
        (source_file,) = source.glob("*.jsonl")
        original = source_file.read_bytes()
        assert json.loads(original.splitlines()[0])["version"] == 3
        fork = tmp_path / "fork"
        config = orchestration._PiNativeLaunchConfig(
            workspace=tmp_path,
            server_url="http://server",
            terminal_launch_args=None,
            external_session_id=None,
            fork_carry_history=True,
        )
        fork_id = await orchestration._resolve_pi_resume_session(
            session_id="conv_fork",
            launch_config=config,
            session_dir=fork,
            workspace=tmp_path,
            server_client=client,
            harness="omp-native",
        )
        assert fork_id and fork_id != external_id
        assert source_file.read_bytes() == original
        assert len(list(fork.glob("*.jsonl"))) == 1
        assert captured[-1]["external_session_id"] == fork_id


def test_omp_controls_share_ordered_inbox(tmp_path: Path) -> None:
    bridge.enqueue_user_message(tmp_path, "first")
    bridge.enqueue_interrupt(tmp_path)
    bridge.enqueue_compact(tmp_path, "retain the task")
    bridge.enqueue_thinking_level_change(tmp_path, "max")
    queued = [json.loads(path.read_text()) for path in sorted((tmp_path / "inbox").glob("*.json"))]
    assert [item["type"] for item in queued] == [
        "user_message",
        "interrupt",
        "compact",
        "thinking_level_change",
    ]
    assert queued[2]["custom_instructions"] == "retain the task"
    assert queued[3]["thinkingLevel"] == "max"
    bridge.clear_inbox(tmp_path)
    assert not list((tmp_path / "inbox").iterdir())


def test_omp_append_discovers_omp_context_without_pi_approve(tmp_path: Path) -> None:
    project = tmp_path / ".omp"
    project.mkdir()
    appendix = project / "APPEND_SYSTEM.md"
    appendix.write_text("OMP project guidance")
    instructions = tmp_path / "authored.md"
    instructions.write_text("Author instructions")
    args = orchestration._build_pi_native_args(
        terminal_launch_args=None,
        extension_path=tmp_path / "bridge.mjs",
        session_dir=tmp_path / "sessions",
        external_session_id=None,
        approve=True,
        instructions_path=instructions,
        context_files=True,
        system_prompt_mode="append",
        workspace=tmp_path,
        agent_dir=tmp_path / "own",
        harness="omp-native",
    )
    assert "--approve" not in args
    assert args.count("--append-system-prompt") == 2
    assert args[args.index("--append-system-prompt") + 1] == str(appendix)
    assert str(instructions) in args


@pytest.mark.parametrize("previous_id", [None, "019efdb8-54c8-7c02-be27-875eb2620635"])
def test_fresh_rotation_carries_prior_uuid_including_uncaptured_state(
    tmp_path: Path,
    previous_id: str | None,
) -> None:
    _, config = bridge.write_extension_files(
        tmp_path,
        session_id="conv_clear",
        server_url="http://server",
        conversation_url="http://server/c/conv_clear",
        previous_external_session_id=previous_id,
        fresh=True,
    )
    payload = json.loads(config.read_text())
    assert "previousExternalSessionId" in payload
    assert payload["previousExternalSessionId"] == previous_id
    _, config = bridge.write_extension_files(
        tmp_path,
        session_id="conv_clear",
        server_url="http://server",
        conversation_url="http://server/c/conv_clear",
    )
    assert "previousExternalSessionId" not in json.loads(config.read_text())
