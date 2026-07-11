"""
Integration tests for ``POST /v1/sessions/validate-bundle``.

The validate-bundle route is the import-dialog preflight: it runs the same
validation the bundled-create path applies (``validate_agent_bundle``) without
creating a session, so config errors surface at import time instead of at
session create. These tests drive the real route through the shared ``client``
fixture and assert:

- a valid bundle returns 200 ``{"ok": True, "agent_name": ...}``,
- an invalid bundle returns 400 with the validation message,
- a request missing the ``bundle`` part returns 422.

The underlying validation logic is covered exhaustively in
``tests/server/test_bundles.py``; here we only assert the HTTP wiring.
"""

from __future__ import annotations

import io
import tarfile

import httpx
import pytest

from tests.server.helpers import build_agent_bundle

pytestmark = pytest.mark.asyncio


def _bundle_bytes(files: dict[str, str]) -> bytes:
    """Build a ``.tar.gz`` from ``{archive_path: content}`` for the bad-bundle case."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, content in files.items():
            data = content.encode()
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


async def test_validate_bundle_accepts_valid_bundle(client: httpx.AsyncClient) -> None:
    """A well-formed bundle validates with 200 and echoes the agent name."""
    bundle = build_agent_bundle(name="preflight-ok-agent")
    resp = await client.post(
        "/v1/sessions/validate-bundle",
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["ok"] is True
    assert body["agent_name"] == "preflight-ok-agent"


async def test_validate_bundle_rejects_missing_spec_version(client: httpx.AsyncClient) -> None:
    """A config.yaml without spec_version is rejected 400 with the validation message."""
    bundle = _bundle_bytes(
        {
            "config.yaml": (
                "name: no-version-agent\n"
                "executor:\n"
                "  type: omnigent\n"
                "  config:\n"
                "    harness: claude-sdk\n"
                "  model: databricks-claude-sonnet-4-6\n"
            )
        }
    )
    resp = await client.post(
        "/v1/sessions/validate-bundle",
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
    )
    assert resp.status_code == 400, resp.text
    assert "spec_version" in resp.text


async def test_validate_bundle_requires_bundle_part(client: httpx.AsyncClient) -> None:
    """Omitting the bundle part is a 422 (missing multipart field)."""
    resp = await client.post(
        "/v1/sessions/validate-bundle",
        files={"notbundle": ("x.txt", b"hi", "text/plain")},
    )
    assert resp.status_code == 422, resp.text
