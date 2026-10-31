"""``omnigent-client`` installs and runs without the ``omnigent`` server package.

Remote callers (CI jobs, orchestrators) install only the client, so the
client must not import or depend on ``omnigent``. The import check runs
in a fresh interpreter where any ``omnigent`` import raises, which is what
a client-only install looks like.
"""

from __future__ import annotations

import subprocess
import sys
import textwrap
import tomllib
from pathlib import Path

import omnigent_client.wire as client_wire

import omnigent.runner.identity as runner_identity
import omnigent.server.schemas as server_schemas

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Runs in a child interpreter with ``omnigent`` made unimportable. Imports
# every client module, then streams a session over a mocked transport so
# the SSE envelopes are parsed and typed by the client alone.
_CLIENT_ONLY_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import importlib
    import importlib.abc
    import pkgutil
    import sys


    class _NoServerPackage(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path=None, target=None):
            if name == "omnigent" or name.startswith("omnigent."):
                raise ModuleNotFoundError(f"No module named {name!r}", name=name)
            return None


    sys.meta_path.insert(0, _NoServerPackage())

    import httpx
    import omnigent_client
    from omnigent_client._sessions import SessionsNamespace
    from omnigent_client.wire import OutputTextDeltaEvent

    for module in pkgutil.walk_packages(omnigent_client.__path__, "omnigent_client."):
        importlib.import_module(module.name)

    body = (
        b"event: response.output_text.delta\\n"
        b'data: {"type": "response.output_text.delta", "delta": "hi"}\\n\\n'
        b"event: made.up.event\\n"
        b'data: {"type": "made.up.event"}\\n\\n'
        b"event: done\\ndata: [DONE]\\n\\n"
    )


    def handler(request):
        return httpx.Response(200, content=body, headers={"content-type": "text/event-stream"})


    async def main():
        async with httpx.AsyncClient(
            base_url="http://srv", transport=httpx.MockTransport(handler)
        ) as http:
            ns = SessionsNamespace(http, "http://srv")
            return [event async for event in ns.stream("conv_abc")]


    events = asyncio.run(main())
    assert len(events) == 1, events
    assert isinstance(events[0], OutputTextDeltaEvent), events
    assert events[0].delta == "hi"
    loaded = sorted(m for m in sys.modules if m == "omnigent" or m.startswith("omnigent."))
    assert not loaded, loaded
    print("ok")
    """
)


def test_client_imports_and_streams_without_server_package() -> None:
    result = subprocess.run(
        [sys.executable, "-c", _CLIENT_ONLY_SCRIPT],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "ok"


def test_client_distribution_does_not_depend_on_server_package() -> None:
    pyproject = tomllib.loads((_REPO_ROOT / "sdks/python-client/pyproject.toml").read_text())

    names = [
        dep.split("==")[0].split(">=")[0].split("[")[0].strip().lower()
        for dep in pyproject["project"]["dependencies"]
    ]

    assert "omnigent" not in names
    assert "omnigent" not in pyproject.get("tool", {}).get("uv", {}).get("sources", {})


def test_server_reuses_client_wire_schemas() -> None:
    """One definition: the server's event union is the client's, not a copy."""
    assert server_schemas.ServerStreamEvent is client_wire.ServerStreamEvent
    assert server_schemas.CompletedEvent is client_wire.CompletedEvent
    assert runner_identity.OMNIGENT_INTERNAL_WS_ORIGIN == client_wire.OMNIGENT_INTERNAL_WS_ORIGIN
