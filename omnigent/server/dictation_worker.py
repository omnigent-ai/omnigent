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

The worker requires a shared bearer token and supports TLS. Prefer TLS
even on private networks; plaintext non-loopback relay URLs are rejected
by the main server unless explicitly allowed.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, HTTPException, Request, status

from omnigent.server.dictation import WORKER_TOKEN_ENV, DictationEngine, get_engine
from omnigent.server.dictation_metrics import metrics
from omnigent.server.routes.dictation import create_dictation_router


@dataclass
class _Readiness:
    state: str = "warming"


def _has_token(request: Request, expected: str) -> bool:
    authorization = request.headers.get("authorization", "")
    prefix = "Bearer "
    provided = authorization[len(prefix) :] if authorization.startswith(prefix) else ""
    return bool(expected) and hmac.compare_digest(provided.encode(), expected.encode())


def create_worker_app(
    *,
    engine_provider: Callable[[], DictationEngine] | None = None,
    shared_token: str | None = None,
) -> FastAPI:
    """Build the authenticated worker app with asynchronous warmup."""
    resolve_engine = engine_provider or get_engine
    token = shared_token if shared_token is not None else os.environ.get(WORKER_TOKEN_ENV, "")
    readiness = _Readiness()

    async def warmup() -> None:
        started_at = time.monotonic()
        try:
            engine = await asyncio.to_thread(resolve_engine)
            create_task = asyncio.create_task(asyncio.to_thread(engine.create_stream))
            try:
                stream = await asyncio.shield(create_task)
            except asyncio.CancelledError:
                stream = await create_task
                await asyncio.to_thread(stream.close)
                raise
            await asyncio.to_thread(stream.close)
        except asyncio.CancelledError:
            raise
        except Exception:
            readiness.state = "failed"
            metrics.warmup(time.monotonic() - started_at, "failed")
            logging.getLogger(__name__).exception("dictation worker model warmup failed")
        else:
            readiness.state = "ready"
            metrics.warmup(time.monotonic() - started_at, "ready")

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        task = asyncio.create_task(warmup())
        yield
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    app = FastAPI(title="omnigent dictation worker", lifespan=lifespan)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/ready")
    async def ready(request: Request) -> dict[str, str]:
        if not token or not _has_token(request, token):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required"
            )
        if readiness.state != "ready":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail={"status": readiness.state},
            )
        return {"status": "ready"}

    app.include_router(
        create_dictation_router(engine_provider=resolve_engine, shared_token=token),
        prefix="/v1",
    )
    return app


def main(argv: Sequence[str] | None = None) -> int:
    """CLI entry point: parse args and serve until interrupted."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="bind address",
    )
    parser.add_argument("--port", type=int, default=8100)
    parser.add_argument("--tls-certfile", help="PEM server certificate chain")
    parser.add_argument("--tls-keyfile", help="PEM server private key")
    args = parser.parse_args(argv)

    token = os.environ.get(WORKER_TOKEN_ENV, "").strip()
    if not token:
        parser.error(f"{WORKER_TOKEN_ENV} must be set")
    if bool(args.tls_certfile) != bool(args.tls_keyfile):
        parser.error("--tls-certfile and --tls-keyfile must be provided together")

    import uvicorn

    logging.basicConfig(level=logging.INFO)
    uvicorn.run(
        create_worker_app(shared_token=token),
        host=args.host,
        port=args.port,
        log_level="info",
        ssl_certfile=args.tls_certfile,
        ssl_keyfile=args.tls_keyfile,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
