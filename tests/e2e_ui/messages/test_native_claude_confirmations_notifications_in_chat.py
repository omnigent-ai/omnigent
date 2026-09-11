r"""Claude-native terminal confirmations + notifications must reach the chat.

The ``claude-native`` ("Claude Code") wrapper is terminal-first: a real ``claude``
CLI runs in the session terminal, the SPA's **Terminal** view attaches to that
live TUI, and the SPA's **Chat** view renders the SAME canonical transcript
(``GET /v1/sessions/{id}/items``). When Claude shows a *terminal-only* numbered
tool-confirmation dialog — e.g. a ``ToolSearch`` prompt carrying its description,
a ``PreToolUse`` warning that Omnigent policy evaluation was unavailable, a
token-refresh failure, and Yes / remember / No choices — the chat gives NO
explanation of what is waiting. Native notifications are missing from chat too.

The bug has two independent facets, each guarded here so a partial fix cannot
pass silently:

facet 1 — the native Claude hook settings register no ``Notification`` hook, so a
    native notification can never be relayed to chat. Asserted against the REAL
    generated ``claude-settings.json`` of a live runner-launched session.

facet 2 — the shared pane capture relays the permission-mode footer and a ``/btw``
    side-chat, but NOT numbered dialogs: ``read_pane_signals`` only parses
    ``PaneSignals(permission_mode, btw_overlay)`` and ``_forward_pane_signals``
    only relays those two, so a captured dialog's warning + choices reach no chat
    item. Reproduced provider-free (per the report) by driving the REAL relay
    with a sanitized ``ToolSearch`` fail-ask dialog pane: the same capture that
    mirrors the mode footer drops the dialog's warning and every choice.

Expected behavior (the fail->pass target for the fix): mirror native
notifications and the visible text of numbered dialogs to chat, preserving the
warning and all displayed choices, without treating them as model output or
auto-answering the prompt.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import httpx

from omnigent.harnesses.claude_native import bridge as claude_native_bridge
from omnigent.harnesses.claude_native import forwarder

# claude-native auto-launch + first-run pre-accept can take a while; the runner
# writes claude-settings.json at launch (before any hook runs), so this only
# waits out the bootstrap, not a model turn.
_SETTINGS_READY_TIMEOUT_S = 120.0

# The file the runner writes the composed hook settings to inside the bridge
# dir (``_INVOCATION_SETTINGS_FILE`` in bridge.py).
_SETTINGS_FILENAME = "claude-settings.json"


# ── facet 2 fixtures: sanitized captured panes ──────────────────────────────
# A settled ``/btw`` side-chat overlay — a signal the relay DOES mirror today.
# Layout mirrors the real capture: inert scrollback, the ``▔`` overlay border,
# the question, the answer, then the copy/fork/close footer.
_BTW_BORDER = "▔" * 40
_BTW_QUESTION = "/btw is this backward compatible?"
_BTW_ANSWER = "Yes, the public API is unchanged."
_BTW_PANE = "\n".join(
    [
        "welcome banner line",
        "another line",
        _BTW_BORDER,
        "",
        f"    {_BTW_QUESTION}",
        "",
        f"      {_BTW_ANSWER}",
        "",
        "    ↑/↓ to scroll · c to copy · f to fork · Esc to close",
    ]
)

# The reported ToolSearch numbered tool-confirmation dialog, sanitized. It
# carries the tool description, the "Omnigent policy evaluation unavailable"
# PreToolUse warning, a token-refresh failure, and Yes / remember / No choices,
# with a permission-mode footer BELOW it (exactly as a real capture would: the
# mode footer is relayed, the dialog above it is not).
_DIALOG_WARNING = "Omnigent policy evaluation unavailable"
_DIALOG_TOKEN_REFRESH = "token refresh failed"
_DIALOG_DESCRIPTION = "Search available tools and skills by query."
_CHOICE_YES = "1. Yes"
_CHOICE_REMEMBER = "2. Yes, and don't ask again for ToolSearch commands"
_CHOICE_NO = "3. No, and tell Claude what to do differently"

# The warning + every displayed choice the fix must mirror to chat.
_DIALOG_NEEDLES = (_DIALOG_WARNING, _CHOICE_YES, _CHOICE_REMEMBER, _CHOICE_NO)

_TOOLSEARCH_DIALOG_PANE = "\n".join(
    [
        "● I'll look up the deployment runbook for you.",
        "",
        "╭" + "─" * 70 + "╮",
        "│ Tool use",
        "│",
        '│   ToolSearch(query: "deploy runbook")',
        f"│   {_DIALOG_DESCRIPTION}",
        "│",
        f"│   ⚠ {_DIALOG_WARNING} (could not reach or",
        f"│     authenticate to the Omnigent server); {_DIALOG_TOKEN_REFRESH}:",
        "│     401 from gateway.",
        "│",
        "│   Do you want to proceed?",
        f"│ ❯ {_CHOICE_YES}",
        f"│   {_CHOICE_REMEMBER}",
        f"│   {_CHOICE_NO} (esc)",
        "╰" + "─" * 70 + "╯",
        "  ⏸ manual mode on",
    ]
)


def _wait_for_generated_settings(base_url: str, session_id: str) -> dict[str, Any]:
    """Read the live session's real generated ``claude-settings.json``.

    Resolves the bridge dir from the session's bridge-id label (the same way
    the SPA's terminal view finds the pane) and polls until the runner has
    written the composed hook settings.

    :param base_url: Spawned server base URL.
    :param session_id: The runner-launched claude-native session id.
    :returns: The parsed settings object.
    """
    from omnigent.harnesses.claude_native.bridge import (
        BRIDGE_ID_LABEL_KEY,
        bridge_dir_for_bridge_id,
    )

    deadline = time.monotonic() + _SETTINGS_READY_TIMEOUT_S
    last_candidate: Path | None = None
    while time.monotonic() < deadline:
        session = httpx.get(f"{base_url}/v1/sessions/{session_id}", timeout=10.0).json()
        labels = session.get("labels") or {}
        bridge_id = labels.get(BRIDGE_ID_LABEL_KEY) or session_id
        last_candidate = bridge_dir_for_bridge_id(bridge_id) / _SETTINGS_FILENAME
        if last_candidate.exists():
            try:
                return json.loads(last_candidate.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass  # mid-write; retry
        time.sleep(1.0)
    raise AssertionError(
        f"claude-settings.json never appeared for session {session_id} within "
        f"{_SETTINGS_READY_TIMEOUT_S}s (last looked at {last_candidate})"
    )


def test_native_hook_settings_register_notification_hook(
    native_claude_mock_session: tuple[str, str],
) -> None:
    """facet 1: the live session's hook settings must register a Notification hook.

    A native ``Notification`` (Claude asking for attention / permission, an idle
    notice, ...) can only reach the chat transcript if the harness registers a
    ``Notification`` hook. Today ``build_hook_settings`` registers none, so the
    notice is terminal-only. This reads the REAL settings the runner generated
    for a live claude-native session.
    """
    base_url, session_id = native_claude_mock_session

    settings = _wait_for_generated_settings(base_url, session_id)
    hooks = settings.get("hooks") or {}

    # Sanity: these are the real generated settings, not an empty / half-written
    # read — several always-on hooks are present.
    assert "SessionStart" in hooks and "Stop" in hooks, (
        f"expected the real generated claude-settings.json hooks; got keys={sorted(hooks)}"
    )

    # The bug: no Notification hook, so native notifications never reach chat.
    assert "Notification" in hooks, (
        "facet 1: claude-native hook settings register no 'Notification' hook, "
        "so native Claude notifications are never relayed to the chat "
        f"transcript. registered hooks={sorted(hooks)}"
    )


def test_pane_relay_mirrors_numbered_dialog(
    tmp_path: Path,
    monkeypatch: Any,
) -> None:
    """facet 2: the pane-capture relay must mirror a numbered dialog's text to chat.

    Drives the REAL forwarder relay (``_forward_pane_signals`` ->
    ``read_pane_signals`` -> the pane parsers) with sanitized captured panes,
    substituting only the ``tmux capture-pane`` read (``_capture_pane``) with a
    fixture — the report's provider-free reproduction. A ``/btw`` overlay pane is
    the positive control (the relay mirrors it today); the ToolSearch dialog pane
    is the bug: the same capture mirrors the permission-mode footer but drops the
    warning and every choice.
    """
    # ``read_pane_signals`` reads tmux coordinates from the bridge dir before
    # capturing; the capture itself is stubbed, so the values are inert.
    (tmp_path / "tmux.json").write_text(
        json.dumps({"socket_path": "sock", "tmux_target": "tgt"}), encoding="utf-8"
    )

    pane_holder = {"pane": ""}

    def _fake_capture(*_args: Any, **_kwargs: Any) -> str:
        """Serve the current fixture pane in place of a live tmux capture."""
        return pane_holder["pane"]

    # ``read_pane_signals`` looks up ``_capture_pane`` as a bridge-module global
    # at call time, so patching it here intercepts the real parser's input.
    monkeypatch.setattr(claude_native_bridge, "_capture_pane", _fake_capture)

    def _drive(pane: str, times: int) -> list[dict[str, Any]]:
        """Run the real pane-signals relay ``times`` polls over ``pane``.

        :returns: The event bodies POSTed to the session's events endpoint.
        """
        pane_holder["pane"] = pane
        calls: list[dict[str, Any]] = []

        def _handler(request: httpx.Request) -> httpx.Response:
            """Record each relayed event body; return a benign success."""
            calls.append(json.loads(request.content.decode("utf-8")))
            return httpx.Response(200, json={"queued": False, "item_id": "item_x"})

        async def _run() -> None:
            dedupe = forwarder._ForwardDedupeState()
            transport = httpx.MockTransport(_handler)
            async with httpx.AsyncClient(transport=transport, base_url="http://ap") as client:
                for _ in range(times):
                    # Clear the per-poll capture throttle so every poll re-reads.
                    dedupe.pane_next_read = 0.0
                    await forwarder._forward_pane_signals(
                        client,
                        session_id="conv-native-claude",
                        bridge_dir=tmp_path,
                        dedupe=dedupe,
                    )

        asyncio.run(_run())
        return calls

    # Positive control: a settled /btw overlay IS relayed (two-read stability
    # guard, so drive twice) — the relay pipeline works for what it parses.
    btw_calls = _drive(_BTW_PANE, times=2)
    btw_types = [c.get("type") for c in btw_calls]
    assert "external_btw_sidechat" in btw_types, (
        f"control: the /btw overlay should relay an external_btw_sidechat event; got {btw_types}"
    )

    # The bug: the ToolSearch numbered dialog. The SAME capture carries a
    # permission-mode footer (relayed) and the dialog (dropped).
    dialog_calls = _drive(_TOOLSEARCH_DIALOG_PANE, times=3)
    dialog_types = [c.get("type") for c in dialog_calls]

    # The relay ran and mirrored the permission mode from this very capture...
    assert "external_permission_mode_change" in dialog_types, (
        "the dialog capture's permission-mode footer should relay an "
        f"external_permission_mode_change event; got {dialog_types}"
    )

    # ...but must ALSO mirror the numbered dialog's warning + every choice.
    relayed = json.dumps(dialog_calls, ensure_ascii=False)
    missing = [needle for needle in _DIALOG_NEEDLES if needle not in relayed]
    assert not missing, (
        "facet 2: the shared pane capture relayed the permission mode but "
        "dropped the numbered tool-confirmation dialog — its warning and "
        f"choices never reached a chat event. missing from the wire: {missing}. "
        f"relayed event types: {dialog_types}"
    )
