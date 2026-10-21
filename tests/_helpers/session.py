"""Bundle and HTTP primitives; callers own session state, checks and cleanup."""

from __future__ import annotations

import io
import json
import tarfile
from collections.abc import Callable, Mapping
from typing import Any, BinaryIO

import httpx


def bundle_files(files: Mapping[str, bytes]) -> bytes:
    """Archive exact member names and bytes, preserving insertion order."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def post_session_bundle(
    post: Callable[..., httpx.Response],
    url: str,
    bundle: bytes | BinaryIO,
    *,
    metadata: Mapping[str, Any] | None = None,
    filename: str = "agent.tar.gz",
    **request_options: Any,
) -> httpx.Response:
    """POST with the caller's client/options; retain its response for exact checks."""
    return post(
        url,
        data={"metadata": json.dumps({} if metadata is None else dict(metadata))},
        files={"bundle": (filename, bundle, "application/gzip")},
        **request_options,
    )


def bind_session_runner(
    patch: Callable[..., httpx.Response],
    base_url: str,
    session_id: str,
    runner_id: str,
    **request_options: Any,
) -> None:
    """Bind only when requested: this PATCH can start native terminals."""
    response = patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        **request_options,
    )
    response.raise_for_status()
