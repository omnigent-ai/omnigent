"""Crash-aware coordinator for the durable server-to-runner turn protocol."""

from __future__ import annotations

import asyncio
import json
import re
from dataclasses import dataclass
from typing import Any

import httpx

from omnigent.entities import TURN_OPERATION_TERMINAL_STATES, TurnOperation
from omnigent.stores.turn_operation_store import TurnOperationStateError, TurnOperationStore

_RUNNER_INCARNATION_RE = re.compile(r"^[0-9a-f]{32}$")
_RUNNER_ACTIVE_STATES = frozenset({"accepted", "running"})
_RUNNER_TERMINAL_STATES = frozenset({"succeeded", "failed", "cancelled"})


class TurnOperationCoordinatorError(RuntimeError):
    """Base class for coordinator failures that preserve durable state."""


class RunnerOperationProtocolError(TurnOperationCoordinatorError):
    """The runner did not honor the operation protocol contract."""


class RunnerOperationUnavailable(TurnOperationCoordinatorError):
    """The runner could not be reached before a dispatch attempt."""


class RunnerOperationRejected(TurnOperationCoordinatorError):
    """The runner definitively rejected a request before accepting it."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code


class RunnerOperationDispatchUnknown(TurnOperationCoordinatorError):
    """The request may have reached the runner and must be reconciled."""

    def __init__(self, operation: TurnOperation, detail: str) -> None:
        super().__init__(detail)
        self.operation = operation


@dataclass(frozen=True)
class _RunnerStatus:
    operation_id: str
    session_id: str
    state: str
    runner_incarnation_id: str


class TurnOperationCoordinator:
    """Bind durable operations to one runner incarnation and reconcile safely.

    Every dispatch preflights the runner status endpoint to capture its current
    incarnation before sending a potentially billable turn. A transport error
    after POST becomes ``dispatch_unknown``. An exact retry is permitted only
    against the same incarnation, where the runner registry deduplicates it.
    Missing state after an incarnation change is terminal ambiguity, never a
    signal to redispatch.
    """

    def __init__(
        self,
        store: TurnOperationStore,
        runner_client: httpx.AsyncClient,
        *,
        timeout: float = 10.0,
    ) -> None:
        if timeout <= 0:
            raise ValueError("turn operation timeout must be positive")
        self._store = store
        self._runner_client = runner_client
        self._timeout = timeout

    async def dispatch(
        self,
        operation_id: str,
        session_id: str,
    ) -> TurnOperation:
        """Dispatch or exact-replay one input-persisted operation."""
        operation = await self._get_required(operation_id, session_id)
        if operation.state in TURN_OPERATION_TERMINAL_STATES:
            return operation
        if operation.state == "accepted":
            raise TurnOperationStateError("operation input is not durably persisted")

        preflight = await self._preflight(operation_id)
        if operation.runner_incarnation_id is not None and (
            operation.runner_incarnation_id != preflight.runner_incarnation_id
        ):
            return await self._terminalize_restart_ambiguity(operation)

        if preflight.state != "not_found":
            if operation.runner_incarnation_id is None:
                raise RunnerOperationProtocolError(
                    "runner reports an operation before any recorded dispatch attempt"
                )
            return await self._apply_runner_status(operation, preflight)
        if operation.state == "dispatched":
            return await self._terminalize_missing_dispatched(operation)

        operation = await asyncio.to_thread(
            self._store.record_dispatch_attempt,
            operation_id,
            preflight.runner_incarnation_id,
        )
        payload = self._dispatch_payload(operation)
        payload["operation_id"] = operation_id
        try:
            response = await self._runner_client.post(
                f"/v1/sessions/{session_id}/events",
                json=payload,
                timeout=self._timeout,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            unknown = await self._mark_dispatch_unknown(
                operation_id,
                error_code="runner_transport_unknown",
                error="runner acceptance was not observed",
            )
            raise RunnerOperationDispatchUnknown(
                unknown,
                "runner acceptance is unknown; reconcile before retry",
            ) from exc

        if response.status_code >= 400:
            detail = self._response_detail(response)
            raise RunnerOperationRejected(response.status_code, detail)

        try:
            runner_status = self._parse_status_response(response)
            self._validate_status_binding(operation, runner_status)
        except RunnerOperationProtocolError as exc:
            unknown = await self._mark_dispatch_unknown(
                operation_id,
                error_code="runner_protocol_unknown",
                error=str(exc),
            )
            raise RunnerOperationDispatchUnknown(unknown, str(exc)) from exc
        return await self._apply_runner_status(operation, runner_status)

    async def reconcile(self, operation_id: str, session_id: str) -> TurnOperation:
        """Observe runner state without starting or replaying a turn."""
        operation = await self._get_required(operation_id, session_id)
        if operation.state in TURN_OPERATION_TERMINAL_STATES:
            return operation
        if operation.runner_incarnation_id is None:
            return operation
        status = await self._preflight(operation_id)
        if status.runner_incarnation_id != operation.runner_incarnation_id:
            return await self._terminalize_restart_ambiguity(operation)
        if status.state == "not_found":
            if operation.state == "dispatched":
                return await self._terminalize_missing_dispatched(operation)
            return operation
        self._validate_status_binding(operation, status)
        return await self._apply_runner_status(operation, status)

    async def _get_required(self, operation_id: str, session_id: str) -> TurnOperation:
        operation = await asyncio.to_thread(self._store.get, operation_id)
        if operation is None:
            raise KeyError(operation_id)
        if operation.conversation_id != session_id:
            raise TurnOperationStateError("operation is bound to another session")
        return operation

    @staticmethod
    def _dispatch_payload(operation: TurnOperation) -> dict[str, Any]:
        if operation.dispatch_request_json is None or operation.dispatch_request_hash is None:
            raise TurnOperationStateError("operation has no durable runner dispatch request")
        try:
            payload = json.loads(operation.dispatch_request_json)
        except (TypeError, ValueError) as exc:
            raise TurnOperationStateError("durable runner dispatch request is invalid") from exc
        if not isinstance(payload, dict):
            raise TurnOperationStateError("durable runner dispatch request is not an object")
        return payload

    async def _preflight(self, operation_id: str) -> _RunnerStatus:
        try:
            response = await self._runner_client.get(
                f"/v1/turn-operations/{operation_id}",
                timeout=self._timeout,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise RunnerOperationUnavailable("runner status preflight failed") from exc
        if response.status_code == 200:
            return self._parse_status_response(response)
        if response.status_code == 404:
            payload = self._json_object(response)
            incarnation = self._require_incarnation(payload)
            if payload.get("error") != "operation_not_found":
                raise RunnerOperationProtocolError("runner 404 omitted operation_not_found")
            return _RunnerStatus(
                operation_id=operation_id,
                session_id="",
                state="not_found",
                runner_incarnation_id=incarnation,
            )
        raise RunnerOperationProtocolError(
            f"runner operation preflight returned status {response.status_code}"
        )

    async def _apply_runner_status(
        self,
        operation: TurnOperation,
        status: _RunnerStatus,
    ) -> TurnOperation:
        if status.state == "not_found":
            return operation
        current = operation
        if current.state in {"input_persisted", "dispatch_unknown"}:
            current = await asyncio.to_thread(
                self._store.mark_dispatch_result,
                current.id,
                state="dispatched",
            )
        if status.state in _RUNNER_ACTIVE_STATES:
            return current
        return await asyncio.to_thread(
            self._store.mark_terminal,
            current.id,
            state=status.state,
        )

    async def _mark_dispatch_unknown(
        self,
        operation_id: str,
        *,
        error_code: str,
        error: str,
    ) -> TurnOperation:
        return await asyncio.to_thread(
            self._store.mark_dispatch_result,
            operation_id,
            state="dispatch_unknown",
            error_code=error_code,
            error=error,
        )

    async def _terminalize_restart_ambiguity(
        self,
        operation: TurnOperation,
    ) -> TurnOperation:
        current = operation
        if current.state == "input_persisted":
            current = await self._mark_dispatch_unknown(
                current.id,
                error_code="runner_restart_ambiguous",
                error="runner incarnation changed after a recorded dispatch attempt",
            )
        return await asyncio.to_thread(
            self._store.mark_terminal,
            current.id,
            state="timed_out",
            error_code="runner_restart_ambiguous",
            error="runner incarnation changed; dispatch outcome cannot be proven",
        )

    async def _terminalize_missing_dispatched(
        self,
        operation: TurnOperation,
    ) -> TurnOperation:
        return await asyncio.to_thread(
            self._store.mark_terminal,
            operation.id,
            state="timed_out",
            error_code="runner_operation_missing",
            error="bound runner no longer reports an operation it accepted",
        )

    @classmethod
    def _parse_status_response(cls, response: httpx.Response) -> _RunnerStatus:
        payload = cls._json_object(response)
        operation_id = payload.get("operation_id")
        session_id = payload.get("session_id")
        state = payload.get("state")
        if not isinstance(operation_id, str) or not _RUNNER_INCARNATION_RE.fullmatch(operation_id):
            raise RunnerOperationProtocolError("runner status has an invalid operation_id")
        if not isinstance(session_id, str) or not session_id:
            raise RunnerOperationProtocolError("runner status has an invalid session_id")
        if not isinstance(state, str) or state not in (
            _RUNNER_ACTIVE_STATES | _RUNNER_TERMINAL_STATES
        ):
            raise RunnerOperationProtocolError("runner status has an invalid state")
        return _RunnerStatus(
            operation_id=operation_id,
            session_id=session_id,
            state=state,
            runner_incarnation_id=cls._require_incarnation(payload),
        )

    @staticmethod
    def _validate_status_binding(
        operation: TurnOperation,
        status: _RunnerStatus,
    ) -> None:
        if status.operation_id != operation.id or status.session_id != operation.conversation_id:
            raise RunnerOperationProtocolError("runner status is bound to another operation")
        if (
            operation.runner_incarnation_id is not None
            and operation.runner_incarnation_id != status.runner_incarnation_id
        ):
            raise RunnerOperationProtocolError("runner status changed incarnation")

    @staticmethod
    def _json_object(response: httpx.Response) -> dict[str, Any]:
        try:
            payload = response.json()
        except (ValueError, TypeError) as exc:
            raise RunnerOperationProtocolError("runner returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RunnerOperationProtocolError("runner returned a non-object response")
        return payload

    @staticmethod
    def _require_incarnation(payload: dict[str, Any]) -> str:
        incarnation = payload.get("runner_incarnation_id")
        if not isinstance(incarnation, str) or not _RUNNER_INCARNATION_RE.fullmatch(incarnation):
            raise RunnerOperationProtocolError("runner response has an invalid incarnation id")
        return incarnation

    @staticmethod
    def _response_detail(response: httpx.Response) -> str:
        try:
            payload = response.json()
        except (ValueError, TypeError):
            return f"runner rejected operation with status {response.status_code}"
        if isinstance(payload, dict):
            detail = payload.get("detail")
            if isinstance(detail, str) and detail.strip():
                return detail.strip()[:500]
        return f"runner rejected operation with status {response.status_code}"
