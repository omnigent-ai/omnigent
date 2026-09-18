"""Session-owned Gemini transport for Databricks-backed native agy."""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import os
import re
import secrets
import signal
import socket
import sys
from collections.abc import Iterable

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse
from starlette.routing import Route

from omnigent.harnesses.antigravity_native.credentials import databricks_token_source
from omnigent.inner.credential_proxy import DatabricksProfileTokenProvider
from omnigent.models.databricks_model_discovery import discover_databricks_gemini_models

PROFILE_ENV = "OMNIGENT_AGY_DATABRICKS_PROFILE"
_GENERATE_PATH = re.compile(r"/v1beta/models/([^/:]+):(generateContent|streamGenerateContent)$")
_RESPONSE_HEADERS = {"content-type", "content-encoding", "content-length"}


def wrap_agy_gateway_launch(argv: list[str], env: dict[str, str]) -> list[str]:
    """Put an opted-in Databricks launch under its transport supervisor."""
    profile = env.pop(PROFILE_ENV, None)
    if profile is None:
        return argv
    # A workspace may contain an older omnigent checkout or shadow a dependency.
    return [sys.executable, "-P", "-m", __name__, "--profile", profile, "--", *argv]


def _model_key(model: str) -> str:
    return (
        model.removeprefix("system.ai.")
        .removeprefix("databricks-")
        .removesuffix("-preview")
        .replace(".", "-")
    )


def resolve_model(model: str, available: Iterable[str]) -> str:
    """Match agy's spelling to the same served version, never another model."""
    matches = [candidate for candidate in available if _model_key(candidate) == _model_key(model)]
    if len(matches) != 1:
        raise ValueError(f"Databricks does not expose an unambiguous Gemini model for {model!r}.")
    return matches[0]


class GeminiDatabricksGateway:
    """Forward Gemini bodies and SSE bytes with profile auth and model translation."""

    def __init__(
        self,
        source: DatabricksProfileTokenProvider,
        models: tuple[str, ...],
        client: httpx.AsyncClient,
    ) -> None:
        self.source = source
        self.models = models
        self.client = client
        self.key = secrets.token_urlsafe(32)
        self.app = Starlette(routes=[Route("/{path:path}", self.forward, methods=["POST"])])

    async def forward(self, request: Request) -> JSONResponse | StreamingResponse:
        if not hmac.compare_digest(request.headers.get("x-goog-api-key", ""), self.key):
            return JSONResponse({"error": {"message": "Invalid gateway session key"}}, 401)
        match = _GENERATE_PATH.fullmatch(request.url.path)
        if match is None:
            return JSONResponse({"error": {"message": "Unsupported Gemini operation"}}, 404)
        try:
            model = resolve_model(match[1], self.models)
        except ValueError as exc:
            return JSONResponse({"error": {"message": str(exc)}}, 404)
        try:
            token = await asyncio.to_thread(self.source.resolve)
            url = f"{self.source.workspace_url}/ai-gateway/gemini/v1beta/models/{model}:{match[2]}"
            upstream = await self.client.send(
                self.client.build_request(
                    "POST",
                    url,
                    params=request.query_params,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                    content=await request.body(),
                ),
                stream=True,
            )
        except Exception:
            return JSONResponse({"error": {"message": "Databricks gateway request failed"}}, 502)
        return StreamingResponse(
            upstream.aiter_raw(),
            status_code=upstream.status_code,
            headers={k: v for k, v in upstream.headers.items() if k in _RESPONSE_HEADERS},
            background=BackgroundTask(upstream.aclose),
        )


class _Server(uvicorn.Server):
    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def run(profile: str, argv: list[str]) -> int:
    """Keep the local gateway alive exactly as long as its agy child."""
    source = await asyncio.to_thread(databricks_token_source, profile)
    token = await asyncio.to_thread(source.resolve)
    models = await asyncio.to_thread(
        discover_databricks_gemini_models, source.workspace_url, token
    )
    if not models:
        raise ValueError("The selected Databricks profile exposes no Gemini model services.")
    timeout = httpx.Timeout(connect=15, read=None, write=60, pool=15)
    async with httpx.AsyncClient(timeout=timeout, follow_redirects=False) as client:
        gateway = GeminiDatabricksGateway(source, models, client)
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            port = listener.getsockname()[1]
            server = _Server(uvicorn.Config(gateway.app, log_level="error", lifespan="off"))
            serving = asyncio.create_task(server.serve(sockets=[listener]))
            child: asyncio.subprocess.Process | None = None
            child_wait: asyncio.Task[int] | None = None
            loop = asyncio.get_running_loop()
            try:
                async with asyncio.timeout(10):
                    while not server.started:
                        if serving.done():
                            await serving
                            raise RuntimeError("Gemini gateway did not start")
                        await asyncio.sleep(0.01)
                env = {
                    **os.environ,
                    "GEMINI_API_KEY": gateway.key,
                    "GOOGLE_GEMINI_BASE_URL": f"http://127.0.0.1:{port}",
                }
                env.pop(PROFILE_ENV, None)
                child = await asyncio.create_subprocess_exec(*argv, env=env)
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    loop.add_signal_handler(sig, _signal_child, child, sig)
                child_wait = asyncio.create_task(child.wait())
                done, _ = await asyncio.wait(
                    (child_wait, serving), return_when=asyncio.FIRST_COMPLETED
                )
                if child_wait in done:
                    return child_wait.result()
                await serving
                raise RuntimeError("The Gemini gateway stopped while agy was still running")
            finally:
                for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
                    loop.remove_signal_handler(sig)
                if child is not None and child.returncode is None:
                    _signal_child(child, signal.SIGTERM)
                    try:
                        await asyncio.wait_for(child.wait(), 5)
                    except TimeoutError:
                        child.kill()
                        await child.wait()
                if child_wait is not None:
                    await child_wait
                server.should_exit = True
                try:
                    await asyncio.wait_for(serving, 5)
                except TimeoutError:
                    serving.cancel()


def _signal_child(child: asyncio.subprocess.Process, sig: signal.Signals) -> None:
    with contextlib.suppress(ProcessLookupError):
        child.send_signal(sig)


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", required=True)
    parser.add_argument("command", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    command = args.command[1:] if args.command[:1] == ["--"] else args.command
    if not command:
        parser.error("an agy command is required")
    try:
        code = asyncio.run(run(args.profile, command))
    except Exception as exc:
        print(f"Cannot start the Databricks Gemini gateway: {exc}", file=sys.stderr)
        code = 1
    raise SystemExit(code)


if __name__ == "__main__":
    main()
