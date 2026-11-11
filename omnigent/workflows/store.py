"""Local checkpoints and an inbox that remains writable while a controller runs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from filelock import FileLock

from omnigent.install_ledger import atomic_write_json
from omnigent.workflows.state import EVENT_ADAPTER, InputEvent


class RunStore:
    """One controller per directory; events are immutable and deduplicated by caller ID."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True)
        self.path = directory / "state.json"
        self._inbox_path = directory / "inbox.json"
        self.controller_lock = FileLock(directory / "controller.lock", timeout=0)
        self._inbox_lock = FileLock(directory / "inbox.lock", timeout=5)

    def save(self, snapshot: dict[str, Any]) -> None:
        atomic_write_json(self.path, snapshot)

    def load(self) -> dict[str, Any]:
        snapshot = json.loads(self.path.read_text())
        if not isinstance(snapshot, dict):
            raise ValueError("Invalid workflow checkpoint")
        return snapshot

    def enqueue(self, event: InputEvent) -> None:
        """Submitting identical input twice succeeds; conflicting reuse of an ID fails."""
        with self._inbox_lock:
            events = self.events()
            for existing in events:
                if existing.id != event.id:
                    continue
                if existing != event:
                    raise ValueError(f"Conflicting event ID: {event.id}")
                return
            atomic_write_json(
                self._inbox_path,
                {"events": [item.model_dump(mode="json") for item in [*events, event]]},
            )

    def events(self) -> list[InputEvent]:
        with self._inbox_lock:
            if not self._inbox_path.exists():
                return []
            inbox = json.loads(self._inbox_path.read_text())
            if not isinstance(inbox, dict) or not isinstance(inbox.get("events"), list):
                raise ValueError("Invalid workflow inbox")
            return [EVENT_ADAPTER.validate_python(item) for item in inbox["events"]]
