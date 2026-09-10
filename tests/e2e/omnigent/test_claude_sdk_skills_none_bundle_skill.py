"""E2E — bundled skill invocation under ``skills: none``.

Drives ``ClaudeSDKExecutor.run_turn()`` directly (in-process, real
``claude`` CLI binary) against a bundle whose spec sets ``skills:
none`` and ships a single skill under ``skills/my-skill/SKILL.md``.
The mock LLM server is scripted to have the model call the native
``Skill`` tool for that skill — exercising the real CLI binary's own
skill-allowlist enforcement, not just the Python-side translation
logic.

Two responses are queued per run: the CLI makes an unrelated
preliminary model call before the scripted user turn is processed (a
generic warm-up request, observed empirically — the first queued
response is otherwise silently consumed by it), so a plain-text
response absorbs that first request and the real assertion targets
the second.

**What breaks if this fails:** ``ClaudeSDKExecutor``'s ``skills:
none`` handling regresses so the agent's own bundled skill goes back
to being listed-but-uninvokable, silently defeating the fix in
``_seed_bundle_skills_when_hermetic`` — reproduced live pre-fix with
the exact reported error, ``Skill my-skill is not in this session's
skills allowlist``.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from shutil import which

import pytest

from omnigent.inner.claude_sdk_executor import ClaudeSDKExecutor
from omnigent.inner.executor import ToolCallComplete, ToolCallStatus
from tests.e2e.omnigent.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

_MODEL_PREFIX = "mock-skills-none-bundle"
_RUN_TIMEOUT_SEC = 60


@pytest.fixture
def claude_sdk_available() -> bool:
    """True when the ``claude`` CLI binary is present on PATH.

    Unlike the subprocess-based per-harness characterization tests,
    this test drives ``ClaudeSDKExecutor`` in-process against the
    CURRENT interpreter's ``claude_agent_sdk`` (the test's own venv,
    which is the omnigent venv itself for this test tree) — so only
    the ``claude`` binary needs a presence check.
    """
    return which("claude") is not None


@pytest.fixture
def skill_bundle(tmp_path: Path) -> Path:
    """A minimal bundle: ``skills: none``, one bundled skill named ``my-skill``."""
    bundle = tmp_path / "skill-test-agent"
    (bundle / "skills" / "my-skill").mkdir(parents=True)
    (bundle / "skills" / "my-skill" / "SKILL.md").write_text(
        "---\n"
        "name: my-skill\n"
        "description: A trivial test skill that just says a magic word.\n"
        "---\n"
        "\n"
        "When invoked, respond with exactly the word: SKILLINVOKED\n"
    )
    return bundle


def test_bundled_skill_is_invocable_under_skills_none(
    mock_credentials_env: dict[str, str],
    mock_llm_server_url: str,
    claude_sdk_available: bool,
    skill_bundle: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With ``skills: none``, the agent's own bundled skill still gets
    invoked successfully through the real ``claude`` CLI's own
    skill-allowlist enforcement — the model's ``Skill`` tool_use for
    ``my-skill`` is NOT denied.

    Pre-fix, the CLI's allowlist (triggered by the empty ``skills=[]``
    that ``"none"`` used to produce unconditionally) rejects the call
    with "not in this session's skills allowlist", surfaced here as a
    :class:`ToolCallComplete` with ``status=ERROR``.
    """
    if not claude_sdk_available:
        pytest.skip(
            "claude-sdk harness prerequisites missing: the 'claude' CLI "
            "binary must be present on PATH. Skipping — binary absent."
        )

    model = f"{_MODEL_PREFIX}-{uuid.uuid4().hex[:8]}"
    reset_mock_llm(mock_llm_server_url)
    configure_mock_llm(
        mock_llm_server_url,
        [
            # Absorbs an observed unrelated preliminary model call the
            # CLI makes before processing the scripted user turn.
            {"text": "preflight"},
            {"tool_calls": [{"name": "Skill", "arguments": '{"skill": "my-skill"}'}]},
        ],
        key=model,
    )
    set_fallback_mock_llm(mock_llm_server_url, model, "done")

    for key, value in mock_credentials_env.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setenv("ANTHROPIC_BASE_URL", mock_llm_server_url)
    monkeypatch.setenv("OMNIGENT_CLAUDE_SDK_NO_SANDBOX", "1")

    executor = ClaudeSDKExecutor(
        bundle_dir=skill_bundle,
        agent_name="skill-test-agent",
        skills_filter="none",
        model=model,
        permission_mode="bypassPermissions",
        api_key_helper="printf %s mock-key",
        cwd="/tmp",
    )

    async def _run() -> list[object]:
        events: list[object] = []
        async for ev in executor.run_turn(
            [
                {
                    "role": "user",
                    "content": (
                        "Use the Skill tool right now to invoke the skill named 'my-skill'."
                    ),
                }
            ],
            [],
            "You are a test agent.",
        ):
            events.append(ev)
        return events

    events = asyncio.run(asyncio.wait_for(_run(), timeout=_RUN_TIMEOUT_SEC))

    skill_completions = [
        ev for ev in events if isinstance(ev, ToolCallComplete) and ev.name == "Skill"
    ]
    assert skill_completions, f"expected a Skill ToolCallComplete event, got: {events}"
    completion = skill_completions[0]
    assert completion.status == ToolCallStatus.SUCCESS, (
        f"bundled skill was denied by the skills allowlist under skills:none "
        f"— result: {completion.result!r}"
    )
    assert "not in this session's skills allowlist" not in (completion.error or "")
