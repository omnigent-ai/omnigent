from __future__ import annotations

import logging
from typing import Any

from omnigent_slack.approvals import ElicitationCoordinator, Verdict
from omnigent_slack.elicitation import (
    ElicitationController,
    ElicitationTurnState,
    PendingElicitation,
)
from omnigent_slack.events import ElicitationRequest
from omnigent_slack.models import SlackTurn, ThreadKey

_KEY = ThreadKey(team_id="T1", channel_id="C1", thread_ts="100.1")


class _RecordingOmnigent:
    """Records resolve_elicitation calls; optionally fails the decline POST."""

    def __init__(self, *, fail_resolve: bool = False) -> None:
        self.resolve_calls: list[tuple[str, str, bool]] = []
        self._fail_resolve = fail_resolve

    async def resolve_elicitation(
        self,
        session_id: str,
        elicitation_id: str,
        *,
        accepted: bool,
        content: dict[str, Any] | None = None,
    ) -> None:
        self.resolve_calls.append((session_id, elicitation_id, accepted))
        if self._fail_resolve:
            raise RuntimeError("resolve POST failed")


class _FakeSlack:
    def __init__(self) -> None:
        self.updates: list[dict[str, Any]] = []

    async def chat_update(self, **kwargs: Any) -> dict[str, Any]:
        self.updates.append(kwargs)
        return {"ok": True}


def _controller() -> ElicitationController:
    async def _post_reply(client: Any, key: ThreadKey, text: str) -> None:
        return None

    return ElicitationController(
        ElicitationCoordinator(),
        server_url="http://omnigent.test",
        post_reply=_post_reply,
        logger=logging.getLogger("test.elicitation"),
    )


def _turn() -> SlackTurn:
    return SlackTurn(
        key=_KEY,
        text="",
        user_id="U1",
        create_if_missing=False,
        title="",
        slack_client=_FakeSlack(),
        agent_id="",
        owner_user_id="U1",
    )


def _pending(**flags: Any) -> PendingElicitation:
    request = ElicitationRequest(elicitation_id="el_1", message="ok?", session_id="conv_1")
    return PendingElicitation(request=request, card_ts="200.1", **flags)


async def test_finish_pending_declines_when_timeout_decline_post_failed() -> None:
    # Regression: a timed-out elicitation whose decline POST ALSO failed leaves
    # the server parked (timed_out=True AND delivery_failed=True). finish_pending
    # must still re-decline to release the park — timed_out must not mask
    # delivery_failed and skip the release, or the session wedges.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    turn = _turn()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending(timed_out=True, delivery_failed=True)

    await controller.finish_pending(omnigent, turn, state)  # type: ignore[arg-type]

    # The park was released with a best-effort decline.
    assert omnigent.resolve_calls == [("conv_1", "el_1", False)]


async def test_finish_pending_no_redecline_when_timeout_decline_succeeded() -> None:
    # A clean timeout (decline POST landed) must NOT be re-declined — the park is
    # already released; a second POST would be wasteful.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending(timed_out=True)

    await controller.finish_pending(omnigent, _turn(), state)  # type: ignore[arg-type]

    assert omnigent.resolve_calls == []


async def test_finish_pending_declines_unanswered() -> None:
    # A card left genuinely unanswered (server still parked) is declined.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending()

    await controller.finish_pending(omnigent, _turn(), state)  # type: ignore[arg-type]

    assert omnigent.resolve_calls == [("conv_1", "el_1", False)]


async def test_finish_pending_no_redecline_when_verdict_delivered() -> None:
    # A delivered verdict already released the park — no re-decline.
    omnigent = _RecordingOmnigent()
    controller = _controller()
    state = ElicitationTurnState()
    state.pending["el_1"] = _pending(verdict=Verdict(accepted=True))

    await controller.finish_pending(omnigent, _turn(), state)  # type: ignore[arg-type]

    assert omnigent.resolve_calls == []
