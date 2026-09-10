"""Content-addressed delivery for installed extension browser bundles."""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import APIRouter, Request
from fastapi.responses import Response

from omnigent.errors import ErrorCode, OmnigentError
from omnigent.extensions.assets import ASSET_MEDIA_TYPES, ResolvedBundle
from omnigent.server.auth import AuthProvider
from omnigent.server.routes._auth_helpers import require_user

_CACHE_CONTROL = "private, max-age=31536000, immutable"


def _not_found() -> OmnigentError:
    return OmnigentError("Extension asset not found", code=ErrorCode.NOT_FOUND)


def _etag_matches(header: str | None, etag: str) -> bool:
    if header is None:
        return False
    candidates = (item.strip().removeprefix("W/") for item in header.split(","))
    return any(item in {etag, "*"} for item in candidates)


def create_extension_assets_router(
    bundles: Mapping[str, ResolvedBundle],
    *,
    auth_provider: AuthProvider | None = None,
) -> APIRouter:
    """Build the authenticated extension asset router, mounted under ``/v1``."""
    router = APIRouter()

    @router.get(
        "/extensions/{extension_id}/assets/{digest}/{asset_name}",
        response_model=None,
        responses={
            200: {
                "description": "Extension JavaScript or CSS asset",
                "content": {
                    "text/javascript": {"schema": {"type": "string", "format": "binary"}},
                    "text/css": {"schema": {"type": "string", "format": "binary"}},
                },
            },
            404: {"description": "Extension asset not found"},
        },
    )
    async def get_extension_asset(
        request: Request,
        extension_id: str,
        digest: str,
        asset_name: str,
    ) -> Response:
        """Return a declared asset only when its bundle digest still matches."""
        require_user(request, auth_provider)
        if asset_name not in ASSET_MEDIA_TYPES:
            raise _not_found()
        bundle = bundles.get(extension_id)
        if bundle is None or bundle.digest != digest:
            raise _not_found()
        asset = bundle.assets.get(asset_name)
        if asset is None:
            raise _not_found()

        etag = f'"{bundle.digest}-{asset.name}"'
        headers = {
            "Cache-Control": _CACHE_CONTROL,
            "Content-Disposition": "inline",
            "Content-Security-Policy": "default-src 'none'; sandbox",
            "ETag": etag,
            "X-Content-Type-Options": "nosniff",
        }
        if _etag_matches(request.headers.get("if-none-match"), etag):
            return Response(status_code=304, headers=headers)
        return Response(content=asset.content, media_type=asset.media_type, headers=headers)

    return router
