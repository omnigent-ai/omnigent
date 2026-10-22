"""E2E: the session list shows an in-progress state while a real host daemon imports sessions."""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests._helpers.host import isolated_host

pytestmark = pytest.mark.min_server_version("0.16.0")

# Enough sessions for the import to stay in flight for several seconds (about 30 ms
# per session end to end); the picker count below must cover them.
_SEEDED_SESSIONS = 600
_LIMIT_OPTION = "Last 1000"
_IMPORT_TIMEOUT_S = 300.0
# Harness homes that would redirect the host's transcript scan away from its HOME.
_HARNESS_HOME_ENV = ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "QWEN_HOME", "PI_CODING_AGENT_DIR")


@dataclass
class _ImportHost:
    host_id: str
    name: str
    # Titles are synthesized from the first user message; keep "import" out of them
    # so the indicator check cannot match an imported row.
    title_prefix: str


def _seed_claude_transcripts(home: Path, count: int, title_prefix: str) -> None:
    projects = home / ".claude" / "projects" / "-repo"
    projects.mkdir(parents=True, exist_ok=True)
    now = time.time()
    for index in range(count):
        session_id = str(uuid.uuid4())
        records = [
            {
                "type": "user",
                "uuid": f"{session_id}-user-1",
                "cwd": "/repo",
                "message": {"role": "user", "content": f"{title_prefix} {index + 1}"},
            },
            {
                "type": "assistant",
                "uuid": f"{session_id}-assistant-1",
                "message": {"role": "assistant", "content": [{"type": "text", "text": "Done."}]},
            },
        ]
        path = projects / f"{session_id}.jsonl"
        path.write_text("".join(json.dumps(r) + "\n" for r in records), encoding="utf-8")
        stamp = now - (count - index) * 10
        os.utime(path, (stamp, stamp))


@pytest.fixture
def import_host(live_server: str, tmp_path: Path) -> Iterator[_ImportHost]:
    """A real host daemon whose HOME carries fresh, not-yet-imported Claude transcripts."""
    tag = uuid.uuid4().hex[:8]
    name = f"transcripts-host-{tag}"
    with isolated_host(
        tmp_path / "host",
        live_server,
        name=name,
        env=dict.fromkeys(_HARNESS_HOME_ENV),
    ) as host:
        title_prefix = f"Seeded Claude transcript {tag}"
        _seed_claude_transcripts(host.home, _SEEDED_SESSIONS, title_prefix)
        with httpx.Client(base_url=live_server) as client:
            host.wait_online(client, timeout=90)
        yield _ImportHost(host_id=host.host_id, name=name, title_prefix=title_prefix)


def _imported_count(live_server: str, title_prefix: str) -> int:
    """Sessions persisted so far for this import, read through the public list API."""
    response = httpx.get(
        f"{live_server}/v1/sessions",
        params={"limit": 1000, "search_query": title_prefix, "visibility": "all"},
        timeout=10.0,
    )
    response.raise_for_status()
    return len(response.json().get("data", []))


@pytest.mark.timeout(600)
@pytest.mark.parametrize(
    "mobile",
    [
        pytest.param(False, id="desktop"),
        pytest.param(
            True,
            id="mobile",
            marks=pytest.mark.browser_context_args(
                viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
            ),
        ),
    ],
)
def test_session_list_shows_import_in_progress(
    request: pytest.FixtureRequest,
    live_server: str,
    import_host: _ImportHost,
    mobile: bool,
) -> None:
    """Going Back to the session list mid-import shows that sessions are being imported."""
    page: Page = request.getfixturevalue("page")
    aside = page.locator("aside[aria-label='Conversations']")

    def leave_settings() -> None:
        if mobile:
            page.get_by_role("button", name="Back to settings menu", exact=True).click()
        page.get_by_role("link", name="Back", exact=True).click()
        if mobile and aside.get_attribute("aria-hidden") == "true":
            page.get_by_role("button", name="Open sidebar", exact=True).click()

    page.goto(f"{live_server}/")
    if mobile:
        page.get_by_role("button", name="Open sidebar", exact=True).click()
    page.get_by_role("link", name="Settings", exact=True).click()
    page.get_by_test_id("settings-nav-import").click()
    expect(page.get_by_test_id("import-sessions-panel")).to_be_visible(timeout=30_000)
    page.get_by_test_id("import-host-select").click()
    page.get_by_role("option").filter(has_text=import_host.name).click()
    page.get_by_test_id("import-source-select").click()
    page.get_by_role("option", name="Claude Code").click()
    page.get_by_test_id("import-limit-select").click()
    page.get_by_role("option", name=_LIMIT_OPTION).click()

    started = time.monotonic()
    page.get_by_test_id("import-submit").click()
    expect(page.get_by_test_id("import-progress")).to_be_visible(timeout=30_000)

    leave_settings()
    session_list = page.get_by_test_id("sidebar-conversation-list")
    expect(session_list).to_be_visible(timeout=30_000)
    indicator = aside.get_by_test_id("sidebar-import-progress")

    indicator_seen_in_flight = False
    checks_in_flight = 0
    while True:
        count = _imported_count(live_server, import_host.title_prefix)
        if count >= _SEEDED_SESSIONS:
            break
        checks_in_flight += 1
        if indicator.count() > 0 and indicator.first.is_visible():
            expect(
                aside.get_by_role("heading").filter(
                    has=page.get_by_test_id("sidebar-import-progress")
                )
            ).to_contain_text("Sessions")
            indicator_seen_in_flight = True
            break
        if time.monotonic() - started > _IMPORT_TIMEOUT_S:
            pytest.fail(
                f"import still at {count}/{_SEEDED_SESSIONS} after {_IMPORT_TIMEOUT_S:.0f}s"
            )
        page.wait_for_timeout(1000)
    elapsed = time.monotonic() - started
    if not indicator_seen_in_flight:
        assert checks_in_flight >= 3, (
            f"import finished in {elapsed:.1f}s, too fast to observe the list"
        )

    assert indicator_seen_in_flight, (
        f"session list showed no in-progress indicator during the {elapsed:.0f}s import "
        f"({checks_in_flight} checks); imported sessions simply appeared"
    )

    indicator.get_by_role("link").click()
    if mobile:
        expect(aside).to_have_attribute("aria-hidden", "true")
    # A panel behind the mobile Settings menu is visible to DOM assertions but
    # cannot receive a tap. Check that navigation really exposes the content.
    page.get_by_test_id("import-progress").click(trial=True)
    expect(page.get_by_test_id("import-limit-select")).to_have_text(_LIMIT_OPTION)
    leave_settings()
    expect(aside.get_by_text(import_host.title_prefix).first).to_be_visible(timeout=60_000)

    # The indicator is transient: once every session has landed the list goes
    # back to plain rows.
    while _imported_count(live_server, import_host.title_prefix) < _SEEDED_SESSIONS:
        if time.monotonic() - started > _IMPORT_TIMEOUT_S:
            pytest.fail(f"import did not finish within {_IMPORT_TIMEOUT_S:.0f}s")
        page.wait_for_timeout(1000)
    expect(indicator).to_have_count(0, timeout=30_000)
