"""UI journey: codex-native chat invokes explicit slash skills.

Two user-observable outcomes of the same defect, each driven from the web
Chat view of a terminal-first ``codex-native`` session against a real
``codex`` CLI and a capturing mock LLM:

1. A Codex host skill (``<codex home>/skills/<name>/SKILL.md``) sent as
   ``/<name> <args>`` must invoke the skill, so its ``SKILL.md`` instructions
   reach the model exactly as they do for Codex's own ``$<name>`` mention,
   instead of being forwarded to the app-server as literal text.
2. A skill installed only under ``~/.agents/skills/<name>/SKILL.md`` must be
   discovered by codex-native sessions and be invocable with ``/<name>``.

Assertions are made at the model boundary: each skill body carries a unique
marker that can only reach the model when the skill is expanded, each turn
carries a nonce, and a marker counts only inside that turn's captured
request. A ``$<skill>`` control turn on a *different* skill precedes every
slash assertion, proving skill registration and the capture path first.
Distinct skills are required because Codex resends the full conversation on
every turn, so one skill's expanded body would otherwise satisfy the next
turn's check.

The composer's slash menu is discovered on the session host, and the
runner-bound sessions this harness creates have none, so the menu is not part
of these journeys; ``tests/spec/test_skill_sources.py`` covers the provider
the menu reads.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import time
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

from omnigent.inner.codex_executor import _codex_home_config_source_from_env
from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm
from tests.e2e_ui.messages.test_message_render_parity import (
    _ASSISTANT,
    _WORKING,
    _ensure_chat_view,
)
from tests.e2e_ui.messages.test_native_codex_render_parity import (
    _CODEX_MOCK_MODEL,
    _MOCK_TURN_TIMEOUT_MS,
    _open_terminal_view,
    _wait_terminal_connected,
)
from tests.helpers.ui_configuration import prepared_repro_environment


@dataclass(frozen=True)
class _InstalledSkills:
    """Skill names and the unique body markers that prove each was expanded."""

    control: str
    control_marker: str
    slash: str
    slash_marker: str
    shared_control: str
    shared_control_marker: str
    shared: str
    shared_marker: str


def _write_skill(root: Path, name: str, marker: str) -> Path:
    """Write ``<root>/<name>/SKILL.md`` whose body (never its description) carries *marker*.

    :param root: Skills root, e.g. ``~/.codex/skills``.
    :param name: Skill directory and frontmatter name.
    :param marker: Unique token embedded only in the instruction body.
    :returns: The created skill directory.
    """
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=False)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: probe skill {name}\n---\n\n"
        f"{marker}: read the named service manifest and report its version.\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def installed_codex_skills() -> Iterator[_InstalledSkills]:
    """Install the journey's skills before the session launches.

    Declare this fixture ahead of ``native_codex_mock_session`` in a test
    signature: the codex-native launch seeds its ``$CODEX_HOME/skills`` once,
    so the skills must already be on disk. Host skills are written under the
    config source the launch itself resolves, which maps an inherited
    Omnigent-private ``CODEX_HOME`` back to the user's real ``~/.codex``;
    shared skills go under ``~/.agents/skills``. Both roots must be the ones
    the *runner* reads: a prepared ``dev.repro_env`` runner has its own
    ``CODEX_HOME``, which ``exec`` does not export, so pytest must be started
    with that value set. Two distinct host skills and two distinct shared
    skills keep each control turn's marker out of the slash turn's check.

    :returns: The installed skill names and markers.
    """
    if prepared_repro_environment()["OMNIGENT_REPRO_SERVER_URL"] and not os.environ.get(
        "CODEX_HOME"
    ):
        pytest.fail(
            "prepared repro environment: start pytest with CODEX_HOME set to the prepared "
            "runner's Codex home (e.g. CODEX_HOME=.omnigent/repro-env/codex-config) so host "
            "skills are seeded where that runner reads them"
        )
    suffix = uuid.uuid4().hex[:8]
    skills = _InstalledSkills(
        control=f"ctrl-skill-{suffix}",
        control_marker=f"CTRLBODY{uuid.uuid4().hex[:10]}",
        slash=f"slash-skill-{suffix}",
        slash_marker=f"SLASHBODY{uuid.uuid4().hex[:10]}",
        shared_control=f"shared-ctrl-{suffix}",
        shared_control_marker=f"SHAREDCTRLBODY{uuid.uuid4().hex[:10]}",
        shared=f"shared-skill-{suffix}",
        shared_marker=f"SHAREDBODY{uuid.uuid4().hex[:10]}",
    )
    codex_root = _codex_home_config_source_from_env() / "skills"
    shared_root = Path.home() / ".agents" / "skills"
    # Teardown must remove exactly the directories this fixture created, never a
    # shared tree a concurrent session already relies on.
    roots_created: list[Path] = []
    for root in (codex_root, shared_root):
        ancestor = root
        while not ancestor.exists() and ancestor not in roots_created:
            roots_created.append(ancestor)
            ancestor = ancestor.parent
    created: list[Path] = []
    try:
        for root, name, marker in (
            (codex_root, skills.control, skills.control_marker),
            (codex_root, skills.slash, skills.slash_marker),
            (shared_root, skills.shared_control, skills.shared_control_marker),
            (shared_root, skills.shared, skills.shared_marker),
        ):
            created.append(_write_skill(root, name, marker))
        yield skills
    finally:
        for skill_dir in created:
            shutil.rmtree(skill_dir, ignore_errors=True)
        for leftover in sorted(roots_created, key=lambda p: len(p.parts), reverse=True):
            with contextlib.suppress(OSError):
                leftover.rmdir()


def _open_chat_composer(page: Page, base_url: str, session_id: str) -> Locator:
    """Open the session, wait for the live Codex TUI, then switch to chat view.

    :param page: The Playwright page.
    :param base_url: Spawned server base URL.
    :param session_id: The codex-native session id.
    :returns: The focused chat composer.
    """
    page.goto(f"{base_url}/c/{session_id}")
    _open_terminal_view(page)
    _wait_terminal_connected(page)
    _ensure_chat_view(page)
    composer = page.get_by_label("Message the agent")
    expect(composer).to_be_visible(timeout=30_000)
    composer.click()
    return composer


def _send_and_capture_marker(
    page: Page,
    composer: Locator,
    mock_url: str,
    text: str,
    nonce: str,
    marker: str,
) -> bool:
    """Send *text* as a chat turn and report whether *marker* reached the model.

    Only a captured request carrying this turn's *nonce* counts, so an earlier
    turn's transcript cannot produce a false pass.

    :param page: The Playwright page, on the session's chat view.
    :param composer: The chat composer.
    :param mock_url: Mock LLM server base URL.
    :param text: The chat turn to send; must contain *nonce*.
    :param nonce: Per-turn token routing the mock reply and filtering captures.
    :param marker: The skill body marker expected in the turn's model request.
    :returns: ``True`` when *marker* appears alongside *nonce* in a capture.
    """
    token = f"ack-{nonce}"
    configure_mock_llm(mock_url, [{"text": token}], key=f"turn-{nonce}", match=nonce)
    composer.fill(text)
    send_button = page.get_by_role("button", name="Send", exact=True)
    expect(send_button).to_be_enabled()
    send_button.click()
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    # The turn's request can land in the capture log slightly after the reply renders.
    deadline = time.monotonic() + 10.0
    while True:
        try:
            captured = httpx.get(f"{mock_url}/mock/requests", timeout=10, trust_env=False).json()[
                "requests"
            ]
        except (httpx.HTTPError, json.JSONDecodeError, KeyError):
            captured = []
        if any(nonce in (blob := json.dumps(req)) and marker in blob for req in captured):
            return True
        if time.monotonic() >= deadline:
            return False
        page.wait_for_timeout(200)


# A prepared dev.repro_env runner is isolated from the ambient LLM_API_KEY and
# always routes to its capturing mock, so only the standalone lane lacks capture.
_needs_mock_capture = pytest.mark.skipif(
    bool(os.environ.get("LLM_API_KEY")) and not os.environ.get("OMNIGENT_REPRO_MODEL_URL"),
    reason="inspects the mock LLM's captured requests; real-gateway mode has no capture",
)


@pytest.mark.nightly
@pytest.mark.timeout(300)
@_needs_mock_capture
def test_codex_native_chat_slash_skill_reaches_model(
    installed_codex_skills: _InstalledSkills,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    page: Page,
) -> None:
    """``/<skill> <args>`` from chat view invokes the skill like ``$<skill>`` does."""
    skills = installed_codex_skills
    base_url, session_id = native_codex_mock_session
    composer = _open_chat_composer(page, base_url, session_id)
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "ready")

    control_nonce = uuid.uuid4().hex[:8]
    control_reached = _send_and_capture_marker(
        page,
        composer,
        mock_llm_server_url,
        f"${skills.control} verify this service {control_nonce}",
        control_nonce,
        skills.control_marker,
    )
    assert control_reached, (
        f"precondition: `${skills.control}` from chat should invoke the host skill "
        "(skill registration or request capture is broken, not the slash path)"
    )

    slash_nonce = uuid.uuid4().hex[:8]
    slash_reached = _send_and_capture_marker(
        page,
        composer,
        mock_llm_server_url,
        f"/{skills.slash} verify this service {slash_nonce}",
        slash_nonce,
        skills.slash_marker,
    )
    assert slash_reached, (
        f"/{skills.slash} was forwarded to Codex as literal text: "
        "its SKILL.md body never reached the model"
    )


@pytest.mark.nightly
@pytest.mark.timeout(300)
@_needs_mock_capture
def test_codex_native_shared_agents_skill_invocable_from_chat(
    installed_codex_skills: _InstalledSkills,
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    page: Page,
) -> None:
    """A skill installed only under ``~/.agents/skills`` is discovered and invocable."""
    skills = installed_codex_skills
    base_url, session_id = native_codex_mock_session
    composer = _open_chat_composer(page, base_url, session_id)
    reset_mock_llm(mock_llm_server_url)
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "ready")

    control_nonce = uuid.uuid4().hex[:8]
    discovered = _send_and_capture_marker(
        page,
        composer,
        mock_llm_server_url,
        f"${skills.shared_control} verify this service {control_nonce}",
        control_nonce,
        skills.shared_control_marker,
    )
    assert discovered, (
        f"${skills.shared_control} is installed only under ~/.agents/skills and was not "
        "expanded: shared Agent Skills are missing from Codex skill discovery"
    )

    nonce = uuid.uuid4().hex[:8]
    shared_reached = _send_and_capture_marker(
        page,
        composer,
        mock_llm_server_url,
        f"/{skills.shared} verify this service {nonce}",
        nonce,
        skills.shared_marker,
    )
    assert shared_reached, (
        f"/{skills.shared} was forwarded to Codex as literal text: "
        "the shared skill's SKILL.md body never reached the model"
    )
