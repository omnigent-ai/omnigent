"""Tests for the shared native startup-additive raw-instruction resolution.

``_native_startup_raw_instructions_from_spec`` is the seam that turns a
session's ``AgentSpec.instructions`` into the value passed to the
managed-host launch paths' startup-additive channels: claude-native's
``--append-system-prompt`` (``_auto_create_claude_terminal``) and
codex-native's ``developer_instructions`` (``_auto_create_codex_terminal``).
It must return the verbatim author text only — never the framework-composed
per-turn string. Terminal launch is not tied to any one turn, while the
composed string is assembled per conversation for the turn about to run, so
carrying one turn's composition would address every later turn with it.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from omnigent.runner.app import ResolvedSpec, _native_startup_raw_instructions_from_spec
from omnigent.runner.native.orchestration import _build_pi_native_args
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
        ("Be a concise assistant.", "Be a concise assistant."),
        (None, None),
        ("   \n  ", None),
    ],
    ids=["present", "absent", "whitespace-only"],
)
def test_native_startup_raw_instructions_from_spec(
    instructions: str | None, expected: str | None
) -> None:
    """Verbatim author text is returned; absent/whitespace-only resolves to None."""
    assert _native_startup_raw_instructions_from_spec(_spec(instructions)) == expected


def test_native_startup_raw_instructions_from_spec_none_spec() -> None:
    """A missing spec yields no instructions (neither channel is injected)."""
    assert _native_startup_raw_instructions_from_spec(None) is None


def test_native_startup_raw_instructions_from_spec_resolved_wrapper() -> None:
    """A ResolvedSpec wrapper unwraps to the same instructions text."""
    wrapped = ResolvedSpec(spec=_spec("Be a concise assistant."), workdir=Path("/tmp"))
    assert _native_startup_raw_instructions_from_spec(wrapped) == "Be a concise assistant."


def test_native_startup_raw_instructions_from_spec_never_returns_stripped_form() -> None:
    """The original resolved text is preserved verbatim, not the stripped form."""
    padded = "  Keep leading/trailing whitespace exactly.  "
    assert _native_startup_raw_instructions_from_spec(_spec(padded)) == padded


@pytest.mark.parametrize(
    ("project", "global_file", "user_args", "mode", "approve", "authored", "expected"),
    [
        (True, True, [], "append", True, True, ["project", "authored"]),
        (False, True, [], "append", True, True, ["global", "authored"]),
        (False, False, [], "append", True, True, ["authored"]),
        (True, True, [], "append", False, True, ["global", "authored"]),
        (True, True, ["--no-approve"], "append", True, True, ["global", "authored"]),
        (True, True, ["-na", "-a"], "append", True, True, ["project", "authored"]),
        (
            True,
            True,
            ["--append-system-prompt", "custom"],
            "append",
            True,
            True,
            ["authored", "custom"],
        ),
        (True, True, ["--append-system-prompt", ""], "append", True, True, ["authored", ""]),
        (True, True, ["--system-prompt", "User base"], "replace", True, True, [""]),
        (True, True, [], "append", True, False, []),
    ],
    ids=[
        "project",
        "global-fallback",
        "missing",
        "no-trust-override",
        "no-approve",
        "trust-last-wins",
        "user-append",
        "user-empty-append",
        "replace-user-base",
        "no-authored",
    ],
)
def test_native_pi_preserves_append_discovery(
    tmp_path: Path,
    project: bool,
    global_file: bool,
    user_args: list[str],
    mode: str,
    approve: bool,
    authored: bool,
    expected: list[str],
) -> None:
    workspace = tmp_path / "workspace with spaces"
    project_config = workspace / ".pi"
    agent_dir = tmp_path / "active-agent"
    project_config.mkdir(parents=True)
    agent_dir.mkdir()
    paths = {
        "project": project_config / "APPEND_SYSTEM.md",
        "global": agent_dir / "APPEND_SYSTEM.md",
        "authored": tmp_path / "authored.md",
    }
    for name, present in (("project", project), ("global", global_file), ("authored", authored)):
        if present:
            paths[name].write_text(name)
    args = _build_pi_native_args(
        terminal_launch_args=user_args,
        extension_path=tmp_path / "extension.js",
        session_dir=tmp_path,
        external_session_id=None,
        instructions_path=paths["authored"] if authored else None,
        context_files=False,
        system_prompt_mode=mode,
        workspace=workspace,
        agent_dir=agent_dir,
        approve=approve,
    )
    assert [
        args[index + 1] for index, arg in enumerate(args) if arg == "--append-system-prompt"
    ] == [str(paths[value].resolve()) if value in paths else value for value in expected]
    if user_args:
        assert args[-len(user_args) :] == user_args
    if mode == "replace":
        assert args[args.index("--system-prompt") + 1] == str(paths["authored"])
        assert args[-2:] == ["--system-prompt", "User base"]
