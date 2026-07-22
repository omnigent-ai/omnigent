"""Standalone dictation worker: serves only ``WS /v1/dictation/stream``.

Lets a machine with spare CPU do speech-to-text for an omnigent server
that can't keep up with the model it wants (designs/server-dictation.md,
"Hardware sizing"). The main server selects the ``remote`` engine and
points ``OMNIGENT_DICTATION_REMOTE_URL`` at this worker; it relays takes
over the same wire protocol the browser speaks, so the worker needs no
new code — it is ``create_dictation_router`` served on its own. The
browser never talks to the worker directly.

Run it wherever the models live::

    pip install omnigent[dictation]
    scripts/fetch-dictation-models.sh
    python -m omnigent.server.dictation_worker --host 0.0.0.0 --port 8100

Then start the main server pointed at it::

    OMNIGENT_DICTATION_ENGINE=remote \\
    OMNIGENT_DICTATION_REMOTE_URL=ws://<worker-host>:8100/v1/dictation/stream \\
    omnigent server ...

The same ``OMNIGENT_DICTATION_*`` env vars configure the worker itself
(model dirs, stream cap, fake engine for tests).

Security: the worker has NO authentication — it accepts raw audio from
anyone who can reach the port and returns transcripts. Bind it to a
trusted network (LAN/VPN) only; the main server enforces user auth on
its own dictation route before relaying.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Sequence

from fastapi import FastAPI

from omnigent.server.routes.dictation import create_dictation_router


def create_worker_app() -> FastAPI:
    """Build the single-route worker app."""
    app = FastAPI(title="omnigent dictation worker")
    app.include_router(create_dictation_router(), prefix="/v1")
    return app


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: parse args and serve until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; use a LAN/VPN address for a remote main server "
        "(the worker is unauthenticated — never expose it publicly)",
    )
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_worker_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
