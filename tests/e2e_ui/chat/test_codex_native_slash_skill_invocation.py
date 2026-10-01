"""UI journey: codex-native chat invokes explicit slash skills.

Two user-observable outcomes of the same defect:

1. **Explicit slash-skill invocation from chat view.** A Codex host skill
   (``~/.codex/skills/<name>/SKILL.md``) sent as ``/<name> <args>`` from the
   web chat view must invoke the skill, so its ``SKILL.md`` instructions
   reach the model exactly as they do for Codex's own ``$<name>`` mention.
   Forwarding the command to the app-server as literal text silently drops
   the skill.

2. **Shared Agent Skills discovery.** A skill installed only under
   ``~/.agents/skills/<name>/SKILL.md`` must be discovered by codex-native
   sessions: seeded into the session's ``$CODEX_HOME/skills`` and invocable
   with ``/<name>`` from chat like any host skill.

Both journeys drive a real ``codex`` CLI against a capturing mock LLM and
assert at the model boundary. Each skill body carries a unique marker that
can only reach the model when the skill is expanded; each turn carries a
nonce, and a marker counts only inside that turn's captured request, so an
earlier turn's transcript cannot satisfy a later assertion. A ``$<skill>``
control turn proves the capture path and skill registration work before the
slash form is checked.

The composer's slash menu is discovered on the session host, and the
runner-bound sessions this harness creates have none, so the menu is not part
of these journeys; ``tests/spec/test_skill_sources.py`` covers the provider
the menu reads.
"""

from __future__ import annotations

import json
import os
import shutil
import uuid
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Locator, Page, expect

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


@dataclass(frozen=True)
class _InstalledSkills:
    """Skill names and the unique body markers that prove each was expanded."""

    control: str
    control_marker: str
    slash: str
    slash_marker: str
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
    signature: the codex-native launch seeds ``$CODEX_HOME/skills`` once, so
    the skills must already be on disk. Two distinct ``~/.codex/skills``
    skills keep the control turn's marker out of the slash turn's check; the
    third lives only in the shared ``~/.agents/skills`` tree.

    :returns: The installed skill names and markers.
    """
    suffix = uuid.uuid4().hex[:8]
    skills = _InstalledSkills(
        control=f"ctrl-skill-{suffix}",
        control_marker=f"CTRLBODY{uuid.uuid4().hex[:10]}",
        slash=f"slash-skill-{suffix}",
        slash_marker=f"SLASHBODY{uuid.uuid4().hex[:10]}",
        shared=f"shared-skill-{suffix}",
        shared_marker=f"SHAREDBODY{uuid.uuid4().hex[:10]}",
    )
    codex_root = Path.home() / ".codex" / "skills"
    shared_root = Path.home() / ".agents" / "skills"
    created = [
        _write_skill(codex_root, skills.control, skills.control_marker),
        _write_skill(codex_root, skills.slash, skills.slash_marker),
        _write_skill(shared_root, skills.shared, skills.shared_marker),
    ]
    try:
        yield skills
    finally:
        for skill_dir in created:
            shutil.rmtree(skill_dir, ignore_errors=True)


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
    page.wait_for_timeout(400)
    page.get_by_role("button", name="Send", exact=True).click()
    expect(page.locator(_ASSISTANT, has_text=token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)
    page.wait_for_timeout(1_000)
    captured = httpx.get(f"{mock_url}/mock/requests", timeout=10).json()["requests"]
    return any(nonce in (blob := json.dumps(req)) and marker in blob for req in captured)


_needs_mock_capture = pytest.mark.skipif(
    bool(os.environ.get("LLM_API_KEY")),
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
    assert control_reached, "precondition: `$<skill>` from chat should invoke the skill"

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
        f"/{skills.shared} is installed only under ~/.agents/skills and was not "
        "invoked: shared Agent Skills are missing from Codex skill discovery"
    )
