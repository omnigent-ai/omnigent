"""A scheduling barrier around an actual unavailable-runner decision."""

from __future__ import annotations

import asyncio
import contextvars
import json
import os
import time
from pathlib import Path

from omnigent.server.routes.sessions import routes_events

_gate = contextvars.ContextVar("binding_race_gate", default=None)
_real_guard = routes_events._raise_if_runner_on_another_replica


class _BindingRaceScope:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        headers = dict(scope.get("headers", []))
        token = _gate.set(
            {"used": False}
            if headers.get(b"x-e2e-binding-race") == b"pause-after-runner-miss"
            else None
        )
        try:
            await self.app(scope, receive, send)
        finally:
            _gate.reset(token)


async def _wait_after_real_runner_miss(conv, app_state, conversation_store):
    gate = _gate.get()
    if gate is not None and not gate["used"]:
        gate["used"] = True
        root = Path(os.environ["OMNIGENT_E2E_BINDING_GATE"])
        observed_path = root / "lookup-missed.tmp"
        observed_path.write_text(
            json.dumps(
                {
                    "session_id": conv.id,
                    "runner_id": conv.runner_id,
                    "host_id": conv.host_id,
                    "runner_last_seen": conv.runner_last_seen,
                }
            )
        )
        observed_path.replace(root / "lookup-missed.json")
        deadline = time.monotonic() + 120
        while not (root / "release").exists():
            if time.monotonic() >= deadline:
                raise TimeoutError("test did not release the in-flight runner lookup")
            await asyncio.sleep(0.02)
    await _real_guard(conv, app_state, conversation_store)


def main() -> None:
    """Start the real CLI server with request-scoped scheduling control."""
    import omnigent.server.app as server_app
    from omnigent.cli import main as cli_main

    real_create_app = server_app.create_app

    def create_app(*args, **kwargs):
        app = real_create_app(*args, **kwargs)
        app.add_middleware(_BindingRaceScope)
        return app

    routes_events._raise_if_runner_on_another_replica = _wait_after_real_runner_miss
    server_app.create_app = create_app
    cli_main()


if __name__ == "__main__":
    main()
