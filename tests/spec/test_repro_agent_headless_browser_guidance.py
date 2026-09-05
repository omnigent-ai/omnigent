"""Contract tests: headless repro runs must not chase dead ``browser_*`` calls.

A headless session (CI — no Omnigent UI has the session open) has no
browser-capable renderer subscribed, so the server strips the ``browser_*``
tools from each turn and any call that still slips through fails immediately
with ``{"error": "no browser renderer is connected"}``. The repro-agent
guidance must present missing browser tools as the expected headless shape
rather than a failure to probe or retry, and must route headless UI journeys
onto the Playwright lane instead.
"""

from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
_GUIDANCE = (_ROOT / "dev" / "repro-agent" / "AGENTS.md").read_text()


def _section(text: str, start: str, end: str) -> str:
    """Return the slice of ``text`` between the unique ``start``/``end`` markers."""
    body = text.split(start, 1)[1]
    return body.split(end, 1)[0]


def test_guidance_names_the_headless_no_renderer_failure() -> None:
    # The exact error a headless browser_* call returns. Naming it verbatim
    # lets a session recognize the failure as terminal for the whole run
    # instead of re-calling browser_navigate hoping a renderer appears.
    assert "no browser renderer is connected" in _GUIDANCE


def test_preflight_expects_browser_tools_to_be_absent_on_headless_runs() -> None:
    preflight = _section(_GUIDANCE, "## Preflight", "## Step 1")
    # The preflight tool check must carry the headless carve-out: browser
    # tools exist only while a UI renderer is attached, so their absence on a
    # headless run is expected rather than an operational failure to retry.
    assert "browser_navigate" in preflight
    assert "headless" in preflight


def test_ui_lane_routes_headless_journeys_through_playwright() -> None:
    ui_lane = _section(_GUIDANCE, "**UI bugs**", "**Backend/behavioral bugs**")
    # With no renderer attached the UI journey must be driven on the
    # Playwright lane, not abandoned to the backend path or retried against
    # browser tools that can never be served.
    assert "headless" in ui_lane
    assert "Playwright" in ui_lane
