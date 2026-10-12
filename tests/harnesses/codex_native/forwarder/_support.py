"""Shared support for Codex forwarder tests."""

from __future__ import annotations

from pathlib import Path

import httpx

from omnigent.harnesses.codex_native.bridge import (
    codex_home_for_bridge_dir,
)


class _RecordingClient:
    """
    Async ``httpx`` client stub that records POSTs and returns HTTP 200.

    Only ``post`` is exercised by ``_post_session_event``; each call is
    recorded so the test can assert exactly what was mirrored.
    """

    def __init__(self) -> None:
        """Initialize with an empty record of posts."""
        self.posts: list[tuple[str, dict]] = []

    async def post(
        self,
        url: str,
        *,
        json: dict,
        timeout: float | None = None,
    ) -> httpx.Response:
        """
        Record ``(url, json)`` and return a 200 response.

        :param url: Request URL, e.g. ``"/v1/sessions/conv_x/events"``.
        :param json: JSON body, e.g.
            ``{"type": "external_model_change", "data": {"model": "gpt-5.4"}}``.
        :param timeout: Ignored request timeout used by transient delta posts.
        :returns: A real ``httpx.Response`` with status 200.
        """
        self.posts.append((url, json))
        return httpx.Response(200, request=httpx.Request("POST", url))


class _RaisingPostClient:
    """Async client stub whose ``post`` always raises a fixed transport error."""

    def __init__(self, exc: httpx.HTTPError) -> None:
        self._exc = exc
        self.calls = 0

    async def post(self, url: str, json: object) -> httpx.Response:
        self.calls += 1
        raise self._exc


def _write_session_config(tmp_path: Path, body: str) -> None:
    """
    Write the session's private ``config.toml`` under the bridge dir.

    :param tmp_path: Bridge directory.
    :param body: Raw TOML body, e.g. ``"[mcp_servers.safe]\ncommand='x'"``.
    """
    home = codex_home_for_bridge_dir(tmp_path)
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(body)
