"""Browser e2e: forking while SWITCHING the agent carries history forward.

The fork dialog's agent picker lets a fork bind a DIFFERENT built-in agent
than the source — "fork this Claude-SDK chat into Claude Code". This drives
that flow end-to-end (real per-message action → dialog → agent select →
``POST /v1/sessions/{id}/fork`` with ``agent_id`` → navigate) for three
targets whose SOURCE is the seeded ``hello_world`` openai-agents SDK agent:

  - SDK → a DIFFERENT SDK agent (``files_probe_env``, openai-agents)
  - SDK → Claude Code   (``claude-native-ui``,  anthropic native, X-family)
  - SDK → Codex         (``codex-native-ui``,   openai native, same-family)

Each case asserts what the server+UI can guarantee WITHOUT a host or a real
native CLI: the fork is created on the TARGET agent, the copied transcript
renders, and the fork's labels route the runner correctly —

  - native targets stamp ``omnigent.fork.carry_history`` (the runner must
    rebuild the native transcript; absent → the clone would launch fresh
    and lose history) and the TARGET ``omnigent.wrapper`` (so the clone
    opens in the right UI mode, not the source's chat mode);
  - the SDK target stamps neither (an SDK target replays the transcript as
    context, and plain chat has no wrapper).

The full "the native clone actually recalls source history" recall path
needs a host + a logged-in ``claude``/``codex`` CLI, which the e2e_ui
harness doesn't spawn; that is covered at the API level by
``tests/e2e/test_host_cross_family_fork_e2e.py``. Directions where the
SOURCE is native (``claude code → *``, ``codex → *``) can't run here at all
— producing the assistant bubble the fork action anchors on would require
the native CLI to take a turn.
"""

from __future__ import annotations

import re

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _FILES_PROBE_ENV_AGENT_NAME

# Unique marker so the copied-transcript assertion can't match UI chrome or
# another test's message.
_MARKER = "tangerine-switch-marker"

_WRAPPER_LABEL_KEY = "omnigent.wrapper"
_CARRY_HISTORY_LABEL_KEY = "omnigent.fork.carry_history"
_SOURCE_EXTERNAL_SESSION_LABEL_KEY = "omnigent.fork.source_external_session_id"


def _agent_id_by_name(base_url: str, name: str) -> str:
    """Resolve a built-in agent's id by name from ``GET /v1/agents``.

    :param base_url: Live server base URL, e.g. ``"http://127.0.0.1:51234"``.
    :param name: Built-in agent name, e.g. ``"codex-native-ui"``.
    :returns: The agent id.
    """
    resp = httpx.get(f"{base_url}/v1/agents", params={"limit": 100}, timeout=30.0)
    resp.raise_for_status()
    agent = next((a for a in resp.json()["data"] if a["name"] == name), None)
    assert agent is not None, (
        f"built-in agent {name!r} not registered on the test server — the SDK "
        f"targets come from OMNIGENT_BUILTIN_AGENT_DIRS and the native targets "
        f"are seeded unconditionally at startup, so absence is a server bug"
    )
    return str(agent["id"])


@pytest.mark.parametrize(
    ("target_name", "expected_wrapper", "expect_carry_history"),
    [
        # SDK → SDK: a different openai-agents built-in. The target replays
        # the transcript as context, so no carry-history marker and no
        # terminal wrapper — the clone stays in plain chat mode.
        pytest.param(_FILES_PROBE_ENV_AGENT_NAME, None, False, id="sdk-to-sdk"),
        # SDK → Claude Code: CROSS-family native target. The runner rebuilds
        # the Claude transcript from the copied items, so carry-history is
        # stamped and the wrapper flips to the claude-native terminal UI.
        pytest.param("claude-native-ui", "claude-code-native-ui", True, id="sdk-to-claude-code"),
        # SDK → Codex: SAME-family native target. Same carry-history rebuild
        # path; the wrapper flips to the codex-native terminal UI.
        pytest.param("codex-native-ui", "codex-native-ui", True, id="sdk-to-codex"),
    ],
)
def test_fork_switch_agent_carries_history(
    page: Page,
    seeded_session: tuple[str, str],
    target_name: str,
    expected_wrapper: str | None,
    expect_carry_history: bool,
) -> None:
    """Fork + switch agent: lands on the target agent with history copied.

    :param page: Playwright page fixture (fresh context per test).
    :param seeded_session: ``(base_url, session_id)`` for a pre-created
        runner-bound ``hello_world`` (openai-agents SDK) session.
    :param target_name: Built-in agent name to switch the fork onto.
    :param expected_wrapper: TARGET ``omnigent.wrapper`` value, or ``None``
        when the target runs as plain chat (SDK).
    :param expect_carry_history: Whether the fork must stamp the
        carry-history label (true only for native targets).
    """
    base_url, session_id = seeded_session
    target_agent_id = _agent_id_by_name(base_url, target_name)

    page.goto(f"{base_url}/c/{session_id}")

    # One marked turn so the fork has content AND an assistant bubble to
    # anchor the "Fork from here" action. Forking from the LAST response is
    # a full clone (no truncation), isolating the agent-switch behavior.
    composer = page.get_by_placeholder("Ask the agent anything…")
    expect(composer).to_be_visible()
    composer.fill(f"Reply with one short word. Marker: {_MARKER}")
    page.get_by_role("button", name="Send", exact=True).click()
    assistant = page.locator('[data-testid="message-bubble"][data-role="assistant"]').first
    expect(assistant).to_be_visible(timeout=60_000)

    assistant.hover()
    page.get_by_test_id("fork-from-response").first.click()
    dialog = page.get_by_test_id("fork-session-dialog")
    expect(dialog).to_be_visible()

    # Pick the cross-/same-family target. The option being present at all is
    # itself under test: the picker hides targets it can't classify
    # (``harness: null``) or that wouldn't preserve history.
    page.get_by_test_id("fork-session-agent-select").click()
    option = page.get_by_test_id(f"fork-session-agent-option-{target_agent_id}")
    expect(option).to_be_visible()
    option.click()
    page.get_by_test_id("fork-session-submit").click()

    # The switch fork succeeds and navigates to a NEW session id.
    expect(page).to_have_url(
        re.compile(rf"/c/(?!{re.escape(session_id)})conv_[0-9a-f]+"),
        timeout=30_000,
    )
    expect(dialog).not_to_be_visible()
    fork_id = page.url.rsplit("/c/", 1)[1].split("?", 1)[0]
    assert fork_id != session_id

    # The copied transcript carries the source's marked user turn (history
    # was deep-copied into the fork). For native targets this also renders
    # in the clone's chat surface before the runner ever rebuilds.
    copied_user = page.locator('[data-testid="message-bubble"][data-role="user"]').filter(
        has_text=_MARKER
    )
    expect(copied_user.first).to_be_visible(timeout=30_000)

    # The fork is bound to the TARGET agent (its cloned name carries the
    # base agent name, with the route's " (fork <id>)" suffix appended).
    agent_resp = httpx.get(f"{base_url}/v1/sessions/{fork_id}/agent", timeout=30.0)
    agent_resp.raise_for_status()
    assert agent_resp.json()["name"].startswith(target_name), (
        f"fork should bind the target agent {target_name!r}, got {agent_resp.json()['name']!r}"
    )

    # Server-side label gating made observable. The SDK source never has a
    # native session id, so the source-external directive is always absent
    # — what matters per target is the carry-history marker and the wrapper.
    snap = httpx.get(f"{base_url}/v1/sessions/{fork_id}", timeout=30.0)
    snap.raise_for_status()
    labels: dict[str, str] = snap.json().get("labels") or {}
    assert _SOURCE_EXTERNAL_SESSION_LABEL_KEY not in labels, (
        f"fork of an SDK source must not stamp a source native session id, got {labels!r}"
    )
    if expect_carry_history:
        assert labels.get(_CARRY_HISTORY_LABEL_KEY) == "1", (
            f"native-target fork must stamp carry-history, got {labels!r}"
        )
        assert labels.get(_WRAPPER_LABEL_KEY) == expected_wrapper, (
            f"native-target fork must present as {expected_wrapper!r}, got {labels!r}"
        )
    else:
        assert _CARRY_HISTORY_LABEL_KEY not in labels, (
            f"SDK-target fork must not stamp carry-history, got {labels!r}"
        )
        assert _WRAPPER_LABEL_KEY not in labels, (
            f"SDK-target fork must stay in chat mode (no wrapper), got {labels!r}"
        )
