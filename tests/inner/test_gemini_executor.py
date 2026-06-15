"""Tests for :class:`omnigent.inner.gemini_executor.GeminiExecutor`."""

from __future__ import annotations

from typing import Any
from unittest.mock import patch

import pytest

from omnigent.inner import gemini_executor as ge
from omnigent.inner.executor import ExecutorConfig, TextChunk, TurnComplete
from omnigent.inner.gemini_executor import GEMINI_PINNED_MODEL, GeminiExecutor


def _make_fake_acp_client(captured: dict[str, Any]) -> type:
    """Build a FakeAcpClient that records constructor + call args into *captured*."""

    class FakeAcpClient:
        running = True

        def __init__(
            self,
            path: str,
            *,
            env: dict[str, str],
            cwd: str | None,
            extra_args: list[str] | None = None,
            subcommand: tuple[str, ...] = ("acp",),
        ) -> None:
            captured["path"] = path
            captured["env"] = env
            captured["cwd"] = cwd
            captured["extra_args"] = extra_args
            captured["subcommand"] = subcommand

        async def start(self) -> dict[str, Any]:
            return {}

        async def new_session(
            self,
            *,
            cwd: str,
            model: str | None,
            mcp_servers: list[dict[str, Any]] | None = None,
        ) -> str:
            captured["new_session"] = {"cwd": cwd, "model": model, "mcp_servers": mcp_servers}
            return "s1"

        async def prompt_stream(self, session_id: str, blocks: list[dict[str, Any]]):
            captured["prompt"] = {"session_id": session_id, "blocks": blocks}
            yield ("update", {"sessionUpdate": "agent_message_chunk", "content": {"text": "ok"}})
            yield ("result", {"stopReason": "end_turn"})

        async def close(self) -> None:
            pass

    return FakeAcpClient


def _make_executor(**kwargs: Any) -> GeminiExecutor:
    with patch("omnigent.inner.gemini_executor._find_gemini", return_value="/usr/bin/gemini"):
        return GeminiExecutor(**kwargs)


def test_missing_gemini_raises_import_error() -> None:
    with patch("omnigent.inner.gemini_executor._find_gemini", return_value=None):
        with pytest.raises(ImportError, match="gemini"):
            GeminiExecutor()


def test_pinned_model_matches_documented_value() -> None:
    """The pin lives in one place; the executor passes it as ``--model``.

    A drift in this constant is a contract change — every Gemini worker would
    silently move to a different model. Catch it here so the change must be
    intentional.
    """
    assert GEMINI_PINNED_MODEL == "gemini-3.1-pro-preview"


def test_clean_gemini_env_allows_gemini_prefix_denies_secrets(monkeypatch) -> None:
    """The deny-by-default allowlist mirrors cursor / mimo: only known-safe
    prefixes pass, unrelated cloud/API secrets do not."""
    monkeypatch.setenv("GEMINI_API_KEY", "k1")
    monkeypatch.setenv("GOOGLE_API_KEY", "k2")
    monkeypatch.setenv("DATABRICKS_TOKEN", "dapi-secret")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    env = ge._clean_gemini_env()

    assert env["GEMINI_API_KEY"] == "k1"
    assert env["GOOGLE_API_KEY"] == "k2"
    assert env["PATH"] == "/usr/bin"
    assert "DATABRICKS_TOKEN" not in env
    assert "OPENAI_API_KEY" not in env


def test_executor_injects_api_key_into_subprocess_env() -> None:
    """An explicit ``api_key`` lands as ``GEMINI_API_KEY`` in the env we pass
    to the ACP subprocess (so a per-spec key wins over an inherited one)."""
    with patch("omnigent.inner.gemini_executor._find_gemini", return_value="/usr/bin/gemini"):
        executor = GeminiExecutor(api_key="from-spec")
    assert executor._env["GEMINI_API_KEY"] == "from-spec"


async def _run_one_turn(executor: GeminiExecutor, config: ExecutorConfig | None) -> list[Any]:
    return [
        event
        async for event in executor.run_turn(
            [{"role": "user", "content": "hello", "session_id": "conv1"}],
            [],
            "system",
            config,
        )
    ]


@pytest.mark.asyncio
async def test_acp_launch_uses_flag_subcommand_and_pins_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ACP server is launched with ``--acp`` (flag, not ``acp``
    subcommand) and the pinned model arrives via ``--model`` along with
    the ``--yolo`` / ``--skip-trust`` headless flags.

    Drift here is catastrophic in either direction — wrong subcommand means
    no ACP at all (gemini parses it as a prompt and starts work in /tmp);
    a missing ``--model`` means a non-pinned model gets selected.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(ge, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo")

    await _run_one_turn(executor, None)

    assert captured["path"] == "/usr/bin/gemini"
    assert captured["cwd"] == "/repo"
    assert captured["subcommand"] == ("--acp",)
    # The exact head must be ``--model gemini-3.1-pro-preview`` — argument
    # order and value both verified.
    assert captured["extra_args"][:2] == ["--model", GEMINI_PINNED_MODEL]
    assert "--yolo" in captured["extra_args"]
    assert "--skip-trust" in captured["extra_args"]
    # ``--sandbox`` is NOT appended for the default ``sandbox=none`` os_env.
    assert "--sandbox" not in captured["extra_args"]


@pytest.mark.asyncio
async def test_session_new_does_not_pass_a_model(monkeypatch: pytest.MonkeyPatch) -> None:
    """``session/new`` is left to the CLI's resolved model.

    The model is pinned via ``--model`` at launch, so the ACP session/new
    parameter must stay ``None`` — passing it again would be redundant and
    risks divergence if the two values ever fell out of sync.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(ge, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo")

    await _run_one_turn(executor, None)

    assert captured["new_session"] == {"cwd": "/repo", "model": None, "mcp_servers": []}


@pytest.mark.asyncio
async def test_executor_config_model_is_ignored(monkeypatch: pytest.MonkeyPatch) -> None:
    """Any ``ExecutorConfig.model`` override is ignored — the pin holds.

    The whole point of pinning is that ``/model`` and per-call configs
    cannot move the worker off ``gemini-3.1-pro-preview``. If this test
    fails, an override path leaked back in.
    """
    captured: dict[str, Any] = {}
    monkeypatch.setattr(ge, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo")

    await _run_one_turn(executor, ExecutorConfig(model="gemini-2.0-flash"))

    # Launch arg is the pinned model regardless of the config's request.
    assert captured["extra_args"][:2] == ["--model", GEMINI_PINNED_MODEL]
    # session/new also stays None — the override never reaches it either.
    assert captured["new_session"]["model"] is None


@pytest.mark.asyncio
async def test_first_turn_prepends_system_prompt_to_user_text(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACP has no system-prompt field, so the first turn's user text is
    ``f"{system}\\n\\n{user}"`` (the cursor / mimo convention)."""
    captured: dict[str, Any] = {}
    monkeypatch.setattr(ge, "AcpClient", _make_fake_acp_client(captured))
    executor = _make_executor(cwd="/repo")

    events = await _run_one_turn(executor, None)

    assert captured["prompt"]["blocks"] == [{"type": "text", "text": "system\n\nhello"}]
    assert isinstance(events[0], TextChunk)
    assert events[0].text == "ok"
    assert isinstance(events[1], TurnComplete)
    assert events[1].response == "ok"


@pytest.mark.asyncio
async def test_sandbox_flag_appended_when_os_env_sandbox_enabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A non-``none`` os_env sandbox enables gemini's own ``--sandbox``."""
    from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec

    captured: dict[str, Any] = {}
    monkeypatch.setattr(ge, "AcpClient", _make_fake_acp_client(captured))
    os_env = OSEnvSpec(
        type="caller_process",
        cwd="/repo",
        sandbox=OSEnvSandboxSpec(type="bwrap"),
        fork=False,
    )
    executor = _make_executor(os_env=os_env)

    await _run_one_turn(executor, None)

    assert "--sandbox" in captured["extra_args"]
