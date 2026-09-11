"""Read-only notebook history through the existing authenticated session clients."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Awaitable, Callable

import httpx
from fastapi import APIRouter, HTTPException, Request

from omnigent.docloop_gateway import DIGEST, MAX_RESPONSE, _response, _snapshot

COMMIT = re.compile(r"(?:[a-f0-9]{40}|[a-f0-9]{64})\Z")
SESSION = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")


def _project(value, session_id: str, commit: str | None):
    if not isinstance(value, dict) or value.get("schema_version") != 1:
        raise ValueError("Invalid history envelope")
    if value.get("session_id") != session_id or not isinstance(value.get("binding_id"), str):
        raise ValueError("Invalid history binding")
    if not DIGEST.fullmatch(value["binding_id"]):
        raise ValueError("Invalid history binding")
    result = {k: value[k] for k in ("schema_version", "session_id", "binding_id")}
    if commit is not None:
        if value.get("commit") != commit or value.get("read_only") is not True:
            raise ValueError("Invalid saved version")
        document = _snapshot(value.get("document"), session_id)
        if document["binding_id"] != value["binding_id"]:
            raise ValueError("Saved version belongs to another binding")
        if any(document["capabilities"].values()) or any(n["editable"] for n in document["nodes"]):
            raise ValueError("Saved version is not read-only")
        return {**result, "commit": commit, "read_only": True, "document": document}
    rows = value.get("versions")
    if not isinstance(rows, list) or len(rows) > 50:
        raise ValueError("Invalid history page")
    projected = []
    for row in rows:
        if (
            not isinstance(row, dict)
            or not isinstance(row.get("commit"), str)
            or not COMMIT.fullmatch(row["commit"])
        ):
            raise ValueError("Invalid history version")
        for key, limit in (("date", 80), ("message", 500)):
            if not isinstance(row.get(key), str) or len(row[key]) > limit:
                raise ValueError("Invalid history description")
        projected.append({k: row[k] for k in ("commit", "date", "message")})
    for key in ("head", "next_before"):
        if value.get(key) is not None and (
            not isinstance(value[key], str) or not COMMIT.fullmatch(value[key])
        ):
            raise ValueError("Invalid history cursor")
    if value.get("next_before") is not None and (
        not projected or value["next_before"] != projected[-1]["commit"]
    ):
        raise ValueError("History cursor does not match the page")
    return {
        **result,
        "head": value.get("head"),
        "versions": projected,
        "next_before": value.get("next_before"),
    }


def notebook_history_router(
    authorize: Callable[..., Awaitable[None]],
    get_client: Callable[[str], Awaitable[httpx.AsyncClient]],
    *,
    prefix: str = "/v1",
) -> APIRouter:
    router = APIRouter(prefix=prefix + "/sessions/{session_id}/docloop")

    async def forward(request: Request, session_id: str, commit=None, before=None):
        await authorize(request, session_id)
        if not SESSION.fullmatch(session_id):
            raise HTTPException(400, "Invalid session identity")
        for value in (commit, before):
            if value is not None and not COMMIT.fullmatch(value):
                raise HTTPException(400, "A full Git version ID is required")
        target = f"/v1/sessions/{session_id}/docloop/versions"
        if commit:
            target += "/" + commit
        elif before:
            target += "?before=" + before
        try:
            async with asyncio.timeout(15):
                client = await get_client(session_id)
                async with client.stream(
                    "GET",
                    target,
                    headers={"Accept": "application/json", "Accept-Encoding": "identity"},
                    follow_redirects=False,
                    timeout=12,
                ) as response:
                    if response.status_code != 200:
                        status = (
                            response.status_code if response.status_code in {404, 413} else 503
                        )
                        raise HTTPException(status, "Saved notebook versions are unavailable")
                    if response.headers.get("content-encoding", "identity").lower() != "identity":
                        raise ValueError("Compressed history response")
                    parts, size = [], 0
                    async for part in response.aiter_raw():
                        size += len(part)
                        if size > MAX_RESPONSE:
                            raise ValueError("History response exceeds the size limit")
                        parts.append(part)
                    value = json.loads(b"".join(parts))
            return _response(_project(value, session_id, commit))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(503, "Saved notebook versions are unavailable") from exc

    @router.get("/versions")
    async def history(request: Request, session_id: str, before: str | None = None):
        return await forward(request, session_id, before=before)

    @router.get("/versions/{commit}")
    async def version(request: Request, session_id: str, commit: str):
        return await forward(request, session_id, commit=commit)

    return router
