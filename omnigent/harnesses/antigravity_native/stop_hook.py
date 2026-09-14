"""Record agy's native Stop hook without exposing the session transcript."""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
import uuid
from pathlib import Path

from omnigent.harnesses.antigravity_native.transcript import transcript_boundary

STOP_EVENTS_FILE = "stop-events.jsonl"


def record_stop_event(bridge_dir: Path, payload: object) -> bool:
    """Append only the fields needed to close a mirrored turn."""
    if not isinstance(payload, dict):
        return False
    conversation_id = payload.get("conversationId")
    if not isinstance(conversation_id, str):
        return False
    try:
        if str(uuid.UUID(conversation_id)) != conversation_id:
            return False
    except ValueError:
        return False
    if payload.get("fullyIdle") is not True:
        return False
    reason = payload.get("terminationReason")
    event: dict[str, object] = {
        "conversation_id": conversation_id,
        "fully_idle": True,
        "failed": bool(payload.get("error")) or reason == "ERROR",
        "cancelled": reason == "USER_CANCELED",
    }
    boundary = transcript_boundary(bridge_dir, conversation_id)
    event["transcript_boundary"] = list(boundary) if boundary is not None else None
    path = bridge_dir / STOP_EVENTS_FILE
    fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(event, separators=(",", ":")) + "\n")
    return True


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--bridge-dir", type=Path, required=True)
    args = parser.parse_args()
    with contextlib.suppress(OSError, ValueError):
        record_stop_event(args.bridge_dir, json.load(sys.stdin))
    print("{}")


if __name__ == "__main__":
    main()
