"""Setup-modal submits with malformed team/user identifiers must not ack success.

A Slack ``view_submission`` whose envelope is missing the team or user id
cannot be stored (configs key on ``(team, user)``), so the submit handler must
keep the modal open with an explicit error. Acking a bare success closes the
modal as if setup worked while nothing was saved — the user gets no
confirmation DM and the next mention just re-prompts setup with no
explanation.
"""

from pathlib import Path
from typing import Any

import pytest
from omnigent_slack.omnigent import OmnigentClientPool
from omnigent_slack.setup import (
    AGENT_ACTION,
    AGENT_BLOCK,
    HOST_ACTION,
    HOST_BLOCK,
    WORKSPACE_ACTION,
    WORKSPACE_BLOCK,
    SetupFlow,
)
from omnigent_slack.store import SQLiteStore

_SERVER = "http://omnigent.test"


class FakeAck:
    """Captures the kwargs slack_bolt handlers pass to ack()."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def __call__(self, **kwargs: Any) -> None:
        self.calls.append(kwargs)


class FakeSetupClient:
    """Records the Slack Web API calls the handler makes."""

    def __init__(self) -> None:
        self.posts: list[dict[str, Any]] = []

    async def conversations_open(self, **kwargs: Any) -> dict[str, Any]:
        return {"channel": {"id": "D123"}}

    async def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posts.append(kwargs)
        return {"ok": True, "ts": "1"}


def _completed_select_view() -> dict[str, Any]:
    """The setup modal with every input completed, as Slack submits it."""
    return {
        "state": {
            "values": {
                AGENT_BLOCK: {
                    AGENT_ACTION: {
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Helper"},
                            "value": "ag_1",
                        }
                    }
                },
                HOST_BLOCK: {
                    HOST_ACTION: {
                        "selected_option": {
                            "text": {"type": "plain_text", "text": "Host One"},
                            "value": "h1",
                        }
                    }
                },
                WORKSPACE_BLOCK: {WORKSPACE_ACTION: {"value": "/home/me/project"}},
            }
        },
    }


@pytest.mark.parametrize(
    "body",
    [
        {},
        {"team": {"id": ""}, "user": {"id": ""}},
        {"team": {"id": "T1"}},
        {"user": {"id": "U1"}},
    ],
    ids=["empty-envelope", "empty-ids", "missing-user", "missing-team"],
)
async def test_select_submit_missing_identity_surfaces_error(
    tmp_path: Path, body: dict[str, Any]
) -> None:
    store = SQLiteStore(tmp_path / "store.sqlite3")
    await store.initialize()
    pool = OmnigentClientPool()
    flow = SetupFlow(store=store, pool=pool, server_url=_SERVER, auth_manager=None)
    ack = FakeAck()
    client = FakeSetupClient()

    try:
        await flow._handle_select_submit(ack, body, _completed_select_view(), client)
    finally:
        await pool.aclose_all()

    # The submission cannot be saved, so the modal must not close as a
    # success: the ack has to carry an explicit response_action (an inline
    # error or an error view), never a bare success ack.
    assert ack.calls, "submit handler never acked the view_submission"
    assert ack.calls[0].get("response_action") in ("errors", "update"), (
        f"malformed submit was acked as success ({ack.calls[0]!r}) — the modal "
        "closes as if setup succeeded while nothing was stored"
    )

    # Nothing may be persisted under a collapsed/empty identity key.
    team_id = str((body.get("team") or {}).get("id") or "")
    user_id = str((body.get("user") or {}).get("id") or "")
    assert await store.get_user_config(team_id, user_id) is None

    # And no "you're set up!" confirmation DM may be sent.
    assert client.posts == []
