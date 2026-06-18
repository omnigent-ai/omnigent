"""Codex-native goal control helpers for the runner app.

The AP server owns the public ``/v1/sessions/{id}/codex_goal`` routes, but the
actual goal state lives inside Codex app-server for the loaded Codex thread.
This module keeps the runner-side JSON-RPC forwarding and direct ``/events``
validation out of the already-large ``omnigent.runner.app`` module.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import Any, Protocol

from fastapi.responses import JSONResponse, Response


class BridgeStateForSession(Protocol):
    """Callable that resolves a live Codex app-server bridge state."""

    async def __call__(
        self,
        conv_id: str,
        *,
        action: str,
        missing_state_log_level: int = logging.WARNING,
    ) -> Any | None: ...


class ClientSafeErrorDetail(Protocol):
    """Callable that logs an exception and returns safe client-facing detail."""

    def __call__(self, exc: BaseException, *, context: str) -> str: ...


class CodexGoalRunner:
    """Forward Codex goal controls from runner events to Codex app-server."""

    def __init__(
        self,
        *,
        bridge_state_for_session: BridgeStateForSession,
        client_safe_error_detail: ClientSafeErrorDetail,
        logger: logging.Logger,
    ) -> None:
        self._bridge_state_for_session = bridge_state_for_session
        self._client_safe_error_detail = client_safe_error_detail
        self._logger = logger

    @staticmethod
    def _goal_to_api(goal: dict[str, Any]) -> dict[str, Any]:
        """
        Convert a Codex app-server goal object to Omnigent API field names.

        Codex app-server speaks camelCase JSON-RPC fields. The AP/UI session
        API uses snake_case, so normalize the goal at the runner boundary
        before returning it through ``/events``.
        """
        return {
            "thread_id": goal["threadId"],
            "objective": goal["objective"],
            "status": goal["status"],
            "token_budget": goal.get("tokenBudget"),
            "tokens_used": goal["tokensUsed"],
            "time_used_seconds": goal["timeUsedSeconds"],
            "created_at": goal.get("createdAt"),
            "updated_at": goal.get("updatedAt"),
        }

    @staticmethod
    def _goal_result(response: dict[str, Any], *, action: str) -> dict[str, Any]:
        """Extract a JSON-RPC ``result`` object from a Codex goal response."""
        result = response.get("result")
        if not isinstance(result, dict):
            raise ValueError(f"Codex goal {action} returned no result object")
        return result

    @staticmethod
    def _bridge_error(action: str) -> JSONResponse:
        """Build the standard no-bridge response for Codex goal operations."""
        return JSONResponse(
            status_code=503,
            content={
                "error": "codex_native_goal_failed",
                "detail": (f"Codex-native goal {action} requires a loaded Codex bridge."),
            },
        )

    @staticmethod
    def _malformed_response(action: str, detail: str) -> JSONResponse:
        """Build the standard malformed-response error for Codex goal operations."""
        return JSONResponse(
            status_code=503,
            content={
                "error": "codex_native_goal_failed",
                "detail": f"Codex-native goal {action} returned {detail}.",
            },
        )

    async def _request(
        self,
        conv_id: str,
        *,
        action: str,
        method: str,
        params: dict[str, Any],
    ) -> dict[str, Any] | JSONResponse:
        """Execute one Codex app-server goal JSON-RPC request."""
        from omnigent.codex_native_app_server import client_for_transport

        state = await self._bridge_state_for_session(conv_id, action=f"goal {action}")
        if state is None:
            return self._bridge_error(action)
        codex_client = client_for_transport(
            state.socket_path,
            client_name="omnigent-codex-native-runner",
        )
        try:
            await codex_client.connect()
            return self._goal_result(
                await codex_client.request(method, {"threadId": state.thread_id, **params}),
                action=action,
            )
        except Exception as exc:  # noqa: BLE001 - surface app-server goal failures to AP.
            self._logger.warning(
                "Codex-native %s failed for session=%s",
                method,
                conv_id,
                exc_info=True,
            )
            return JSONResponse(
                status_code=503,
                content={
                    "error": "codex_native_goal_failed",
                    "detail": self._client_safe_error_detail(
                        exc,
                        context=f"codex-native goal {action}",
                    ),
                },
            )
        finally:
            with contextlib.suppress(Exception):
                await codex_client.close()

    async def get(self, conv_id: str) -> Response:
        """Read the current Codex app-server goal for a loaded thread."""
        result = await self._request(
            conv_id,
            action="read",
            method="thread/goal/get",
            params={},
        )
        if isinstance(result, JSONResponse):
            return result
        goal = result.get("goal")
        if goal is not None and not isinstance(goal, dict):
            return self._malformed_response("read", "invalid goal object")
        return JSONResponse({"goal": None if goal is None else self._goal_to_api(goal)})

    async def set(
        self,
        conv_id: str,
        *,
        objective: str,
        token_budget: int | None,
        token_budget_provided: bool,
        status: str | None,
    ) -> Response:
        """Create or replace the current Codex app-server goal for a thread."""
        params: dict[str, Any] = {
            "objective": objective,
        }
        if token_budget_provided:
            params["tokenBudget"] = token_budget
        if status is not None:
            params["status"] = status
        result = await self._request(
            conv_id,
            action="set",
            method="thread/goal/set",
            params=params,
        )
        if isinstance(result, JSONResponse):
            return result
        goal = result.get("goal")
        if not isinstance(goal, dict):
            return self._malformed_response("set", "invalid goal object")
        return JSONResponse({"goal": self._goal_to_api(goal)})

    async def update_status(self, conv_id: str, *, status: str) -> Response:
        """
        Update the current Codex app-server goal status.

        Codex models pause/resume as ``thread/goal/set`` calls with only the
        status field. Objective and token-budget fields are omitted so Codex
        keeps the existing goal text and budget.
        """
        result = await self._request(
            conv_id,
            action="update status",
            method="thread/goal/set",
            params={"status": status},
        )
        if isinstance(result, JSONResponse):
            return result
        goal = result.get("goal")
        if not isinstance(goal, dict):
            return self._malformed_response(
                "update status",
                "invalid goal object",
            )
        return JSONResponse({"goal": self._goal_to_api(goal)})

    async def clear(self, conv_id: str) -> Response:
        """Clear the current Codex app-server goal for a loaded thread."""
        result = await self._request(
            conv_id,
            action="clear",
            method="thread/goal/clear",
            params={},
        )
        if isinstance(result, JSONResponse):
            return result
        cleared = result.get("cleared")
        if not isinstance(cleared, bool):
            return self._malformed_response("clear", "invalid cleared flag")
        return JSONResponse({"cleared": cleared})

    async def handle_event(
        self,
        conv_id: str,
        body_type: str | None,
        body: Any,
        *,
        session_harness_name: Callable[[str], str | None],
    ) -> Response | None:
        """Handle direct runner ``/events`` payloads for Codex goal controls."""
        if body_type not in {"goal_get", "goal_set", "goal_status", "goal_clear"}:
            return None

        if session_harness_name(conv_id) != "codex-native":
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_input",
                    "detail": "Codex goal controls require a codex-native session",
                },
            )

        if body_type == "goal_get":
            return await self.get(conv_id)

        if body_type == "goal_clear":
            return await self.clear(conv_id)

        if body_type == "goal_set":
            # The AP route validates this too, but direct runner callers should
            # get a clear 400 instead of an app-server validation error.
            objective = body.get("objective") if isinstance(body, dict) else None
            if not isinstance(objective, str) or not objective.strip():
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_input",
                        "detail": "Body 'objective' must be a non-empty string",
                    },
                )
            token_budget_provided = isinstance(body, dict) and "token_budget" in body
            token_budget = body.get("token_budget") if token_budget_provided else None
            if token_budget is not None and (
                isinstance(token_budget, bool)
                or not isinstance(token_budget, int)
                or token_budget <= 0
            ):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_input",
                        "detail": "Body 'token_budget' must be a positive integer or null",
                    },
                )
            status = body.get("status") if isinstance(body, dict) else None
            if status is not None and status not in ("active", "paused"):
                return JSONResponse(
                    status_code=400,
                    content={
                        "error": "invalid_input",
                        "detail": "Body 'status' must be 'active' or 'paused'",
                    },
                )
            return await self.set(
                conv_id,
                objective=objective.strip(),
                token_budget=token_budget,
                token_budget_provided=token_budget_provided,
                status=status,
            )

        status = body.get("status") if isinstance(body, dict) else None
        if status not in ("active", "paused"):
            return JSONResponse(
                status_code=400,
                content={
                    "error": "invalid_input",
                    "detail": "Body 'status' must be 'active' or 'paused'",
                },
            )
        return await self.update_status(conv_id, status=status)
