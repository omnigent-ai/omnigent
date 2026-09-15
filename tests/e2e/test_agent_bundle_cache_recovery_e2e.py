"""End-to-end regression coverage: incomplete agent bundle caches must heal.

Guarded regression: if the runner's ``_resolve_agent_spec_from_server``
treats an existing versioned cache directory as proof that an agent bundle
was successfully extracted and loaded, a bundle whose load fails (e.g. the
archive is missing ``config.yaml``) leaves an incomplete directory behind;
every later resolve of the same agent version then skips extraction and
fails with ``FileNotFoundError: config.yaml not found`` — native session
startup stays broken even though the server is now serving a valid bundle.

These tests drive the runner's **real** resolver code over a **real** TCP
socket against a live session-scoped agent-contents endpoint. The only thing
simulated is the fault *condition* — the bundle bytes the endpoint serves —
which is exactly the trigger the report names:

* ``test_valid_bundle_recovers_after_failed_extraction``: the endpoint serves
  a partial tar (no ``config.yaml``) once, then a valid bundle for the same
  agent version. The second resolve must succeed instead of reusing the
  poisoned cache entry.
* ``test_incomplete_cache_dir_is_rebuilt_from_valid_response``: a versioned
  cache directory already exists holding an unrelated partial file and no
  ``config.yaml`` (what an interrupted extraction leaves behind). A resolve
  against a healthy endpoint must rebuild the entry from the fetched response
  rather than trusting the leftover directory.

No LLM, server subprocess, or runner tunnel is needed: the defect is entirely
in the runner-side bundle resolution, so the tests own a tiny stub HTTP
server for the single endpoint the resolver calls.
"""

from __future__ import annotations

import io
import tarfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from omnigent.runner._entry import _resolve_agent_spec_from_server

_AGENT_VERSION = "7"
_AGENT_NAME = "cache-recovery-agent"
_VALID_CONFIG = (
    b"spec_version: 1\n"
    b"name: cache-recovery-agent\n"
    b"executor:\n"
    b"  config:\n"
    b"    harness: claude-sdk\n"
)


def _tar_bytes(files: dict[str, bytes]) -> bytes:
    """Build an in-memory ``.tar.gz`` bundle from a name->bytes mapping.

    :param files: Archive members, e.g. ``{"config.yaml": b"..."}``.
    :returns: The gzipped tarball bytes.
    """
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, data in files.items():
            info = tarfile.TarInfo(name=name)
            info.size = len(data)
            tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# A bundle whose extraction succeeds but whose load must fail: it carries a
# stray file and no config.yaml, like a truncated/corrupted bundle body.
_PARTIAL_BUNDLE = _tar_bytes({"partial.txt": b"truncated bundle placeholder\n"})
_VALID_BUNDLE = _tar_bytes({"config.yaml": _VALID_CONFIG})


@dataclass
class _BundleEndpoint:
    """A live agent-contents endpoint serving a scripted bundle sequence.

    :param responses: Bundle payloads to serve in order; the last entry
        repeats for any further requests.
    :param requested_paths: URL paths of every request served, in order.
    """

    responses: list[bytes]
    requested_paths: list[str] = field(default_factory=list)
    base_url: str = ""


@pytest.fixture()
def bundle_endpoint(request: pytest.FixtureRequest) -> Iterator[_BundleEndpoint]:
    """Serve ``GET /v1/sessions/{id}/agent/contents`` over a real TCP socket.

    The bundle sequence is provided per-test via indirect parametrization or
    by mutating ``endpoint.responses`` before the first request.

    :param request: Pytest fixture request (unused; keeps signature uniform).
    :returns: The running endpoint descriptor with its ``base_url`` filled in.
    """
    endpoint = _BundleEndpoint(responses=[_VALID_BUNDLE])

    class _Handler(BaseHTTPRequestHandler):
        """Request handler serving the scripted bundle sequence."""

        def do_GET(self) -> None:  # noqa: N802 - http.server API name
            """Serve the next scripted bundle with the fixed version header."""
            index = min(len(endpoint.requested_paths), len(endpoint.responses) - 1)
            endpoint.requested_paths.append(self.path)
            body = endpoint.responses[index]
            self.send_response(200)
            self.send_header("X-Agent-Version", _AGENT_VERSION)
            self.send_header("X-Agent-Session-Scoped", "true")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: object) -> None:
            """Silence per-request stderr logging."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint.base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        yield endpoint
    finally:
        server.shutdown()
        thread.join(timeout=10)


async def test_valid_bundle_recovers_after_failed_extraction(
    bundle_endpoint: _BundleEndpoint,
    tmp_path: Path,
) -> None:
    """A failed bundle load must not poison later resolves of the version.

    The endpoint first serves a partial bundle (no ``config.yaml``) for agent
    version 7 — that resolve fails, as the bundle is genuinely invalid. It
    then serves a valid bundle under the same version header. The second
    resolve must load the valid response instead of failing forever on the
    leftover cache directory.

    :param bundle_endpoint: Live stub agent-contents endpoint.
    :param tmp_path: Runner-local spec cache root for extracted bundles.
    :returns: None.
    """
    bundle_endpoint.responses = [_PARTIAL_BUNDLE, _VALID_BUNDLE]
    async with httpx.AsyncClient(base_url=bundle_endpoint.base_url) as client:
        # First resolve: the served bundle is invalid, so failing is correct.
        with pytest.raises(FileNotFoundError, match="config.yaml not found"):
            await _resolve_agent_spec_from_server(
                client, tmp_path, "ag_recovery", session_id="conv_recovery"
            )

        # Second resolve: the server now serves a valid bundle for the same
        # version. This must succeed; reusing the incomplete cache directory
        # (FileNotFoundError again) is the regression under test.
        resolved = await _resolve_agent_spec_from_server(
            client, tmp_path, "ag_recovery", session_id="conv_recovery"
        )

    assert resolved is not None
    assert resolved.name == _AGENT_NAME
    assert resolved.workdir is not None
    assert (resolved.workdir / "config.yaml").read_bytes() == _VALID_CONFIG
    # Both resolves reached the server; the recovery came from the valid
    # response, not from skipping the fetch.
    assert bundle_endpoint.requested_paths == [
        "/v1/sessions/conv_recovery/agent/contents",
        "/v1/sessions/conv_recovery/agent/contents",
    ]


async def test_incomplete_cache_dir_is_rebuilt_from_valid_response(
    bundle_endpoint: _BundleEndpoint,
    tmp_path: Path,
) -> None:
    """An existing cache entry without ``config.yaml`` is rebuilt, not trusted.

    Seeds the versioned cache directory with an unrelated partial file and no
    ``config.yaml`` — the state an interrupted extraction leaves behind — and
    resolves against an endpoint serving a valid bundle for that version. The
    resolver must rebuild the entry from the fetched response without
    preserving the leftover file.

    :param bundle_endpoint: Live stub agent-contents endpoint.
    :param tmp_path: Runner-local spec cache root for extracted bundles.
    :returns: None.
    """
    poisoned = tmp_path / f"ag_recoveryb-v{_AGENT_VERSION}"
    poisoned.mkdir(parents=True)
    (poisoned / "leftover.txt").write_text("interrupted extraction leftover\n")

    async with httpx.AsyncClient(base_url=bundle_endpoint.base_url) as client:
        resolved = await _resolve_agent_spec_from_server(
            client, tmp_path, "ag_recoveryb", session_id="conv_recoveryb"
        )

    assert resolved is not None
    assert resolved.name == _AGENT_NAME
    assert resolved.workdir is not None
    assert (resolved.workdir / "config.yaml").read_bytes() == _VALID_CONFIG
    assert not (resolved.workdir / "leftover.txt").exists()
    assert bundle_endpoint.requested_paths == [
        "/v1/sessions/conv_recoveryb/agent/contents",
    ]
