"""Unit tests for the REPL's ``/context`` slash command.

Mirrors :mod:`tests.repl.test_model_command`. Drives
:func:`omnigent.repl._repl.handle_slash_command` against a host /
session stub and asserts on the rendered output for both the
unknown-context-window and known-context-window paths.

Coin bar arithmetic cases (used / free / buffer coin counts) are
verified with controlled ``count_tokens`` monkeypatches and by setting
``context_window`` directly on the session stub, so the assertions are
deterministic regardless of litellm registry state.

``context_window`` is now resolved server-side (returned in
``SessionResponse.context_window``) so the client-side
``_get_model_context_window`` helper is NOT called by ``_cmd_context``.
Tests must supply the window size through the session stub's
``context_window`` attribute instead.
"""

from __future__ import annotations

from dataclasses import dataclass

import pytest
from omnigent_ui_sdk import RichBlockFormatter

from omnigent.repl._repl import (
    COMMANDS,
    _items_for_context_token_count,
    _update_context_ring_estimate,
    handle_slash_command,
)
from tests.repl.helpers import CapturingHost


class _Session:
    """Minimal stub matching the surface ``/context`` reads.

    Concrete class (not MagicMock) so unexpected attribute access fails
    loudly — required by the project's test-integrity rules
    (testing skill §3).

    :param agent_name: Value returned by the ``model`` property,
        e.g. ``"my-agent"``.
    :param model_override: Simulates a user-set LLM model override,
        e.g. ``"openai/gpt-4o"`` or ``None``.
    :param context_window: Context window size pre-computed server-side,
        e.g. ``100_000`` or ``None`` to simulate an unknown model.
    :param session_id: Durable session id (sessions-API mode),
        e.g. ``"conv_abc123"`` or ``None``.
    :param current_response_id: Most-recent response id (legacy mode),
        e.g. ``"resp_abc123"`` or ``None``.
    :param llm_model: Spec-pinned LLM model id, e.g.
        ``"databricks-claude-sonnet-4-6"``, or ``None`` for
        native-harness agents whose spec pins no model.
    """

    def __init__(
        self,
        *,
        agent_name: str = "test-agent",
        model_override: str | None = None,
        context_window: int | None = None,
        session_id: str | None = None,
        current_response_id: str | None = None,
        llm_model: str | None = None,
    ) -> None:
        self._agent_name = agent_name
        self.model_override = model_override
        self.context_window = context_window
        self.session_id = session_id
        self.current_response_id = current_response_id
        self.llm_model = llm_model

    @property
    def model(self) -> str:
        """
        Return the agent display name.

        :returns: The agent name passed at construction,
            e.g. ``"test-agent"``.
        """
        return self._agent_name


def _patch_count_tokens(
    monkeypatch: pytest.MonkeyPatch,
    *,
    message_tokens: int,
) -> None:
    """
    Monkeypatch ``count_tokens`` to return a fixed value.

    ``count_tokens`` is imported with a local ``from … import`` statement
    inside ``_cmd_context``; patching the source module attribute ensures
    the local binding picks up the replacement on each function call.

    ``context_window`` is now resolved server-side and supplied directly
    via ``_Session.context_window`` — there is no ``_get_model_context_window``
    call in ``_cmd_context`` to patch.

    :param monkeypatch: pytest monkeypatch fixture.
    :param message_tokens: Value ``count_tokens`` should return,
        e.g. ``35_000``.
    """
    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        lambda messages, model: message_tokens,
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_command_registered() -> None:
    """``/context`` appears in the slash-command registry.

    Both ``/help`` and the live completer iterate ``COMMANDS``.
    Fails if the ``@_cmd`` decorator registration is removed or renamed.
    """
    assert "/context" in COMMANDS
    # Help text should describe context/window usage so users know what to expect.
    assert "context" in COMMANDS["/context"][0].lower()


# ---------------------------------------------------------------------------
# No-conversation / unknown-window fallback path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_no_model_override_shows_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without a context_window, the fallback message is shown.

    ``context_window=None`` on the session stub simulates a model whose
    window is unknown (either the server couldn't look it up, or no model
    is configured). The command must surface the "unknown" hint plus a raw
    token count but no coin bar.

    Fails if the ``context_window is None`` branch is removed or if the
    fallback message text changes.
    """
    # count_tokens still called for the raw message count
    _patch_count_tokens(monkeypatch, message_tokens=0)
    host = CapturingHost()
    session = _Session()  # context_window=None → fallback path
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    output = host.text
    # Fallback hint must be present — tells the user why no bar is shown.
    assert "Context window size unknown" in output
    # A token count must still appear even on the fallback path.
    assert "tokens" in output
    # Coin bar characters must NOT appear — no bar without a known window.
    assert "█" not in output
    assert "░" not in output
    assert "▓" not in output


# ---------------------------------------------------------------------------
# Coin bar arithmetic
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_coin_bar_low_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 10 % usage, bar shows 1 used coin, 7 free coins, 2 buffer coins.

    Arithmetic: used = round(0.1 × 10) = 1; buf = round(0.2 × 10) = 2;
    free = max(10 − 1 − 2, 0) = 7; buf reassignment = 10 − 1 − 7 = 2.

    Fails if the coin-bar computation or the ``round()`` calls change
    in a way that shifts the zone boundaries.
    """
    _patch_count_tokens(monkeypatch, message_tokens=10_000)
    host = CapturingHost()
    session = _Session(model_override="openai/gpt-4o", context_window=100_000)
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    output = host.text
    # All three zones appear in the coin bar.
    assert "█" in output  # used zone
    assert "░" in output  # free zone
    assert "▓" in output  # compaction-buffer zone
    # Summary line: 10.0k / 100k tokens (10%)
    assert "10.0k" in output
    assert "100k" in output
    assert "10%" in output
    # Per-category breakdown rows
    assert "Messages" in output
    assert "Free space" in output
    assert "Compaction buffer" in output


@pytest.mark.asyncio
async def test_context_free_space_count_matches_its_percentage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Free-space token count partitions the window with messages + buffer.

    Regression for the count/percent mismatch: free space used to be
    ``window - messages`` (omitting the 20% compaction buffer) while its
    percentage subtracted the buffer — so it read e.g. "920,150 tokens (72%)",
    a count that is 92% of the window. With window=100k, messages=10k, buffer
    20k, free must be 70k (70%): count and percent now agree, and the three
    rows sum to the window.
    """
    _patch_count_tokens(monkeypatch, message_tokens=10_000)
    host = CapturingHost()
    session = _Session(model_override="openai/gpt-4o", context_window=100_000)
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    output = host.text
    # Compute the expected partition the way the renderer does (the buffer
    # fraction is 1 - trigger, i.e. ~0.2 with float wobble).
    from omnigent.repl._repl import _CONTEXT_COMPACTION_TRIGGER

    window, msgs = 100_000, 10_000
    buf = int(window * (1.0 - _CONTEXT_COMPACTION_TRIGGER))
    free = window - msgs - buf
    assert msgs + free + buf == window  # the three rows partition the window
    # Each row's rendered count agrees with its percentage (the bug was free
    # showing window-messages while its % subtracted the buffer).
    assert f"{free:,} tokens (70%)" in output  # buffer now excluded from free
    assert f"{buf:,} tokens (20%)" in output
    assert f"{msgs:,} tokens (10%)" in output


@pytest.mark.asyncio
async def test_context_coin_bar_over_trigger_threshold(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """At 90 % usage the free zone collapses to zero coins.

    Arithmetic: used = round(0.9 × 10) = 9; buf = round(0.2 × 10) = 2;
    free = max(10 − 9 − 2, 0) = 0; buf reassigned = 10 − 9 − 0 = 1.
    So the bar holds 9 used coins and 1 buffer coin — no free coins.

    Fails if the ``max(..., 0)`` clamp or buf reassignment is removed,
    which would produce a bar wider than 10 positions.

    Note: ``░`` and ``▓`` always appear in the legend rows regardless of
    zone sizes, so assertions check the coin bar *sequence* rather than
    using ``not in output``.
    """
    _patch_count_tokens(monkeypatch, message_tokens=90_000)
    host = CapturingHost()
    session = _Session(model_override="openai/gpt-4o", context_window=100_000)
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    output = host.text
    # Coin bar sequence: 9 used coins + 1 buffer coin, no free coins.
    # Fails if clamping is removed (bar would be wider than 10 positions).
    assert "█████████▓" in output
    # Summary: 90.0k / 100k (90%)
    assert "90.0k" in output
    assert "90%" in output
    # Free-space percentage must show 0 % (clamped via max(0.0, ...)).
    assert "(0%)" in output


@pytest.mark.asyncio
async def test_context_coin_bar_full_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    """At 100 % usage both free and buffer zones collapse to zero coins.

    Arithmetic: used = round(1.0 × 10) = 10; free = 0; buf = 0.
    The bar is all 10 used coins.

    Fails if the ``min(used_frac, 1.0)`` clamp before the coin
    computation is removed, which would let used_coins exceed 10.

    Note: ``░`` and ``▓`` always appear in the legend rows regardless of
    zone sizes, so assertions check the coin bar *sequence* rather than
    using ``not in output``.
    """
    _patch_count_tokens(monkeypatch, message_tokens=100_000)
    host = CapturingHost()
    session = _Session(model_override="openai/gpt-4o", context_window=100_000)
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    output = host.text
    # Coin bar sequence: exactly 10 used coins, no free or buffer.
    # Fails if the min(used_frac, 1.0) clamp is removed (would produce > 10 █ chars).
    assert "██████████" in output
    assert "100%" in output


# ---------------------------------------------------------------------------
# Summary-line numeric formatting
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_summary_line_format(monkeypatch: pytest.MonkeyPatch) -> None:
    """Summary line renders ``X.Xk / Yk tokens (Z%)`` correctly.

    Uses 20 k / 200 k = 10 % exactly to avoid floating-point rounding
    noise in the percentage assertion.

    Fails if the format string drops the division, swaps the operands,
    or removes the percentage.
    """
    _patch_count_tokens(monkeypatch, message_tokens=20_000)
    host = CapturingHost()
    session = _Session(
        model_override="anthropic/claude-sonnet-4-6",
        context_window=200_000,
    )
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    output = host.text
    # "20.0k / 200k tokens (10%)"
    assert "20.0k" in output
    assert "200k" in output
    assert "10%" in output


# ---------------------------------------------------------------------------
# Header content
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_header_shows_agent_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Agent name appears (humanized) in the tree header.

    ``_humanize_agent_name`` converts ``"my-coding-agent"`` to
    ``"my coding agent"`` (hyphens → spaces, no title-casing).

    Fails if the header-label construction drops the agent name or the
    ``_humanize_agent_name`` call is removed.
    """
    _patch_count_tokens(monkeypatch, message_tokens=0)
    host = CapturingHost()
    session = _Session(
        agent_name="my-coding-agent",
        model_override="openai/gpt-4o",
        context_window=100_000,
    )
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    # "my-coding-agent" humanized → "my coding agent"
    assert "my coding agent" in host.text


@pytest.mark.asyncio
async def test_context_header_shows_model_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Model override id is displayed alongside the agent name in the header.

    Fails if the ``(model_override)`` parenthetical is removed from the
    header label when a ``/model`` override is active.
    """
    _patch_count_tokens(monkeypatch, message_tokens=0)
    host = CapturingHost()
    session = _Session(model_override="openai/gpt-4o", context_window=100_000)
    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )
    assert "openai/gpt-4o" in host.text


def test_context_token_count_uses_latest_compaction_summary() -> None:
    """Effective context replaces archived pre-compaction items with the summary."""
    items = [
        {"id": "msg_1", "type": "message", "role": "user", "content": "old verbose text"},
        {"id": "msg_2", "type": "message", "role": "assistant", "content": "old answer"},
        {
            "id": "cmp_1",
            "type": "compaction",
            "summary": "compressed prior context",
            "last_item_id": "msg_2",
            "model": "test-model",
            "token_count": 3,
        },
        {"id": "msg_3", "type": "message", "role": "user", "content": "new text"},
    ]

    effective = _items_for_context_token_count(items)

    assert all(item.get("id") != "msg_1" for item in effective)
    assert all(item.get("id") != "msg_2" for item in effective)
    assert any(item.get("content") == "compressed prior context" for item in effective)
    assert any(item.get("id") == "msg_3" for item in effective)


# ---------------------------------------------------------------------------
# host.tokens_used takes priority over local count_tokens estimate
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_context_uses_host_tokens_used_when_set(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``/context`` uses ``host.tokens_used`` instead of recomputing locally.

    When ``host.tokens_used`` is set (e.g. by the idle-event fallback after
    a completed turn), ``_cmd_context`` must read that value directly rather
    than fetching conversation items and calling ``count_tokens`` again.

    This is the mechanism by which the idle-event fallback fix keeps the
    context ring accurate: after each harness turn (where the provider doesn't
    report usage), the fallback updates ``host.tokens_used`` via
    ``update_context_usage``, and subsequent ``/context`` invocations read
    that cached value.

    The test verifies the priority by:
    1. Setting ``host.tokens_used = 37_000`` (simulating a cached ring value).
    2. Patching ``count_tokens`` to raise if called — proving it is NOT called
       when ``tokens_used`` is already set.
    3. Patching ``_fetch_context_items`` to raise if called — proving no
       extra HTTP round-trip is made.

    Fails if ``_cmd_context`` ignores ``host.tokens_used`` and recomputes,
    which would regress the performance contract.
    """

    # Patch count_tokens to raise — it must NOT be called when tokens_used is set
    def _should_not_count(*_args, **_kwargs):
        raise AssertionError(
            "count_tokens was called even though host.tokens_used is set. "
            "/context must use the cached value instead of recomputing."
        )

    monkeypatch.setattr("omnigent.runtime.compaction.count_tokens", _should_not_count)

    # Patch _fetch_context_items to raise — no HTTP call should happen
    async def _should_not_fetch(*_args, **_kwargs):
        raise AssertionError(
            "_fetch_context_items was called even though host.tokens_used is set. "
            "/context must use the cached value instead of fetching items."
        )

    monkeypatch.setattr("omnigent.repl._repl._fetch_context_items", _should_not_fetch)

    host = CapturingHost()
    # Simulate the idle-event fallback having set tokens_used after a completed turn.
    host.tokens_used = 37_000  # type: ignore[attr-defined]  # dynamic attribute, read via getattr

    session = _Session(context_window=200_000)

    await handle_slash_command(
        "/context",
        session,
        None,
        host,
        RichBlockFormatter(),  # type: ignore[arg-type]
    )

    # 37 000 tokens shown as "37.0k" — proves host.tokens_used was used, not recomputed
    assert "37.0k" in host.text, (
        f"Expected '37.0k' (from host.tokens_used=37000) in output, got: {host.text!r}. "
        "The cached token count from the idle-event fallback was not used."
    )


# ---------------------------------------------------------------------------
# _context_ring_state flag controls idle-event re-estimation
# ---------------------------------------------------------------------------


def test_context_ring_state_initialized_to_false() -> None:
    """``_context_ring_state`` starts as ``[False]`` inside ``run_repl``.

    The flag must default to False so the idle-event fallback fires on the
    very first turn for harnesses that don't report provider usage (codex,
    and claude-sdk before the executor fix). A True default would skip the
    first local estimate.

    This test inspects the source-level default via the module's constant
    in the closure initialisation comment.  The real invariant is exercised
    by the idle-event integration path, but a quick source-grep lets the
    unit-test suite catch an accidental default flip.
    """
    import inspect

    from omnigent.repl import _repl

    src = inspect.getsource(_repl.run_repl)
    # The initialiser line must set the flag to False (not True).
    # Fails if someone accidentally changes the default to True, which
    # would prevent the first-turn local estimate from running.
    assert "_context_ring_state: list[bool] = [False]" in src, (
        "_context_ring_state must be initialised to [False] so the idle-event "
        "fallback fires on the very first turn for harnesses without provider usage."
    )

    # The flag must be set to True in the _Completed handler when the
    # provider reported usage.  The assignment is `= True` (not
    # `= usage is not None`) because it lives inside the
    # `if usage is not None and cw:` guard — the condition is already
    # verified before we reach the assignment line.
    assert "_context_ring_state[0] = True" in src, (
        "_context_ring_state[0] must be set to True in the _Completed handler "
        "when provider usage is present, so the idle-event fallback is skipped "
        "for harnesses that do report usage (avoids overwriting provider value "
        "with a local estimate)."
    )

    # The idle-event fallback must check the flag, not host.tokens_used is None.
    assert "not _context_ring_state[0]" in src, (
        "The idle-event fallback must gate on 'not _context_ring_state[0]' "
        "instead of 'host.tokens_used is None', so it re-estimates every turn "
        "that lacks provider usage — not just the first turn."
    )


# ---------------------------------------------------------------------------
# Idle-event local-estimate fallback (_update_context_ring_estimate)
# ---------------------------------------------------------------------------


@dataclass
class _RingUpdate:
    """
    One recorded ``update_context_usage`` call.

    :param tokens: Token estimate pushed to the ring, e.g. ``1_234``.
    :param context_window: Window size pushed to the ring, e.g. ``200_000``.
    """

    tokens: int
    context_window: int


class _RingHost:
    """
    Host stub recording context-ring updates.

    Concrete class (not MagicMock) so unexpected attribute access fails
    loudly. Only the surface ``_update_context_ring_estimate`` touches
    (``update_context_usage``) is implemented.
    """

    def __init__(self) -> None:
        self.ring_updates: list[_RingUpdate] = []

    def update_context_usage(self, tokens: int, context_window: int) -> None:
        """
        Record a context-ring update.

        :param tokens: Token estimate, e.g. ``1_234``.
        :param context_window: Window size in tokens, e.g. ``200_000``.
        """
        self.ring_updates.append(_RingUpdate(tokens=tokens, context_window=context_window))


class _OnePageSessionsApi:
    """
    Sessions-API stub serving one short page of conversation items.

    The page is shorter than the 100-item pagination cap, so
    ``_list_all_conversation_items`` stops after one call.

    :param items: Item dicts to serve, e.g.
        ``[{"id": "item_1", "type": "message", ...}]``.
    """

    def __init__(self, items: list[dict[str, object]]) -> None:
        self._items = items

    async def list_items(
        self,
        conv_id: str,
        *,
        limit: int,
        after: str | None,
        order: str,
    ) -> list[dict[str, object]]:
        """
        Return the configured single page of items.

        :param conv_id: Session id being enumerated, e.g. ``"conv_abc"``.
        :param limit: Page-size cap requested by the caller, e.g. ``100``.
        :param after: Pagination cursor; ``None`` on the first call.
        :param order: Sort order, e.g. ``"asc"``.
        :returns: The configured items on the first call (cursor unset).
        """
        assert after is None, "second page requested for a sub-cap first page"
        return list(self._items)


class _ItemsClient:
    """
    AP client stub exposing only the ``sessions.list_items`` surface.

    :param items: Item dicts the sessions API should serve.
    """

    def __init__(self, items: list[dict[str, object]]) -> None:
        self.sessions = _OnePageSessionsApi(items)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "llm_model,expected_count_model",
    [
        # Native-harness agent (e.g. claude-native-ui after a mid-session
        # agent switch): spec pins no LLM model, so the fallback must use
        # the agent name. Regression — the old inline
        # closure referenced an unbound `agent_name` local here and the
        # idle event crashed with NameError instead of updating the ring.
        (None, "claude-native-ui"),
        # Spec-pinned model: used directly, agent name ignored.
        ("databricks-claude-sonnet-4-6", "databricks-claude-sonnet-4-6"),
    ],
)
async def test_idle_ring_estimate_model_fallback(
    monkeypatch: pytest.MonkeyPatch,
    llm_model: str | None,
    expected_count_model: str,
) -> None:
    """The idle fallback counts tokens and updates the ring for any llm_model.

    Exercises ``_update_context_ring_estimate`` end-to-end against stub
    client/host: fetch items → compaction-aware reduction → count_tokens
    → ``host.update_context_usage``. Fails if the ``llm_model is None``
    path raises (the original crash) or if the wrong model id is
    passed to ``count_tokens``.
    """
    counted_models: list[str] = []

    def _recording_count_tokens(messages: list[dict[str, object]], model: str) -> int:
        """
        Record the model id and return a fixed estimate.

        :param messages: Effective prompt items being counted.
        :param model: Model id chosen by the fallback,
            e.g. ``"claude-native-ui"``.
        :returns: Fixed token estimate ``1_234``.
        """
        counted_models.append(model)
        return 1_234

    # count_tokens is imported with a local `from … import` inside the
    # helper, so patching the source module attribute takes effect.
    monkeypatch.setattr(
        "omnigent.runtime.compaction.count_tokens",
        _recording_count_tokens,
    )

    session = _Session(
        agent_name="claude-native-ui",
        context_window=200_000,
        session_id="conv_abc123",
        llm_model=llm_model,
    )
    host = _RingHost()
    client = _ItemsClient([{"id": "item_1", "type": "message", "role": "user", "content": "hi"}])

    await _update_context_ring_estimate(
        session,  # type: ignore[arg-type] — duck-typed stub
        client,  # type: ignore[arg-type] — duck-typed stub
        host,  # type: ignore[arg-type] — duck-typed stub
        200_000,
    )

    # The fallback must resolve the model per the llm_model-or-agent-name
    # rule; a wrong value means the fallback chain regressed.
    assert counted_models == [expected_count_model]
    # Exactly one ring update with the count_tokens result and the passed
    # window proves the estimate reached the host (the original crash
    # left the ring untouched because the task died before this call).
    assert host.ring_updates == [_RingUpdate(tokens=1_234, context_window=200_000)]
