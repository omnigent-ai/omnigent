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

import omnigent_client.session_lifecycle as client_session_lifecycle
import omnigent_client.wire as client_wire
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

import omnigent.runner.identity as runner_identity
import omnigent.server.schemas as server_schemas
import omnigent.util.session_lifecycle as server_session_lifecycle

_REPO_ROOT = Path(__file__).resolve().parents[3]

# Runs in a child interpreter with ``omnigent`` made unimportable. Imports
# every client and UI SDK module, streams a session over a mocked transport
# so the SSE envelopes are parsed and typed by the client alone, and drives
# the terminal host's sub-agent tree and enter/exit.
_CLIENT_ONLY_SCRIPT = textwrap.dedent(
    """
    import asyncio
    import importlib
    import importlib.abc
    import logging
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
    import omnigent_ui_sdk
    from omnigent_client._sessions import SessionsNamespace
    from omnigent_client.wire import OutputTextDeltaEvent
    from omnigent_ui_sdk import TerminalHost

    for package in (omnigent_client, omnigent_ui_sdk):
        for module in pkgutil.walk_packages(package.__path__, package.__name__ + "."):
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

    errors = []
    handler = logging.Handler(logging.ERROR)
    handler.emit = lambda record: errors.append(record.getMessage())
    logging.getLogger().addHandler(handler)


    async def enter_and_exit(host):
        async with host:
            pass


    host = TerminalHost()
    host.upsert_subagent(
        "conv_child", parent_id="conv_parent", child={"labels": {"omnigent.closed": "true"}}
    )
    assert not host.is_subagent_chattable("conv_child")
    asyncio.run(enter_and_exit(host))
    assert not errors, errors
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

    names = {
        canonicalize_name(Requirement(dep).name) for dep in pyproject["project"]["dependencies"]
    }

    assert "omnigent" not in names
    assert "omnigent" not in pyproject.get("tool", {}).get("uv", {}).get("sources", {})


def test_server_reuses_client_wire_schemas() -> None:
    """One definition: the server's event union and closed-session check are the client's."""
    assert server_schemas.ServerStreamEvent is client_wire.ServerStreamEvent
    assert server_schemas.CompletedEvent is client_wire.CompletedEvent
    assert server_session_lifecycle.is_session_closed is client_session_lifecycle.is_session_closed
    assert runner_identity.OMNIGENT_INTERNAL_WS_ORIGIN == client_wire.OMNIGENT_INTERNAL_WS_ORIGIN
