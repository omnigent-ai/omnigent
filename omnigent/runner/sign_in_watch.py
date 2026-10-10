"""Runner-side watcher that detects native-harness sign-in completion and notifies the session."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable
from typing import Any

import httpx

from omnigent.runner.resource_registry import SessionResourceRegistry

_logger = logging.getLogger("omnigent.runner.app")


# How often the pane behind a sign-in card is read for the sign-in to complete.
_SIGN_IN_WATCH_INTERVAL_S = 3.0


@dataclasses.dataclass(frozen=True)
class SignInWatch:
    """Sign-in watcher entry points the rest of the runner app calls."""

    native_pane_names: Callable[[str], set[str]]
    start_sign_in_watch: Callable[[str], None]


def build_sign_in_watch(
    *,
    _background_tasks: set[asyncio.Task[Any]],
    _session_harness_name: Callable[[str], str | None],
    _sign_in_watchers: dict[str, asyncio.Task[None]],
    resource_registry: SessionResourceRegistry,
    server_client: httpx.AsyncClient,
) -> SignInWatch:
    """Build the sign-in-completion watcher over the runner app's session state.

    The keyword arguments are the runner app's shared session state and helpers.
    """

    def _native_pane_names(conv_id: str) -> set[str]:
        """
        Return the terminal names that can carry a session's launcher sign-in prompt.

        Only the native agent's own pane counts: a shell the person opened
        alongside it (``gh auth login``, a docs page) must not supply the
        sign-in address or hold back the "signed in" notice. With the harness
        known this is its one pane; otherwise any native agent pane.
        """
        from omnigent.harness_plugins import native_agents

        harness = _session_harness_name(conv_id)
        names = {agent.terminal_name for agent in native_agents() if agent.harness == harness}
        return names or {agent.terminal_name for agent in native_agents()}

    def _start_sign_in_watch(conv_id: str) -> None:
        """Watch the pane behind a sign-in card so the chat learns when the sign-in worked."""
        existing = _sign_in_watchers.get(conv_id)
        if existing is not None and not existing.done():
            return
        task = asyncio.get_running_loop().create_task(
            _watch_sign_in_completion(conv_id), name=f"sign-in-watch-{conv_id}"
        )
        _sign_in_watchers[conv_id] = task
        task.add_done_callback(_background_tasks.discard)
        _background_tasks.add(task)

    async def _watch_sign_in_completion(conv_id: str) -> None:
        """
        Post one "Signed in to Databricks" notice once the agent behind a sign-in card is ready.

        A turn failed because the session's launcher was parked on a sign-in
        prompt. This reads the session's running panes until no prompt is on
        screen and the agent can take a message, then posts the notice. It ends
        silently when no pane is running any more: the launcher exited, which
        the next send reports on its own.

        :param conv_id: Session whose turn failed with ``databricks_sign_in_pending``.
        """
        from omnigent.harnesses.diagnostics import detect_sign_in_prompt

        harness = _session_harness_name(conv_id)
        pane_names = _native_pane_names(conv_id)
        while True:
            await asyncio.sleep(_SIGN_IN_WATCH_INTERVAL_S)
            registry = resource_registry.terminal_registry
            entries = registry.list_for_conversation(conv_id) if registry is not None else []
            screens: list[str] = []
            for entry in entries:
                if entry.terminal_name not in pane_names or not entry.instance.running:
                    continue
                result = await entry.instance.read(join_wrapped=True)
                screen = result.get("screen") if isinstance(result, dict) else None
                screens.append(screen if isinstance(screen, str) else "")
            if not screens:
                return
            if any(detect_sign_in_prompt(screen) is not None for screen in screens):
                continue
            if _sign_in_agent_ready(conv_id, harness, screens):
                await _post_sign_in_completed_notice(conv_id, harness)
                return

    def _sign_in_agent_ready(conv_id: str, harness: str | None, screens: list[str]) -> bool:
        """Return whether the agent can take a message now that no sign-in prompt is on screen."""
        if harness == "codex-native":
            from omnigent.harnesses.codex_native.bridge import (
                bridge_dir_for_bridge_id,
                read_bridge_state,
            )

            # Thread discovery publishes the bridge state the moment the TUI starts a thread.
            return read_bridge_state(bridge_dir_for_bridge_id(conv_id)) is not None
        if harness == "claude-native":
            from omnigent.harnesses.claude_native.bridge import _claude_prompt_rendered

            return any(_claude_prompt_rendered(screen) for screen in screens)
        return True

    async def _post_sign_in_completed_notice(conv_id: str, harness: str | None) -> None:
        """Append the neutral "Signed in to Databricks" notice to the session transcript."""
        agent = {"codex-native": "Codex", "claude-native": "Claude Code"}.get(
            harness or "", "The agent"
        )
        try:
            resp = await server_client.post(
                f"/v1/sessions/{conv_id}/events",
                json={
                    "type": "external_conversation_item",
                    "data": {
                        "item_type": "error",
                        "item_data": {
                            "source": "harness",
                            "code": "databricks_sign_in_completed",
                            "title": "Signed in to Databricks",
                            "message": f"{agent} is ready. Send your message again.",
                            "level": "info",
                        },
                    },
                },
                timeout=10.0,
            )
            resp.raise_for_status()
        except (httpx.HTTPError, RuntimeError):
            _logger.warning(
                "Failed to post the sign-in completed notice for %s", conv_id, exc_info=True
            )
            return
        _logger.info(
            "Databricks sign-in completed for %s; %s is ready",
            conv_id,
            agent,
            extra={"session_id": conv_id},
        )

    return SignInWatch(
        native_pane_names=_native_pane_names,
        start_sign_in_watch=_start_sign_in_watch,
    )
