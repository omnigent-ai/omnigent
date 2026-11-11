from __future__ import annotations

import json
import os
from collections import defaultdict
from dataclasses import dataclass, replace
from pathlib import Path
from threading import Lock

from omnigent.util.json_types import JsonObject

HISTORICAL_SKILL_MARKER_PREFIX = "omnigent-skill-history:"
_SELECTED_SKILL_KIND = "skills.selected_skill_instructions"


@dataclass(frozen=True)
class SkillExpansion:
    item_id: str
    content: list[JsonObject]


UserMessageId = tuple[str, str]


@dataclass(frozen=True)
class RolloutUser:
    ids: frozenset[UserMessageId]
    expansions: tuple[SkillExpansion, ...] = ()


def user_message_ids(item_id: object, client_id: object) -> frozenset[UserMessageId]:
    return frozenset(
        (kind, value)
        for kind, value in (("item", item_id), ("client", client_id))
        if isinstance(value, str) and value
    )


class CodexSkillRollout:
    def __init__(self, path: Path, thread_id: str) -> None:
        self.path = path
        self.thread_id = thread_id
        self._lock = Lock()
        self._file_id: tuple[int, int] | None = None
        self._offset = 0
        self._legacy_turn_id: str | None = None
        self._users: dict[str, list[RolloutUser]] = defaultdict(list)

    def read(self, turn_id: str) -> list[RolloutUser]:
        with self._lock, self.path.open("rb") as stream:
            stat = os.fstat(stream.fileno())
            file_id = (stat.st_dev, stat.st_ino)
            if self._file_id != file_id or stat.st_size < self._offset:
                self._file_id = file_id
                self._offset = 0
                self._legacy_turn_id = None
                self._users.clear()
            stream.seek(self._offset)
            for line in stream:
                if not line.endswith(b"\n"):
                    break
                offset = self._offset
                self._offset += len(line)
                try:
                    record = json.loads(line)
                except (json.JSONDecodeError, UnicodeDecodeError):
                    continue
                self._index(record, offset)
            return list(self._users.get(turn_id, ()))

    def _index(self, record: object, offset: int) -> None:
        if not isinstance(record, dict) or not isinstance(payload := record.get("payload"), dict):
            return
        if record.get("type") == "event_msg":
            kind = payload.get("type")
            turn_id = payload.get("turn_id")
            item = payload.get("item")
            if kind == "task_started" and isinstance(turn_id, str):
                self._legacy_turn_id = turn_id
            elif kind in ("task_complete", "turn_aborted"):
                self._legacy_turn_id = None
            elif kind == "user_message" and self._legacy_turn_id is not None:
                self._users[self._legacy_turn_id].append(
                    RolloutUser(user_message_ids(None, payload.get("client_id")))
                )
            elif (
                kind == "item_completed"
                and isinstance(item, dict)
                and item.get("type") == "UserMessage"
                and isinstance(turn_id, str)
            ):
                self._users[turn_id].append(
                    RolloutUser(user_message_ids(item.get("id"), item.get("client_id")))
                )
            return
        if (
            record.get("type") != "response_item"
            or payload.get("type") != "message"
            or payload.get("role") != "user"
        ):
            return
        metadata = payload.get("internal_chat_message_metadata_passthrough")
        if not isinstance(metadata, dict) or not isinstance(
            turn_id := metadata.get("turn_id"), str
        ):
            return
        content = payload.get("content")
        if (
            not isinstance(content, list)
            or not content
            or metadata.get("content_item_kinds") != [_SELECTED_SKILL_KIND] * len(content)
            or not all(
                isinstance(block, dict)
                and block.get("type") == "input_text"
                and isinstance(block.get("text"), str)
                for block in content
            )
        ):
            return
        users = self._users.get(turn_id)
        if not users:
            return
        item_id = payload.get("id")
        expansion = SkillExpansion(
            item_id if isinstance(item_id, str) and item_id else f"skill-rollout:{offset}",
            content,
        )
        user = users[-1]
        users[-1] = replace(user, expansions=(*user.expansions, expansion))
