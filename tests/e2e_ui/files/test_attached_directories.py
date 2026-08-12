"""E2E: the Files panel keeps attached directory roots distinct.

An attached directory is a separate environment resource, not another path
inside the primary workspace. This journey gives the browser two environment
IDs, opens a file that exists only in the attached root, and verifies the file
content request remains qualified by that root's ID.
"""

from __future__ import annotations

import json
import re
from urllib.parse import urlparse

from playwright.sync_api import Page, Route, expect

from tests.e2e_ui.conftest import open_right_rail

_ATTACHED_ENVIRONMENT_ID = "dir_00000000000000000000000000004620"
_ATTACHED_FILE = "attached-note.md"
_ATTACHED_CONTENT = "hello from the attached repository"


def test_files_panel_opens_a_file_from_an_attached_directory(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """Render both roots and open the attached root's environment-scoped file."""
    base_url, session_id = seeded_session
    resource_path = f"/v1/sessions/{session_id}/resources/environments"
    observed_environment_ids: set[str] = set()

    def handle_resources(route: Route) -> None:
        request_path = urlparse(route.request.url).path
        if request_path == resource_path:
            response = route.fetch()
            payload = response.json()
            payload["data"] = [
                {
                    "id": "default",
                    "name": "Primary environment",
                    "metadata": {
                        "filesystem": True,
                        "root": "/home/e2e/repo",
                        "home": "/home/e2e",
                    },
                },
                {
                    "id": _ATTACHED_ENVIRONMENT_ID,
                    "name": "Shared repository",
                    "metadata": {
                        "filesystem": True,
                        "root": "/home/e2e/shared",
                        "home": "/home/e2e",
                    },
                },
            ]
            route.fulfill(
                status=200,
                headers={**response.headers, "content-type": "application/json"},
                body=json.dumps(payload),
            )
            return

        suffix = request_path.removeprefix(f"{resource_path}/")
        environment_id = suffix.split("/", 1)[0]
        if environment_id:
            observed_environment_ids.add(environment_id)
        if environment_id != _ATTACHED_ENVIRONMENT_ID:
            route.continue_()
            return

        attached_suffix = suffix.removeprefix(_ATTACHED_ENVIRONMENT_ID)
        if attached_suffix == "":
            body = {
                "id": _ATTACHED_ENVIRONMENT_ID,
                "name": "Shared repository",
                "metadata": {
                    "filesystem": True,
                    "root": "/home/e2e/shared",
                    "home": "/home/e2e",
                },
            }
        elif attached_suffix == "/changes":
            body = {"object": "list", "data": [], "has_more": False}
        elif attached_suffix == "/filesystem":
            body = {
                "object": "list",
                "data": [
                    {
                        "id": _ATTACHED_FILE,
                        "name": _ATTACHED_FILE,
                        "path": _ATTACHED_FILE,
                        "type": "file",
                        "bytes": len(_ATTACHED_CONTENT),
                        "modified_at": 0,
                    }
                ],
                "has_more": False,
            }
        elif attached_suffix == f"/filesystem/{_ATTACHED_FILE}":
            body = {
                "object": "session.environment.filesystem.file_content",
                "path": _ATTACHED_FILE,
                "content_type": "text/markdown",
                "encoding": "utf-8",
                "content": _ATTACHED_CONTENT,
                "bytes": len(_ATTACHED_CONTENT),
            }
        else:
            route.fulfill(status=404, content_type="application/json", body="{}")
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    page.route(
        re.compile(rf"/v1/sessions/{re.escape(session_id)}/resources/environments(?:/|\?|$)"),
        handle_resources,
    )

    try:
        page.goto(f"{base_url}/c/{session_id}")
        open_right_rail(page)
        rail = page.get_by_role("complementary", name="Workspace")
        rail.get_by_role("tab", name=re.compile("^Files")).click()

        # The two resource IDs render as separate directory groups.
        expect(rail.get_by_text("Working folder")).to_be_visible(timeout=30_000)
        expect(rail.get_by_text("Shared repository")).to_be_visible(timeout=30_000)

        file_button = rail.get_by_role(
            "button", name=re.compile(re.escape(_ATTACHED_FILE))
        ).filter(has_text=_ATTACHED_FILE)
        expect(file_button).to_be_visible(timeout=30_000)
        file_button.click()

        file_viewer = rail.get_by_test_id("file-viewer")
        expect(file_viewer).to_be_visible()
        expect(file_viewer.get_by_text(_ATTACHED_CONTENT).first).to_be_visible(timeout=20_000)
        assert {"default", _ATTACHED_ENVIRONMENT_ID}.issubset(observed_environment_ids)
    finally:
        page.unroute_all(behavior="ignoreErrors")
