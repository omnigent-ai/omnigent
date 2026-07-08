"""
Integration tests for ``POST /v1/sessions/from-github``.

The route fetches a public repo's codeload tarball (mocked here with
``respx``), repackages it, and creates a session-scoped agent through the
same path as a multipart upload. These tests assert the happy path (201 +
session_id) and that a fetch failure surfaces as a 4xx rather than a 500.
"""

from __future__ import annotations

import io
import tarfile

import httpx
import pytest
import respx

pytestmark = pytest.mark.asyncio

_CONFIG = (
    "spec_version: 1\n"
    "name: gh-agent\n"
    "llm:\n"
    "  model: gh-agent\n"
    "  api_key: sk-test\n"
    "executor:\n"
    "  type: omnigent\n"
    "  model: gh-agent\n"
    "  config:\n"
    "    harness: claude-sdk\n"
)


def _codeload(files: dict[str, str]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@respx.mock
async def test_from_github_creates_session(client: httpx.AsyncClient) -> None:
    raw = _codeload(
        {
            "acme-bot-sha/config.yaml": _CONFIG,
            "acme-bot-sha/.git/HEAD": "junk",
        }
    )
    respx.get("https://codeload.github.com/acme/bot/tar.gz/HEAD").mock(
        return_value=httpx.Response(200, content=raw)
    )
    resp = await client.post(
        "/v1/sessions/from-github",
        json={"source_url": "https://github.com/acme/bot", "metadata": {}},
    )
    assert resp.status_code == 201, resp.text
    assert "session_id" in resp.json()


@respx.mock
async def test_from_github_missing_repo_is_client_error(client: httpx.AsyncClient) -> None:
    respx.get("https://codeload.github.com/acme/missing/tar.gz/HEAD").mock(
        return_value=httpx.Response(404)
    )
    resp = await client.post(
        "/v1/sessions/from-github",
        json={"source_url": "acme/missing", "metadata": {}},
    )
    assert resp.status_code < 500, resp.text
    assert resp.status_code >= 400


async def test_from_github_requires_source_url(client: httpx.AsyncClient) -> None:
    resp = await client.post("/v1/sessions/from-github", json={"metadata": {}})
    assert resp.status_code == 422, resp.text
