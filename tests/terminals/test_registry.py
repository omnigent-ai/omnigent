"""
Unit tests for :class:`omnigent.terminals.TerminalRegistry`.

Covers the registry's lifecycle invariants directly, without going
through the ``sys_terminal_*`` tools. The tools are tested separately
in ``tests/tools/builtins/test_sys_terminal.py``; these tests pin
down the registry's contract so a future tool refactor can't
silently regress the lifecycle.

All tests that need a running tmux are skipped when tmux is missing.
A handful of pure-bookkeeping tests (close/cleanup of unknown ids,
empty list, active_conversation_ids) run regardless.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from omnigent.inner.datamodel import OSEnvSandboxSpec, OSEnvSpec, TerminalEnvSpec
from omnigent.inner.terminal import TerminalInstance
from omnigent.terminals import TerminalRegistry
from omnigent.terminals.registry import conversation_link_for_id

# ── Pure bookkeeping (no tmux) ────────────────────────────────


def test_active_conversation_ids_empty_on_fresh_registry() -> None:
    """
    A freshly-constructed registry reports no active conversations.

    What breaks if this fails: shutdown/cleanup paths that iterate
    ``active_conversation_ids()`` would surprise themselves with
    state from prior runs (registry is supposed to be a clean
    singleton at startup).
    """
    reg = TerminalRegistry()
    assert reg.active_conversation_ids() == []


def test_get_returns_none_for_unknown_triple() -> None:
    """
    ``get`` is total — never raises for unknown ids. Returns
    ``None`` so callers can use it as a presence check.
    """
    reg = TerminalRegistry()
    assert reg.get("conv_nope", "bash", "s1") is None


def test_list_for_conversation_returns_empty_for_unknown_id() -> None:
    """
    Listing a conversation that never registered terminals must
    return ``[]`` (not raise, not return ``None``). The
    ``sys_terminal_list`` tool serializes the result as JSON; an
    empty list keeps the LLM-facing shape stable.
    """
    reg = TerminalRegistry()
    assert reg.list_for_conversation("conv_nope") == []


def test_conversation_link_for_id_uses_relative_path_by_default() -> None:
    """
    Conversation links stay relative when no Omnigent origin is known.

    This is the embedded/test-runner fallback: there is no stable
    hostname to show in the tmux status bar, but the path still points
    at the conversation route when rendered by the Omnigent web UI.
    """
    assert conversation_link_for_id("conv with/slash") == "/c/conv%20with%2Fslash"


def test_conversation_link_for_id_uses_base_url_when_provided() -> None:
    """
    Conversation links include the hostname when the runner knows one.

    This pins the out-of-process runner path: ``RUNNER_SERVER_URL`` is
    threaded into :class:`TerminalRegistry`, so managed tmux status bars
    show a complete URL instead of only ``/c/<id>``.
    """
    assert (
        conversation_link_for_id(
            "conv with/slash",
            base_url="http://127.0.0.1:8000/",
        )
        == "http://127.0.0.1:8000/c/conv%20with%2Fslash"
    )


def test_conversation_link_for_id_maps_workspace_hosted_server_to_ui_mount(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Workspace-hosted runners link to the SPA mount, not the API mount.

    The runner threads ``RUNNER_SERVER_URL`` — the API proxy base
    (``/api/2.0/omnigent``) — into the registry. A naive
    ``{base}/c/<id>`` would put the JSON API path in the tmux status
    bar; the link must instead land on the ``/omnigent`` SPA mount and
    carry the ``?o=<org>`` selector ``omnigent login`` recorded, exactly
    like the CLI's ``Web UI:`` line. Pins parity with
    :func:`omnigent.conversation_browser.conversation_url`.

    :param tmp_path: Pytest tmp dir for the stubbed auth-token file.
    :param monkeypatch: Pytest monkeypatch fixture.
    """
    from omnigent.cli_auth import store_databricks_auth

    monkeypatch.setattr(
        "omnigent.cli_auth._token_file_path",
        lambda: tmp_path / "auth_tokens.json",
    )
    server = "https://example.databricks.com/api/2.0/omnigent"
    store_databricks_auth(
        server,
        "https://example.databricks.com",
        org_id="2850744067564480",
    )

    assert (
        conversation_link_for_id("conv_abc123", base_url=server)
        == "https://example.databricks.com/omnigent/c/conv_abc123?o=2850744067564480"
    )


async def test_close_unknown_triple_returns_false() -> None:
    """
    Closing a never-launched (or already-closed) terminal returns
    ``False`` and does not raise. Idempotent close is the
    contract — workflow finally-blocks and LLM-driven cleanup
    both depend on it.
    """
    reg = TerminalRegistry()
    assert await reg.close("conv_nope", "bash", "s1") is False


async def test_cleanup_conversation_unknown_id_is_noop() -> None:
    """
    ``cleanup_conversation`` on an id with no terminals must
    return without raising — workflow finally-blocks always
    fire, even when the workflow never launched a terminal.
    """
    reg = TerminalRegistry()
    # Should not raise.
    await reg.cleanup_conversation("conv_nope")


async def test_shutdown_on_empty_registry_is_noop() -> None:
    """Shutdown of an empty registry is a no-op."""
    reg = TerminalRegistry()
    await reg.shutdown()
    assert reg.active_conversation_ids() == []


def test_transfer_moves_terminal_without_closing_tmux(tmp_path: Path) -> None:
    """
    Terminal transfer changes ownership without touching the instance.

    This is the load-bearing invariant for native Claude ``/clear``:
    moving ``claude/main`` from the old Omnigent conversation to the fresh
    one must not call ``close()``, because closing kills the tmux pane
    that still contains the live Claude process.
    """
    reg = TerminalRegistry()
    instance = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "claude.sock",
        private_dir=tmp_path / "claude",
        running=True,
    )
    lock = threading.Lock()
    reg._by_conversation["conv_old"] = {("claude", "main"): instance}
    reg._instance_locks[("conv_old", "claude", "main")] = lock

    moved = reg.transfer("conv_old", "conv_new", "claude", "main")

    assert moved is True
    assert reg.get("conv_old", "claude", "main") is None
    assert reg.get("conv_new", "claude", "main") is instance
    assert instance.running is True
    assert reg.get_instance_lock("conv_old", "claude", "main") is None
    assert reg.get_instance_lock("conv_new", "claude", "main") is lock
    assert reg.active_conversation_ids() == ["conv_new"]


def test_transfer_rejects_target_collision_without_moving_source(tmp_path: Path) -> None:
    """
    Transfer refuses to overwrite an existing target terminal.

    A collision means two live panes would claim the same
    ``(session, terminal, key)`` resource. The registry must fail loud
    and leave both original owners intact.
    """
    reg = TerminalRegistry()
    source = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "source.sock",
        private_dir=tmp_path / "source",
        running=True,
    )
    target = TerminalInstance(
        name="claude",
        session_key="main",
        socket_path=tmp_path / "target.sock",
        private_dir=tmp_path / "target",
        running=True,
    )
    reg._by_conversation["conv_old"] = {("claude", "main"): source}
    reg._by_conversation["conv_new"] = {("claude", "main"): target}

    with pytest.raises(RuntimeError, match="already exists"):
        reg.transfer("conv_old", "conv_new", "claude", "main")

    assert reg.get("conv_old", "claude", "main") is source
    assert reg.get("conv_new", "claude", "main") is target


# ── Real-tmux lifecycle tests ─────────────────────────────────

pytestmark_tmux = pytest.mark.skipif(
    shutil.which("tmux") is None,
    reason="tmux not installed; registry lifecycle tests need a real tmux on PATH",
)


@pytest.fixture
def bash_spec(tmp_path: Path) -> TerminalEnvSpec:
    """A minimal :class:`TerminalEnvSpec` with sandbox=none anchored at tmp_path.

    :param tmp_path: Pytest's tmpdir — the working directory for the
        spawned tmux. Sandbox is forced off so the test doesn't depend
        on bwrap / macOS sandbox availability.
    :returns: A :class:`TerminalEnvSpec` ready to launch.
    """
    return TerminalEnvSpec(
        command="bash",
        os_env=OSEnvSpec(
            type="caller_process",
            cwd=str(tmp_path),
            sandbox=OSEnvSandboxSpec(type="none"),
        ),
    )


@pytest.fixture
async def cleanup(reg_with_tmux: TerminalRegistry) -> AsyncIterator[None]:
    """
    Tear down every terminal at test exit. Must come AFTER the
    registry fixture so its teardown runs before pytest discards
    the registry.

    :param reg_with_tmux: The registry fixture.
    :yields: ``None`` — value isn't consumed.
    """
    yield
    await reg_with_tmux.shutdown()


@pytest.fixture
def reg_with_tmux() -> TerminalRegistry:
    """Fresh registry for each tmux-backed test."""
    return TerminalRegistry()


@pytestmark_tmux
async def test_launch_get_close_round_trip(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    ``launch`` registers the instance, ``get`` finds it, ``close``
    removes it. The most basic happy path through the registry.
    """
    del cleanup  # consumed for teardown side-effect
    instance = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    assert instance.running, (
        "Instance should be running after launch; if not, tmux spawn failed silently."
    )
    # Same instance is returned on get.
    assert reg_with_tmux.get("conv_a", "bash", "s1") is instance

    closed = await reg_with_tmux.close("conv_a", "bash", "s1")
    assert closed is True, (
        "Close should return True for a live instance. False "
        "would mean the registry forgot the entry between "
        "launch and close."
    )
    # And the entry is gone.
    assert reg_with_tmux.get("conv_a", "bash", "s1") is None


@pytestmark_tmux
async def test_launch_idempotent_returns_existing_instance(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    Launching the same triple twice returns the SAME instance
    (no second tmux spawned). Required so the LLM's retries on
    transient errors don't leak orphan tmux sessions.
    """
    del cleanup
    first = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    second = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    assert first is second, (
        "Idempotent launch must return the existing instance. If "
        "first is not second, the registry is spawning a fresh "
        "tmux on every launch — a leak."
    )


@pytestmark_tmux
async def test_distinct_session_keys_get_distinct_instances(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    Two different session_keys for the same terminal name produce
    two independent :class:`TerminalInstance` objects with distinct
    sockets. Validates the (name, session_key) keying.
    """
    del cleanup
    s1 = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    s2 = await reg_with_tmux.launch("conv_a", "bash", "s2", bash_spec)
    assert s1 is not s2
    assert s1.socket_path != s2.socket_path, (
        "Distinct session keys must spawn distinct tmux sockets. "
        "If they match, the registry collapsed (name, key) to "
        "name-only."
    )


@pytestmark_tmux
async def test_distinct_conversations_isolated(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    Two conversations launching ``bash:s1`` get two distinct
    instances. Conversations must not share terminal state.
    """
    del cleanup
    inst_a = await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    inst_b = await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)
    assert inst_a is not inst_b
    # Cross-conversation get returns None — no leakage.
    assert reg_with_tmux.get("conv_a", "bash", "s1") is inst_a
    assert reg_with_tmux.get("conv_b", "bash", "s1") is inst_b


@pytestmark_tmux
async def test_list_for_conversation_returns_only_owners_terminals(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
    cleanup: None,
) -> None:
    """
    ``list_for_conversation`` returns only the requested
    conversation's terminals — no leakage of other conversations'
    sessions.
    """
    del cleanup
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.launch("conv_a", "bash", "s2", bash_spec)
    await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)

    listed_a = reg_with_tmux.list_for_conversation("conv_a")
    listed_b = reg_with_tmux.list_for_conversation("conv_b")
    a_keys = {(e.terminal_name, e.session_key) for e in listed_a}
    b_keys = {(e.terminal_name, e.session_key) for e in listed_b}
    assert a_keys == {("bash", "s1"), ("bash", "s2")}
    assert b_keys == {("bash", "s1")}


@pytestmark_tmux
async def test_cleanup_conversation_closes_all_owners_terminals(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
) -> None:
    """
    ``cleanup_conversation`` closes every terminal owned by the
    conversation and drops the per-conversation slot. Other
    conversations' terminals are untouched.

    What breaks if this fails: workflow exit doesn't release tmux
    sessions; orphans accumulate across the Omnigent process lifetime.
    """
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.launch("conv_a", "bash", "s2", bash_spec)
    inst_b = await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)

    await reg_with_tmux.cleanup_conversation("conv_a")

    # conv_a slot is gone — both entries cleared.
    assert reg_with_tmux.list_for_conversation("conv_a") == []
    assert reg_with_tmux.get("conv_a", "bash", "s1") is None
    assert reg_with_tmux.get("conv_a", "bash", "s2") is None

    # conv_b is untouched — isolation invariant.
    assert reg_with_tmux.get("conv_b", "bash", "s1") is inst_b

    # Cleanup the surviving instance.
    await reg_with_tmux.cleanup_conversation("conv_b")


@pytestmark_tmux
async def test_close_after_cleanup_returns_false(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
) -> None:
    """
    Closing a terminal that ``cleanup_conversation`` already
    removed returns ``False`` — no double-close errors.
    """
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.cleanup_conversation("conv_a")
    # Subsequent close on the now-cleared entry: idempotent False.
    assert await reg_with_tmux.close("conv_a", "bash", "s1") is False


@pytestmark_tmux
async def test_shutdown_clears_all_conversations(
    reg_with_tmux: TerminalRegistry,
    bash_spec: TerminalEnvSpec,
) -> None:
    """
    ``shutdown`` closes every terminal in every conversation and
    leaves the registry empty. Drives the FastAPI lifespan
    shutdown path.
    """
    await reg_with_tmux.launch("conv_a", "bash", "s1", bash_spec)
    await reg_with_tmux.launch("conv_b", "bash", "s1", bash_spec)
    assert sorted(reg_with_tmux.active_conversation_ids()) == ["conv_a", "conv_b"]

    await reg_with_tmux.shutdown()
    assert reg_with_tmux.active_conversation_ids() == []
    # Confirm the per-conversation slot is also gone (not just
    # emptied with a stale key).
    assert reg_with_tmux.list_for_conversation("conv_a") == []
    assert reg_with_tmux.list_for_conversation("conv_b") == []
