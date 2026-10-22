"""Delay one real linked handoff until its runner acceptance lease expires."""

from __future__ import annotations

import asyncio
import os
import time
from pathlib import Path

from omnigent.session_authority import LINKED_REQUEST_FIELD


def main() -> None:
    from omnigent.cli import main as cli_main
    from omnigent.server.routes._sessions import orchestration

    forward = orchestration._forward_event_to_runner
    gate = Path(os.environ["OMNIGENT_E2E_LINK_GATE"])

    async def delayed_forward(session_id, conv, body, conversation_store, runner_client, **kwargs):
        if request_id := body._trusted_transport.get(LINKED_REQUEST_FIELD):
            (gate / "claimed").write_text(request_id)
            deadline = time.monotonic() + 35
            while not (gate / "release").exists():
                if time.monotonic() >= deadline:
                    raise TimeoutError("test did not release the linked handoff")
                await asyncio.sleep(0.02)
        return await forward(session_id, conv, body, conversation_store, runner_client, **kwargs)

    orchestration._forward_event_to_runner = delayed_forward
    cli_main()


if __name__ == "__main__":
    main()
