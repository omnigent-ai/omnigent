"""``harness: cursor-cloud`` executor — Cursor Cloud / Background Agents.

Where :class:`omnigent.inner.cursor_executor.CursorExecutor` drives a *local*
``cursor-agent`` over a bridge (editing the session ``cwd``), this executor
launches a **cloud** run: the same ``cursor-sdk`` ``AsyncClient`` connected to
Cursor's backend (``https://api.cursor.com``) creates a background agent that
clones a GitHub repo into a fresh VM, works autonomously, and pushes a branch /
opens a PR. The local working tree is never touched.

The cloud runtime deliberately drops the local harness's machinery:

- **No tool bridge / custom tools** — tools run inside the cloud VM, so the
  ``tools`` argument is ignored and ``handles_tools_internally`` is ``True``.
  ``tool_call`` stream events surface as *informational* ExecutorEvents.
- **No ``preToolUse`` policy hook / native approval** — there is no local
  process to gate.
- **Persistent agent across turns** — within one Omnigent session the same
  cloud ``AsyncAgent`` is reused, so follow-up messages continue on the same
  branch / PR without opening a second one. The first turn seeds the agent with
  the full conversation history; subsequent turns send only the latest user
  message. Cancel (``interrupt_session``) calls ``run.cancel()`` on the
  in-flight cloud run so the user can stop a runaway agent.

What it shares with the local executor (imported, not reimplemented): the
``SDKMessage`` → ExecutorEvent mapping, model-id resolution drop logic, usage
normalization, and prompt building.
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Any, TypeAlias

from omnigent.cursor_cloud_repo import CursorCloudRepo
from omnigent.llms._usage_observer import notify_from_dict as _notify_usage_from_dict

from .cursor_executor import (
    _build_cursor_prompt,
    _normalize_cursor_usage,
    _safe_close,
    _sdk_message_to_events,
    _session_key,
)
from .datamodel import OSEnvSpec
from .executor import (
    Executor,
    ExecutorConfig,
    ExecutorError,
    ExecutorEvent,
    Message,
    TextChunk,
    ToolSpec,
    TurnCancelled,
    TurnComplete,
)

logger = logging.getLogger(__name__)

# Cloud agents run a curated model set in Max Mode; ``composer-2.5`` is the
# documented default. A gateway-routed (``databricks-*``) id carried by a spec
# authored for another harness is dropped to this default.
_DEFAULT_CLOUD_MODEL = "composer-2.5"

# Dashboard URL that triggers first-time repo environment setup. The Cursor API
# has no programmatic equivalent, so a never-onboarded repo fails to launch —
# we point the user here (see ``_onboarding_hint``).
_ONBOARD_URL = "https://cursor.com/onboard?repository={url}"

_SDKClient: TypeAlias = Any  # type: ignore[explicit-any]  # cursor_sdk.AsyncClient
_SDKAgent: TypeAlias = Any  # type: ignore[explicit-any]  # cursor_sdk.AsyncAgent
_SDKRun: TypeAlias = Any  # type: ignore[explicit-any]  # cursor_sdk.AsyncRun


def _resolve_cloud_model(model: str | None) -> str:
    """Resolve the cloud model id, dropping ids the cloud API can't honor.

    Mirrors :func:`cursor_executor._resolve_model` but defaults to the cloud
    model rather than the local ``auto`` select.
    """
    if not model or model.startswith(("databricks-", "databricks/")):
        if model:
            logger.warning(
                "CursorCloudExecutor: requested model %r is not a Cursor cloud "
                "model id; falling back to %r.",
                model,
                _DEFAULT_CLOUD_MODEL,
            )
        return _DEFAULT_CLOUD_MODEL
    return model


def _onboarding_hint(repo_url: str, error: str) -> str:
    """Augment a launch error with the dashboard-onboarding URL when relevant.

    Cloud runs fail if the repo has never had its environment set up in the
    Cursor dashboard, and there is no API to trigger it. When the failure looks
    like a setup/repo-access problem, append the onboarding URL so the user has
    an actionable next step rather than an opaque error.
    """
    lowered = error.lower()
    if any(
        token in lowered
        for token in ("set up", "setup", "not found", "repository", "repo", "access", "onboard")
    ):
        return (
            f"{error}\n\nIf this repository has not been set up for Cursor cloud "
            f"agents yet, complete the one-time environment setup at "
            f"{_ONBOARD_URL.format(url=repo_url)} (there is no API to trigger it)."
        )
    return error


@dataclass
class _CloudSessionState:
    """Per-Omnigent-conversation cloud SDK session state."""

    client: _SDKClient = None
    agent: _SDKAgent = None
    active_run: _SDKRun = None
    has_sent_prompt: bool = False


class CursorCloudExecutor(Executor):
    """Execute agent turns as Cursor Cloud / Background Agent runs."""

    def __init__(
        self,
        *,
        cwd: str | None = None,
        os_env: OSEnvSpec | None = None,
        model: str | None = None,
        api_key: str | None = None,
        repo_url: str | None = None,
        ref: str | None = None,
        agent_name: str | None = None,
        auto_create_pr: bool = True,
        extra_repos: list[CursorCloudRepo] | None = None,
    ) -> None:
        self._cwd = cwd or (os_env.cwd if os_env else None)
        self._model_override = model
        self._api_key = api_key
        self._repo_url = repo_url
        self._ref = ref
        self._agent_name = agent_name
        self._auto_create_pr = auto_create_pr
        self._extra_repos: list[CursorCloudRepo] = extra_repos or []
        self._session_states: dict[str, _CloudSessionState] = {}

    # ── capability flags ──────────────────────────────────────────────
    def supports_streaming(self) -> bool:
        return True

    def supports_tool_calling(self) -> bool:
        return True

    def handles_tools_internally(self) -> bool:
        # Tools execute in the cloud VM; the Session must not re-execute them.
        return True

    def supports_live_message_queue(self) -> bool:
        return False

    async def interrupt_session(self, session_key: str) -> bool:
        """Cancel the in-flight cloud run, if any.

        The runtime adapter passes the same session key that is attached to the
        messages, so cancel can target the per-session active run.
        """
        state = self._session_states.get(session_key)
        if state is None:
            return False
        run = state.active_run
        if run is None:
            return False
        try:
            await run.cancel()
            return True
        except Exception as exc:  # noqa: BLE001 — best-effort
            try:
                from cursor_sdk.errors import (  # lazy: optional dep
                    UnsupportedRunOperationError,
                )

                if isinstance(exc, UnsupportedRunOperationError):
                    return False  # already terminal — nothing to cancel
            except ImportError:
                pass
            logger.debug("CursorCloudExecutor: run cancel failed: %s", exc)
            return False

    def _format_result(self, response_text: str, result: Any) -> str | None:  # type: ignore[explicit-any]
        """Compose the final turn text: streamed summary + branch/PR links.

        Appends each pushed branch and its PR URL (when ``auto_create_pr``
        produced one) from ``RunResult.git.branches`` so the user gets the
        merge-ready artifact link in-conversation.
        """
        parts: list[str] = []
        body = response_text or getattr(result, "result", "") or ""
        if body:
            parts.append(body)
        git = getattr(result, "git", None)
        branches = getattr(git, "branches", None) or []
        links: list[str] = []
        for branch in branches:
            pr_url = getattr(branch, "pr_url", None)
            branch_name = getattr(branch, "branch", None)
            if pr_url:
                links.append(f"- PR: {pr_url}")
            elif branch_name:
                links.append(f"- Branch pushed: `{branch_name}` (no PR opened)")
        if links:
            parts.append("**Cloud agent result:**\n" + "\n".join(links))
        final = "\n\n".join(parts)
        return final or None

    async def run_turn(
        self,
        messages: list[Message],
        tools: list[ToolSpec],  # noqa: ARG002 — cloud runs its own tools; not bridged
        system_prompt: str,
        config: ExecutorConfig | None = None,
    ) -> AsyncIterator[ExecutorEvent]:
        if not self._repo_url:
            yield ExecutorError(
                message=(
                    "cursor-cloud has no repository to run against. Set a GitHub "
                    "repo via the cwd's 'origin' remote or an explicit override."
                )
            )
            return

        model = _resolve_cloud_model((config.model if config else None) or self._model_override)
        session_key = _session_key(messages)
        state = self._session_states.setdefault(session_key, _CloudSessionState())
        is_first_turn = not state.has_sent_prompt
        prompt = _build_cursor_prompt(
            messages, is_first_turn=is_first_turn, system_prompt=system_prompt
        )
        if not prompt:
            yield TurnComplete(response=None)
            return

        try:
            from cursor_sdk import (  # lazy: optional dependency
                AsyncClient,
                CloudAgentOptions,
                CloudRepository,
            )
        except ImportError as exc:
            yield ExecutorError(
                message=(
                    "CursorCloudExecutor requires the 'cursor-sdk' package. "
                    "Install it with: uv pip install 'omnigent[cursor]'"
                )
            )
            logger.debug("cursor-sdk import failed: %s", exc)
            return

        if state.agent is None:
            try:
                # Cloud runs route through the SDK's bundled bridge — the SAME
                # entry the local cursor harness uses. A direct
                # ``AsyncClient(base_url=...)`` hits the wrong RPC route (404).
                # The bridge authenticates from ``CURSOR_API_KEY``; mirror our
                # resolved key into the env before launching it and also pass
                # it to ``create_agent``.
                if self._api_key:
                    os.environ["CURSOR_API_KEY"] = self._api_key
                state.client = await AsyncClient.launch_bridge()
                repos = [CloudRepository(url=self._repo_url, starting_ref=self._ref)]
                repos += [
                    CloudRepository(url=r.url, starting_ref=r.ref) for r in self._extra_repos
                ]
                cloud = CloudAgentOptions(repos=repos, auto_create_pr=self._auto_create_pr)
                state.agent = await state.client.create_agent(
                    model=model,
                    api_key=self._api_key,
                    name=self._agent_name,
                    cloud=cloud,
                )
            except Exception as exc:  # noqa: BLE001 — launch failure surfaced w/ onboarding hint
                # A launch/create failure on cloud is most often the repo not
                # having had its one-time Cursor environment set up (the API can
                # return a bare ``internal error`` in that case), so always point
                # the user at the dashboard onboarding step. Drop the half-built
                # state so the next turn retries a fresh bridge.
                yield ExecutorError(
                    message=(
                        f"cursor-cloud launch failed: {exc}\n\nIf this repository "
                        f"has not been set up for Cursor cloud agents yet, complete "
                        f"the one-time environment setup at "
                        f"{_ONBOARD_URL.format(url=self._repo_url)} (there is no API "
                        f"to trigger it)."
                    ),
                    retryable=False,
                )
                await self.close_session(session_key)
                return

        response_text = ""
        turn_usage: dict[str, Any] | None = None  # type: ignore[explicit-any]  # SDK usage dict
        try:
            run = await state.agent.send(prompt)
            state.active_run = run
            async for stream_event in run.events():
                sdk_message = getattr(stream_event, "sdk_message", None)
                if sdk_message is not None:
                    for event in _sdk_message_to_events(sdk_message):
                        if isinstance(event, TextChunk):
                            response_text += event.text
                        yield event
                iu = getattr(stream_event, "interaction_update", None)
                if iu is not None and getattr(iu, "type", None) == "turn-ended":
                    raw_usage = getattr(iu, "usage", None)
                    if isinstance(raw_usage, dict) and raw_usage:
                        turn_usage = _normalize_cursor_usage(raw_usage, model)
            result = await run.wait()
            # Mark success only after the run completes cleanly so that a
            # mid-run failure (events/wait raises) leaves is_first_turn intact
            # and the retry re-seeds full history into a fresh agent.
            state.has_sent_prompt = True
        except Exception as exc:  # noqa: BLE001 — mid-run SDK failure surfaced as retryable
            yield ExecutorError(message=f"cursor-cloud run failed: {exc}", retryable=True)
            if not state.has_sent_prompt:
                await self.close_session(session_key)
            return
        finally:
            state.active_run = None

        status = str(getattr(result, "status", "") or "").lower()
        if status == "error":
            detail = getattr(result, "result", "") or "cursor-cloud run reported an error"
            yield ExecutorError(
                message=_onboarding_hint(self._repo_url, f"cursor-cloud run error: {detail}"),
                retryable=True,
            )
            return
        if status == "expired":
            detail = getattr(result, "result", "") or "cursor-cloud run expired"
            yield ExecutorError(message=f"cursor-cloud run expired: {detail}", retryable=True)
            return
        if status == "cancelled":
            yield TurnCancelled(reason="cursor-cloud run cancelled")
            return
        if status and status != "finished":
            detail = getattr(result, "result", "") or "unknown status"
            yield ExecutorError(
                message=f"cursor-cloud run returned non-finished status {status!r}: {detail}",
                retryable=True,
            )
            return

        if turn_usage:
            _notify_usage_from_dict(model=model, usage=turn_usage)
        yield TurnComplete(response=self._format_result(response_text, result), usage=turn_usage)

    async def _close_state(self, state: _CloudSessionState) -> None:
        await _safe_close(state.agent)
        state.agent = None
        await _safe_close(state.client)
        state.client = None

    async def close_session(self, session_key: str) -> None:
        state = self._session_states.pop(session_key, None)
        if state is not None:
            await self._close_state(state)

    async def close(self) -> None:
        for key in list(self._session_states.keys()):
            await self.close_session(key)
