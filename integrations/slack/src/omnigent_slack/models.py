from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ThreadKey:
    team_id: str
    channel_id: str
    thread_ts: str

    @classmethod
    def from_event(cls, team_id: str, event: dict[str, object]) -> ThreadKey:
        channel_id = str(event["channel"])
        thread_ts = str(event.get("thread_ts") or event["ts"])
        return cls(team_id=team_id, channel_id=channel_id, thread_ts=thread_ts)

    def display(self) -> str:
        return f"{self.team_id}:{self.channel_id}:{self.thread_ts}"


@dataclass(frozen=True, slots=True)
class SlackTurn:
    key: ThreadKey
    text: str
    user_id: str
    create_if_missing: bool
    title: str
    slack_client: Any
