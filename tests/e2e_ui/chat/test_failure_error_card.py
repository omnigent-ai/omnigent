"""Failure error cards render as clear English, not a raw code + log blob.

A harness launch/turn failure persists an ``error`` transcript item. Before,
the chat rendered it as ``Error · <code>`` over the raw message. Now the
banner leads with a human headline: a classified failure shows its friendly
title, and an unclassified one still maps its ``code`` to a plain-English
sentence (mirror of ``describe_failure_code`` /
``FAILURE_CODE_DESCRIPTIONS``) rather than exposing the enum.

Seeds the error item straight into the store (like ``seed_committed_turn``)
so the assertion is on transcript hydration + rendering, deterministic and
independent of any live turn.
"""

from __future__ import annotations

from playwright.sync_api import Page, expect

from tests.e2e_ui.conftest import _server_state


def _seed_error_item(session_id: str, *, code: str, message: str) -> None:
    """Append a committed ``error`` transcript item to the session's store.

    Mirrors ``seed_committed_turn`` but writes an error banner item, so the
    chat hydrates and renders it through the same path a real
    ``response.error`` persists.

    :param session_id: Session to append to, e.g. ``"conv_abc123"``.
    :param code: Error classifier, e.g. ``"required_terminal_exited"``.
    :param message: Raw error message stored alongside the code.
    :raises RuntimeError: If the server under test isn't one we spawned.
    """
    from omnigent.entities import ErrorData, NewConversationItem
    from omnigent.stores.conversation_store.sqlalchemy_store import (
        SqlAlchemyConversationStore,
    )

    database_uri = _server_state.get("database_uri")
    if not database_uri:
        raise RuntimeError(
            "seeding an error item needs the spawned server's database; it is "
            "unavailable when running against --ui-base-url."
        )
    SqlAlchemyConversationStore(str(database_uri)).append(
        session_id,
        [
            NewConversationItem(
                type="error",
                response_id="resp_seeded_error",
                data=ErrorData(source="execution", code=code, message=message),
            ),
        ],
    )


def test_unclassified_failure_renders_english_headline_not_raw_code(
    page: Page,
    seeded_session: tuple[str, str],
) -> None:
    """A known failure code reads as a sentence, never the bare enum.

    :param page: Playwright page fixture.
    :param seeded_session: ``(base_url, session_id)`` from the local server.
    :returns: None.
    """
    base_url, session_id = seeded_session
    _seed_error_item(
        session_id,
        code="required_terminal_exited",
        message="Required terminal exited unexpectedly; the runtime is no longer available.",
    )

    page.goto(f"{base_url}/c/{session_id}")

    alert = page.get_by_role("alert")
    # The friendly, code-derived headline is shown...
    expect(alert).to_contain_text("The agent's terminal exited unexpectedly", timeout=15_000)
    # ...and the raw enum is not surfaced as the headline.
    expect(alert).not_to_contain_text("Error · required_terminal_exited", timeout=15_000)
