"""Runner-side watcher that detects native-harness sign-in completion and notifies the session."""

from __future__ import annotations

import asyncio
import dataclasses
import logging
from collections.abc import Callable, Coroutine, Mapping
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
    post_sign_in_hold_notice: Callable[[str, Mapping[str, Any]], Coroutine[Any, Any, None]]


def build_sign_in_watch(
    *,
    _background_tasks: set[asyncio.Task[Any]],
    _session_harness_name: Callable[[str], str | None],
    _claim_sign_in_release: Callable[[str], bool],
    _release_sign_in_hold: Callable[[str], None],
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
        the next send reports on its own. However it ends, it then releases a
        message held for the sign-in, so the session's queue sends it.

        :param conv_id: Session whose turn failed with ``databricks_sign_in_pending``.
        """
        from omnigent.harnesses.diagnostics import detect_sign_in_prompt

        try:
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
                    try:
                        result = await entry.instance.read(join_wrapped=True)
                    except Exception:  # A failed read must still release the hold.
                        _logger.debug(
                            "Failed to read the sign-in pane for %s", conv_id, exc_info=True
                        )
                        result = None
                    screen = result.get("screen") if isinstance(result, dict) else None
                    screens.append(screen if isinstance(screen, str) else "")
                if not screens:
                    return
                if any(detect_sign_in_prompt(screen) is not None for screen in screens):
                    continue
                if _sign_in_agent_ready(conv_id, harness, screens):
                    # Post before the release on exit, so the notice lands before the resend
                    # and a resend refused again can rewatch.
                    await _post_sign_in_completed_notice(
                        conv_id, harness, held=_claim_sign_in_release(conv_id)
                    )
                    return
        finally:
            # Any exit resumes a held message; teardown paths drop the queue themselves.
            _release_sign_in_hold(conv_id)

    def _sign_in_agent_ready(conv_id: str, harness: str | None, screens: list[str]) -> bool:
        """Return whether the agent can take a message now that no sign-in prompt is on screen."""
        if harness == "codex-native":
            from omnigent.harnesses.codex_native.bridge import read_bridge_state
            from omnigent.runner.native.orchestration import codex_native_bridge_dir

            # Thread discovery publishes the bridge state the moment the TUI starts a thread,
            # in the directory the launch used (a rotated session's label directory).
            return read_bridge_state(codex_native_bridge_dir(conv_id)) is not None
        if harness == "claude-native":
            from omnigent.harnesses.claude_native.bridge import _claude_prompt_rendered

            return any(_claude_prompt_rendered(screen) for screen in screens)
        return True

    async def _post_sign_in_completed_notice(
        conv_id: str, harness: str | None, *, held: bool = False
    ) -> None:
        """
        Append the neutral "Signed in to Databricks" notice to the session transcript.

        :param conv_id: Session whose sign-in completed.
        :param harness: The session's harness, e.g. ``"claude-native"``.
        :param held: Whether a message waited for this sign-in and is being sent now.
        """
        agent = _agent_name(harness)
        next_step = "Sending your message now." if held else "Send your message again."
        try:
            await _post_sign_in_item(
                conv_id,
                {
                    "code": "databricks_sign_in_completed",
                    "title": "Signed in to Databricks",
                    "message": f"{agent} is ready. {next_step}",
                },
            )
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

    async def _post_sign_in_hold_notice(conv_id: str, error: Mapping[str, Any]) -> None:
        """
        Tell the chat its message waits for a launcher sign-in instead of failing.

        :param conv_id: Session whose message is held.
        :param error: The harness refusal, carrying the sign-in next step as
            ``remediation``.
        """
        from omnigent.harnesses.diagnostics import SIGN_IN_RESEND_STEP

        agent = _agent_name(_session_harness_name(conv_id))
        item: dict[str, Any] = {
            "code": "databricks_sign_in_pending",
            # A collapsed card shows only its headline, so the headline says the message
            # waits; the refusal's own "can't start" title reads as a failure to resend.
            "title": "Your message will be sent once you sign in to Databricks",
            "message": (
                f"{agent} is waiting for a sign-in in this session's terminal. "
                "You don't need to send your message again."
            ),
        }
        remediation = error.get("remediation")
        if isinstance(remediation, str):
            # Keep the sign-in step; the held message is sent without the person resending it.
            item["remediation"] = remediation.replace(SIGN_IN_RESEND_STEP, ".")
        try:
            await _post_sign_in_item(conv_id, item)
        except (httpx.HTTPError, RuntimeError):
            _logger.warning(
                "Failed to post the sign-in hold notice for %s", conv_id, exc_info=True
            )

    async def _post_sign_in_item(conv_id: str, item: Mapping[str, Any]) -> None:
        """Append one neutral sign-in notice item to the session transcript."""
        resp = await server_client.post(
            f"/v1/sessions/{conv_id}/events",
            json={
                "type": "external_conversation_item",
                "data": {
                    "item_type": "error",
                    "item_data": {"source": "harness", **item, "level": "info"},
                },
            },
            timeout=10.0,
        )
        resp.raise_for_status()

    return SignInWatch(
        native_pane_names=_native_pane_names,
        start_sign_in_watch=_start_sign_in_watch,
        post_sign_in_hold_notice=_post_sign_in_hold_notice,
    )


def _agent_name(harness: str | None) -> str:
    """Name the native agent in a sign-in notice, e.g. ``"Claude Code"``."""
    return {"codex-native": "Codex", "claude-native": "Claude Code"}.get(
        harness or "", "The agent"
    )
