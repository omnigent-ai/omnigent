"""UI journey: the composer's context ring belongs in the gray workspace bar, grayscale.

The session composer's context-usage ring (the circular meter showing e.g.
``92%``) must live inside the gray directory/branch bar above the composer
(``composer-workspace-controls``), aligned to the bar's right side, and use the
bar's neutral gray tones at every usage level. The reported bug: the ring
rendered BELOW the composer — in the status tray under the card
(``composer-status-line``) — and still carried the colored green/yellow/red
treatment, turning red at high usage.

Journey driven here, on the real web SPA against a live server + runner and
the real claude CLI pointed at the mock Anthropic endpoint:

1. start a claude-sdk session (spec declares a 200K context window)
2. send one message; the mock scripts every API call the turn makes to report
   a 184,000-token prompt, so the session settles at a high context fill
   (~92% of the spec window; >80% under any effective-window resolution)
3. look at the area around the composer
4. observable failure: the ring + percentage render underneath the composer
   instead of right-aligned in the gray directory/branch bar, and the ring is
   red at high fill instead of grayscale

Regression guard: the final combined assertion (ring sits right-aligned inside
the workspace bar AND its color is neutral gray) FAILS on the current build —
the ring is in the below-composer status line with the ``text-destructive``
red — and passes once the ring moves into the bar and drops the color scale.
Both facets are collected before asserting so a partial fix keeps failing with
a message naming the facet(s) still broken.
"""

from __future__ import annotations

import io
import json
import re
import tarfile
import uuid

import httpx
import pytest
import yaml
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _ensure_runner_online, _server_state, configure_mock_llm

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'
# The context ring exposes its value via aria-label, e.g. "92% of context used".
_RING = '[aria-label$="of context used"]'
# The gray directory/branch bar above the composer — where the ring belongs.
_WORKSPACE_BAR = '[data-testid="composer-workspace-controls"]'
# The status tray below the composer — where the bug leaves the ring.
_STATUS_LINE = '[data-testid="composer-status-line"]'

# Spec-declared window; the scripted prompt usage derives the fill from it.
_CONTEXT_WINDOW = 200_000
# 184000/200000 = 92% with the spec window; a catalog/default 128K effective
# window caps the ring at 100%. Either way the fill lands >80% — the band the
# colored treatment painted red — so the grayscale check is exercised at the
# reported severity without over-fitting the effective-window resolution.
_INPUT_TOKENS = 184_000

# Neutral grays have near-zero RGB chroma (max−min channel spread; the theme's
# muted grays spread ≤ a couple of points). The colored treatment's
# --destructive (#c8324c light / #e65b77 dark) spreads ≥139 and --warning
# (#d4972a) ≥170, so 32 separates "grayscale" from "colored" with wide margin.
_MAX_GRAY_CHROMA = 32.0


def _build_claude_sdk_bundle(name: str, mock_llm_server_url: str) -> bytes:
    """Build a one-file claude-sdk agent bundle wired at the mock LLM.

    Mirrors ``tests/e2e/conftest.register_inline_agent``'s claude-sdk shape:
    ``executor.auth`` (type api_key + base_url) routes the claude CLI's
    ``ANTHROPIC_BASE_URL`` at the mock server, which serves the Anthropic
    ``/v1/messages`` SSE format. ``context_window`` is declared so the SPA's
    context ring renders with a known denominator.

    :param name: Agent name (unique per test run).
    :param mock_llm_server_url: Mock server base URL WITHOUT ``/v1`` (the
        Anthropic SDK appends ``/v1/messages`` itself).
    :returns: The ``.tar.gz`` bundle bytes for multipart upload.
    """
    config = {
        "name": name,
        "prompt": "You are a terse assistant. Answer in as few words as possible.",
        "executor": {
            "harness": "claude-sdk",
            "model": "claude-sonnet-4-20250514",
            "context_window": _CONTEXT_WINDOW,
            "auth": {
                "type": "api_key",
                "api_key": "mock-key",
                "base_url": mock_llm_server_url,
            },
        },
    }
    with io.BytesIO() as buf:
        with tarfile.open(fileobj=buf, mode="w:gz") as tar:
            yaml_bytes = yaml.safe_dump(config, sort_keys=False).encode()
            info = tarfile.TarInfo(f"{name}.yaml")
            info.size = len(yaml_bytes)
            tar.addfile(info, io.BytesIO(yaml_bytes))
        return buf.getvalue()


def _create_claude_sdk_session(base_url: str, runner_id: str, mock_llm_server_url: str) -> str:
    """Create a runner-bound session for a fresh claude-sdk agent.

    :param base_url: Live server base URL.
    :param runner_id: Token-bound runner id to PATCH-bind.
    :param mock_llm_server_url: Mock server base URL (no ``/v1``).
    :returns: The new session id.
    """
    name = f"ctx-ring-{uuid.uuid4().hex[:8]}"
    bundle = _build_claude_sdk_bundle(name, mock_llm_server_url)
    create_resp = httpx.post(
        f"{base_url}/v1/sessions",
        data={"metadata": json.dumps({})},
        files={"bundle": ("agent.tar.gz", bundle, "application/gzip")},
        timeout=30.0,
    )
    create_resp.raise_for_status()
    session_id = create_resp.json()["session_id"]
    patch_resp = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"runner_id": runner_id},
        timeout=10.0,
    )
    patch_resp.raise_for_status()
    return session_id


def _send(page: Page, text: str) -> None:
    """Type *text* into the composer and click Send."""
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible()
    composer.fill(text)
    page.get_by_role("button", name="Send", exact=True).click()


def _rgb_chroma(css_color: str) -> float:
    """Return the RGB chroma (max−min channel spread) of a computed CSS color.

    :param css_color: ``getComputedStyle`` output, e.g. ``"rgb(200, 50, 76)"``
        or ``"rgba(245, 245, 244, 0.56)"``.
    :returns: The spread between the largest and smallest RGB channel —
        ~0 for a neutral gray, large for a saturated red/amber.
    """
    m = re.match(r"rgba?\(\s*(\d+(?:\.\d+)?)[,\s]+(\d+(?:\.\d+)?)[,\s]+(\d+(?:\.\d+)?)", css_color)
    assert m is not None, f"unparseable computed color: {css_color!r}"
    channels = [float(m.group(i)) for i in (1, 2, 3)]
    return max(channels) - min(channels)


@pytest.mark.timeout(600)
def test_context_ring_sits_right_aligned_in_workspace_bar_and_is_grayscale(
    page: Page,
    live_server: str,
    mock_llm_server_url: str,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The context ring renders right-aligned in the gray bar, in grayscale.

    The mock scripts every API call of the single turn to report a
    184,000-token prompt, so the ring settles at a high fill (>80%) — the band
    the buggy colored treatment paints red. Both reported facets are then
    checked against the settled composer area:

    - placement: the ring is inside ``composer-workspace-controls`` (the gray
      directory/branch bar above the composer), on the bar's right half, and
      NOT in the ``composer-status-line`` tray below the composer;
    - grayscale: the ring's computed color is a neutral gray (near-zero RGB
      chroma), not the green/yellow/red scale.

    Pre-fix both facets fail: the ring renders in the below-composer status
    line with the ``text-destructive`` red.
    """
    respawned = _ensure_runner_online(live_server, tmp_path_factory)
    try:
        runner_id = str(_server_state["runner_id"])
        session_id = _create_claude_sdk_session(live_server, runner_id, mock_llm_server_url)
        try:
            uid = uuid.uuid4().hex[:6]
            token = f"ctxring-{uid}"
            # Every API call this turn makes (the CLI can add follow-up
            # calls, e.g. skills/system-reminder) sees the same 184K-token
            # prompt usage, so the LAST observed call — which is what
            # context_tokens reports — is deterministic at the high fill.
            configure_mock_llm(
                mock_llm_server_url,
                [{"text": "ack", "usage": {"input_tokens": _INPUT_TOKENS}}] * 6,
                key=f"ctx-ring-{uid}",
                match=token,
            )

            page.goto(f"{live_server}/c/{session_id}")

            # ── One successful turn; the session settles at a high fill ──
            _send(page, f"Say ack. {token}")
            expect(page.locator(_ASSISTANT).first).to_be_visible(timeout=120_000)
            expect(page.locator(_WORKING)).to_have_count(0, timeout=120_000)

            ring = page.locator(_RING).first
            expect(ring).to_be_visible(timeout=30_000)
            expect(ring).to_have_attribute(
                "aria-label",
                re.compile(r"^(?:8[0-9]|9[0-9]|100)% of context used$"),
                timeout=30_000,
            )

            # ── Collect both reported facets, then assert them together so
            # a partial fix still fails, naming the facet(s) left broken ──
            failures: list[str] = []

            # Facet 1 — placement: right-aligned inside the gray bar above
            # the composer, not in the status tray below it.
            bar = page.locator(_WORKSPACE_BAR)
            expect(bar).to_be_visible()
            rings_in_bar = page.locator(f"{_WORKSPACE_BAR} {_RING}").count()
            rings_below_composer = page.locator(f"{_STATUS_LINE} {_RING}").count()
            if rings_in_bar != 1 or rings_below_composer != 0:
                failures.append(
                    "placement: ring is not in the gray directory/branch bar "
                    f"(in-bar={rings_in_bar}, below-composer={rings_below_composer})"
                )
            else:
                bar_box = bar.bounding_box()
                ring_box = page.locator(f"{_WORKSPACE_BAR} {_RING}").bounding_box()
                assert bar_box is not None and ring_box is not None
                ring_center_x = ring_box["x"] + ring_box["width"] / 2
                bar_center_x = bar_box["x"] + bar_box["width"] / 2
                if ring_center_x <= bar_center_x:
                    failures.append(
                        "placement: ring sits in the bar's left half "
                        f"(ring center x={ring_center_x:.0f}, bar center x={bar_center_x:.0f}); "
                        "expected right-aligned (directory/branch stay left)"
                    )

            # Facet 2 — grayscale: at >80% fill the colored treatment turned
            # the ring red; the agreed design keeps it neutral gray at every
            # level. Checked on the ring wherever it rendered, so this facet
            # is guarded even while the placement facet is still broken.
            ring_color = ring.evaluate("el => getComputedStyle(el).color")
            chroma = _rgb_chroma(ring_color)
            if chroma > _MAX_GRAY_CHROMA:
                failures.append(
                    f"grayscale: ring color {ring_color} is saturated "
                    f"(RGB chroma {chroma:.0f} > {_MAX_GRAY_CHROMA:.0f}); "
                    "expected the bar's neutral gray at every usage level"
                )

            assert not failures, "context ring regressions: " + "; ".join(failures)
        finally:
            httpx.delete(f"{live_server}/v1/sessions/{session_id}", timeout=10.0)
    finally:
        if respawned is not None:
            respawned.terminate()
            try:
                respawned.wait(timeout=5)
            except Exception:  # best-effort teardown
                respawned.kill()
                respawned.wait(timeout=5)
