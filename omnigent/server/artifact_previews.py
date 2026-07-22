"""Capability-scoped browser previews for workspace HTML artifacts."""

from __future__ import annotations

import asyncio
import secrets
import time
import urllib.parse
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import httpx
from fastapi import APIRouter, Request
from fastapi.responses import Response
from starlette.types import ASGIApp, Receive, Scope, Send

_DEFAULT_TTL_SECONDS = 600
_DEFAULT_REQUEST_BUDGET = 256
_DEFAULT_TRANSFER_BUDGET = 64 * 1024 * 1024
_MAX_RESOURCE_BYTES = 10 * 1024 * 1024

PREVIEW_CSP = "; ".join(
    [
        "default-src 'none'",
        "script-src 'self'",
        "style-src 'self' 'unsafe-inline'",
        "img-src 'self' data: blob:",
        "font-src 'self' data:",
        "media-src 'self' blob:",
        "connect-src 'self'",
        "base-uri 'none'",
        "form-action 'none'",
        "frame-src 'none'",
        "child-src 'none'",
        "worker-src 'none'",
        "object-src 'none'",
        "sandbox allow-scripts allow-same-origin",
    ]
)


class ArtifactPreviewNotFound(Exception):
    """Raised for every invalid, expired, exhausted, or inaccessible grant."""


class ArtifactPreviewUnavailable(Exception):
    """Raised when no runner is available to serve an artifact preview."""


@dataclass(frozen=True)
class ArtifactPreviewGrant:
    token: str
    url: str
    expires_at: float


@dataclass(frozen=True)
class ArtifactPreviewResource:
    content: bytes
    content_type: str


@dataclass
class _GrantState:
    session_id: str
    entry_path: str
    artifact_root: str
    expires_at: float
    requests_remaining: int
    bytes_remaining: int


def _canonical_entry_path(entry_path: str) -> tuple[str, str]:
    if not entry_path or "\\" in entry_path or entry_path.startswith("/"):
        raise ValueError("entry_path must be a relative POSIX path")
    parts = entry_path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ValueError("entry_path must be normalized")
    if parts[0] != "artifacts" or not parts[-1].lower().endswith(".html"):
        raise ValueError("entry_path must point to HTML under artifacts/")
    if len(parts) == 2:
        return entry_path, entry_path
    if len(parts) == 3 and parts[-1].lower() == "index.html":
        return entry_path, "/".join(parts[:-1])
    raise ValueError("entry_path must be artifacts/<slug>.html or artifacts/<slug>/index.html")


def _canonical_resource_path(path: str) -> str:
    if not path or "\\" in path or path.startswith("/"):
        raise ArtifactPreviewNotFound
    parts = path.split("/")
    if any(not part or part in {".", ".."} for part in parts):
        raise ArtifactPreviewNotFound
    return "/".join(parts)


class ArtifactPreviewService:
    """Issue short-lived grants and proxy scoped resources from a runner."""

    def __init__(
        self,
        *,
        preview_origin: str,
        runner_client_for_session: Callable[[str], Awaitable[httpx.AsyncClient | None]],
        ttl_seconds: int = _DEFAULT_TTL_SECONDS,
        request_budget: int = _DEFAULT_REQUEST_BUDGET,
        transfer_budget: int = _DEFAULT_TRANSFER_BUDGET,
    ) -> None:
        origin = preview_origin.rstrip("/")
        parsed = urllib.parse.urlsplit(origin)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("preview_origin must be an absolute HTTP(S) origin")
        self.preview_origin = origin
        self.preview_hostname = parsed.hostname.lower()
        self._runner_client_for_session = runner_client_for_session
        self._ttl_seconds = ttl_seconds
        self._request_budget = request_budget
        self._transfer_budget = transfer_budget
        self._grants: dict[str, _GrantState] = {}
        self._lock = asyncio.Lock()

    async def create_grant(self, session_id: str, entry_path: str) -> ArtifactPreviewGrant:
        entry_path, artifact_root = _canonical_entry_path(entry_path)
        if await self._runner_client_for_session(session_id) is None:
            raise ArtifactPreviewUnavailable
        token = secrets.token_urlsafe(32)
        expires_at = time.monotonic() + self._ttl_seconds
        async with self._lock:
            self._grants[token] = _GrantState(
                session_id=session_id,
                entry_path=entry_path,
                artifact_root=artifact_root,
                expires_at=expires_at,
                requests_remaining=self._request_budget,
                bytes_remaining=self._transfer_budget,
            )
        quoted_path = urllib.parse.quote(entry_path, safe="/")
        return ArtifactPreviewGrant(
            token=token,
            url=f"{self.preview_origin}/p/{token}/{quoted_path}",
            expires_at=time.time() + self._ttl_seconds,
        )

    async def read(self, token: str, path: str) -> ArtifactPreviewResource:
        resource_path = _canonical_resource_path(path)
        async with self._lock:
            grant = self._grants.get(token)
            if grant is None or grant.expires_at <= time.monotonic():
                self._grants.pop(token, None)
                raise ArtifactPreviewNotFound
            if grant.artifact_root == grant.entry_path:
                allowed = resource_path == grant.entry_path
            else:
                allowed = resource_path.startswith(f"{grant.artifact_root}/")
            if not allowed or grant.requests_remaining <= 0 or grant.bytes_remaining <= 0:
                raise ArtifactPreviewNotFound
            grant.requests_remaining -= 1
            session_id = grant.session_id
            artifact_root = grant.artifact_root
            max_bytes = min(_MAX_RESOURCE_BYTES, grant.bytes_remaining)

        runner_client = await self._runner_client_for_session(session_id)
        if runner_client is None:
            raise ArtifactPreviewNotFound
        quoted_path = urllib.parse.quote(resource_path, safe="/")
        session_path = urllib.parse.quote(session_id, safe="")
        try:
            response = await runner_client.get(
                f"/v1/sessions/{session_path}/artifact-preview/{quoted_path}",
                params={"artifact_root": artifact_root, "max_bytes": max_bytes},
                timeout=10.0,
            )
        except (httpx.HTTPError, ConnectionError) as exc:
            raise ArtifactPreviewNotFound from exc
        if response.status_code != 200:
            raise ArtifactPreviewNotFound

        content = response.content
        async with self._lock:
            current = self._grants.get(token)
            if current is None or len(content) > current.bytes_remaining:
                self._grants.pop(token, None)
                raise ArtifactPreviewNotFound
            current.bytes_remaining -= len(content)
        return ArtifactPreviewResource(
            content=content,
            content_type=response.headers.get("content-type", "application/octet-stream"),
        )


class ArtifactPreviewHostMiddleware:
    """Restrict the preview hostname to capability resources only."""

    def __init__(self, app: ASGIApp, *, preview_hostname: str) -> None:
        self.app = app
        self.preview_hostname = preview_hostname.lower()

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            host = headers.get(b"host", b"").decode("latin-1").split(":", 1)[0].lower()
            if host == self.preview_hostname and not scope.get("path", "").startswith("/p/"):
                response = Response(status_code=404)
                await response(scope, receive, send)
                return
        await self.app(scope, receive, send)


def create_artifact_preview_public_router(service: ArtifactPreviewService) -> APIRouter:
    """Create the unauthenticated capability responder for the preview host."""
    router = APIRouter()

    @router.api_route(
        "/p/{token}/{path:path}",
        methods=["GET", "HEAD"],
        include_in_schema=False,
    )
    async def read_preview_resource(request: Request, token: str, path: str) -> Response:
        if (request.url.hostname or "").lower() != service.preview_hostname:
            return Response(status_code=404)
        try:
            resource = await service.read(token, path)
        except ArtifactPreviewNotFound:
            return Response(status_code=404)
        return Response(
            content=resource.content,
            media_type=resource.content_type,
            headers={
                "Cache-Control": "no-store",
                "Content-Security-Policy": PREVIEW_CSP,
                "Cross-Origin-Resource-Policy": "same-origin",
                "Referrer-Policy": "no-referrer",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
