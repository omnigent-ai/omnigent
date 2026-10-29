"""Requester evidence owned by one native harness executor, queried over control IPC.

Provider IDs locate consumed input; they are not credentials. The caller must
authenticate registrations separately from the model-facing MCP relay.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import uuid4

from omnigent.harnesses.codex_native.execution import CodexExecutionIdentity
from omnigent.runtime.requester import ExecutionRequester, InputRequester, ToolRequester


@dataclass
class _Submission:
    thread_id: str
    expected_turn_id: str | None
    origin: InputRequester
    consumed_by: str | None = None


class CodexRequesterRegistry:
    """Retain inputs and execution restrictions in an explicitly owned instance.

    At capacity, retire consumed registrations together with execution records.
    Retired IDs become unknown, including in delayed or older full snapshots.
    Unresolved inputs remain barriers until consumption or a proven refusal;
    if they alone fill the limit, admission requires resolution or scope replacement.

    A full thread/read checks copied IDs, but is not an immutable history:
    rollback can remove turns, and absent input may still be queued. Absence
    therefore never retires a registration or proves that pending input is safe.
    """

    _MAX_RECORDS = 8192

    def __init__(self) -> None:
        self.revision = 0
        self._submissions: dict[str, _Submission] = {}
        self._executions: dict[CodexExecutionIdentity, ExecutionRequester] = {}

    def begin(self, origin: InputRequester, thread_id: str, expected_turn_id: str | None) -> str:
        """Register before submission; starts may race into an existing turn."""
        if max(len(self._submissions), len(self._executions)) >= self._MAX_RECORDS:
            self._retire_consumed()
        if len(self._submissions) >= self._MAX_RECORDS:
            raise RuntimeError("Native requester capacity exhausted by unresolved inputs")
        client_id = uuid4().hex
        self._submissions[client_id] = _Submission(thread_id, expected_turn_id, origin)
        self.revision += 1
        return client_id

    def _retire_consumed(self) -> None:
        self._submissions = {
            client_id: submission
            for client_id, submission in self._submissions.items()
            if submission.consumed_by is None
        }
        # A retained ambiguous input must not reconstruct an evicted execution's authority.
        for submission in self._submissions.values():
            submission.origin = InputRequester()
        self._executions.clear()
        self.revision += 1

    def finish(self, client_id: str, *, refused: bool) -> None:
        """Release proven refusals; acknowledgements cannot establish consumption."""
        submission = self._submissions.get(client_id)
        if submission is None:
            raise ValueError("Native input registration is missing")
        if refused:
            del self._submissions[client_id]
            self.revision += 1

    def snapshot(
        self, identity: CodexExecutionIdentity, response: object, *, revision: int
    ) -> ToolRequester | None:
        """Resolve a complete thread/read obtained after the supplied revision.

        Registrations fence concurrent reads. Missing input, copied client IDs,
        and mixed accounts cannot establish a requester for the execution.
        """
        if revision != self.revision or not isinstance(response, dict):
            return None
        result = response.get("result")
        thread = result.get("thread") if isinstance(result, dict) else None
        if (
            not isinstance(thread, dict)
            or thread.get("id") != identity.thread_id
            or thread.get("sessionId") != identity.session_id
        ):
            return None
        turns = thread.get("turns")
        if not isinstance(turns, list):
            return None
        target_items: list[object] | None = None
        consumed: dict[str, list[str]] = {}
        for turn in turns:
            if not isinstance(turn, dict) or turn.get("itemsView", "full") != "full":
                return None
            turn_id, items = turn.get("id"), turn.get("items")
            if not isinstance(turn_id, str) or not isinstance(items, list):
                return None
            if turn_id == identity.turn_id:
                if target_items is not None:
                    return None
                target_items = items
            for item in items:
                if isinstance(item, dict) and item.get("type") == "userMessage":
                    client_id = item.get("clientId")
                    if isinstance(client_id, str):
                        consumed.setdefault(client_id, []).append(turn_id)
        if target_items is None:
            return None
        origins: list[InputRequester] = []
        for item in target_items:
            if not isinstance(item, dict) or not isinstance(item.get("type"), str):
                return None
            if item["type"] != "userMessage":
                continue
            client_id = item.get("clientId")
            submission = self._submissions.get(client_id) if isinstance(client_id, str) else None
            if (
                not isinstance(client_id, str)
                or submission is None
                or submission.thread_id != identity.thread_id
                or consumed.get(client_id) != [identity.turn_id]
                or submission.expected_turn_id not in (None, identity.turn_id)
                or submission.consumed_by not in (None, identity.turn_id)
            ):
                origins.append(InputRequester())
            else:
                origins.append(submission.origin)
        if not origins:
            return None
        execution = self._executions.get(identity)
        if execution is None:
            if len(self._executions) >= self._MAX_RECORDS:
                return None
            execution = ExecutionRequester(origins[0])
            self._executions[identity] = execution
        for origin in origins:
            execution.consume(origin)
        for client_id, submission in self._submissions.items():
            if submission.thread_id != identity.thread_id:
                continue
            locations = consumed.get(client_id, [])
            # Another resolver may already have observed a newer consumption view.
            if submission.consumed_by == identity.turn_id and locations != [identity.turn_id]:
                return ToolRequester(execution_id=execution.execution_id, origin=InputRequester())
            if len(locations) == 1 and submission.consumed_by in (None, locations[0]):
                submission.consumed_by = locations[0]
            elif locations:
                submission.consumed_by = None
            if submission.consumed_by is None and submission.expected_turn_id in (
                None,
                identity.turn_id,
            ):
                return ToolRequester(execution_id=execution.execution_id, origin=InputRequester())
        return execution.snapshot()
