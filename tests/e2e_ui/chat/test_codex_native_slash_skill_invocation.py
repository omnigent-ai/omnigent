r"""UI journey: codex-native chat invokes an explicit slash skill.

Two user-observable facets of the same defect:

1. **Explicit slash-skill invocation from chat view.** A Codex host skill
   (``~/.codex/skills/<name>/SKILL.md``) is offered by the web composer's
   slash menu, but sending ``/<name> <args>`` from chat view forwards the
   literal text to the Codex app-server (``ChatPage.handleSendSlashCommand``
   is gated off for native wrappers, so the composer falls through to the
   plaintext send, and ``codex_native_executor._content_to_input_items``
   forwards it verbatim to ``turn/start``). Codex never receives a
   structured skill input, so the skill's ``SKILL.md`` instructions never
   reach the model. The test drives the real journey — install skill, start
   a codex-native session, see the skill in the composer menu, send — and
   asserts the skill *body* reaches the LLM (captured by the mock LLM
   server). Reproduced ⇒ only the literal ``/<name> …`` text arrives and
   the assertion fails.

2. **Shared Agent Skills discovery.** A skill installed under
   ``~/.agents/skills/<name>/SKILL.md`` (the cross-agent shared location the
   generic host walk already understands) never appears in a codex-native
   session's slash menu, because ``codex_host_skills`` only scans
   ``<bundle>/skills`` + ``~/.codex/skills``. The test installs a shared
   skill, opens the composer, types ``/<name>`` and asserts the menu offers
   it.

Both tests ride the ``native_codex_mock_session`` fixture (real ``codex``
CLI, mock LLM backend when ``LLM_API_KEY`` is absent), matching the native
codex render-parity suite. Facet 1 inspects the mock LLM's captured
requests, so it is skipped in real-gateway mode.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import uuid
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest
from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import configure_mock_llm, reset_mock_llm, set_fallback_mock_llm

_log = logging.getLogger(__name__)

_COMPOSER = "Send a message…"
_ASSISTANT = '[data-testid="message-bubble"][data-role="assistant"]'
_WORKING = '[data-testid="working-indicator"]'

# Mock LLM responds instantly; budget covers native CLI boot + turn settle.
_MOCK_TURN_TIMEOUT_MS = 60_000
# Codex boots in the terminal on bind; the auto-launch + first-run
# pre-accept can take a while on a cold CI runner.
_TERMINAL_READY_TIMEOUT_MS = 120_000
# Runner-resolved skills land via the background skills fetch after bind —
# give the menu time to receive them.
_SKILL_MENU_TIMEOUT_MS = 90_000

# Must match the model in the mock openai provider config written by the
# native_codex_mock_session fixture (conftest._CODEX_MOCK_MODEL).
_CODEX_MOCK_MODEL = "gpt-4o"


def _write_skill(root: Path, name: str, description: str, body: str) -> Path:
    """Write ``<root>/<name>/SKILL.md`` with frontmatter and *body*.

    :param root: Skills root, e.g. ``~/.codex/skills``.
    :param name: Skill (directory and frontmatter) name.
    :param description: One-line frontmatter description.
    :param body: Markdown instruction body below the frontmatter.
    :returns: The created skill directory.
    """
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=False)
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {description}\n---\n\n{body}\n",
        encoding="utf-8",
    )
    return skill_dir


@pytest.fixture
def installed_codex_host_skill() -> Iterator[tuple[str, str]]:
    """Install a uniquely-named Codex host skill under ``~/.codex/skills``.

    Installed BEFORE the session fixture runs (declare it first in the test
    signature) so the runner's skill discovery and the codex-native launch
    both see it.

    :returns: ``(skill_name, body_marker)`` — the marker is a unique token
        embedded only in the SKILL.md *body* (never the description, which
        Codex advertises in its system prompt), so it can only reach the
        model if the skill is actually invoked/expanded.
    """
    nonce = uuid.uuid4().hex[:8]
    name = f"ask-matt-{nonce}"
    body_marker = f"SKILL-BODY-{nonce}"
    skill_dir = _write_skill(
        Path.home() / ".codex" / "skills",
        name,
        "Verify a service like Matt would",
        f"When invoked, reply including the token {body_marker} and audit the service.",
    )
    try:
        yield (name, body_marker)
    finally:
        shutil.rmtree(skill_dir, ignore_errors=True)


@pytest.fixture
def installed_shared_agents_skill() -> Iterator[str]:
    """Install a uniquely-named shared Agent Skill under ``~/.agents/skills``.

    :returns: The skill name.
    """
    nonce = uuid.uuid4().hex[:8]
    name = f"shared-agents-{nonce}"
    skill_dir = _write_skill(
        Path.home() / ".agents" / "skills",
        name,
        "A shared Agent Skill available to every harness",
        "When invoked, say SHARED-AGENTS-SKILL ran.",
    )
    try:
        yield name
    finally:
        shutil.rmtree(skill_dir, ignore_errors=True)


def _open_chat_view(page: Page) -> None:
    """Switch the terminal-first codex session to its Chat view.

    :param page: The Playwright page, freshly navigated to ``/c/{id}``.
    """
    expect(page.get_by_test_id("view-mode-toggle")).to_be_visible(
        timeout=_TERMINAL_READY_TIMEOUT_MS
    )
    segment = page.get_by_test_id("view-mode-chat")
    expect(segment).to_be_enabled(timeout=30_000)
    segment.click()


def _composer(page: Page):
    """Return the chat composer textarea locator (visible-checked).

    :param page: The Playwright page, on the session's Chat view.
    :returns: The composer locator.
    """
    composer = page.get_by_placeholder(_COMPOSER)
    expect(composer).to_be_visible(timeout=30_000)
    return composer


@pytest.mark.nightly
@pytest.mark.timeout(300)
@pytest.mark.skipif(
    bool(os.environ.get("LLM_API_KEY")),
    reason="inspects the mock LLM's captured requests; real-gateway mode has no capture",
)
def test_codex_native_chat_slash_skill_reaches_model(
    installed_codex_host_skill: tuple[str, str],
    native_codex_mock_session: tuple[str, str],
    mock_llm_server_url: str,
    page: Page,
) -> None:
    """An explicit ``/<skill> <args>`` sent from chat view must invoke the skill.

    Journey: install an enabled Codex skill → start a codex-native session →
    the composer's slash menu offers the skill → send ``/<name> …`` from chat
    view → the skill's instructions (its SKILL.md body) must reach the model.

    Today the composer falls through to the plaintext send for native
    wrappers, so the model only ever receives the literal ``/<name> …`` text
    and the body marker never appears in any captured LLM request.
    """
    skill_name, body_marker = installed_codex_host_skill
    base_url, session_id = native_codex_mock_session
    _log.info(
        "codex-native session ready: base_url=%s session_id=%s skill=%s",
        base_url,
        session_id,
        skill_name,
    )

    nonce = uuid.uuid4().hex[:8]
    arg_marker = f"ARG-{nonce}"
    assistant_token = f"AST-{nonce}"
    reset_mock_llm(mock_llm_server_url)
    # Content-routed queue: whichever internal call carries our turn's arg
    # marker gets the settle token; everything else falls back to "".
    configure_mock_llm(
        mock_llm_server_url,
        [{"text": assistant_token}],
        key=arg_marker,
        match=arg_marker,
    )
    set_fallback_mock_llm(mock_llm_server_url, _CODEX_MOCK_MODEL, "")

    page.goto(f"{base_url}/c/{session_id}")
    _open_chat_view(page)
    composer = _composer(page)

    # Precondition: the ~/.codex host skill surfaces in the slash menu (this
    # part of discovery works today — the menu is exactly what tells the user
    # the command is available).
    composer.fill(f"/{skill_name}")
    expect(page.get_by_test_id(f"slash-menu-item-{skill_name}")).to_be_visible(
        timeout=_SKILL_MENU_TIMEOUT_MS
    )
    _log.info("slash menu offers /%s — sending the command", skill_name)

    # The user journey: send the advertised command with arguments.
    composer.fill(f"/{skill_name} verify this service {arg_marker}")
    page.get_by_role("button", name="Send", exact=True).click()

    # Let the turn settle so codex has made its model call(s).
    expect(page.locator(_ASSISTANT, has_text=assistant_token).first).to_be_visible(
        timeout=_MOCK_TURN_TIMEOUT_MS
    )
    expect(page.locator(_WORKING)).to_have_count(0, timeout=_MOCK_TURN_TIMEOUT_MS)

    # The bug: the skill's SKILL.md body never reaches the model — codex
    # received the literal "/<name> …" text, not a structured skill input.
    captured = httpx.get(f"{mock_llm_server_url}/mock/requests", timeout=10.0).json()
    serialized = json.dumps(captured)
    assert arg_marker in serialized, (
        "harness plumbing broke: the sent turn text never reached the mock LLM"
    )
    assert body_marker in serialized, (
        f"explicit slash skill was not invoked: /{skill_name} was forwarded to "
        f"Codex as literal text and its SKILL.md instructions (marker "
        f"{body_marker}) never reached the model"
    )


@pytest.mark.nightly
@pytest.mark.timeout(300)
def test_codex_native_shared_agents_skill_discovered(
    installed_shared_agents_skill: str,
    native_codex_mock_session: tuple[str, str],
    page: Page,
) -> None:
    """A shared Agent Skill under ``~/.agents/skills`` must be discoverable.

    Journey: install a shared Agent Skill → start a codex-native session →
    type ``/<name>`` in the chat composer → the slash menu must offer it.

    Today ``codex_host_skills`` scans only ``<bundle>/skills`` and
    ``~/.codex/skills``, so the shared skill never surfaces.
    """
    skill_name = installed_shared_agents_skill
    base_url, session_id = native_codex_mock_session

    page.goto(f"{base_url}/c/{session_id}")
    _open_chat_view(page)
    composer = _composer(page)

    composer.fill(f"/{skill_name}")
    # A regression here means the menu never offers the shared skill and this
    # expect times out: ~/.agents/skills absent from codex-native discovery.
    expect(page.get_by_test_id(f"slash-menu-item-{skill_name}")).to_be_visible(
        timeout=_SKILL_MENU_TIMEOUT_MS
    )
