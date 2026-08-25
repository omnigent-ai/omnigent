"""E2E: what a routed session shows in the chat surface.

Two user-visible consequences of Smart Routing owning a session:

1. **One chip per pick.** A routed create records the pick as a ``session``
   chip, and the session's first turn routes again and records the same verdict
   as a ``turn`` chip. Two audit rows, one decision — so the transcript renders
   a single ``routing_decision`` card (the turn chip, paired below the user
   message it decided), carrying the Databricks mark when the AI-Gateway router
   answered. Two cards saying the same thing above and below one message was
   the bug.
2. **A named Model row.** The router pins its fully-qualified pick
   (``databricks-claude-opus-4-8``), which the harness catalog carries only
   under an alias — or not at all. The gear modal's Model row must still name
   the model the session is on instead of rendering blank (Radix falls back to
   the placeholder for a value no item declares).

The transcript items are written straight into the spawned server's store (the
same seam :func:`tests.e2e_ui.conftest.seed_committed_turn` uses) so neither
test needs a router, a gateway credential, or a real turn. The claude-native
snapshot patch is shared with ``chat/test_claude_model_picker.py``.
"""

from __future__ import annotations

import httpx
from playwright.sync_api import Page, expect

from tests.e2e_ui.chat.test_claude_model_picker import _patch_session_as_claude_native
from tests.e2e_ui.conftest import seed_committed_turn

# The router's pick: an AI-Gateway serving-endpoint id. Deliberately absent
# from the claude-native alias catalog (opus / sonnet / haiku) — that mismatch
# is what used to blank the Model row.
_ROUTED_MODEL = "databricks-claude-opus-4-8"


def _seed_routed_first_turn(session_id: str, *, prompt: str, reply: str) -> None:
    """Seed the transcript a routed session's first turn leaves behind.

    Persistence order mirrors the native path: the create-time ``session``
    chip, then the first turn's ``turn`` chip, then the turn's messages.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    :param prompt: User message text.
    :param reply: Assistant message text.
    """
    from omnigent.entities import NewConversationItem
    from omnigent.entities.conversation import parse_item_data
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from tests.e2e_ui.conftest import _server_state

    database_uri = _server_state.get("database_uri")
    assert database_uri, "needs the spawned server's database (not --ui-base-url)"

    decision = {
        "model": _ROUTED_MODEL,
        "applied": True,
        "rationale": "Multi-file refactor needs deep reasoning.",
        "harness": "claude-native",
        "decision_id": "e2e-decision-1",
        "router_source": "databricks-aigw",
    }
    store = SqlAlchemyConversationStore(str(database_uri))
    store.append(
        session_id,
        [
            NewConversationItem(
                type="routing_decision",
                response_id="routing_e2e_create",
                data=parse_item_data("routing_decision", {**decision, "scope": "session"}),
            ),
            NewConversationItem(
                type="routing_decision",
                response_id="routing_e2e_turn",
                data=parse_item_data("routing_decision", {**decision, "scope": "turn"}),
            ),
        ],
    )
    seed_committed_turn(session_id, prompt=prompt, reply=reply)


def test_routed_session_renders_one_routing_chip(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The create-time and first-turn picks render as ONE chip, marked Databricks.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; its transcript is seeded with a routed first turn.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _seed_routed_first_turn(session_id, prompt="refactor the auth module", reply="on it")

    # Both audit rows really are in the transcript, so the single card below is
    # the render collapsing them — not a seed that silently wrote one row.
    items = httpx.get(f"{base_url}/v1/sessions/{session_id}/items", timeout=10.0)
    items.raise_for_status()
    decisions = [i for i in items.json()["data"] if i["type"] == "routing_decision"]
    assert len(decisions) == 2, decisions

    page.goto(f"{base_url}/c/{session_id}")

    chip = page.get_by_test_id("routing-decision-card")
    expect(chip).to_have_count(1, timeout=15_000)
    expect(chip).to_contain_text("Smart routing")
    # The pick itself, and who made it.
    expect(chip).to_contain_text("opus")
    expect(chip.get_by_test_id("routing-decision-source-databricks")).to_be_visible()
    session_harness = chip.get_by_test_id("routing-decision-harness")
    expect(session_harness).to_contain_text("claude")
    expect(session_harness).not_to_contain_text("claude-native")
    # The turn chip is the survivor, so the scope reads as the turn's.
    expect(chip).to_have_attribute("data-applied", "true")


def test_spawn_chip_says_the_short_harness_name(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A sub-agent spawn chip says "codex", not the "-native" implementation id.

    Every chip shortens the id this way; the session's own chip is covered
    above.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session.
    :returns: None.
    """
    from omnigent.entities import NewConversationItem
    from omnigent.entities.conversation import parse_item_data
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )
    from tests.e2e_ui.conftest import _server_state

    base_url, session_id = seeded_session
    database_uri = _server_state.get("database_uri")
    assert database_uri, "needs the spawned server's database (not --ui-base-url)"
    store = SqlAlchemyConversationStore(str(database_uri))
    store.append(
        session_id,
        [
            NewConversationItem(
                type="routing_decision",
                response_id="routing_e2e_spawn",
                data=parse_item_data(
                    "routing_decision",
                    {
                        "model": "databricks-gpt-5-6-luna",
                        "applied": True,
                        "rationale": "Trivial task; cheapest arm.",
                        "harness": "codex-native",
                        "scope": "native_subagent",
                        "decision_id": "e2e-decision-spawn",
                        "router_source": "databricks-aigw",
                        "agent_name": "researcher",
                    },
                ),
            ),
        ],
    )
    seed_committed_turn(session_id, prompt="spawn a researcher", reply="spawned")
    # The spawn chip renders only while the session's switch is on.
    patched = httpx.patch(
        f"{base_url}/v1/sessions/{session_id}",
        json={"subagent_routing_override": "on"},
        timeout=10.0,
    )
    patched.raise_for_status()

    page.goto(f"{base_url}/c/{session_id}")

    chip = page.get_by_test_id("routing-decision-card")
    expect(chip).to_have_count(1, timeout=15_000)
    harness = chip.get_by_test_id("routing-decision-harness")
    expect(harness).to_contain_text("codex")
    expect(harness).not_to_contain_text("codex-native")
    expect(chip.get_by_test_id("routing-decision-scope")).to_contain_text("subagent")


def test_routed_session_config_modal_names_the_routed_model(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """The gear modal's Model row names the router's pick, not a blank row.

    The pick is a gateway serving-endpoint id the claude-native alias catalog
    has no entry for; the row carries it as its own option so the trigger reads
    the model the session actually runs on.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` for a real server-backed
        session; the browser snapshot is patched to claude-native.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _patch_session_as_claude_native(page, session_id, llm_model=_ROUTED_MODEL)

    page.goto(f"{base_url}/c/{session_id}")

    gear = page.get_by_test_id("composer-config-gear")
    expect(gear).to_be_visible(timeout=15_000)
    gear.click()

    model_row = page.get_by_test_id("composer-config-model")
    expect(model_row).to_be_visible()
    expect(model_row).to_contain_text(_ROUTED_MODEL)

    # The row also OFFERS it, alongside the harness's own aliases — a pinned
    # model that no option declares is what rendered blank.
    model_row.click()
    expect(page.locator(f'[role="option"][data-model-id="{_ROUTED_MODEL}"]')).to_have_count(1)
    expect(page.locator('[role="option"][data-model-id="sonnet"]')).to_have_count(1)
