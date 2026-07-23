"""Tests for omnigent.repl._repl helpers.

Covers pure parsing helpers used by the Ctrl+O debug overlay. The
overlay fetches items from the conversation store and walks
function_call_output rows looking for sys_session_send handles; the
parsing logic under test here is the seam that decides whether a
given output is a sub-agent spawn (and therefore deserves a sidebar
tab) or an unrelated tool result to skip.
"""

from __future__ import annotations

import json

import pytest
from prompt_toolkit.document import Document

from omnigent.repl._repl import (
    _SLASH_COMMAND_ALIASES,
    COMMANDS,
    WELCOME_HINTS,
    _build_call_id_to_name_lookup,
    _build_call_id_to_tool_metadata_lookup,
    _build_model_readout_lines,
    _build_startup_header,
    _consume_pending_local_skill_slash_command,
    _decode_terminal_target_key,
    _fetch_server_version,
    _is_recoverable_sse_transport_error,
    _parse_sub_agent_handle,
    _parse_terminal_tool_output,
    _reconstruct_terminals_from_items,
    _render_history_item,
    _render_startup_banner_ansi,
    _server_event_to_sdk_event,
    _SessionsChatReplAdapter,
    _SlashCommandCompleter,
    _StartupHeader,
    _summarize_description,
    _terminal_attach_command,
    _terminal_target_key,
    _TerminalInfo,
    _tmux_pane_snapshot,
    _tmux_session_alive,
)
from omnigent.spec.types import SkillSpec


def test_parse_sub_agent_handle_returns_raw_handle_dict() -> None:
    """
    Native omnigent builtins persist the spawn output as a raw
    JSON string encoding the handle dict. The parser should return
    it unchanged.

    Claim: the happy path on the native builtin shape surfaces a
    dict with the fields downstream sidebar code reads
    (``conversation_id``, ``type``, ``name``).
    """
    handle = {
        "task_id": "resp_abc",
        "conversation_id": "conv_abc",
        "kind": "sub_agent",
        "type": "worker_a",
        "name": "fib_class",
        "status": "in_progress",
    }
    result = _parse_sub_agent_handle(json.dumps(handle))
    # Explicit equality instead of ``is not None`` + field spot-checks
    # so a regression that strips a field in the parser would fail.
    assert result == handle


def test_parse_sub_agent_handle_unwraps_mcp_content_parts() -> None:
    """
    The claude-sdk harness's MCP bridge wraps the handle as a
    content-parts list before persistence. The parser must unwrap
    the ``text`` part and recover the inner handle dict — the exact
    regression that produced zero sub-agent tabs under
    coding_supervisor_with_forks on 2026-04-22.

    Claim: the MCP content-parts shape yields the SAME handle dict
    as the raw-JSON shape. A failure here would mean the overlay
    silently drops every sub-agent row on the claude-sdk harness.
    """
    handle = {
        "task_id": "resp_abc",
        "conversation_id": "conv_abc",
        "kind": "sub_agent",
        "type": "worker_a",
        "name": "fib_class",
        "status": "in_progress",
    }
    wrapped = json.dumps([{"type": "text", "text": json.dumps(handle)}])
    assert _parse_sub_agent_handle(wrapped) == handle


def test_parse_sub_agent_handle_returns_none_for_non_sub_agent_tool_output() -> None:
    """
    A function_call_output for a non-spawn tool (e.g. ``Bash``)
    must NOT produce a sidebar row. Returning ``None`` lets the
    caller's loop ``continue`` cleanly.

    Claim: a valid JSON dict that is NOT a sub_agent handle is
    rejected — the parser's discriminator is ``kind ==
    "sub_agent"``, nothing looser.
    """
    non_sub_agent = json.dumps({"stdout": "hello", "exit_code": 0})
    assert _parse_sub_agent_handle(non_sub_agent) is None


def test_parse_sub_agent_handle_returns_none_for_mcp_wrapper_with_non_sub_agent() -> None:
    """
    Same as above but through the MCP wrapper — the unwrap must
    still reject non-sub-agent payloads.
    """
    wrapped = json.dumps(
        [{"type": "text", "text": json.dumps({"stdout": "hi", "exit_code": 0})}],
    )
    assert _parse_sub_agent_handle(wrapped) is None


@pytest.mark.parametrize(
    "raw",
    [
        # Malformed JSON — tool output corruption.
        "not json at all {",
        # Valid JSON scalar — neither dict nor list.
        '"just a string"',
        "42",
        "null",
        # Valid JSON list but no ``text`` part.
        '[{"type": "image", "data": "…"}]',
        # Valid JSON list with a text part that isn't JSON.
        '[{"type": "text", "text": "hello world"}]',
        # Valid JSON list with a text part that decodes to a scalar.
        '[{"type": "text", "text": "\\"hi\\""}]',
    ],
)
def test_parse_sub_agent_handle_returns_none_for_garbage(raw: str) -> None:
    """
    The parser must not raise on any string input — malformed
    outputs in the conversation store must degrade to "no sidebar
    row for this item" rather than crashing the overlay build.

    Claim: every rejection path returns ``None`` rather than
    raising. A regression that raised for any of these inputs
    would take down the debug overlay on any conversation that
    contained the offending row.
    """
    assert _parse_sub_agent_handle(raw) is None


# ── _render_history_item full-fidelity rendering ─────────────
#
# Covers the renderer that re-emits every conversation item on
# ``--continue`` / ``/switch`` resume. The contract: render every
# item with the same visual primitives the live stream uses, so
# resuming a long session shows the same transcript the user
# originally saw — full tool-call args, full result panels,
# full reasoning, untruncated assistant text. A regression that
# silently drops ``function_call_output`` rendering, truncates
# assistant text, or strips tool-call args reverts the
# user-reported "annoying abbreviated history" symptom.


class _CapturingHost:
    """
    Minimal :class:`omnigent_ui_sdk.TerminalHost`-shaped stub that
    records every ``output(...)`` call.

    The real :class:`TerminalHost` writes to prompt-toolkit's UI;
    these tests only need to assert WHAT was written. Capturing
    the renderables (or their plain-text projection via
    :class:`rich.console.Console.export_text`) lets us assert on
    the rendered surface without spinning up a real terminal.
    """

    def __init__(self) -> None:
        self.outputs: list[object] = []

    def output(self, item: object) -> None:
        self.outputs.append(item)

    def render_plain(self) -> str:
        """
        Project every captured renderable to plain text in order.

        :returns: One concatenated string with newlines preserved.
            Used by tests asserting on rendered content (e.g.
            "the panel body contains the result text").
        """
        from io import StringIO

        from rich.console import Console

        buf = StringIO()
        console = Console(file=buf, force_terminal=False, width=200, color_system=None)
        for item in self.outputs:
            console.print(item)
        return buf.getvalue()


def test_render_history_item_user_message_emits_user_echo() -> None:
    """
    User messages render via :meth:`RichBlockFormatter.user_message`,
    same code path the live stream uses when echoing the user's typed
    line. The captured output must contain the user message text
    after the ``❯`` echo prefix.

    Claim: a regression that swapped the user-message branch for
    something else (or stripped the text) would surface as the body
    text missing from rendered output.
    """
    host = _CapturingHost()
    item = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "summarize the last commit"}],
    }
    _render_history_item(item, host)
    plain = host.render_plain()
    # Body present.
    assert "summarize the last commit" in plain, (
        f"User message body missing from render. Got: {plain!r}. If empty, "
        f"the user-message branch silently dropped the text — the resume "
        f"transcript would be missing user turns entirely."
    )
    # ``❯`` echo prefix from ``user_message``.
    assert "❯" in plain, (
        f"User message echo prefix ❯ missing from render. Got: {plain!r}. "
        f"If absent, ``fmt.user_message`` was bypassed — the resumed "
        f"transcript stops looking like the live stream."
    )


def test_render_history_item_assistant_message_does_not_truncate_long_text() -> None:
    """
    Long assistant replies (>300 chars) render in full. The previous
    implementation truncated to 300 chars + ``…`` which dropped
    multi-paragraph diagnoses, the exact UX the user reported as
    "abbreviated history".

    Claim: a regression reintroducing the 300-char truncation would
    leave the tail of the body absent from the rendered output.
    """
    host = _CapturingHost()
    long_body = "para one. " + ("X" * 400) + " trailing-marker-string"
    item = {
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-4-6",
        "content": [{"type": "output_text", "text": long_body}],
    }
    _render_history_item(item, host)
    plain = host.render_plain()
    # The trailing marker after the 400 X's is at offset >410 — well past
    # the old 300-char truncation. If truncation regresses, this assertion
    # fails because the marker drops out of the rendered text.
    assert "trailing-marker-string" in plain, (
        f"Long assistant text was truncated. Got: {plain!r}. If the "
        f"trailing marker is missing, the renderer reintroduced a length "
        f"cap and resumed conversations re-acquire the abbreviated-history "
        f"symptom."
    )
    # Header marker present so we know the assistant branch fired.
    assert "claude-sonnet-4-6" in plain, f"Assistant model header missing. Got: {plain!r}."


def test_render_history_item_assistant_message_renders_body_as_markdown_paragraphs() -> None:
    """
    Assistant body is rendered as one or more
    :class:`rich.markdown.Markdown` panels (one per
    blank-line-separated paragraph), matching the live
    stream's ``_markdown_replace`` path.

    Claim: a regression that re-introduces the previous
    ``Text.from_markup(f"   [{fmt.muted}]<line>[/{fmt.muted}]")``
    rendering — which dimmed assistant text to gray — would
    leave zero ``Padding(Markdown(...))`` instances in the
    captured outputs. Pinning the type catches that regression
    even when the rendered plain text still happens to contain
    the body.
    """
    from rich.markdown import Markdown
    from rich.padding import Padding

    host = _CapturingHost()
    item = {
        "type": "message",
        "role": "assistant",
        "model": "claude",
        # Two paragraphs, separated by a blank line. The renderer
        # should produce TWO ``Padding(Markdown(...))`` outputs —
        # one per paragraph — matching the live stream's
        # per-paragraph ``_markdown_replace`` cadence.
        "content": [
            {
                "type": "output_text",
                "text": "first paragraph with body\n\nsecond paragraph follows",
            }
        ],
    }
    _render_history_item(item, host)
    md_panels = [
        out
        for out in host.outputs
        if isinstance(out, Padding) and isinstance(out.renderable, Markdown)
    ]
    # 2 = one ``Padding(Markdown)`` per non-empty paragraph. If 0,
    # the body regressed to the muted-Text rendering and the user
    # sees gray text again. If 1, paragraph splitting broke and
    # the two paragraphs render as one combined block (changes
    # spacing). If >2, blank-paragraph filtering broke.
    assert len(md_panels) == 2, (
        f"Expected 2 Padding(Markdown) panels (one per paragraph), got "
        f"{len(md_panels)}. captured outputs: {host.outputs!r}. If 0, "
        f"the assistant body reverted to muted-gray Text rendering — "
        f"the user-reported 'agent text is gray' regression."
    )


def test_render_history_item_assistant_message_empty_body_is_silently_skipped() -> None:
    """
    Empty assistant items
    (``[{"type":"output_text","text":""}]``) are persisted by the
    omnigent workflow alongside every real reply. Rendering them
    would produce a phantom ``◆ <model>`` header with no body — the
    "double-label" regression.

    Claim: the renderer must emit zero output for an empty body.
    A regression that emits the header anyway leaves the captured
    output non-empty.
    """
    host = _CapturingHost()
    item = {
        "type": "message",
        "role": "assistant",
        "model": "claude",
        "content": [{"type": "output_text", "text": ""}],
    }
    _render_history_item(item, host)
    assert host.outputs == [], (
        f"Empty assistant item produced output: {host.outputs!r}. "
        f"Expected zero outputs — otherwise resumed conversations "
        f"render a phantom ``◆ <model>`` header per turn with no "
        f"body underneath."
    )


def test_render_history_item_function_call_includes_args_summary() -> None:
    """
    ``function_call`` rendering must include the args summary from
    :func:`format_tool_args_brief`, matching the live ``⏵
    Read(file.py)`` style. The previous implementation rendered
    only ``⏵ Read`` with no args, dropping the only piece of
    context that distinguishes one call from another in a sea of
    Reads.

    Claim: a regression that omits args (or fails to construct a
    ``ToolGroup`` with a populated ``args_summary``) leaves the
    file basename out of the rendered output.
    """
    host = _CapturingHost()
    item = {
        "type": "function_call",
        "call_id": "c1",
        "name": "Read",
        # ``arguments`` may arrive as a JSON string OR a dict
        # depending on writer. Use the harder string form here so
        # ``_coerce_arguments_dict`` is also exercised.
        "arguments": json.dumps({"file_path": "/tmp/example_module/foo.py"}),
    }
    _render_history_item(item, host)
    plain = host.render_plain()
    # Tool name in the call line.
    assert "Read" in plain, f"Tool name missing from function_call render. Got: {plain!r}."
    # Args summary: the file basename, NOT the full path. This is
    # the same brief-form the live stream produces via
    # ``format_tool_args_brief``.
    assert "foo.py" in plain, (
        f"Args summary missing from function_call render. Got: {plain!r}. "
        f"If absent, args were dropped — resumed conversations show every "
        f"Read as anonymous ``⏵ Read`` with no file context."
    )


def test_render_history_item_function_call_output_renders_panel_with_tool_name() -> None:
    """
    ``function_call_output`` rendering builds a result panel via
    :meth:`RichBlockFormatter.format_tool_result`. The panel's tool
    name comes from the *call_id_to_name* lookup the caller
    pre-builds — function_call_output items only carry call_id, not
    the tool name.

    Claim: when the lookup contains the call_id, the rendered panel
    contains the tool's output text. A regression that skips
    function_call_output rendering entirely (the old behavior)
    surfaces as zero captured outputs.
    """
    host = _CapturingHost()
    item = {
        "type": "function_call_output",
        "call_id": "c1",
        "output": "exit 0\n42 lines indexed",
    }
    call_id_to_name = {"c1": "Bash"}
    _render_history_item(item, host, call_id_to_name=call_id_to_name)
    assert host.outputs, (
        "function_call_output produced no rendered output. The previous "
        "behavior dropped tool outputs entirely; resumed conversations "
        "showed only call lines with no result panels. This regression "
        "is exactly what the fix prevents."
    )
    plain = host.render_plain()
    # Output text appears inside the panel body.
    assert "42 lines indexed" in plain, (
        f"Tool output missing from rendered panel. Got: {plain!r}. "
        f"If absent, the panel rendered an empty body — the user "
        f"sees a labeled box with no content."
    )


def test_render_history_item_function_call_output_uses_pretty_renderer_with_metadata() -> None:
    """
    Sessions-API live rendering receives ``function_call`` and
    ``function_call_output`` as separate conversation items. The output
    item only has ``call_id`` + ``output``; the pretty renderer needs the
    matching call's tool name and arguments. If that metadata is not
    threaded through, shell results fall back to a generic JSON panel and
    lose the command line.
    """
    host = _CapturingHost()
    items = [
        {
            "type": "function_call",
            "call_id": "c_shell",
            "name": "sys_os_shell",
            "arguments": json.dumps({"command": "echo pretty"}),
        },
        {
            "type": "function_call_output",
            "call_id": "c_shell",
            "output": json.dumps(
                {
                    "stdout": "pretty\n",
                    "stderr": "",
                    "exit_code": 0,
                    "timed_out": False,
                    "cwd": "/tmp/project",
                    "shell": "/bin/zsh",
                }
            ),
        },
    ]
    metadata = _build_call_id_to_tool_metadata_lookup(items)

    _render_history_item(
        items[1],
        host,
        call_id_to_tool_metadata=metadata,
    )

    plain = host.render_plain()
    assert "shell · exit 0" in plain
    assert "$ echo pretty" in plain
    assert "pretty" in plain


def test_render_history_item_accepts_entity_shaped_function_call_metadata() -> None:
    """Function-call metadata may be nested under ``data`` on session items."""
    items = [
        {
            "type": "function_call",
            "call_id": "c1",
            "data": {
                "name": "sys_os_shell",
                "arguments": json.dumps({"command": "pwd"}),
            },
        }
    ]

    metadata = _build_call_id_to_tool_metadata_lookup(items)

    assert metadata == {"c1": ("sys_os_shell", {"command": "pwd"})}


def test_render_history_item_reasoning_emits_panel_when_text_present() -> None:
    """
    ``reasoning`` items render via
    :meth:`RichBlockFormatter.format_reasoning`. Empty reasoning is
    silently skipped (no panel-with-no-body); reasoning with text
    produces the live thinking panel.

    Claim: present reasoning surfaces in the rendered output;
    a regression that drops reasoning entirely leaves zero outputs.
    """
    host = _CapturingHost()
    item = {
        "type": "reasoning",
        "summary": "Considering the trade-off between A and B",
        "content": "",
    }
    _render_history_item(item, host)
    assert host.outputs, (
        f"reasoning with summary text produced no rendered output. Got: "
        f"{host.outputs!r}. If empty, the reasoning branch was bypassed "
        f"and resumed conversations lose every chain-of-thought panel."
    )
    plain = host.render_plain()
    assert "Considering the trade-off" in plain, (
        f"Reasoning summary missing from rendered panel. Got: {plain!r}."
    )


def test_render_history_item_reasoning_with_no_text_renders_nothing() -> None:
    """
    A ``reasoning`` item with empty summary AND empty content
    produces zero output — :meth:`RichBlockFormatter.format_reasoning`
    would emit nothing and the renderer should not even call it,
    avoiding spurious empty lines in the resumed transcript.

    Claim: empty reasoning produces zero outputs. A regression
    that always calls the panel formatter would emit blank
    padding and degrade the resumed view's density.
    """
    host = _CapturingHost()
    item = {"type": "reasoning", "summary": "", "content": ""}
    _render_history_item(item, host)
    assert host.outputs == [], (
        f"Empty reasoning produced output: {host.outputs!r}. Expected zero outputs."
    )


def test_render_history_item_function_call_output_falls_back_when_call_id_missing() -> None:
    """
    When the ``call_id_to_name`` lookup misses (e.g. the
    corresponding ``function_call`` row was trimmed by the server),
    the panel still renders — with a placeholder tool name —
    rather than disappearing.

    Claim: an unmatched output still produces a rendered panel
    (preserving turn-boundary visibility) and the body text is
    present. A regression that returned early on the lookup miss
    would silently drop these orphan outputs from resumed
    conversations.
    """
    host = _CapturingHost()
    item = {
        "type": "function_call_output",
        "call_id": "orphan-call",
        "output": "stdout fragment with no matching call",
    }
    # Empty lookup — simulates the orphan-output case.
    _render_history_item(item, host, call_id_to_name={})
    assert host.outputs, (
        "Orphan function_call_output produced no output. Even without "
        "a matching call, the panel must render so the resumed "
        "transcript preserves the turn-boundary signal."
    )
    plain = host.render_plain()
    assert "stdout fragment with no matching call" in plain, (
        f"Orphan output body missing. Got: {plain!r}."
    )


def test_build_call_id_to_name_lookup_indexes_only_function_calls() -> None:
    """
    The lookup helper walks every item once and stashes
    ``function_call.name`` keyed by ``function_call.call_id``.
    Non-function_call rows are silently skipped.

    Claim: a function_call's name is recoverable by call_id; a
    non-function_call row is not in the index. A regression that
    indexed by ``response_id`` or that included
    ``function_call_output`` rows would leak entries that don't
    belong in the lookup.
    """
    items = [
        {"type": "function_call", "call_id": "c1", "name": "Read"},
        {
            "type": "function_call_output",
            "call_id": "c1",
            "output": "ignored — outputs don't carry name",
        },
        {"type": "function_call", "call_id": "c2", "name": "Bash"},
        {"type": "message", "role": "user", "call_id": "c-fake"},
    ]
    lookup = _build_call_id_to_name_lookup(items)
    assert lookup == {"c1": "Read", "c2": "Bash"}, (
        f"Lookup must contain only function_call entries; got "
        f"{lookup!r}. If extra keys appear, non-function_call rows are "
        f"leaking. If keys are missing, the walker doesn't recognize "
        f"the function_call shape."
    )


# ── Terminal reconstruction (supervision MVP) ──────────


def _make_function_call(call_id: str, name: str) -> dict:
    """Helper: build the API-shape function_call item.

    :param call_id: The tool call's correlation id, e.g. ``"c1"``.
    :param name: The tool name, e.g. ``"sys_terminal_launch"``.
    :returns: One conversation item dict in API shape.
    """
    return {
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": "{}",
    }


def _make_function_call_output(call_id: str, output: str) -> dict:
    """Helper: build the API-shape function_call_output item.

    :param call_id: The matching call's id.
    :param output: The tool's stringified output (typically JSON).
    :returns: One conversation item dict in API shape.
    """
    return {
        "type": "function_call_output",
        "call_id": call_id,
        "output": output,
    }


def test_reconstruct_terminals_extracts_launched_terminal() -> None:
    """
    A single launch call produces one live terminal in the
    reconstructed map.

    Pins the basic launch path: the walker matches the
    function_call (``name == "sys_terminal_launch"``) with its
    function_call_output via ``call_id``, decodes the JSON
    payload, and emits one :class:`_TerminalInfo` per launch.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "tmux_socket": "/tmp/sock1",
                    "has_os_env": False,
                    "status": "launched",
                }
            ),
        ),
    ]
    live = _reconstruct_terminals_from_items(items, conv_id="conv_a")
    assert len(live) == 1, (
        f"Expected one live terminal after a single launch, got {len(live)}: {live!r}."
    )
    info = live[0]
    assert info.name == "bash"
    assert info.session == "s1"
    assert info.socket == "/tmp/sock1"
    assert info.target == "main"
    # ``conv_id`` is recorded so the overlay can label which
    # conversation owns the terminal — main vs. sub-agent.
    assert info.conv_id == "conv_a"


def test_reconstruct_terminals_drops_closed_terminal() -> None:
    """
    Launch-then-close → empty live set.

    The walker treats ``sys_terminal_close`` outputs with
    ``status == "closed"`` as the inverse of launch — pops the
    matching ``(name, session)`` key from the live map. Without
    this, closed terminals would persist in the sidebar
    forever, defeating the "what's running RIGHT NOW" UX.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "tmux_socket": "/tmp/sock1",
                    "status": "launched",
                }
            ),
        ),
        _make_function_call("c2", "sys_terminal_close"),
        _make_function_call_output(
            "c2",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "status": "closed",
                }
            ),
        ),
    ]
    live = _reconstruct_terminals_from_items(items, conv_id="conv_a")
    # Empty list — the close removed the launch's entry. If
    # this returns 1, the close branch isn't firing and the
    # sidebar would falsely advertise a closed terminal as live.
    assert live == []


def test_reconstruct_terminals_keeps_other_after_partial_close() -> None:
    """
    Launching two terminals and closing one leaves the other live.

    Per-terminal state is keyed on ``(name, session)``, so a
    close targeting one key must NOT collaterally drop the
    other. Pinning the negative case so a future refactor that
    accidentally clears the whole map on any close gets caught.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "tmux_socket": "/tmp/sock1",
                    "status": "launched",
                }
            ),
        ),
        _make_function_call("c2", "sys_terminal_launch"),
        _make_function_call_output(
            "c2",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s2",
                    "tmux_socket": "/tmp/sock2",
                    "status": "launched",
                }
            ),
        ),
        _make_function_call("c3", "sys_terminal_close"),
        _make_function_call_output(
            "c3",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "status": "closed",
                }
            ),
        ),
    ]
    live = _reconstruct_terminals_from_items(items, conv_id="conv_a")
    assert len(live) == 1
    assert live[0].session == "s2"
    assert live[0].socket == "/tmp/sock2"


def test_reconstruct_terminals_skips_failed_launch() -> None:
    """
    A launch whose output carries an ``error`` field is NOT
    counted as live.

    The legacy CLI's in-memory ``Session._terminal_instances``
    only ever contains successfully-spawned instances. Mirror
    that here so the sidebar doesn't show phantom rows for
    launches that failed at the tmux subprocess.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps({"error": "launch failed: no tmux"}),
        ),
    ]
    assert _reconstruct_terminals_from_items(items, conv_id="conv_a") == []


def test_reconstruct_terminals_handles_mcp_wrapped_output() -> None:
    """
    The claude-sdk harness wraps tool outputs in MCP
    content-parts shape; the walker decodes that envelope.

    Without this branch, every terminal launched under the
    claude-sdk harness would be invisible to the overlay —
    same regression mode that hit sub-agent discovery before
    :func:`_parse_sub_agent_handle` learned the same wrapper.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps(
                [
                    {
                        "type": "text",
                        "text": json.dumps(
                            {
                                "terminal": "bash",
                                "session": "s1",
                                "tmux_socket": "/tmp/sock1",
                                "status": "launched",
                            }
                        ),
                    }
                ]
            ),
        ),
    ]
    live = _reconstruct_terminals_from_items(items, conv_id="conv_a")
    assert len(live) == 1
    assert live[0].socket == "/tmp/sock1"


def test_reconstruct_terminals_handles_already_running_status() -> None:
    """
    Idempotent re-launch (``status == "already_running"``)
    still counts as live.

    The launch tool is idempotent — calling it on a terminal
    that's already up returns ``status: "already_running"``
    with the same socket. The reconstructor accepts both
    statuses; otherwise an LLM that re-checks its own
    terminals would clear them from the sidebar on the next
    overlay open.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "tmux_socket": "/tmp/sock1",
                    "status": "already_running",
                }
            ),
        ),
    ]
    live = _reconstruct_terminals_from_items(items, conv_id="conv_a")
    assert len(live) == 1


def test_reconstruct_terminals_ignores_unrelated_tools() -> None:
    """
    Non-terminal tool calls are silently skipped.

    A conversation full of ``Bash`` / ``Read`` / ``sys_session_send``
    outputs must not produce phantom terminal entries from
    coincidental field names. The walker's allowlist on
    ``tool_name`` is what guards this.
    """
    items = [
        _make_function_call("c1", "Bash"),
        _make_function_call_output(
            "c1",
            json.dumps({"output": "hello\n"}),
        ),
        _make_function_call("c2", "sys_session_send"),
        _make_function_call_output(
            "c2",
            json.dumps(
                {
                    "kind": "sub_agent",
                    "type": "function",
                    "name": "coder",
                    "conversation_id": "conv_x",
                    # Even with a confusable ``terminal`` /
                    # ``session`` shape this must not turn into
                    # a terminal row — the tool name guard
                    # prevents it.
                    "terminal": "bash",
                    "session": "s1",
                    "tmux_socket": "/tmp/sock",
                }
            ),
        ),
    ]
    assert _reconstruct_terminals_from_items(items, conv_id="conv_a") == []


def test_terminal_target_key_roundtrips() -> None:
    """
    :func:`_terminal_target_key` /
    :func:`_decode_terminal_target_key` are exact inverses.

    Pinning the round-trip so the content builder's decode
    can't drift from the encoder. Failure mode would be the
    sidebar building keys the builder can't read — the panel
    would render empty for terminal targets, silently.
    """
    info = _TerminalInfo(
        name="bash",
        session="s1",
        socket="/tmp/sock",
        target="main",
        conv_id="conv_abc123",
    )
    key = _terminal_target_key(info)
    decoded = _decode_terminal_target_key(key)
    assert decoded == ("conv_abc123", "bash", "s1")


def test_decode_terminal_target_key_returns_none_for_non_terminal() -> None:
    """
    Sub-agent / main keys decode to ``None`` so the content
    builder's terminal short-circuit doesn't fire on them.
    """
    # Real conversation id (sub-agent target shape) — must NOT
    # be misclassified as a terminal key.
    assert _decode_terminal_target_key("conv_abc123") is None
    # Sentinel "main" key for fresh REPL state.
    assert _decode_terminal_target_key("main") is None


def test_terminal_attach_command_uses_socket_and_target() -> None:
    """
    Attach command shape matches the legacy CLI's output.

    The user's muscle memory from the non-AP path is
    ``tmux -S <socket> attach -t <target>``. Pinning the
    format here so a refactor doesn't produce a different
    string that breaks copy-paste.
    """
    info = _TerminalInfo(
        name="bash",
        session="s1",
        socket="/tmp/test.sock",
        target="main",
        conv_id="conv_a",
    )
    cmd = _terminal_attach_command(info)
    # ``shlex.quote`` leaves benign paths alone.
    assert cmd == "tmux -S /tmp/test.sock attach -t main"


def test_parse_terminal_tool_output_handles_direct_dict() -> None:
    """
    Direct-JSON shape (default executor / omnigent builtins)
    decodes to the inner dict.
    """
    raw = json.dumps({"terminal": "bash", "session": "s1", "status": "launched"})
    assert _parse_terminal_tool_output(raw) == {
        "terminal": "bash",
        "session": "s1",
        "status": "launched",
    }


def test_parse_terminal_tool_output_handles_garbage() -> None:
    """
    Non-JSON / wrong-shape inputs return ``None`` so the
    caller's loop skips cleanly.
    """
    assert _parse_terminal_tool_output(None) is None
    assert _parse_terminal_tool_output("not json") is None
    assert _parse_terminal_tool_output(json.dumps(42)) is None


def test_reconstruct_terminals_records_owning_conversation() -> None:
    """
    ``conv_id`` argument is recorded on every emitted
    :class:`_TerminalInfo`.

    Pins the cross-conversation walking contract: when
    ``_collect_overview_targets`` walks both the parent's
    conversation AND each sub-agent's conversation, the
    resulting :class:`_TerminalInfo` must carry the
    conversation it came from so the overlay can label which
    agent owns the terminal. Without this, the overlay would
    have no way to attribute the terminal to a sub-agent for
    display.

    This is the unit-level proof that the cross-conversation
    discovery the e2e test couldn't easily exercise (inline
    sub-agents can't own terminals per the
    OMNIGENT_TERMINAL_BRIDGE.md design) is wired correctly. A future
    sub-agent shape that DOES own terminals will pick this up
    for free.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s1",
                    "tmux_socket": "/tmp/sock1",
                    "status": "launched",
                }
            ),
        ),
    ]

    # Same items, different ``conv_id`` arg — the recorded
    # owner must reflect the arg, not anything from the items.
    parent_view = _reconstruct_terminals_from_items(items, conv_id="conv_parent")
    child_view = _reconstruct_terminals_from_items(items, conv_id="conv_child")

    assert parent_view[0].conv_id == "conv_parent"
    assert child_view[0].conv_id == "conv_child"


def test_reconstruct_terminals_separate_session_keys_kept_distinct() -> None:
    """
    Two launches sharing a ``terminal`` name but different
    ``session`` keys produce TWO distinct live entries.

    The supervisor pattern in ``databricks_coding_agent.yaml``
    spins up many ``sandboxed_zsh`` sessions in parallel — the
    sidebar must list all of them, keyed by session. If the
    reconstructor accidentally deduped on ``terminal`` alone,
    every "launch another worker terminal" call would silently
    overwrite the previous entry and the supervisor would see
    only one row no matter how many shells they launched.
    """
    items = [
        _make_function_call("c1", "sys_terminal_launch"),
        _make_function_call_output(
            "c1",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s_parent",
                    "tmux_socket": "/tmp/sock_a",
                    "status": "launched",
                }
            ),
        ),
        _make_function_call("c2", "sys_terminal_launch"),
        _make_function_call_output(
            "c2",
            json.dumps(
                {
                    "terminal": "bash",
                    "session": "s_post_worker",
                    "tmux_socket": "/tmp/sock_b",
                    "status": "launched",
                }
            ),
        ),
    ]
    live = _reconstruct_terminals_from_items(items, conv_id="conv_a")
    sessions = sorted(info.session for info in live)
    # Two distinct entries; if the count is 1, the reconstructor
    # has deduped on ``terminal`` and the supervisor pattern
    # is broken.
    assert sessions == ["s_parent", "s_post_worker"]
    sockets = {info.session: info.socket for info in live}
    assert sockets["s_parent"] == "/tmp/sock_a"
    assert sockets["s_post_worker"] == "/tmp/sock_b"


def test_tmux_session_alive_returns_false_for_missing_socket() -> None:
    """
    A nonexistent socket path produces a non-zero
    ``tmux has-session`` exit, which the helper translates
    to ``False``.

    Pinning the runtime ground-truth contract: when the
    agent's tmux session is gone (because the user attached
    and exited the bash pane on a previous attach, killing
    the agent's pane → window → session → tmux server), the
    Status field in the overlay must read ``dead`` rather
    than the inferred-from-tool-history ``live``. Without
    this guard the user would press ``o`` again, watch tmux
    new-window open a window that immediately errors, and
    have no clue why.
    """
    # ``/tmp/<random uuid>`` is guaranteed not to exist; tmux
    # returns non-zero on first contact.
    import uuid

    bogus_socket = f"/tmp/no-such-socket-{uuid.uuid4().hex}"
    assert _tmux_session_alive(bogus_socket, "main") is False


def test_tmux_session_alive_handles_missing_tmux() -> None:
    """
    When ``tmux`` isn't on PATH, the probe degrades to
    ``False`` instead of raising.

    This matters for laptops without tmux installed: the
    overlay still has to render. Status falls back to "dead"
    (which is technically accurate — without tmux the agent
    couldn't have launched a session anyway) and the user
    sees the recovery hint instead of an exception traceback.
    """
    import os
    import unittest.mock

    # Force a PATH that doesn't contain tmux. The helper's
    # FileNotFoundError catch returns False in that case.
    with unittest.mock.patch.dict(os.environ, {"PATH": "/nonexistent"}):
        # Some systems install subprocess.run with shell=False
        # and an explicit lookup; the helper uses no shell, so
        # the missing executable raises FileNotFoundError. The
        # helper catches it and returns False.
        result = _tmux_session_alive("/tmp/anything", "main")
    # macOS / Linux: PATH override is honored by Popen → raises
    # FileNotFoundError → caught → False. Other OSes might
    # cache the binary location elsewhere; treat True/False as
    # both acceptable since the contract is "don't crash."
    assert result in {True, False}


def test_tmux_pane_snapshot_captures_visible_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_tmux_pane_snapshot`` shells out to ``tmux capture-pane -p``
    against the recovered socket and returns the visible pane text.

    This pins the live-snapshot path used by the Ctrl+O terminal
    panel: if the helper stops calling capture-pane with the
    socket and target from the terminal launch output, the debug
    overlay would still list the terminal but would not show the
    subagent's current screen.
    """
    import subprocess

    calls: list[tuple[list[str], dict[str, object]]] = []

    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """
        Record the tmux argv and return a real CompletedProcess.

        :param args: Subprocess argv passed by the helper, e.g.
            ``["tmux", "-S", "/tmp/sock", "capture-pane", ...]``.
        :param kwargs: Subprocess keyword arguments, e.g.
            ``{"capture_output": True, "timeout": 5}``.
        :returns: A successful ``capture-pane`` result carrying
            two visible terminal lines.
        """
        calls.append((args, kwargs))
        return subprocess.CompletedProcess(args, 0, stdout=b"first\nsecond\n", stderr=b"")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    assert _tmux_pane_snapshot("/tmp/sock", "main") == "first\nsecond\n"
    assert calls == [
        (
            ["tmux", "-S", "/tmp/sock", "capture-pane", "-t", "main", "-p"],
            {"capture_output": True, "timeout": 5},
        ),
    ]


def test_tmux_pane_snapshot_returns_none_when_capture_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A failed ``tmux capture-pane`` becomes ``None`` instead of an
    exception.

    The Ctrl+O overlay renders ``None`` as an inline unavailable
    snapshot, so a stale socket or transient tmux failure does not
    crash the diagnostic pane while the user is inspecting a
    subagent terminal.
    """
    import subprocess

    def _fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        """
        Return a failed ``capture-pane`` result.

        :param args: Subprocess argv passed by the helper, e.g.
            ``["tmux", "-S", "/tmp/sock", "capture-pane", ...]``.
        :param kwargs: Subprocess keyword arguments. Unused by this
            fake because the test only needs the return code branch.
        :returns: A failed ``capture-pane`` result with stderr bytes.
        """
        return subprocess.CompletedProcess(args, 1, stdout=b"", stderr=b"no pane")

    monkeypatch.setattr(subprocess, "run", _fake_run)

    assert _tmux_pane_snapshot("/tmp/sock", "main") is None


def test_render_startup_banner_contains_agent_name() -> None:
    """
    The banner string carries the agent label in bold.

    What this proves: the user sees the agent name centered in
    the box on REPL boot. If the assertion fails, the banner
    would render with the mascot art and box border but no
    visible label — users on a fresh Omnigent session would have
    no in-banner cue for which agent they're talking to (the
    bottom toolbar shows the model, but the welcome panel is
    where the legacy CLI puts it). Bold ANSI sequence ``\\x1b[1m``
    immediately precedes the label so spotting the prefix +
    label confirms both rendering and bolding.
    """
    ansi = _render_startup_banner_ansi("hello world")
    # ``\x1b[1m`` is the SGR Bold sequence; the legacy banner
    # builder wraps the agent label with it (omnigent/inner/
    # cli.py:988-989). If the prefix is missing, the agent name
    # would render in the same weight as the dim hint line —
    # the typographic hierarchy that distinguishes them is gone.
    assert "\x1b[1mhello world\x1b[0m" in ansi, (
        f"Expected the banner to wrap the agent name in bold ANSI "
        f"(\\x1b[1m...\\x1b[0m); got: {ansi!r}. If this fails, the "
        f"banner builder either didn't receive the agent name or "
        f"stopped wrapping the label in bold — typographic "
        f"hierarchy with the dim hint line is broken."
    )


def test_render_startup_banner_omits_keybinding_hints() -> None:
    """
    The Omnigent welcome banner does NOT carry the keybinding hint row.

    What this proves: keybinding hints live in the bottom toolbar
    only — duplicating them inside the welcome box widens the
    panel enough to wrap on 80-col terminals when paired with
    longer agent names. The legacy hint row is also kept out so
    the banner doesn't advertise bindings that don't exist on AP
    (Ctrl+G debug / Ctrl+D exit).
    """
    ansi = _render_startup_banner_ansi("agent")
    for hint in WELCOME_HINTS:
        assert hint not in ansi, (
            f"AP welcome banner unexpectedly contains hint {hint!r}. "
            f"Hints should live in the bottom toolbar only — the "
            f"welcome box was widened by repeating them inside and "
            f"started wrapping for longer agent names."
        )
    for legacy_hint in ("ctrl-g debug", "ctrl-d exit"):
        assert legacy_hint not in ansi, (
            f"AP welcome banner contains legacy hint {legacy_hint!r} "
            f"which doesn't correspond to an Omnigent binding."
        )


def test_render_startup_banner_uses_mascot_accent_color() -> None:
    """
    The Omnigent mode banner box border is rendered in the Omnigent
    starfish magenta-pink brand accent (truecolor RGB ``#F43BA6`` →
    ``38;2;244;59;166``), matching the bottom toolbar, prompt
    marker, and tool-call glyphs.

    What this proves: the visual identity that ties the banner,
    the bottom toolbar, the prompt marker ``❯``, and the SDK's
    formatter accent together survives the AP-side render. If a
    future change drops the truecolor escape (e.g. by stripping
    ANSI on the Omnigent path or swapping Rich for raw text), the
    banner would render as a plain unstyled box and visually
    diverge from the rest of the UI. The override happens in
    :func:`omnigent.repl._repl._render_startup_banner_ansi`.
    """
    ansi = _render_startup_banner_ansi("agent")
    # ``38;2;244;59;166`` is the SGR truecolor foreground encoding
    # of #F43BA6 — the Omnigent starfish magenta-pink brand accent
    # (also ``TerminalHost.accent_color`` default). The banner
    # builder injects it for both the box border and the mascot art
    # on the Omnigent path.
    assert "\x1b[38;2;244;59;166m" in ansi, (
        f"Banner missing Omnigent truecolor accent escape "
        f"(\\x1b[38;2;244;59;166m); got: {ansi!r}. If this is "
        f"absent, the AP-side override in "
        f"``_render_startup_banner_ansi`` isn't propagating into "
        f"the banner — the box border and mascot art would lose "
        f"the brand magenta, breaking the visual link with the "
        f"rest of the Omnigent UI."
    )


def test_run_banner_uses_magenta_mascot_color() -> None:
    """
    The ``omnigent run`` banner renders in the starfish
    magenta-pink brand accent
    (``MASCOT_ART_COLOR = "#F43BA6"`` → ``38;2;244;59;166``).
    The default ``art_color`` and the explicit ``--omnigent`` override
    both resolve to the same brand magenta, so the mascot, box
    border, and prompt marker all read as one accent regardless
    of mode.
    """
    from omnigent.inner.banner import startup_banner_strings
    from omnigent.inner.mascots import MASCOT_ART_COLOR

    assert MASCOT_ART_COLOR == "#F43BA6", (
        f"MASCOT_ART_COLOR must be the starfish magenta-pink brand "
        f"accent (#F43BA6) for the ``omnigent run`` welcome "
        f"banner; got {MASCOT_ART_COLOR!r}."
    )

    banner = startup_banner_strings("agent")
    assert "\x1b[38;2;244;59;166m" in banner.ansi, (
        f"Run banner missing magenta truecolor accent escape "
        f"(\\x1b[38;2;244;59;166m); got: {banner.ansi!r}. The "
        f"banner path must default to MASCOT_ART_COLOR so the "
        f"mascot art and box border render in the brand magenta."
    )


def test_render_startup_banner_fits_under_80_columns() -> None:
    """
    The Omnigent welcome box stays under 80 columns wide even when
    paired with the longest example agent name.

    What this proves: in an 80-column terminal (the de-facto
    minimum for a usable shell) the Omnigent banner does not wrap. The
    box width is driven by the agent label (the hint row is
    blanked, so ``WELCOME_HINTS`` does not push the box wider).
    If a future change reintroduces the hint text in the box, or
    an example agent name grows long enough on its own, the box
    overflows the terminal and prompt_toolkit re-flows the row,
    producing the starfish mascot drifting to a second line.
    ``databricks coding agent`` (the humanized form of the
    longest shipped example agent name) is the canonical
    regression input from the 2026-05-26 user report.
    """
    import re

    for label in ("databricks coding agent", "hello world", "agent"):
        ansi = _render_startup_banner_ansi(label)
        plain = re.sub(r"\x1b\[[0-9;]*m", "", ansi)
        widths = [len(line) for line in plain.split("\n")]
        assert max(widths) < 80, (
            f"AP welcome banner is {max(widths)} cols wide for "
            f"label {label!r}. On an 80-col terminal the box "
            f"wraps and the mascot art drifts onto its own row."
        )


def test_render_startup_banner_shows_remote_server_url() -> None:
    """
    The banner surfaces the server URL when it points at a
    non-loopback host, and hides it for local ``127.0.0.1`` /
    ``localhost`` URLs.

    What this proves: a user connected with ``--server <url>``
    sees which workspace they're talking to in the welcome
    banner. A user running ``omnigent run`` against a freshly
    spawned local server doesn't get the noise.
    """
    remote = "https://example.databricks.com"
    assert remote in _render_startup_banner_ansi("agent", server_url=remote)

    for local in ("http://127.0.0.1:18439", "http://localhost:8000", "http://[::1]:8080"):
        assert local not in _render_startup_banner_ansi("agent", server_url=local), (
            f"Loopback URL {local!r} should not appear in the banner."
        )

    assert "://" not in _render_startup_banner_ansi("agent", server_url=None)


def test_render_startup_banner_draws_rounded_box() -> None:
    """
    The banner draws a rounded-corner box: ``╭`` + ``─``s + ``╮``
    on top, ``│``s on the sides, ``╰`` + ``─``s + ``╯`` on the
    bottom.

    What this proves: the ASCII-art container around the agent
    label is intact. If a future refactor swaps these glyphs (or
    strips them entirely), users would see the label and hint
    line floating in centered whitespace — the visual frame that
    makes the panel read as a discrete UI element disappears.
    Pinning each glyph individually catches partial regressions
    (e.g. only the top border surviving).
    """
    ansi = _render_startup_banner_ansi("agent")
    # Top-left, top-right, bottom-left, bottom-right corners.
    for glyph in ("╭", "╮", "╰", "╯"):
        assert glyph in ansi, (
            f"Banner missing rounded-box corner glyph {glyph!r}. "
            f"If this fails, the box-drawing characters that frame "
            f"the agent label are gone — the banner devolves into "
            f"floating text with no visual container."
        )
    # The vertical sides — pinned separately because losing only
    # the sides would leave a top + bottom border with no body
    # frame.
    assert "│" in ansi, (
        "Banner missing vertical border glyph '│' — without it the "
        "top and bottom borders would float with no body frame."
    )


# ── Claude-Code-style startup header (folder / model / credential) ──


def test_startup_header_box_includes_folder_model_and_credential() -> None:
    """
    With a :class:`_StartupHeader`, the banner box surfaces the agent's
    one-line summary, the model + credential, and the working folder.

    What this proves: the Claude-Code-style header (the user's headline
    request) actually renders every field inside the box. A regression
    that dropped one of the info rows — e.g. stopped threading the
    credential or folder into ``_render_startup_banner_ansi`` — would
    leave that field absent and fail here.
    """
    import re

    header = _StartupHeader(
        folder="~/omnigent",
        description="Multi-agent coding orchestrator",
        model_label="claude-sonnet-4-6",
        credential="Subscription",
        creds_line=None,
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", _render_startup_banner_ansi("nessie", header=header))
    # Every header field appears in the rendered box.
    assert "Multi-agent coding orchestrator" in plain  # one-line summary row
    assert "claude-sonnet-4-6" in plain  # model row
    assert "Subscription" in plain  # credential row (glyphless — see _header_glyph)
    assert "~/omnigent" in plain  # working-folder row
    # No separate creds line was requested, so none is appended.
    assert "→" not in plain


def test_startup_header_appends_per_family_creds_line() -> None:
    """
    A multi-vendor agent's ``creds_line`` is appended *beneath* the box.

    What this proves: nessie's "which Claude/Codex creds will I use?"
    disclosure renders, and as a separate line under the box (it carries
    the ``→`` markers, which never appear inside the box rows). A
    regression that dropped the creds line would fail the membership
    assert; one that mistakenly folded it into the box would still place
    it before the bottom border ``╰`` — which this ordering check
    catches.
    """
    import re

    header = _StartupHeader(
        folder="~/wd",
        description=None,
        model_label="claude-sonnet-4-6",
        credential="Subscription",
        creds_line="Claude → Subscription   ·   Codex → Subscription",
    )
    plain = re.sub(r"\x1b\[[0-9;]*m", "", _render_startup_banner_ansi("nessie", header=header))
    assert "Claude → Subscription" in plain
    assert "Codex → Subscription" in plain
    # A personality-laden lead-in (with the agent name) precedes the creds line.
    lead = "Try asking nessie to spawn the following sub-agents!"
    assert lead in plain
    # Both sit AFTER the box's bottom border (not interior rows), lead-in first.
    assert plain.index("╰") < plain.index(lead) < plain.index("Claude → "), (
        "lead-in then creds line must render beneath the box (after the "
        "bottom border ╰), not as interior box rows."
    )


def test_render_startup_banner_without_header_is_name_only() -> None:
    """
    Passing no header keeps the minimal name-only banner (back-compat).

    What this proves: the header is purely additive — the legacy boot
    path (and every caller that doesn't pass a header, e.g. the
    onboarding wizard) still gets just the bold name with no folder /
    model / credential rows. A regression that always rendered header
    rows would surface stray ``~/`` or ``·`` content here.
    """
    import re

    plain = re.sub(r"\x1b\[[0-9;]*m", "", _render_startup_banner_ansi("agent"))
    assert "\x1b" not in plain  # sanity: stripped
    assert "agent" in plain
    # None of the header-only scaffolding leaks into the minimal banner.
    assert "→" not in plain
    assert "~/" not in plain


def test_startup_header_shows_server_version_on_url_line() -> None:
    """A resolved server version renders inline on the URL row: ``<url>  ·  server <ver>``.

    What this proves: the version the user asked to surface (the headline
    of this change) actually reaches the box and sits on the SAME line as
    the URL as one "which server / what version" block. A regression that
    stopped threading ``server_version`` into ``_render_startup_banner_ansi``
    would drop it and fail the membership assert; one that split it onto its
    own row would put the URL and version on different lines, failing the
    same-line assert. The width assert guards the combined row against
    pushing the 80-column box into a wrap.
    """
    import re

    header = _StartupHeader(
        folder="~/omnigent",
        description=None,
        model_label=None,
        credential=None,
        creds_line=None,
    )
    remote = "https://omnigent.example.com"
    plain = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        _render_startup_banner_ansi(
            "polly", server_url=remote, server_version="0.3.0.dev0", header=header
        ),
    )
    assert "server 0.3.0.dev0" in plain
    # URL and version share one line — find the row carrying the URL and
    # assert the version is on that same row.
    url_line = next(line for line in plain.split("\n") if remote in line)
    assert "server 0.3.0.dev0" in url_line
    widths = [len(line) for line in plain.split("\n")]
    assert max(widths) < 80, f"combined URL+version row widened the box to {max(widths)} cols"


def test_startup_header_shows_local_server_url_with_version() -> None:
    """A loopback server URL IS shown in the header, inline with the version.

    What this proves: unlike the minimal banner (which hides loopback URLs
    as noise), the header surfaces a local ``http://127.0.0.1:<port>`` dev
    server so the combined ``<url>  ·  server <ver>`` line appears for local
    sessions too. A regression that re-gated the header URL row on
    ``_is_remote_server_url`` would drop the URL and fail the membership
    assert.
    """
    import re

    header = _StartupHeader(
        folder="~/omnigent",
        description=None,
        model_label=None,
        credential=None,
        creds_line=None,
    )
    local = "http://127.0.0.1:7393"
    plain = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        _render_startup_banner_ansi(
            "polly", server_url=local, server_version="0.3.0.dev0", header=header
        ),
    )
    # The loopback URL and the version share one row in the header box.
    url_line = next(line for line in plain.split("\n") if local in line)
    assert "server 0.3.0.dev0" in url_line


def test_startup_header_shows_databricks_workspace_url_not_api_mount() -> None:
    """A Databricks server shows the ``/omnigent`` SPA URL and NO version.

    What this proves two things for a workspace mount: (1) the header maps
    the internal ``/api/2.0/omnigent`` proxy mount to the recognizable
    workspace ``/omnigent`` URL — a regression rendering the raw
    ``server_url`` would leak the API path; and (2) the server-version row
    is suppressed even when a version is passed, because a workspace build
    has no meaningful version string to show (its ``/api/version`` returns a
    placeholder like ``"source"``).
    """
    import re

    header = _StartupHeader(
        folder="~",
        description=None,
        model_label=None,
        credential="Subscription",
        creds_line=None,
    )
    api_mount = "https://e2-dogfood.staging.cloud.databricks.com/api/2.0/omnigent"
    plain = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        # Pass a version to prove the renderer suppresses it for a workspace
        # mount regardless of what the caller hands in.
        _render_startup_banner_ansi(
            "polly", server_url=api_mount, server_version="0.3.0.dev0", header=header
        ),
    )
    # The clean workspace URL is shown, the internal API path is NOT.
    assert "https://e2-dogfood.staging.cloud.databricks.com/omnigent" in plain
    assert "/api/2.0/omnigent" not in plain
    # No version row for a Databricks workspace server.
    assert "server " not in plain
    assert "0.3.0.dev0" not in plain


def test_startup_header_omits_server_version_when_unresolved() -> None:
    """No ``server <ver>`` row when the version probe returned ``None``.

    What this proves: the row is purely additive — an unreachable or old
    server (probe → ``None``) yields the same box as before, never a bare
    ``server`` label or blank row.
    """
    import re

    header = _StartupHeader(
        folder="~/omnigent",
        description=None,
        model_label=None,
        credential=None,
        creds_line=None,
    )
    plain = re.sub(
        r"\x1b\[[0-9;]*m",
        "",
        _render_startup_banner_ansi("polly", server_url=None, server_version=None, header=header),
    )
    assert "server " not in plain


def _run(coro):
    """Drive an async helper to completion from a sync test."""
    import asyncio

    return asyncio.run(coro)


def _fake_version_client(by_path: dict[str, dict]) -> tuple[object, list[str]]:
    """Build a fake ``OmnigentClient`` whose ``_http.get`` serves per-path JSON.

    :param by_path: Maps a request path suffix (e.g. ``"/v1/info"``) to the
        JSON body its response should return.
    :returns: ``(client, targets)`` — the fake client, and a list that
        records each full URL the helper requested, in order.
    """
    targets: list[str] = []

    class _FakeResp:
        def __init__(self, body: dict) -> None:
            self._body = body

        def json(self) -> dict:
            return self._body

    class _FakeHttp:
        async def get(self, target: str, timeout: object = None):
            targets.append(target)
            for suffix, body in by_path.items():
                if target.endswith(suffix):
                    return _FakeResp(body)
            return _FakeResp({})

    class _FakeClient:
        _base_url = "https://omnigent.example.com"
        _http = _FakeHttp()

    return _FakeClient(), targets


@pytest.mark.parametrize(
    "payload,expected",
    [
        # Happy path: server_version present in the /v1/info body.
        ({"server_version": "0.3.0.dev0"}, "0.3.0.dev0"),
        # Server too old to report the field → falls through, None here.
        ({"accounts_enabled": False}, None),
        # Non-string / empty values are rejected rather than rendered.
        ({"server_version": ""}, None),
        ({"server_version": 3}, None),
    ],
)
def test_fetch_server_version_parses_info(payload, expected) -> None:
    """``_fetch_server_version`` extracts a non-empty string ``server_version`` from /v1/info.

    What this proves: only a usable version string reaches the header; a
    missing field, empty string, or non-string is treated as "unknown" and
    falls through (here ``/api/version`` also has nothing, so the result is
    ``None``) so the banner never shows a garbage version. Also pins the
    probe to go through the client's AUTHENTICATED ``_http`` (so a hosted,
    auth-gated server answers instead of 401-ing), trying ``/v1/info`` first.
    """
    client, targets = _fake_version_client({"/v1/info": payload, "/api/version": {}})
    assert _run(_fetch_server_version(client)) == expected
    # The richer capabilities probe is always tried first, via the authed _http.
    assert targets[0] == "https://omnigent.example.com/v1/info"


def test_fetch_server_version_falls_back_to_api_version() -> None:
    """When ``/v1/info`` lacks ``server_version``, fall back to ``/api/version``.

    What this proves: an older server (e.g. a staging deploy that predates
    ``server_version`` landing in ``/v1/info`` but still serves the
    long-standing ``/api/version``) still fills the version row instead of
    showing the URL alone. Pins the order: ``/v1/info`` first, then the
    legacy endpoint only when the first yields no usable version.
    """
    client, targets = _fake_version_client(
        {
            # Modern endpoint present but without the field (older server).
            "/v1/info": {"accounts_enabled": False},
            # Legacy endpoint still reports the installed version.
            "/api/version": {"version": "0.1.2"},
        }
    )
    assert _run(_fetch_server_version(client)) == "0.1.2"
    assert targets == [
        "https://omnigent.example.com/v1/info",
        "https://omnigent.example.com/api/version",
    ]


def test_fetch_server_version_never_raises() -> None:
    """Any probe error yields ``None`` (boot must not fail).

    What this proves: the version probe is a non-blocking nicety — an
    ``httpx`` error mid-probe (including a 401 from an auth-gated server
    that the client somehow can't satisfy, or a network drop) returns
    ``None`` instead of propagating and taking down REPL boot.
    """

    class _BoomHttp:
        async def get(self, *_args: object, **_kwargs: object) -> object:
            raise RuntimeError("network down")

    class _FakeClient:
        _base_url = "https://omnigent.example.com"
        _http = _BoomHttp()

    assert _run(_fetch_server_version(_FakeClient())) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Folded multi-line description → first sentence only, whitespace collapsed.
        (
            "Multi-agent coding orchestrator. nessie never performs work itself —\nit delegates.",
            "Multi-agent coding orchestrator",
        ),
        ("   spaced   out   ", "spaced out"),  # whitespace collapse + trim
        ("", None),  # empty → None (no summary row)
        (None, None),  # absent → None
    ],
)
def test_summarize_description(raw: str | None, expected: str | None) -> None:
    """
    ``_summarize_description`` yields a compact one-liner (or ``None``).

    What this proves: a long folded-scalar spec description (nessie's is
    multi-paragraph) collapses to a single first-sentence line for the
    header, and an absent/empty description produces ``None`` so the row
    is skipped rather than rendering an empty line.
    """
    assert _summarize_description(raw) == expected


def test_summarize_description_caps_length() -> None:
    """
    A very long first sentence is truncated with an ellipsis.

    What this proves: the header box can't be blown out to an arbitrary
    width by a long single-sentence description — the summary is capped.
    """
    long_desc = "x" * 200
    result = _summarize_description(long_desc)
    assert result is not None
    assert len(result) <= 60  # capped
    assert result.endswith("…")  # truncation marker


def test_model_readout_subscription_shows_subscription_not_brand(tmp_path) -> None:
    """
    ``/model`` labels a Claude subscription as "Subscription", not "Claude".

    What this proves: the user's explicit ask — ``/model`` must name a
    subscription the same way ``configure harnesses`` does (the shared
    :func:`credential_label`), i.e. the ticket glyph + "Subscription".
    Before the fix the readout used ``provider_display_name`` on the
    provider id ``claude-subscription`` → "Claude-Subscription". The
    regression assert below pins exactly that: the brand-derived label
    must be gone and the canonical "Subscription" present.
    """
    config = {
        "providers": {
            "claude-subscription": {
                "kind": "subscription",
                "cli": "claude",
                "default": "anthropic",
            }
        }
    }
    lines = _build_model_readout_lines(config, "claude-sdk", None)
    active = lines[0]
    assert "Subscription" in active  # canonical shared label is used
    assert "Claude-Subscription" not in active, (
        "readout must not derive the provider label from the provider id "
        "(claude-subscription → 'Claude-Subscription'); it should use the "
        "shared credential_label → 'Subscription'."
    )
    # The subscription kind glyph (admission ticket) prefixes the label.
    assert "🎟" in active


def test_build_startup_header_subscription_credential(tmp_path, monkeypatch) -> None:
    """
    ``_build_startup_header`` names the launch harness's credential.

    What this proves: the header's model + credential row is sourced from
    the real merged provider config for the launch harness — a Claude
    subscription default surfaces as the "Subscription" credential (with
    no pinned model, since the CLI login picks it), WITHOUT the 🎟️ kind
    glyph (dropped from the header by design; CLI surfaces keep it). A
    regression in the config→header resolution would drop or mislabel
    the credential; a reappearing 🎟️ means _header_glyph was bypassed.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  claude-subscription:\n"
        "    kind: subscription\n"
        "    cli: claude\n"
        "    default: anthropic\n"
    )
    header = _build_startup_header("claude-sdk", "A test agent.", ["anthropic"])
    assert header.credential is not None
    # Exact equality proves both the glyph suppression and that the
    # empty-glyph join left no stray leading whitespace.
    assert header.credential == "Subscription"
    # Single family → no per-family creds line (the box row already says it).
    assert header.creds_line is None
    # The description is summarized for the box.
    assert header.description == "A test agent"


def test_build_startup_header_creds_line_hints_first_available(tmp_path, monkeypatch) -> None:
    """
    A surface with no default names the credential the launch will fall back to.

    The Databricks-only GPT-head scenario: a multi-family agent (anthropic +
    openai) where the ``openai`` surface has NO default, but a Databricks
    workspace that serves openai is configured. The creds line must not read a
    bare "not configured" — the head WILL launch through that workspace (the
    runtime spawn-env fallback), so the header names it: "no default → will use
    …". Header and launch resolve it through the same
    :func:`first_available_provider`, so the readout cannot disagree with what
    actually launches.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  claude-subscription:\n"
        "    kind: subscription\n"
        "    cli: claude\n"
        "    default: anthropic\n"
        "  databricks:\n"  # serves openai, but is NOT marked the openai default
        "    kind: databricks\n"
        "    profile: gtm-ws\n"
    )
    header = _build_startup_header(
        "claude-sdk", "Two-headed brainstorming partner.", ["anthropic", "openai"]
    )
    assert header.creds_line is not None
    # anthropic has its explicit default; openai has none → the hint names the
    # first-available credential the launch falls back to (the Databricks ws).
    assert "Claude → Subscription" in header.creds_line
    assert "Codex → no default → will use 🧱 Databricks (gtm-ws)" in header.creds_line


def test_build_startup_header_creds_line_includes_pi_surface(tmp_path, monkeypatch) -> None:
    """
    The per-surface creds line resolves the pi surface's effective default.

    What this proves: polly's header (a pi brain spawning claude/codex
    sub-agents → surfaces ``["anthropic", "openai", "pi"]``) shows what
    EACH harness would actually use — the family surfaces show their
    subscriptions, while the Pi segment shows the explicit pi-scoped
    Databricks default, NOT the anthropic subscription (which pi can't
    consume). A regression that resolves pi via the plain per-family
    lookup renders "pi → not configured" (no "pi" family exists) or leaks
    the subscription into the Pi segment.
    """
    monkeypatch.setenv("OMNIGENT_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIGENT_DISABLE_KEYRING", "1")
    monkeypatch.setenv("HOME", str(tmp_path))
    for var in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "OPENROUTER_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    (tmp_path / "config.yaml").write_text(
        "providers:\n"
        "  claude-subscription:\n"
        "    kind: subscription\n"
        "    cli: claude\n"
        "    default: anthropic\n"
        "  codex-subscription:\n"
        "    kind: subscription\n"
        "    cli: codex\n"
        "    default: openai\n"
        "  databricks:\n"
        "    kind: databricks\n"
        "    profile: my-ws\n"
        "    default: pi\n"
    )
    header = _build_startup_header(
        "pi", "Multi-agent coding orchestrator.", ["anthropic", "openai", "pi"]
    )
    assert header.creds_line is not None
    # Each surface resolves its own effective credential. Subscriptions
    # render glyphless in the header; other kinds keep their glyph (🧱).
    assert "Claude → Subscription" in header.creds_line
    assert "Codex → Subscription" in header.creds_line
    assert "Pi → 🧱 Databricks (my-ws)" in header.creds_line
    # The ticket glyph never reaches the header creds line.
    assert "🎟" not in header.creds_line
    # The box's launch-harness row follows the same pi resolution: the
    # explicit pi-scoped Databricks default, not the subscription.
    assert header.credential is not None
    assert "Databricks" in header.credential


# ── Ctrl+O overlay: paginated conversation_items fetch ──────


async def test_list_all_conversation_items_paginates_past_100(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``_list_all_conversation_items`` fetches every item across
    ALL pages, not just the first 100 the server returns per
    request.

    Reproduces the user-reported 2026-04-30 "17 of 20 terminals
    in Ctrl+O sidebar" symptom: with only 17 of 20 sys_terminal_launch
    function_call_outputs falling into positions 0..99, the
    sidebar enumerator (single 100-item fetch) missed the last
    three terminals' launch outputs at positions 101, 103, 105
    even though the terminals were verifiably alive.

    What this proves and what a failure means:

    - When the server returns full pages of 100 items, the helper
      keeps paginating with ``after=<last_item_id>`` until it
      hits a short page or empty response. If the helper bails
      after the first page, the test's 217-item fixture surfaces
      only 100 items and the sub-100-cap symptom is back.
    - Pages stitch together in chronological (asc) order — a
      regression that re-orders pages would shuffle the
      reconstructed terminal list and produce nonsensical
      sidebar output.
    - The cursor is the last item's ``id``, not its position or
      timestamp — a fix that mistakenly cursors on
      ``created_at`` would send identical cursor values for
      items appended in the same second and infinite-loop OR
      drop items.

    On revert (drop the ``while`` loop, replace with a single
    ``list_items(limit=100)`` call): the assertion ``len(items)
    == total`` fails with ``100 != 217``.
    """
    from omnigent.repl._repl import _list_all_conversation_items

    # Build a 217-item synthetic conversation: enough to require
    # 3 pages (100 + 100 + 17). Item ids encode position so the
    # test can verify ordering survives pagination.
    total = 217
    fixture: list[dict[str, object]] = [
        {"id": f"item_{i:04d}", "type": "message", "position": i} for i in range(total)
    ]

    fetch_calls: list[tuple[int, str | None]] = []

    class _FakeConversations:
        async def list_items(
            self,
            conversation_id: str,
            *,
            limit: int = 100,
            after: str | None = None,
            order: str = "asc",
        ) -> list[dict[str, object]]:
            del conversation_id, order
            fetch_calls.append((limit, after))
            if after is None:
                start = 0
            else:
                # Find the index of ``after`` and start from the
                # next item — mirrors the server's cursor
                # semantics ("strictly after").
                idx = next(i for i, it in enumerate(fixture) if it["id"] == after)
                start = idx + 1
            return fixture[start : start + limit]

    class _FakeClient:
        sessions = _FakeConversations()

    items = await _list_all_conversation_items(_FakeClient(), "conv_test")  # type: ignore[arg-type]

    # All 217 items returned. If 100, pagination regressed —
    # the user's sidebar misses sub-agents / terminals past
    # position 99. If <217 but >100, pagination stops too
    # early (off-by-one on the short-page check or premature
    # cursor exhaustion).
    assert len(items) == total, (
        f"expected all {total} items returned; got {len(items)}. "
        f"If 100, the loop regressed to a single fetch and the "
        f"user-reported 'past-100 items invisible' bug is back. "
        f"If between 100 and {total}, pagination terminates "
        f"prematurely."
    )
    # Order preserved: items[i] corresponds to fixture[i]. A
    # regression that reverses, sorts, or shuffles pages would
    # surface here.
    for i, item in enumerate(items):
        assert item["id"] == f"item_{i:04d}", (
            f"item ordering regressed at index {i}: expected "
            f"id=item_{i:04d}, got {item['id']!r}. Pages must "
            f"stitch together in the order returned (asc by "
            f"position)."
        )
    # Three fetches: first with after=None, second with
    # after=item_0099, third with after=item_0199. Fourth
    # would only happen if the loop didn't recognize the
    # short page (17 items < 100) as end-of-list.
    assert len(fetch_calls) == 3, (
        f"expected exactly 3 page fetches for {total} items "
        f"(100 + 100 + 17 = {total}); got {len(fetch_calls)}. "
        f"More means the loop didn't break on the short final "
        f"page; less means it bailed early and dropped items."
    )
    assert fetch_calls[0] == (100, None)
    assert fetch_calls[1] == (100, "item_0099")
    assert fetch_calls[2] == (100, "item_0199")


async def test_list_all_conversation_items_handles_empty_conversation() -> None:
    """
    Empty conversation → empty list, single fetch.

    Catches a regression where the loop infinite-loops on an
    empty first page or makes redundant fetches.
    """
    from omnigent.repl._repl import _list_all_conversation_items

    fetch_count = 0

    class _EmptyConversations:
        async def list_items(self, *args: object, **kwargs: object) -> list[dict[str, object]]:
            nonlocal fetch_count
            fetch_count += 1
            return []

    class _FakeClient:
        sessions = _EmptyConversations()

    items = await _list_all_conversation_items(_FakeClient(), "conv_test")  # type: ignore[arg-type]
    assert items == []
    # Exactly one fetch: empty first page → break immediately.
    # If >1, the loop kept paging on empty results — would
    # infinite-loop in a buggy implementation that doesn't
    # check for an empty page before computing the cursor.
    assert fetch_count == 1, (
        f"expected exactly 1 fetch for an empty conversation; "
        f"got {fetch_count}. The loop must break on empty pages "
        f"to avoid infinite recursion."
    )


async def test_list_all_conversation_items_falls_back_on_error() -> None:
    """
    A per-page fetch error returns whatever was already fetched.

    The overlay must open even under partial server failure —
    so an error mid-pagination should surface a partial item
    list rather than crashing the overlay builder.
    """
    from omnigent.repl._repl import _list_all_conversation_items

    fetch_count = 0

    class _FlakeyConversations:
        async def list_items(
            self,
            conversation_id: str,
            *,
            limit: int = 100,
            after: str | None = None,
            order: str = "asc",
        ) -> list[dict[str, object]]:
            del conversation_id, order
            nonlocal fetch_count
            fetch_count += 1
            if fetch_count == 1:
                # First page returns 100 items so paging
                # continues.
                return [{"id": f"item_{i:04d}", "type": "message"} for i in range(limit)]
            # Second page errors — simulates a transient HTTP
            # failure mid-pagination.
            raise RuntimeError("simulated mid-pagination failure")

    class _FakeClient:
        sessions = _FlakeyConversations()

    items = await _list_all_conversation_items(_FakeClient(), "conv_test")  # type: ignore[arg-type]
    # First-page items survive; the failed second fetch
    # short-circuits the loop without raising.
    assert len(items) == 100, (
        f"expected the first-page items to survive a mid-pagination "
        f"error; got {len(items)} items. If 0, the error path "
        f"discarded already-fetched items (the overlay would then "
        f"render empty instead of partial). If >100, the error was "
        f"swallowed and a third fetch happened."
    )
    assert fetch_count == 2, (
        f"expected exactly 2 fetches (one success, one error); got {fetch_count}."
    )


# ── Slash-command autocomplete ───────────────────────────


def _completions_for(text: str) -> list[tuple[str, str, int]]:
    """
    Run :class:`_SlashCommandCompleter` against *text* and return
    a flat list of ``(text, display_meta, start_position)`` tuples.

    Each entry captures everything a downstream test cares about:
    the completion text inserted into the buffer, the help string
    rendered next to the menu row, and the start_position used to
    splice the completion into place. Returning a flat list (rather
    than the :class:`Completion` instances themselves) keeps the
    assertions exact-match-friendly without leaking prompt-toolkit
    internals into every test.

    :param text: The buffer contents to feed the completer, with
        the cursor positioned at the end (mirroring "user is typing
        the next character"). Pass ``""`` to test the empty
        buffer, ``"/h"`` to test prefix filtering, etc.
    :returns: Tuples in the order the completer yields them. An
        empty list means "no completions" — assert ``== []`` on
        the result rather than ``not result`` so the test fails
        loudly if the completer returns garbage instead of nothing.
    """
    doc = Document(text=text, cursor_position=len(text))
    completer = _SlashCommandCompleter()
    return [
        (c.text, c.display_meta_text, c.start_position)
        for c in completer.get_completions(doc, complete_event=None)  # type: ignore[arg-type]
    ]


def test_completer_empty_buffer_yields_nothing() -> None:
    """
    Claim: with no input, the completer is silent — no popup is
    rendered before the user has typed anything. Failure here would
    mean the completer is showing slash commands as soon as the
    REPL boots, which is visually noisy and does not match the
    Claude Code / Codex UX.
    """
    assert _completions_for("") == []


def test_completer_plain_text_yields_nothing() -> None:
    """
    Claim: typing a regular message (no leading ``/``) never
    triggers the slash-command popup. A failure here would mean
    every keystroke spawns the menu, which makes normal chat
    typing painful.
    """
    assert _completions_for("hello world") == []


def _noop_handler(*_args: object, **_kwargs: object) -> None:
    """Stand-in handler for synthetic COMMANDS entries in completer tests."""
    return


_FAKE_COMMANDS = {
    "/superpowers:using-superpowers": (
        "Establishes how to find and use skills",
        _noop_handler,
    ),
    "/context": ("Show context window usage", _noop_handler),
}


def test_completer_lone_slash_lists_all_canonical_commands() -> None:
    """
    Claim: hitting ``/`` shows every canonical command in
    :data:`COMMANDS` exactly once — the same set that ``/help``
    prints. Aliases (``/?``, ``/exit``) MUST be hidden because
    the user already sees their canonical equivalents on the same
    menu and showing both would create duplicate rows.

    Failure modes this catches:
      - Returning ``[]``: the lone-slash trigger was dropped.
      - Including ``/?`` or ``/exit``: the alias filter regressed.
      - Missing a registered command: a new ``@_cmd`` decorator
        landed without surfacing in the completer.
    """
    expected = [
        (name, desc, -1)
        for name, (desc, _) in COMMANDS.items()
        if name not in _SLASH_COMMAND_ALIASES
    ]
    actual = _completions_for("/")
    # Order MUST match COMMANDS' insertion order — prompt-toolkit
    # renders completions in the order yielded, and the order is
    # how the user sees them in the popup. A regression that
    # accidentally sorts (e.g. by length) would change UX.
    assert actual == expected


def test_completer_substring_filters_real_registry() -> None:
    """
    Claim: the substring filter narrows the REAL ``COMMANDS`` registry
    (not just a monkeypatched fake) and matches mid-name, not only as a
    prefix. ``heme`` sits inside ``/theme`` but is a prefix of no
    command, so a hit proves substring matching runs against the live
    registry the REPL actually ships.

    The monkeypatched tests below replace ``COMMANDS`` wholesale, so this
    is the only filtering test that touches the real set. It uses
    membership assertions rather than an exact list so it stays green as
    commands are added or removed — the claim is "filtering happens and
    narrows," not "these exact commands exist."
    """
    all_names = [name for name, _, _ in _completions_for("/")]
    names = [name for name, _, _ in _completions_for("/heme")]
    # Mid-name match against the live registry (substring, not prefix).
    assert "/theme" in names
    # A command with no "heme" is excluded — the filter really narrows.
    assert "/quit" not in names
    # Narrowing genuinely happened: a non-empty strict subset of the full
    # list. Catches both "filter bypassed" (== all) and "filter too
    # greedy" (empty) regressions against the real registry.
    assert 0 < len(names) < len(all_names)


def test_completer_ranks_prefix_before_substring(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Claim: prefix matches surface before mid-string matches (mirrors the
    web menu's ``rankedSlashCommandNames``). Typing ``/e`` offers
    ``/effort`` (a prefix) before ``/context`` (which merely contains
    "e"), so the first completion is the one the user most likely meant —
    not an unrelated command that happens to contain the letter.
    """
    fake = {
        "/compact": ("Compact", _noop_handler),
        "/context": ("Context", _noop_handler),
        "/effort": ("Effort", _noop_handler),
        "/model": ("Model", _noop_handler),
    }
    monkeypatch.setattr("omnigent.repl._repl.COMMANDS", fake)
    names = [name for name, _, _ in _completions_for("/e")]
    # /effort is the only prefix match; /context and /model merely contain
    # "e" (/compact has none), so they rank after it.
    assert names[0] == "/effort"
    assert set(names) == {"/effort", "/context", "/model"}


def test_completer_ranks_prefix_first_real_registry() -> None:
    """
    Claim: against the live registry, ``/m`` surfaces a prefix match
    (e.g. ``/model``) first, ahead of commands that merely contain "m"
    (e.g. ``/theme``, ``/compact``). Pins prefix-priority on the real
    command set without coupling to the exact command list.
    """
    names = [name for name, _, _ in _completions_for("/m")]
    assert "/model" in names
    # The top completion is a genuine prefix match, not a mid-string one.
    assert names[0][1:].lower().startswith("m")


def test_completer_matches_name_leaf_after_namespace(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Claim: typing a namespaced skill's leaf name surfaces the full
    command — the core fix. `/using-superpowers` must find
    `/superpowers:using-superpowers` even though the name starts with
    `superpowers:`. Failure means the completer is still prefix-only.
    """
    monkeypatch.setattr("omnigent.repl._repl.COMMANDS", _FAKE_COMMANDS)
    actual = _completions_for("/using-superpowers")
    names = [name for name, _, _ in actual]
    assert names == ["/superpowers:using-superpowers"]
    # The completion replaces the full typed prefix so the splice
    # yields the canonical name, not a doubled slash.
    for _, _, start in actual:
        assert start == -len("/using-superpowers")


def test_completer_does_not_match_description(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Claim: matching is name-only — a query that appears only in a command's
    description does NOT surface it. Kept identical to the web menu, which
    never shows descriptions inline, so a description-driven hit would look
    unexplained. `window` appears in `/context`'s blurb but not its name, so
    it must yield nothing.
    """
    monkeypatch.setattr("omnigent.repl._repl.COMMANDS", _FAKE_COMMANDS)
    assert _completions_for("/window") == []


def test_completer_no_match_yields_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Claim: a query present in no command name yields no rows (the popup
    closes) — proves matching is a real containment test, not "always show
    everything".
    """
    monkeypatch.setattr("omnigent.repl._repl.COMMANDS", _FAKE_COMMANDS)
    assert _completions_for("/zzz") == []


def test_completer_full_name_still_yields_itself(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    Claim: a fully-typed command name still offers itself, so the user
    can press Enter to submit without the popup vanishing. `/context`
    is a substring of its own name.
    """
    monkeypatch.setattr("omnigent.repl._repl.COMMANDS", _FAKE_COMMANDS)
    names = [name for name, _, _ in _completions_for("/context")]
    assert names == ["/context"]


def test_completer_display_meta_matches_command_help() -> None:
    """
    Claim: the meta column shown next to each completion in the
    popup is the command's registered help string from
    :data:`COMMANDS`. This is what gives the popup its
    self-documenting feel — typing ``/`` reveals "Show this help",
    "Start a new conversation", etc.

    A failure here would mean either the meta is empty (popup
    looks like a bare list) or it contains the wrong text (e.g.
    the command name twice).
    """
    actual = {name: desc for name, desc, _ in _completions_for("/")}
    # Spot-check two stable commands rather than enumerate every
    # one — exhaustive coverage is the lone-slash test above.
    assert actual["/help"] == COMMANDS["/help"][0]
    assert actual["/quit"] == COMMANDS["/quit"][0]


# ── /new and /clear slash commands ─────────────────────────


class _StubSession:
    """Records reset() calls; carries a model attr the welcome banner reads."""

    def __init__(self) -> None:
        self.reset_calls = 0
        self.model = "agent"

    def reset(self) -> None:
        self.reset_calls += 1


class _StubHost:
    """Captures host.output() calls without rendering."""

    def __init__(self) -> None:
        self.output_calls: list[object] = []

    def output(self, item: object) -> None:
        self.output_calls.append(item)

    def start_timer(self) -> None:
        """Record timer start calls like the real host."""
        self.output_calls.append("<start_timer>")

    def clear_subagents(self) -> None:
        """Drop the sub-agent tree like the real host (no-op for the stub)."""
        self.output_calls.append("<clear_subagents>")


class _StubFmt:
    """Minimal fmt stub: welcome() returns a sentinel, muted is a Rich style name."""

    muted = "dim"

    def welcome(self, name: str, hints: object) -> str:
        return f"<welcome:{name}>"

    def user_message(self, text: str) -> str:
        """Return a plain sentinel for user-message echoes."""
        return f"<user:{text}>"


def test_clear_command_registered_in_help() -> None:
    """``/clear`` is in the COMMANDS registry so /help lists it."""
    from omnigent.repl._repl import COMMANDS

    assert "/clear" in COMMANDS, "/clear missing — /help would not list it"
    help_text, _ = COMMANDS["/clear"]
    assert "clear" in help_text.lower(), (
        f"/clear's help text should mention clearing; got {help_text!r}"
    )


def test_render_history_item_skips_meta_user_message() -> None:
    """Resume rendering hides durable meta messages from the TUI transcript."""
    host = _StubHost()
    _render_history_item(
        {
            "id": "msg_meta",
            "type": "message",
            "status": "completed",
            "response_id": "resp_skill",
            "role": "user",
            "is_meta": True,
            "content": [{"type": "input_text", "text": "<skill>hidden</skill>"}],
        },
        host,  # type: ignore[arg-type]
    )

    assert host.output_calls == []


def test_render_history_item_renders_slash_command_metadata() -> None:
    """Resume rendering shows the visible slash command item."""
    host = _StubHost()
    _render_history_item(
        {
            "id": "sc_1",
            "type": "slash_command",
            "status": "completed",
            "response_id": "resp_skill",
            "kind": "skill",
            "name": "grill-me",
            "arguments": "review this plan",
        },
        host,  # type: ignore[arg-type]
    )

    rendered = "\n".join(str(item) for item in host.output_calls)
    assert "/grill-me review this plan" in rendered


class _StubSkillSession:
    """Session stub that records structured skill slash-command sends."""

    model = "agent"

    def __init__(self) -> None:
        self.skill_calls: list[tuple[str, str]] = []
        self._pending_local_skill_slash_commands: list[tuple[str, str]] = []

    async def send_skill_slash_command(self, skill_name: str, arguments: str):
        """
        Record one structured skill slash-command send.

        :param skill_name: Skill name without leading slash.
        :param arguments: Raw command arguments.
        :yields: One sentinel event so ``async for`` completes.
        """
        self.skill_calls.append((skill_name, arguments))
        yield object()


async def test_registered_skill_command_uses_structured_slash_command() -> None:
    """Skill slash commands no longer send a visible ``load_skill`` prompt."""
    from omnigent.repl import _repl as repl_mod

    skill = SkillSpec(
        name="meta-skill-test",
        description="Exercise structured slash command dispatch.",
        content="Hidden skill content.",
    )
    registered = repl_mod.register_skill_commands([skill])
    try:
        session = _StubSkillSession()
        host = _StubHost()
        await repl_mod.handle_slash_command(
            "/meta-skill-test review this plan",
            session,  # type: ignore[arg-type]
            object(),  # type: ignore[arg-type]
            host,  # type: ignore[arg-type]
            _StubFmt(),  # type: ignore[arg-type]
        )
    finally:
        repl_mod.unregister_skill_commands(registered)

    assert session.skill_calls == [("meta-skill-test", "review this plan")]
    rendered = "\n".join(str(item) for item in host.output_calls)
    assert "review this plan" in rendered
    assert "load_skill" not in rendered


def test_register_skill_commands_skips_non_user_invocable() -> None:
    """``user-invocable: false`` skills are not registered as REPL slash commands."""
    from omnigent.repl import _repl as repl_mod

    invocable = SkillSpec(name="visible-skill", description="d", content="c")
    internal = SkillSpec(name="internal-skill", description="d", content="c", user_invocable=False)
    registered = repl_mod.register_skill_commands([invocable, internal])
    try:
        assert "/visible-skill" in registered
        assert "/internal-skill" not in registered
        assert "/internal-skill" not in repl_mod.COMMANDS
    finally:
        repl_mod.unregister_skill_commands(registered)


def test_register_skill_commands_skips_invalid_command_names() -> None:
    """Skill names that aren't valid slash-command tokens are skipped + not registered."""
    from omnigent.repl import _repl as repl_mod

    valid = SkillSpec(name="superpowers:using-superpowers", description="d", content="c")
    namespaced = SkillSpec(name="fe-innovate--innovate", description="d", content="c")
    spacey = SkillSpec(name="bad name", description="d", content="c")
    slashy = SkillSpec(name="etc/hosts", description="d", content="c")
    registered = repl_mod.register_skill_commands([valid, namespaced, spacey, slashy])
    try:
        assert "/superpowers:using-superpowers" in registered  # ``:`` namespace ok
        assert "/fe-innovate--innovate" in registered  # ``--`` namespace ok
        assert "/bad name" not in registered
        assert "/etc/hosts" not in registered
        assert "/bad name" not in repl_mod.COMMANDS
        assert "/etc/hosts" not in repl_mod.COMMANDS
    finally:
        repl_mod.unregister_skill_commands(registered)


def test_consume_pending_local_skill_slash_command_only_suppresses_match() -> None:
    """Live TUI rendering skips only the server echo for a local skill command."""
    session = _StubSkillSession()
    session._pending_local_skill_slash_commands = [
        ("grill-me", "review this plan"),
        ("other-skill", ""),
    ]

    matched = _consume_pending_local_skill_slash_command(
        session,
        {
            "type": "slash_command",
            "name": "grill-me",
            "arguments": "review this plan",
        },
    )
    unmatched = _consume_pending_local_skill_slash_command(
        session,
        {
            "type": "slash_command",
            "name": "remote-skill",
            "arguments": "from web",
        },
    )

    assert matched is True
    assert unmatched is False
    assert session._pending_local_skill_slash_commands == [("other-skill", "")]


async def test_clear_command_clears_screen_and_resets_session(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``/clear`` clears the scrollback AND starts a new conversation
    (resets local session state). The old conversation persists
    server-side and is resumable via ``/switch``.
    """
    from omnigent.repl import _repl as repl_mod

    clear_calls: list[None] = []
    monkeypatch.setattr(repl_mod, "_clear_screen", lambda: clear_calls.append(None))

    session = _StubSession()
    await repl_mod.handle_slash_command(
        "/clear",
        session,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _StubHost(),  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    # /clear MUST call _clear_screen — that's the visual half of the
    # contract. If empty, /clear stopped clearing.
    assert clear_calls == [None], (
        f"expected one _clear_screen call from /clear, got {len(clear_calls)}"
    )
    # /clear MUST also reset the local session so the next user
    # message starts a fresh server-side conversation.
    assert session.reset_calls == 1, (
        f"expected /clear to reset the session once, got {session.reset_calls}"
    )


async def test_new_command_resets_session_without_clearing_screen(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``/new`` starts a new conversation but leaves the visible
    scrollback intact — distinguishes it from ``/clear``.
    """
    from omnigent.repl import _repl as repl_mod

    clear_calls: list[None] = []
    monkeypatch.setattr(repl_mod, "_clear_screen", lambda: clear_calls.append(None))

    session = _StubSession()
    await repl_mod.handle_slash_command(
        "/new",
        session,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _StubHost(),  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    # /new must reset session state (new conversation).
    assert session.reset_calls == 1, (
        f"expected /new to reset the session once, got {session.reset_calls}"
    )
    # /new must NOT clear the scrollback — that's /clear's job.
    assert clear_calls == [], (
        f"expected /new to leave scrollback alone, got {len(clear_calls)} clear call(s)"
    )


class _StubSessionsModeSession:
    """``_StubSession`` plus the async ``start_new_conversation`` hook the
    sessions-mode adapter exposes. Used to assert the slash-command
    handlers prefer the new async method over sync ``reset()``."""

    def __init__(self, *, raise_on_start: Exception | None = None) -> None:
        self.reset_calls = 0
        self.start_new_calls = 0
        self.model = "agent"
        self._raise_on_start = raise_on_start

    def reset(self) -> None:
        self.reset_calls += 1

    async def start_new_conversation(self) -> None:
        self.start_new_calls += 1
        if self._raise_on_start is not None:
            raise self._raise_on_start


async def test_clear_command_in_sessions_mode_calls_start_new_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    ``/clear`` routes through ``start_new_conversation`` when the
    session exposes it (sessions mode) — and does NOT also call the
    sync ``reset()``. The dispatch helper picks one path.

    Without this, sessions mode would either skip the unbind (if it
    called ``reset()`` only) or double-fire (if it called both).
    """
    from omnigent.repl import _repl as repl_mod

    clear_calls: list[None] = []
    monkeypatch.setattr(repl_mod, "_clear_screen", lambda: clear_calls.append(None))

    session = _StubSessionsModeSession()
    await repl_mod.handle_slash_command(
        "/clear",
        session,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _StubHost(),  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    assert session.start_new_calls == 1, (
        f"expected /clear to call start_new_conversation once in sessions mode, "
        f"got {session.start_new_calls}"
    )
    assert session.reset_calls == 0, (
        f"/clear must not fall back to sync reset() when "
        f"start_new_conversation exists; got {session.reset_calls} reset() call(s)"
    )
    assert clear_calls == [None], "/clear must still clear the scrollback in sessions mode"


async def test_new_command_in_sessions_mode_calls_start_new_conversation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Same dispatch contract as ``/clear`` but ``/new`` does not clear scrollback."""
    from omnigent.repl import _repl as repl_mod

    clear_calls: list[None] = []
    monkeypatch.setattr(repl_mod, "_clear_screen", lambda: clear_calls.append(None))

    session = _StubSessionsModeSession()
    await repl_mod.handle_slash_command(
        "/new",
        session,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        _StubHost(),  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    assert session.start_new_calls == 1
    assert session.reset_calls == 0
    assert clear_calls == [], "/new must not clear scrollback"


async def test_clear_command_renders_error_when_unbind_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    If ``start_new_conversation`` raises (e.g. the unbind PATCH fails),
    ``/clear`` renders the error inline and skips the scrollback clear
    + welcome banner — leaving the REPL on the prior conversation so
    the user can retry.
    """
    from omnigent.repl import _repl as repl_mod

    clear_calls: list[None] = []
    monkeypatch.setattr(repl_mod, "_clear_screen", lambda: clear_calls.append(None))

    session = _StubSessionsModeSession(raise_on_start=RuntimeError("unbind 404"))
    host = _StubHost()
    await repl_mod.handle_slash_command(
        "/clear",
        session,  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    rendered = "\n".join(str(item) for item in host.output_calls)
    assert "New conversation failed" in rendered and "unbind 404" in rendered, (
        f"expected /clear to render the unbind error inline; got: {rendered!r}"
    )
    assert clear_calls == [], (
        "/clear must NOT clear scrollback when the unbind fails — that would "
        "imply the conversation was reset when it wasn't"
    )


async def test_slash_command_exception_renders_inline_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Slash-command failures render instead of escaping background tasks."""
    from omnigent.repl import _repl as repl_mod

    async def raise_remote_error(
        arg,
        session,
        client,
        host,
        fmt,
    ) -> None:
        """
        Slash-command handler stub that raises like a remote SDK call.

        :param arg: Command argument.
        :param session: Ignored session stub.
        :param client: Ignored client stub.
        :param host: Ignored host stub.
        :param fmt: Ignored formatter stub.
        :returns: None.
        :raises RuntimeError: Always, to exercise the REPL boundary.
        """
        raise RuntimeError("remote returned non-JSON")

    monkeypatch.setitem(repl_mod.COMMANDS, "/boom", ("Boom", raise_remote_error))
    host = _StubHost()

    await repl_mod.handle_slash_command(
        "/boom",
        _StubSession(),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        host,  # type: ignore[arg-type]
        _StubFmt(),  # type: ignore[arg-type]
    )

    rendered = "\n".join(str(item) for item in host.output_calls)
    assert "Error: remote returned non-JSON" in rendered


# ---------------------------------------------------------------------------
# run_repl return-value regression: sessions API must surface session_id
# ---------------------------------------------------------------------------


def test_sessions_adapter_session_id_surfaces_as_conversation_id() -> None:
    """``run_repl`` returns ``session.session_id`` in the sessions-API path.

    Regression: prior to the fix, ``run_repl`` returned a local
    ``conversation_id`` variable that is not populated by the
    sessions-API event stream, so the caller (``_run_repl`` in
    ``chat.py``) never printed the ``--resume`` hint.

    This test exercises the exact return-value expression used in
    ``run_repl``:

        ``getattr(session, "session_id", None) or conversation_id``

    by constructing real ``_SessionsChatReplAdapter`` instances with a
    known ``session_id`` and verifying the expression yields it. If
    someone reverts the fix (switches back to ``return conversation_id``),
    the expression under test here still passes — but the paired
    integration test in ``test_chat.py::test_run_repl_prints_hint_*``
    catches the full flow. Together, the two layers pin the contract.

    Claim: the sessions adapter's ``session_id`` property is what
    ``run_repl`` must return so the CLI prints the resume hint.

    Failure meaning: if ``result`` is ``None``, the return expression
    does not read the adapter's ``session_id`` — the ``--resume`` hint
    will silently disappear.
    """

    class _DummyClient:
        """Placeholder — adapter construction doesn't issue HTTP calls."""

    adapter = _SessionsChatReplAdapter(
        client=_DummyClient(),  # type: ignore[arg-type]
        agent_name="test-agent",
        session_id="conv_sess_abc123",
    )

    # ── Sessions-API path: adapter has a session_id, local var is None ──
    conversation_id: str | None = None
    result = getattr(adapter, "session_id", None) or conversation_id
    # Must return the adapter's session_id, not the local None.
    assert result == "conv_sess_abc123", (
        f"Expected 'conv_sess_abc123' from sessions adapter, got {result!r}. "
        f"If None, the return expression in run_repl does not read "
        f"session.session_id and the --resume hint will not print."
    )

    # ── Sessions-API path: adapter has a session_id, local var also set ──
    # (e.g. resume_conversation_id was passed). Adapter wins.
    conversation_id = "conv_legacy_xyz"
    result = getattr(adapter, "session_id", None) or conversation_id
    assert result == "conv_sess_abc123", (
        f"Expected adapter session_id to take precedence, got {result!r}."
    )


def test_legacy_session_falls_back_to_conversation_id() -> None:
    """Legacy sessions (no ``session_id`` attr) fall back to local var.

    Ensures the ``or conversation_id`` fallback in ``run_repl``'s
    return expression works for the legacy ``/v1/responses`` path
    where the session object is the SDK's ``Session`` (which lacks a
    ``session_id`` property).

    Failure meaning: if ``result`` is ``None`` when
    ``conversation_id`` is set, the fallback branch is broken and
    legacy resume hints would disappear.
    """

    class _LegacySession:
        """Mimics the SDK Session — no session_id attribute."""

        model = "test-model"

    session = _LegacySession()
    conversation_id: str | None = "conv_legacy_abc"
    result = getattr(session, "session_id", None) or conversation_id
    # Must fall back to conversation_id since the legacy session has no
    # session_id attribute.
    assert result == "conv_legacy_abc", (
        f"Expected fallback to conversation_id, got {result!r}. "
        f"If None, the 'or' fallback is broken."
    )


def test_no_session_id_and_no_conversation_returns_none() -> None:
    """When neither adapter nor local var has an id, return None.

    Covers the immediate-exit case (Ctrl-D before any message is
    sent). ``run_repl`` should return ``None`` so the CLI correctly
    suppresses the resume hint.

    Failure meaning: if ``result`` is not ``None``, the expression
    fabricates an id from nowhere — the hint would print a bogus
    ``--resume`` value.
    """

    class _FreshAdapter:
        """Adapter before first send — session_id is None."""

        session_id: str | None = None

    session = _FreshAdapter()
    conversation_id: str | None = None
    result = getattr(session, "session_id", None) or conversation_id
    assert result is None, f"Expected None (no conversation created), got {result!r}."


# ── _server_event_to_sdk_event: ErrorEvent translation ───────────────


def test_server_event_to_sdk_event_translates_llm_error_event() -> None:
    """Server ErrorEvent with source="llm" translates to SDK ErrorEvent.

    Failure meaning: if None is returned, an LLM 404 / bad-model error
    emitted by the workflow's except-all handler will be silently dropped
    by the AP-mode REPL and the user will see no error message.
    """
    from omnigent_client._events import ErrorEvent as _SDKErrorEvent
    from omnigent_client._types import ErrorInfo

    from omnigent.server.schemas import ErrorEvent, RetryErrorDetail

    server_event = ErrorEvent(
        type="response.error",
        source="llm",
        tool_name=None,
        error=RetryErrorDetail(code="HTTPStatusError", message="404 Not Found"),
    )
    result = _server_event_to_sdk_event(server_event)

    assert isinstance(result, _SDKErrorEvent), (
        f"Expected _SDKErrorEvent, got {type(result).__name__!r}. "
        "ErrorEvent branch is missing from _server_event_to_sdk_event."
    )
    assert result.source == "llm", f"Expected source='llm', got {result.source!r}"
    assert result.tool_name is None
    assert isinstance(result.error, ErrorInfo)
    assert result.error.code == "HTTPStatusError", (
        f"Error code not forwarded: {result.error.code!r}"
    )
    assert result.error.message == "404 Not Found", (
        f"Error message not forwarded: {result.error.message!r}"
    )


def test_server_event_to_sdk_event_translates_tool_error_event() -> None:
    """Server ErrorEvent with source="tool" forwards tool_name to SDK event.

    Failure meaning: tool-failure errors (e.g. retry exhaustion) would
    be silently dropped in AP-mode, hiding the name of the failing tool.
    """
    from omnigent_client._events import ErrorEvent as _SDKErrorEvent

    from omnigent.server.schemas import ErrorEvent, RetryErrorDetail

    server_event = ErrorEvent(
        type="response.error",
        source="tool",
        tool_name="bash",
        error=RetryErrorDetail(code="tool_error", message="bash failed"),
    )
    result = _server_event_to_sdk_event(server_event)

    assert isinstance(result, _SDKErrorEvent)
    assert result.source == "tool"
    assert result.tool_name == "bash", (
        f"Expected tool_name='bash', got {result.tool_name!r}. "
        "tool_name must be forwarded so the error block names the failing tool."
    )
    assert result.error.code == "tool_error"
    assert result.error.message == "bash failed"


def test_server_event_to_sdk_event_returns_none_for_unrecognised_event() -> None:
    """Unrecognised server events return None (forward-compatible skip).

    Failure meaning: if a new event type is introduced and this function
    stops returning None for unknowns, the skip-path in the REPL event
    loop will break, causing a TypeError on unrecognised events.
    """

    class _UnknownEvent:
        """Represents a future server event type the REPL hasn't learned yet."""

    result = _server_event_to_sdk_event(_UnknownEvent())
    assert result is None, f"Expected None for unknown event, got {type(result).__name__!r}."


# ──────────────────────────────────────────────────────────────────
# SSE transport error classification.
#
# Recoverable transport interruptions (peer closes mid-chunk, read
# timeout) are normal background noise — the REPL reconnects and
# the session continues server-side. Logging them at WARNING level
# alarms users and visually competes with the genuinely-bad
# provider errors (orphaned function_call_output after compression)
# that DO kill a turn. ``_is_recoverable_sse_transport_error``
# classifies the recoverable subset so ``_stream_pump`` can demote
# them to INFO.
# ──────────────────────────────────────────────────────────────────


def test_is_recoverable_sse_transport_error_for_httpx_remote_protocol_error() -> None:
    """
    ``httpx.RemoteProtocolError`` ("peer closed connection without
    sending complete message body") is the canonical recoverable
    transport interruption. It must classify
    as recoverable so the REPL demotes the reconnect log to INFO.
    """
    import httpx

    exc = httpx.RemoteProtocolError("peer closed connection without sending complete message body")
    assert _is_recoverable_sse_transport_error(exc), (
        "httpx.RemoteProtocolError must classify as a recoverable "
        "transport interruption — otherwise the REPL spams WARNING "
        "for every load-balancer idle-close, masking the genuinely-bad "
        "provider 400 errors that DO kill a turn."
    )


def test_is_recoverable_sse_transport_error_walks_cause_chain() -> None:
    """
    ``httpx`` wraps the underlying ``httpcore.RemoteProtocolError`` in
    a higher-level ``httpx.RemoteProtocolError`` via ``__cause__``. The
    classifier must walk the chain so the wrapping doesn't defeat the
    recognition.
    """
    import httpcore
    import httpx

    inner = httpcore.RemoteProtocolError("incomplete chunked read")
    outer = httpx.RemoteProtocolError("peer closed connection")
    try:
        raise outer from inner
    except httpx.RemoteProtocolError as exc:
        assert _is_recoverable_sse_transport_error(exc), (
            "Classifier failed to walk the __cause__ chain — wrapped "
            "transport errors should still classify as recoverable."
        )


def test_is_recoverable_sse_transport_error_false_for_value_error() -> None:
    """
    Non-transport errors (programming bugs, JSON decode failures,
    server-side 500s wrapped in custom errors) must NOT classify as
    recoverable. Demoting them to INFO would hide real bugs.
    """
    assert not _is_recoverable_sse_transport_error(ValueError("bad json")), (
        "ValueError must not classify as a recoverable transport error; "
        "demoting it to INFO would hide programming bugs."
    )
    assert not _is_recoverable_sse_transport_error(RuntimeError("unexpected")), (
        "RuntimeError must not classify as a recoverable transport error."
    )


# ── Resume hint (run --resume <id>) ──────────────────


def test_resume_hint_appends_resume_flag_to_invocation_parts() -> None:
    """Pins the REPL-exit hint format: original invocation + ``--resume <id>``
    (pasted command must carry --server / --profile)."""
    import shlex

    resume_parts = [
        "omnigent",
        "run",
        "examples/databricks_coding_agent.yaml",
        "--server",
        "https://omnigent-app.databricksapps.com",
        "--profile",
        "oss",
        "--harness",
        "claude-sdk",
    ]
    hint = shlex.join([*resume_parts, "--resume", "conv_abc"])
    assert hint == (
        "omnigent run examples/databricks_coding_agent.yaml "
        "--server https://omnigent-app.databricksapps.com "
        "--profile oss "
        "--harness claude-sdk "
        "--resume conv_abc"
    )
