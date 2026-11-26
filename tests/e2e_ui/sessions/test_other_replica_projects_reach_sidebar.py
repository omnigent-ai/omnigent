"""Browser e2e: projects that another server replica creates reach an open sidebar.

Two real servers share one database but not the in-process fan-out behind the
``WS /v1/sessions/updates`` discovery push, like replicas behind a load
balancer. The browser loads the UI from replica A while another client writes
through replica B, as a REST script or an agent's ``sys_session_create`` does
when its requests are routed to a different replica than the browser's stream.
B's ``projects_changed`` and ``session_added`` pushes reach only B's streams, so
the sidebar must pick these writes up through its own periodic refresh.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, WebSocket, expect

from tests._helpers.live_server import isolated_local_server
from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle

# A push through the browser's own replica arrives within seconds.
_PUSH_WINDOW_MS = 10_000
# The sidebar refreshes its lists once a minute; allow one slow round on top.
_REFRESH_WINDOW_S = 90


@pytest.fixture
def replicas(tmp_path: Path, built_spa: None) -> Iterator[tuple[str, str]]:
    """Start two servers on one database and artifact store; yield their URLs.

    :param tmp_path: Per-test root for both servers' homes, logs and the shared stores.
    :param built_spa: Ensures the UI bundle the servers serve is built.
    :returns: ``(replica_a_url, replica_b_url)``.
    """
    shared = tmp_path / "shared"
    shared.mkdir()
    shared_stores = [
        "--database-uri",
        f"sqlite:///{shared / 'chat.db'}",
        "--artifact-location",
        str(shared / "artifacts"),
    ]
    with (
        isolated_local_server(tmp_path / "a", extra_args=shared_stores) as replica_a,
        isolated_local_server(tmp_path / "b", extra_args=shared_stores) as replica_b,
    ):
        yield replica_a, replica_b


def _create_project(base_url: str, name: str) -> str:
    response = httpx.post(f"{base_url}/v1/projects", json={"name": name}, timeout=10.0)
    response.raise_for_status()
    return response.json()["id"]


def _create_session(base_url: str, **metadata: object) -> str:
    response = post_session_bundle(
        httpx.post,
        f"{base_url}/v1/sessions",
        _build_hello_world_bundle(),
        metadata=metadata,
        timeout=30.0,
    )
    response.raise_for_status()
    return response.json()["session_id"]


def _section_header(page: Page, title: str) -> Locator:
    """The collapse header of the sidebar section titled *title* (Sessions, or a project)."""
    return page.get_by_role("button", name=title, exact=True)


def _section(page: Page, title: str) -> Locator:
    return page.locator("section").filter(has=_section_header(page, title))


def _row_in_folder(page: Page, project: str, session_id: str) -> bool:
    """Expand *project*'s folder once it is listed, then report whether it shows the session."""
    header = _section_header(page, project)
    if not header.is_visible():
        return False
    if header.get_attribute("aria-expanded") == "false":
        # The chevron toggles the folder; the rest of the header opens the project.
        header.locator("[data-section-toggle]").first.click()
    return _section(page, project).locator(f'a[href="/c/{session_id}"]').is_visible()


@pytest.mark.timeout(240)
def test_projects_created_through_another_replica_reach_the_sidebar(
    page: Page, replicas: tuple[str, str]
) -> None:
    """Folders for projects written through replica B appear in a tab served by replica A.

    The control project, written through A, arrives by push. Everything written
    through B can arrive only by the sidebar's own refresh: an empty project, a
    project holding a session, and an unfiled session (which the session list's
    refresh already covers). Each is checked on its own, and a failure names
    whichever did not appear.

    :param page: Playwright page (default desktop viewport) served by replica A.
    :param replicas: ``(replica_a_url, replica_b_url)`` sharing one database.
    """
    replica_a, replica_b = replicas
    uniq = uuid.uuid4().hex[:6]
    snapshots: list[str | bytes] = []

    def watch_updates(socket: WebSocket) -> None:
        if "/v1/sessions/updates" in socket.url:
            socket.on(
                "framereceived",
                lambda frame: snapshots.append(frame) if '"snapshot"' in str(frame) else None,
            )

    existing_id = _create_session(replica_a, title=f"Existing {uniq}")
    page.on("websocket", watch_updates)
    page.goto(replica_a)
    expect(page.locator(f'a[href="/c/{existing_id}"]')).to_be_visible(timeout=30_000)
    # The tab's updates stream is connected to A and serving its watched rows.
    # Waiting through Playwright lets it deliver the frame events a sleep would hold back.
    deadline = time.monotonic() + 30
    while not snapshots and time.monotonic() < deadline:
        page.wait_for_timeout(250)
    assert snapshots, "replica A's updates stream sent this tab no snapshot"

    same_replica = f"Same replica {uniq}"
    _create_project(replica_a, same_replica)
    expect(_section_header(page, same_replica)).to_be_visible(timeout=_PUSH_WINDOW_MS)

    empty_project = f"Other replica {uniq}"
    filed_project = f"Other replica filed {uniq}"
    _create_project(replica_b, empty_project)
    filed_id = _create_session(
        replica_b,
        title=f"Filed from replica B {uniq}",
        project_id=_create_project(replica_b, filed_project),
    )
    unfiled_id = _create_session(replica_b, title=f"Unfiled from replica B {uniq}")
    # A reads B's writes from the shared database.
    listed = httpx.get(f"{replica_a}/v1/sessions/projects", timeout=10.0)
    listed.raise_for_status()
    assert {empty_project, filed_project} <= {project["name"] for project in listed.json()}

    checks: dict[str, Callable[[], bool]] = {
        f"folder {empty_project!r}": lambda: _section_header(page, empty_project).is_visible(),
        f"session in folder {filed_project!r}": lambda: _row_in_folder(
            page, filed_project, filed_id
        ),
        "unfiled session in Sessions": lambda: (
            _section(page, "Sessions").locator(f'a[href="/c/{unfiled_id}"]').is_visible()
        ),
    }
    shown_after: dict[str, float] = {}
    started = time.monotonic()
    while len(shown_after) < len(checks) and time.monotonic() - started < _REFRESH_WINDOW_S:
        for name, check in checks.items():
            if name not in shown_after and check():
                shown_after[name] = round(time.monotonic() - started, 1)
        page.wait_for_timeout(500)

    missing = [name for name in checks if name not in shown_after]
    assert not missing, (
        f"not shown within {_REFRESH_WINDOW_S}s of the writes through replica B: {missing}; "
        f"shown (seconds after the writes): {shown_after}"
    )
