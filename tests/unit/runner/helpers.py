"""Shared helpers for runner tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from omnigent.inner.os_env import OSEnvironment
from omnigent.inner.terminal import TerminalInstance


class NullServerClient:
    """Minimal fake Omnigent server client for tests that do not exercise Omnigent interactions.

    Returns empty/204 responses to all requests so the runner's Omnigent calls
    (session fetch, label patch, history load, etc.) succeed silently.
    Used wherever ``create_runner_app`` is called in tests that only
    exercise runner-local behavior with no real Omnigent server.
    """

    class _Response:
        """Stub HTTP response that looks like a 200 with an empty body."""

        status_code = 200

        def json(self) -> dict[str, Any]:
            """Return an empty JSON object."""
            return {}

        def raise_for_status(self) -> None:
            """No-op: stub always succeeds."""

    async def get(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any GET request.

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: Stub 200 response with empty JSON body.
        """
        del url, kwargs
        return self._Response()

    async def post(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any POST request.

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: Stub 200 response with empty JSON body.
        """
        del url, kwargs
        return self._Response()

    async def patch(self, url: str, **kwargs: Any) -> _Response:
        """Return an empty 200 for any PATCH request.

        :param url: Request URL (ignored).
        :param kwargs: Extra keyword arguments (ignored).
        :returns: Stub 200 response with empty JSON body.
        """
        del url, kwargs
        return self._Response()


class RunningFlagTerminalInstance(TerminalInstance):
    """Terminal instance stub whose liveness follows its running flag."""

    async def set_conversation_link(self, conversation_link: str | None) -> None:
        """
        Update the in-memory status link without shelling out to tmux.

        :param conversation_link: Conversation URL to show, e.g.
            ``"/c/conv_abc123"``, or ``None`` to clear it.
        :returns: None.
        """
        self.conversation_link = conversation_link

    async def is_alive(self) -> bool:
        """
        Report liveness without shelling out to tmux.

        :returns: The current ``running`` flag.
        """
        return self.running


def make_test_terminal_instance(
    name: str,
    session_key: str,
    tmp_path: Path,
    *,
    running: bool = True,
    os_env: OSEnvironment | None = None,
) -> TerminalInstance:
    """
    Build a terminal instance stub for runner/resource tests.

    :param name: Terminal name, e.g. ``"bash"``.
    :param session_key: Per-launch session key, e.g. ``"s1"``.
    :param tmp_path: Temporary directory for placeholder paths.
    :param running: Initial in-memory running flag.
    :param os_env: Optional terminal-specific OS environment.
    :returns: A test terminal instance.
    """
    return RunningFlagTerminalInstance(
        name=name,
        session_key=session_key,
        socket_path=tmp_path / f"{name}-{session_key}.sock",
        private_dir=tmp_path / f"{name}-{session_key}",
        os_env=os_env,
        running=running,
    )
