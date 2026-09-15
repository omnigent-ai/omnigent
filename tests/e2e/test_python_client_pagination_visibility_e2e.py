"""The python-client's listing methods must not hide pagination.

``GET /v1/sessions/{id}/items`` and ``GET /v1/sessions/{id}/child_sessions``
both return the documented paginated envelope (``data`` + ``first_id`` /
``last_id`` / ``has_more`` — see ``routes_items.py``), and the child listing
accepts an ``after`` cursor. The Python client's public wrappers drop all of
it:

* ``SessionsNamespace.list_items`` returns the bare ``data`` rows, so a caller
  who received exactly ``limit`` rows cannot learn whether more exist — a
  truncated transcript is indistinguishable from a complete one.
* ``SessionsNamespace.child_sessions`` does the same AND exposes no ``after``
  parameter, so children past the first page are unreachable through the
  client at all.
* ``SessionsNamespace.resolve_agent`` is the one cursor-following walk, and it
  treats a stalled cursor (a page reporting ``has_more`` without a ``last_id``)
  as a clean end of the listing — answering "no agent named X" for an agent
  that lives on a page it never fetched.

The first two tests drive the real client against a LIVE server,
deterministically (no LLM turns: items are seeded via ``POST /v1/imports``,
children via ``POST /v1/sessions`` with ``parent_session_id``). The
stalled-cursor walk needs a degenerate listing page the real server never
emits (``paginate_in_memory`` always stamps ``last_id`` on a non-empty page),
so that test speaks real HTTP to a local stub serving the injected fault.

The assertions accept any honest fix shape — pagination metadata carried on
the return, or a client that follows the cursor itself and returns everything
— and fail on the silent-prefix behavior of a client that drops the metadata.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import httpx
import pytest
from omnigent_client._sessions import SessionsNamespace


def _import_session_with_items(http_client: httpx.Client, item_count: int) -> str:
    """Seed a session holding *item_count* message items via ``POST /v1/imports``.

    The import route appends the normalized items synchronously and needs no
    runner or LLM, so the resulting transcript length is exact and stable.
    """
    items: list[dict[str, Any]] = []
    for turn in range(item_count):
        role = "user" if turn % 2 == 0 else "assistant"
        block_type = "input_text" if role == "user" else "output_text"
        data: dict[str, Any] = {
            "role": role,
            "content": [{"type": block_type, "text": f"turn {turn}"}],
        }
        if role == "assistant":
            data["agent"] = "claude-native-ui"
        items.append({"type": "message", "response_id": f"turn-{turn // 2}", "data": data})
    resp = http_client.post(
        "/v1/imports",
        json={
            "source": "claude",
            "external_session_id": f"pagination-visibility-{uuid.uuid4().hex}",
            "items": items,
        },
    )
    resp.raise_for_status()
    return str(resp.json()["session_id"])


def _rows_of(result: object) -> list[Any]:
    """Extract the row list from a bare list or any paginated-envelope shape."""
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        data = result.get("data")
        return data if isinstance(data, list) else []
    data = getattr(result, "data", None)
    return data if isinstance(data, list) else []


def _has_more_of(result: object) -> bool | None:
    """Read a truncation signal off the result, whatever shape a fix chose."""
    if isinstance(result, dict):
        value = result.get("has_more")
    else:
        value = getattr(result, "has_more", None)
    return None if value is None else bool(value)


def test_list_items_truncation_is_observable(
    live_server: str,
    http_client: httpx.Client,
) -> None:
    """A ``list_items`` page smaller than the transcript must be detectably truncated."""
    session_id = _import_session_with_items(http_client, 4)

    # Contract precondition, not the bug: the endpoint reports the truncation.
    resp = http_client.get(f"/v1/sessions/{session_id}/items", params={"limit": 2})
    resp.raise_for_status()
    server_page = resp.json()
    assert server_page["has_more"] is True, f"server page unexpectedly complete: {server_page}"
    assert server_page["last_id"], f"server page carries no cursor: {server_page}"

    async def _drive() -> object:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            return await SessionsNamespace(ac, live_server).list_items(session_id, limit=2)

    listing = asyncio.run(_drive())

    rows = _rows_of(listing)
    assert (_has_more_of(listing) is True) or (len(rows) >= 4), (
        f"list_items(limit=2) on a 4-item session returned {len(rows)} bare "
        "row(s) with no truncation signal — the client discards the server's "
        "has_more/last_id, so a truncated listing is indistinguishable from a "
        "complete one"
    )


def test_child_sessions_truncation_observable_and_page_two_reachable(
    live_server: str,
    http_client: httpx.Client,
) -> None:
    """A child listing past ``limit`` must be observable and reachable."""
    parent = _import_session_with_items(http_client, 2)
    snapshot = http_client.get(f"/v1/sessions/{parent}")
    snapshot.raise_for_status()
    agent_id = str(snapshot.json()["agent_id"])
    children: set[str] = set()
    for _ in range(2):
        resp = http_client.post(
            "/v1/sessions",
            json={"agent_id": agent_id, "parent_session_id": parent},
        )
        resp.raise_for_status()
        children.add(str(resp.json()["id"]))

    # Contract precondition, not the bug: the endpoint reports the truncation
    # and accepts an ``after`` cursor for the next page.
    resp = http_client.get(f"/v1/sessions/{parent}/child_sessions", params={"limit": 1})
    resp.raise_for_status()
    server_page = resp.json()
    assert server_page["has_more"] is True, f"server page unexpectedly complete: {server_page}"
    assert server_page["last_id"], f"server page carries no cursor: {server_page}"

    async def _drive() -> object:
        async with httpx.AsyncClient(timeout=30.0) as ac:
            return await SessionsNamespace(ac, live_server).child_sessions(parent, limit=1)

    listing = asyncio.run(_drive())
    rows = _rows_of(listing)

    truncation_observable = (_has_more_of(listing) is True) or (len(rows) >= 2)
    signature = inspect.signature(SessionsNamespace.child_sessions)
    page_two_reachable = ("after" in signature.parameters) or (len(rows) >= 2)
    assert truncation_observable and page_two_reachable, (
        f"child_sessions(limit=1) under a parent with {len(children)} children "
        f"returned {len(rows)} bare row(s): truncation observable="
        f"{truncation_observable} (has_more discarded), second page reachable="
        f"{page_two_reachable} (no 'after' cursor parameter) — children past "
        "the first page are unreachable through the client"
    )


class _StalledCursorAgentListing(BaseHTTPRequestHandler):
    """``GET /v1/agents`` page that reports more but supplies no cursor."""

    def do_GET(self) -> None:
        if not self.path.startswith("/v1/agents"):
            self.send_error(404)
            return
        body = json.dumps(
            {
                "object": "list",
                "data": [{"id": "ag_decoy", "name": "decoy", "harness": None}],
                "first_id": "ag_decoy",
                "last_id": None,
                "has_more": True,
            }
        ).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args: object) -> None:
        """Keep the stub quiet in test output."""


def test_resolve_agent_raises_on_stalled_cursor_instead_of_clean_miss() -> None:
    """A stalled listing cursor must not be reported as "agent does not exist".

    The walk's only quiet end is the server saying ``has_more`` is false;
    stopping for any other reason (here: ``has_more=True`` with no ``last_id``
    to follow) must raise rather than answer with a clean
    ``LookupError("No agent named …")`` for an agent on the unreached page.
    """
    server = ThreadingHTTPServer(("127.0.0.1", 0), _StalledCursorAgentListing)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base_url = f"http://127.0.0.1:{server.server_address[1]}"
    try:

        async def _drive() -> None:
            async with httpx.AsyncClient(timeout=10.0) as ac:
                await SessionsNamespace(ac, base_url).resolve_agent("needle")

        try:
            asyncio.run(_drive())
        except LookupError as exc:
            assert "No agent named" not in str(exc), (
                "resolve_agent hit a page reporting has_more=True with no "
                "last_id cursor and reported a clean miss for an agent on the "
                f"unreached page: {exc!r} — a stalled cursor must raise an "
                "error, not masquerade as 'agent does not exist'"
            )
        except Exception:
            # Any loud failure is acceptable fixed behavior: the walk must not
            # end quietly for any reason other than has_more=False.
            pass
        else:
            pytest.fail(
                "resolve_agent('needle') returned successfully from a listing "
                "whose second page was never fetchable"
            )
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()
