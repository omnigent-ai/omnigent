"""Native startup carries author text and stable guidance, without turn metadata."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from omnigent.runner.app import (
    ResolvedSpec,
    _native_startup_instructions_from_spec,
)
from omnigent.runner.native.orchestration import _opencode_session_instructions_from_spec
from omnigent.runtime.prompt import WORKSPACE_IMAGE_INSTRUCTION
from omnigent.spec.types import AgentSpec, ExecutorSpec


def _spec(instructions: str | None) -> AgentSpec:
    """Build a minimal agent spec carrying *instructions*."""
    return AgentSpec(
        spec_version=1,
        name="claude_code",
        instructions=instructions,
        executor=ExecutorSpec(),
    )


@pytest.mark.parametrize(
    ("instructions", "expected"),
    [
        ("Be a concise assistant.", f"Be a concise assistant.\n\n{WORKSPACE_IMAGE_INSTRUCTION}"),
        (None, WORKSPACE_IMAGE_INSTRUCTION),
        ("   \n  ", WORKSPACE_IMAGE_INSTRUCTION),
    ],
    ids=["present", "absent", "whitespace-only"],
)
def test_native_startup_instructions_from_spec(instructions: str | None, expected: str) -> None:
    """Author text is preserved and every startup receives image guidance."""
    assert _native_startup_instructions_from_spec(_spec(instructions)) == expected


def test_native_startup_instructions_from_spec_none_spec() -> None:
    """A missing spec still receives stable image guidance."""
    assert _native_startup_instructions_from_spec(None) == WORKSPACE_IMAGE_INSTRUCTION


@pytest.mark.parametrize("has_spec", [False, True])
def test_opencode_startup_carries_image_guidance(has_spec: bool) -> None:
    """OpenCode's session snapshot carries guidance even without authored text."""
    result = _opencode_session_instructions_from_spec(_spec(None) if has_spec else None)
    assert result is not None
    assert result.count(WORKSPACE_IMAGE_INSTRUCTION) == 1


def test_native_startup_instructions_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same instructions text."""
    wrapped = ResolvedSpec(spec=_spec("Be a concise assistant."), workdir=Path("/tmp"))
    assert _native_startup_instructions_from_spec(wrapped) == (
        f"Be a concise assistant.\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
    )


def test_native_startup_instructions_from_spec_never_returns_stripped_form() -> None:
    """The original resolved text is preserved verbatim, not the stripped form."""
    padded = "  Keep leading/trailing whitespace exactly.  "
    assert _native_startup_instructions_from_spec(_spec(padded)) == (
        f"{padded}\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
    )


# ─── Composed startup instructions (stable text, notes, memory guidance) ─


@pytest.fixture
def memory_guidance(monkeypatch: pytest.MonkeyPatch) -> list[tuple[object, ...]]:
    """Stub the memory index fetch and record each call."""
    calls: list[tuple[object, ...]] = []

    async def _guidance(*args: object, workspace: object = None) -> str:
        calls.append((*args, workspace))
        return "MEMORY GUIDANCE"

    monkeypatch.setattr("omnigent.runner.native.orchestration.memory_startup_guidance", _guidance)
    return calls


@pytest.mark.asyncio
async def test_startup_instructions_join_stable_text_notes_and_memory_in_order(
    memory_guidance: list[tuple[object, ...]],
) -> None:
    from omnigent.runner.native.orchestration import _native_startup_instructions

    spec = _spec("Be a concise assistant.")
    client = object()

    text = await _native_startup_instructions(
        spec, client, "sess", "ROUTING NOTE", None, workspace="/work/omnigent"
    )

    assert text == (
        f"Be a concise assistant.\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
        "\n\nROUTING NOTE\n\nMEMORY GUIDANCE"
    )
    # The workspace reaches the guidance, which names the session's project from it.
    assert memory_guidance == [(client, "sess", spec, "/work/omnigent")]


@pytest.mark.parametrize(
    ("external_id", "write_failure"),
    [
        (None, None),
        ("existing-cascade", None),
        (None, "permission"),
        (None, "containment"),
    ],
    ids=["fresh", "resume", "rule-permission", "rule-containment"],
)
async def test_antigravity_startup_rule_is_ready_before_terminal_launch(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    memory_guidance: list[tuple[object, ...]],
    external_id: str | None,
    write_failure: str | None,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The real launch composer delivers authored text, image help and memory."""
    from omnigent.harnesses.antigravity_native import bridge
    from omnigent.runner.native import orchestration

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
    monkeypatch.setattr(bridge, "_BRIDGE_ROOT", tmp_path / "bridges")
    monkeypatch.setenv("RUNNER_SERVER_URL", "http://server.invalid")
    monkeypatch.setattr("omnigent.runner._entry._make_auth_token_factory", lambda: None)
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.launch.build_agy_launch",
        lambda **_kwargs: (["agy"], {}),
    )
    monkeypatch.setattr(
        "omnigent.harnesses.antigravity_native.gateway.wrap_agy_gateway_launch",
        lambda argv, _env: argv,
    )

    async def cancel_reader(_session_id: str) -> None:
        pass

    monkeypatch.setattr(orchestration, "_cancel_auto_forwarder_task", cancel_reader)
    labels = {bridge.ANTIGRAVITY_NATIVE_BRIDGE_ID_LABEL_KEY: "startup-bridge"}
    previous_rule = None
    if write_failure is not None:
        bridge_dir = bridge.bridge_dir_for_bridge_id("startup-bridge")
        bridge.write_agent_instructions(bridge_dir, "Keep previous startup guidance.")
        previous_rule = (
            bridge.agy_gemini_dir(bridge_dir) / "config/rules/omnigent_instructions.md"
        ).read_text(encoding="utf-8")
        error = (
            PermissionError("read-only rules")
            if write_failure == "permission"
            else ValueError("rules outside isolated directory")
        )

        def fail_write(*_args: object) -> None:
            raise error

        monkeypatch.setattr(bridge, "write_agent_instructions", fail_write)
    relays: list[dict[str, object]] = []

    async def relay(_session_id: str, **kwargs: object) -> None:
        relays.append(kwargs)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/sess"
        return httpx.Response(
            200,
            json={
                "workspace": str(workspace),
                "labels": labels,
                "external_session_id": external_id,
            },
        )

    class LaunchObserved(Exception):
        """Stop at the inert terminal boundary before any process can start."""

    captured: list[str] = []

    class Registry:
        async def launch_required_terminal(self, **_kwargs: object) -> None:
            rule = (
                bridge.agy_gemini_dir(bridge.bridge_dir_for_bridge_id("startup-bridge"))
                / "config"
                / "rules"
                / "omnigent_instructions.md"
            )
            captured.append(rule.read_text(encoding="utf-8"))
            raise LaunchObserved

    spec = _spec("Keep the session's authored instructions.")
    async with httpx.AsyncClient(
        base_url="http://server.invalid", transport=httpx.MockTransport(handler)
    ) as client:
        with pytest.raises(LaunchObserved):
            await orchestration._launch_antigravity(
                orchestration.NativeLaunchContext(
                    session_id="sess",
                    resource_registry=Registry(),
                    publish_event=lambda *_args: None,
                    server_client=client,
                    ensure_comment_relay=relay,
                    agent_spec=spec,
                )
            )

    if write_failure is not None:
        assert captured == [previous_rule]
        assert "Could not write Antigravity startup instructions" in caplog.text
    else:
        assert captured == [
            "---\ntrigger: always_on\n---\n\n"
            f"Keep the session's authored instructions.\n\n{WORKSPACE_IMAGE_INSTRUCTION}"
            "\n\nMEMORY GUIDANCE\n"
        ]
    assert memory_guidance == [(client, "sess", spec, workspace)]
    assert relays[0]["session_labels"] == labels


@pytest.mark.asyncio
@pytest.mark.parametrize("resolution_failure", [None, "fetch", "policy", "unexpected"])
async def test_startup_instructions_without_a_spec_ask_the_server_nothing(
    resolution_failure: str | None, caplog: pytest.LogCaptureFixture
) -> None:
    """With no spec there are no memory tools, so the launch makes no memory request."""
    from omnigent.runner._entry import AgentSpecFetchError
    from omnigent.runner.native.orchestration import (
        _antigravity_startup_agent_spec,
        _native_startup_instructions,
    )
    from omnigent.runner.policy import AgentStartPolicyError

    error = {
        "fetch": AgentSpecFetchError("bundle unavailable"),
        "policy": AgentStartPolicyError("startup", "denied"),
        "unexpected": RuntimeError("unexpected resolver failure"),
    }.get(resolution_failure or "")

    async def resolve(session_id: str) -> None:
        assert session_id == "sess"
        if error is not None:
            raise error

    if resolution_failure in ("policy", "unexpected"):
        assert error is not None
        with pytest.raises(type(error)) as raised:
            await _antigravity_startup_agent_spec("sess", resolve)
        assert raised.value is error
        assert "preparing framework guidance" not in caplog.text
        return

    spec = await _antigravity_startup_agent_spec("sess", resolve)
    assert spec is None
    assert ("preparing framework guidance" in caplog.text) is (resolution_failure == "fetch")

    class _NoCalls:
        def __getattr__(self, name: str) -> object:
            raise AssertionError(f"unexpected server call: {name}")

    assert await _native_startup_instructions(
        spec, _NoCalls(), "sess"
    ) == _native_startup_instructions_from_spec(None)


@pytest.mark.asyncio
@pytest.mark.parametrize("instructions", ["  Keep this whitespace.  ", None])
async def test_startup_instructions_without_memory_are_unchanged(
    monkeypatch: pytest.MonkeyPatch, instructions: str | None
) -> None:
    """With memory unavailable the result is the pre-memory value, byte for byte."""
    from omnigent.runner.native.orchestration import _native_startup_instructions

    async def _no_memory(*_args: object, workspace: object = None) -> None:
        return None

    monkeypatch.setattr("omnigent.runner.native.orchestration.memory_startup_guidance", _no_memory)
    spec = _spec(instructions)

    assert await _native_startup_instructions(
        spec, None, "sess"
    ) == _native_startup_instructions_from_spec(spec)
