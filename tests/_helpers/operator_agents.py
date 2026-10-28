"""Provision operator-owned fixtures through the local server's stores."""

from __future__ import annotations

import tempfile
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any, BinaryIO

import httpx

_local_stores: dict[str, tuple[str, Path]] = {}


@contextmanager
def operator_agent_store(base_url: str, database_uri: str, artifact_dir: Path) -> Iterator[None]:
    """Expose only a fixture-owned server's local stores for trusted provisioning."""
    if base_url in _local_stores:
        raise ValueError(f"operator stores already configured for {base_url}")
    _local_stores[base_url] = (database_uri, artifact_dir)
    try:
        yield
    finally:
        del _local_stores[base_url]


def post_operator_session(
    post: Callable[..., httpx.Response],
    url: str,
    bundle: bytes | BinaryIO,
    *,
    metadata: Mapping[str, Any] | None = None,
    filename: str = "agent.tar.gz",
    **request_options: Any,
) -> httpx.Response:
    """Register a trusted test agent like ``server --agent``, then create by id.

    Requires local stores explicitly supplied by the server fixture. HTTP upload
    tests must continue using ``post_session_bundle`` to exercise validation.
    """
    from omnigent.cli import _preregister_agent
    from omnigent.runtime.agent_cache import AgentCache
    from omnigent.spec import extract_safe
    from omnigent.stores.agent_store.sqlalchemy_store import SqlAlchemyAgentStore
    from omnigent.stores.artifact_store.local import LocalArtifactStore

    del filename
    client = getattr(post, "__self__", None)
    target = str(client.base_url.join(url)) if isinstance(client, httpx.Client) else url
    base_url = target.removesuffix("/v1/sessions")
    if target == base_url or base_url not in _local_stores:
        raise ValueError(f"no fixture-owned operator stores for {target}")
    database_uri, artifact_dir = _local_stores[base_url]
    artifact_store = LocalArtifactStore(str(artifact_dir))
    cache = AgentCache(artifact_store, artifact_dir / ".cache")
    with tempfile.TemporaryDirectory() as tmpdir:
        root = extract_safe(bundle if isinstance(bundle, bytes) else bundle.read(), Path(tmpdir))
        agent_id = _preregister_agent(
            root, SqlAlchemyAgentStore(database_uri), artifact_store, cache
        )
    if agent_id is None:
        raise ValueError("operator fixture must name its agent")
    response = post(url, json={**dict(metadata or {}), "agent_id": agent_id}, **request_options)
    # Multipart creates expose session_id; JSON creates expose id.
    if response.is_success:
        payload = response.json()
        payload["session_id"] = payload["id"]
        response = httpx.Response(
            response.status_code,
            json=payload,
            headers={
                key: value for key, value in response.headers.items() if key != "content-length"
            },
            request=response.request,
        )
    return response
