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

Security: set the same ``OMNIGENT_DICTATION_WORKER_TOKEN`` (e.g.
``openssl rand -hex 32``) on the worker and the main server so the worker
requires ``Authorization: Bearer <token>``. With a token, the main server
only connects over ``wss://`` (or ``ws://`` to loopback); serve TLS with
uvicorn directly::

    python -m uvicorn omnigent.server.dictation_worker:create_worker_app \\
        --factory --host 0.0.0.0 --port 8100 \\
        --ssl-certfile cert.pem --ssl-keyfile key.pem

Without a token the worker has NO authentication — it accepts raw audio
from anyone who can reach the port. Bind it to a trusted network
(LAN/VPN) only. The main server enforces user auth on its own dictation
route before relaying.
"""

from __future__ import annotations

import argparse
import logging
import secrets
from collections.abc import Sequence

from fastapi import FastAPI
from starlette.requests import HTTPConnection

from omnigent.server.auth import AuthProvider
from omnigent.server.dictation import worker_token
from omnigent.server.routes.dictation import create_dictation_router

_WORKER_USER_ID = "dictation-worker"


class _WorkerTokenAuth(AuthProvider):
    """Accepts exactly one ``Authorization: Bearer <token>`` header."""

    def __init__(self, token: str) -> None:
        self._token = token.encode("ascii")

    def __repr__(self) -> str:
        return "_WorkerTokenAuth()"

    def get_user_id(self, request: HTTPConnection) -> str | None:
        values = request.headers.getlist("authorization")
        if len(values) != 1:
            return None
        scheme, _, credential = values[0].partition(" ")
        if scheme.lower() != "bearer" or not credential or " " in credential:
            return None
        # Starlette decodes headers as latin-1, so this round-trips any bytes.
        if not secrets.compare_digest(credential.encode("latin-1"), self._token):
            return None
        return _WORKER_USER_ID


def create_worker_app() -> FastAPI:
    """Build the single-route worker app.

    :raises ValueError: When ``OMNIGENT_DICTATION_WORKER_TOKEN`` is set
        but invalid, so a typo fails startup instead of disabling auth.
    """
    token = worker_token()
    auth_provider = _WorkerTokenAuth(token) if token is not None else None
    app = FastAPI(title="omnigent dictation worker")
    app.include_router(create_dictation_router(auth_provider=auth_provider), prefix="/v1")
    return app


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: parse args and serve until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address; use a LAN/VPN address for a remote main server "
        "(without OMNIGENT_DICTATION_WORKER_TOKEN the worker is unauthenticated)",
    )
    parser.add_argument("--port", type=int, default=8100)
    args = parser.parse_args(argv)

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(create_worker_app(), host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
