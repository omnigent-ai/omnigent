"""Tests for the ``cursor-cloud`` CLI group (list / archive / unarchive).

Mocks ``cursor_sdk.AsyncClient`` and ``cursor_sdk.AsyncAgent`` so no real
bridge subprocess is spawned. All tests use ``click.testing.CliRunner`` to
invoke commands synchronously.
"""

from __future__ import annotations

import os
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from omnigent.cli import cli

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_agent(
    *,
    agent_id: str = "agent-abc",
    name: str = "Test Agent",
    status: str | None = "idle",
    archived: bool = False,
    repos: tuple[str, ...] = ("https://github.com/org/repo",),
    last_modified: str | None = "2024-01-01T00:00:00Z",
) -> MagicMock:
    """Return a MagicMock shaped like ``SDKAgentInfo``."""
    info = MagicMock()
    info.agent_id = agent_id
    info.name = name
    info.status = status
    info.archived = archived
    info.repos = repos
    info.last_modified = last_modified
    return info


class _FakeListResult:
    """Minimal async-iterable stand-in for ``AsyncListResult[SDKAgentInfo]``."""

    def __init__(self, items: list[Any]) -> None:
        self._items = items

    async def auto_paging_iter(self):  # type: ignore[override]
        for item in self._items:
            yield item


def _fake_client(
    agents: list[Any] | None = None,
    *,
    list_exc: Exception | None = None,
) -> MagicMock:
    """Build a mock ``AsyncClient`` pre-wired with a list result."""
    client = MagicMock()
    client.aclose = AsyncMock()
    if list_exc:
        client.list_agents = AsyncMock(side_effect=list_exc)
    else:
        client.list_agents = AsyncMock(return_value=_FakeListResult(agents or []))
    return client


@pytest.fixture()
def runner() -> CliRunner:
    return CliRunner()


def _with_key(env: dict[str, str] | None = None) -> dict[str, str]:
    """Return an env mapping that always contains CURSOR_API_KEY."""
    return {"CURSOR_API_KEY": "test-key", **(env or {})}


# ---------------------------------------------------------------------------
# list
# ---------------------------------------------------------------------------


def test_list_renders_agents(runner: CliRunner) -> None:
    """``cursor-cloud list`` prints agent_id, status, and repos."""
    agent = _make_agent(
        agent_id="agent-abc",
        name="My Agent",
        status="running",
        repos=("gh:org/repo",),
    )
    client = _fake_client(agents=[agent])

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            mock_cls.launch_bridge = AsyncMock(return_value=client)
            result = runner.invoke(cli, ["cursor-cloud", "list"])

    assert result.exit_code == 0, result.output
    assert "agent-abc" in result.output
    assert "running" in result.output
    assert "gh:org/repo" in result.output


def test_list_no_agents_prints_friendly_message(runner: CliRunner) -> None:
    """``cursor-cloud list`` with zero results prints a friendly empty message."""
    client = _fake_client(agents=[])

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            mock_cls.launch_bridge = AsyncMock(return_value=client)
            result = runner.invoke(cli, ["cursor-cloud", "list"])

    assert result.exit_code == 0, result.output
    assert "No cloud agents found" in result.output


def test_list_all_passes_include_archived(runner: CliRunner) -> None:
    """``cursor-cloud list --all`` passes ``include_archived=True`` to the SDK."""
    client = _fake_client(agents=[])

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            mock_cls.launch_bridge = AsyncMock(return_value=client)
            runner.invoke(cli, ["cursor-cloud", "list", "--all"])

    client.list_agents.assert_called_once_with(runtime="cloud", include_archived=True)


# ---------------------------------------------------------------------------
# archive
# ---------------------------------------------------------------------------


def test_archive_calls_sdk_and_confirms(runner: CliRunner) -> None:
    """``cursor-cloud archive <id>`` calls ``AsyncAgent.archive`` and confirms."""
    client = _fake_client()

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            with patch("cursor_sdk.AsyncAgent", create=True) as mock_agent:
                mock_agent.archive = AsyncMock()
                mock_cls.launch_bridge = AsyncMock(return_value=client)
                result = runner.invoke(cli, ["cursor-cloud", "archive", "agent-xyz"])

    assert result.exit_code == 0, result.output
    mock_agent.archive.assert_called_once_with("agent-xyz", client=client)
    assert "agent-xyz" in result.output


def test_archive_uses_stored_cursor_key(runner: CliRunner) -> None:
    """A key resolved from setup storage is mirrored into CURSOR_API_KEY."""
    client = _fake_client()
    env_without_key = {k: v for k, v in os.environ.items() if k != "CURSOR_API_KEY"}

    with patch.dict("os.environ", env_without_key, clear=True):
        with patch("omnigent.onboarding.cursor_auth.resolve_cursor_api_key") as resolve_key:
            resolve_key.return_value = "stored-key"
            with patch("cursor_sdk.AsyncClient") as mock_cls:
                with patch("cursor_sdk.AsyncAgent", create=True) as mock_agent:
                    mock_agent.archive = AsyncMock()
                    mock_cls.launch_bridge = AsyncMock(return_value=client)
                    result = runner.invoke(cli, ["cursor-cloud", "archive", "agent-xyz"])
                    mirrored_key = os.environ.get("CURSOR_API_KEY")

    assert result.exit_code == 0, result.output
    assert mirrored_key == "stored-key"
    mock_agent.archive.assert_called_once_with("agent-xyz", client=client)


# ---------------------------------------------------------------------------
# unarchive
# ---------------------------------------------------------------------------


def test_unarchive_calls_sdk_and_confirms(runner: CliRunner) -> None:
    """``cursor-cloud unarchive <id>`` calls ``AsyncAgent.unarchive`` and confirms."""
    client = _fake_client()

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            with patch("cursor_sdk.AsyncAgent", create=True) as mock_agent:
                mock_agent.unarchive = AsyncMock()
                mock_cls.launch_bridge = AsyncMock(return_value=client)
                result = runner.invoke(cli, ["cursor-cloud", "unarchive", "agent-xyz"])

    assert result.exit_code == 0, result.output
    mock_agent.unarchive.assert_called_once_with("agent-xyz", client=client)
    assert "agent-xyz" in result.output


# ---------------------------------------------------------------------------
# Error paths — missing cursor-sdk (parametrized over all three subcommands)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["cursor-cloud", "list"],
        ["cursor-cloud", "archive", "agent-id"],
        ["cursor-cloud", "unarchive", "agent-id"],
    ],
)
def test_missing_cursor_sdk_gives_clean_error(runner: CliRunner, argv: list[str]) -> None:
    """Missing ``cursor_sdk`` prints a helpful install hint and exits non-zero."""
    with patch.dict("os.environ", _with_key()):
        with patch.dict("sys.modules", {"cursor_sdk": None}):
            result = runner.invoke(cli, argv)

    assert result.exit_code != 0
    assert "omnigent[cursor]" in result.output


# ---------------------------------------------------------------------------
# Error paths — no CURSOR_API_KEY (parametrized over all three subcommands)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv",
    [
        ["cursor-cloud", "list"],
        ["cursor-cloud", "archive", "agent-id"],
        ["cursor-cloud", "unarchive", "agent-id"],
    ],
)
def test_no_api_key_gives_clean_error(runner: CliRunner, argv: list[str]) -> None:
    """Absent Cursor key prints a helpful message and exits non-zero."""
    env_without_key = {k: v for k, v in os.environ.items() if k != "CURSOR_API_KEY"}
    with patch.dict("os.environ", env_without_key, clear=True):
        with patch("omnigent.onboarding.cursor_auth.resolve_cursor_api_key", return_value=None):
            result = runner.invoke(cli, argv)

    assert result.exit_code != 0
    assert "CURSOR_API_KEY" in result.output


# ---------------------------------------------------------------------------
# Error paths — SDK call raises (one test per subcommand)
# ---------------------------------------------------------------------------


def test_list_sdk_exception_exits_nonzero(runner: CliRunner) -> None:
    """An exception from ``list_agents`` exits non-zero with the error in output."""
    client = _fake_client(list_exc=RuntimeError("network failure"))

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            mock_cls.launch_bridge = AsyncMock(return_value=client)
            result = runner.invoke(cli, ["cursor-cloud", "list"])

    assert result.exit_code != 0
    assert "RuntimeError: network failure" in result.output


def test_archive_sdk_exception_exits_nonzero(runner: CliRunner) -> None:
    """An exception from ``AsyncAgent.archive`` exits non-zero."""
    client = _fake_client()

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            with patch("cursor_sdk.AsyncAgent", create=True) as mock_agent:
                mock_agent.archive = AsyncMock(side_effect=RuntimeError("permission denied"))
                mock_cls.launch_bridge = AsyncMock(return_value=client)
                result = runner.invoke(cli, ["cursor-cloud", "archive", "agent-xyz"])

    assert result.exit_code != 0
    assert "RuntimeError: permission denied" in result.output


def test_unarchive_sdk_exception_exits_nonzero(runner: CliRunner) -> None:
    """An exception from ``AsyncAgent.unarchive`` exits non-zero."""
    client = _fake_client()

    with patch.dict("os.environ", _with_key()):
        with patch("cursor_sdk.AsyncClient") as mock_cls:
            with patch("cursor_sdk.AsyncAgent", create=True) as mock_agent:
                mock_agent.unarchive = AsyncMock(side_effect=RuntimeError("not found"))
                mock_cls.launch_bridge = AsyncMock(return_value=client)
                result = runner.invoke(cli, ["cursor-cloud", "unarchive", "agent-xyz"])

    assert result.exit_code != 0
    assert "RuntimeError: not found" in result.output
