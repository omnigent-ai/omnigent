"""Archived sessions on the shared live server, seeded over the REST API for browser journeys."""

from __future__ import annotations

import contextlib

import httpx

from tests._helpers.session import post_session_bundle
from tests.e2e_ui.conftest import _build_hello_world_bundle

# Reserved label key that stores project membership (see
# ``sqlalchemy_store.list_projects`` and ``web/src/lib/sessionListCache.ts``).
_PROJECT_LABEL_KEY = "omni_project"


def seed_archived_session(base_url: str, *, title: str, project: str | None) -> str:
    """Create and archive a titled session in an optional project.

    :param base_url: The live server base URL.
    :param title: Unique title so the row is easy to spot among other tests'
        sessions on the shared server.
    :param project: Project name to file the session under, or ``None`` to
        leave it unfiled.
    :returns: The new session id.
    """
    create_resp = post_session_bundle(
        httpx.post, f"{base_url}/v1/sessions", _build_hello_world_bundle(), timeout=30.0
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]

    body: dict[str, object] = {"title": title, "archived": True}
    if project is not None:
        body["labels"] = {_PROJECT_LABEL_KEY: project}
    try:
        httpx.patch(
            f"{base_url}/v1/sessions/{session_id}",
            json=body,
            timeout=10.0,
        ).raise_for_status()
    except httpx.HTTPError:
        # The caller never gets the id, so it can't clean the session up itself.
        delete_sessions(base_url, [session_id])
        raise
    return session_id


def delete_sessions(base_url: str, session_ids: list[str]) -> None:
    """Best-effort cleanup so seeded sessions don't leak into other tests."""
    for session_id in session_ids:
        with contextlib.suppress(httpx.HTTPError):
            httpx.delete(f"{base_url}/v1/sessions/{session_id}", timeout=10.0)
