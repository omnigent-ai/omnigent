"""Tests for native Claude Code bridge helpers."""

from __future__ import annotations

import asyncio
import json
import os
import select
import shlex
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from typing import Any, TextIO

import pytest

from omnigent import claude_native_bridge, native_cost_popup
from omnigent.claude_native_bridge import (
    _claude_prompt_rendered,
    _hook_record_from_jsonl_record,
    _JsonlRecord,
    augment_claude_args,
    count_hook_events,
    display_cost_approval_popup,
    ensure_claude_workspace_trusted,
    inject_interrupt,
    inject_user_message,
    kill_session,
    post_tools_changed,
    prepare_bridge_dir,
    read_assistant_text_since,
    read_hook_events_from_offset,
    read_launch_model,
    read_message_deltas_from_offset,
    read_permission_hook_config,
    read_transcript_items_from_offset,
    read_transcript_items_since,
    read_transcript_path,
    record_hook_event,
    start_tool_relay,
    stop_hook_seen_since,
    write_tmux_target,
)
from omnigent.reasoning_effort import CLAUDE_EFFORTS


@pytest.fixture(autouse=True)
def _trust_tmp_bridge_parent(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """
    Treat each test's temp dir as the Claude bridge root.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param tmp_path: Per-test temp directory.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path)


@pytest.fixture
def subprocess_bridge_root() -> Iterator[Path]:
    """
    Yield a bridge root accepted by subprocess bridge tests.

    :yields: Temporary directory path under the production trusted
        Claude bridge root, so a child
        ``python -m omnigent.claude_native_bridge`` accepts bridge
        writes without inheriting pytest monkeypatches.
    """
    production_root = Path("/tmp") / f"omnigent-{os.getuid()}" / "claude-native"
    production_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(production_root.parent, 0o700)
    os.chmod(production_root, 0o700)
    with tempfile.TemporaryDirectory(prefix="bridge-test-", dir=production_root) as raw:
        yield Path(raw)


class _NoPrefixReadFile:
    """
    File wrapper that fails if a reader touches bytes before a cursor.

    :param handle: Wrapped file object returned by :class:`Path.open`.
    :param min_read_offset: Smallest byte offset that may be read.
    """

    def __init__(self, handle: Any, min_read_offset: int) -> None:
        """
        Initialize the tracking wrapper.

        :param handle: Wrapped file object returned by :class:`Path.open`.
        :param min_read_offset: Smallest byte offset that may be read.
        :returns: None.
        """
        self._handle = handle
        self._min_read_offset = min_read_offset

    def __enter__(self) -> _NoPrefixReadFile:
        """
        Enter the wrapped file context.

        :returns: This wrapper.
        """
        self._handle.__enter__()
        return self

    def __exit__(self, *args: object) -> object:
        """
        Exit the wrapped file context.

        :param args: Context-manager exception details.
        :returns: Wrapped ``__exit__`` return value.
        """
        return self._handle.__exit__(*args)

    def seek(self, *args: object) -> object:
        """
        Delegate a seek operation to the wrapped file.

        :param args: Seek arguments.
        :returns: Wrapped ``seek`` return value.
        """
        return self._handle.seek(*args)

    def tell(self) -> int:
        """
        Return the wrapped file's current offset.

        :returns: Current byte offset.
        """
        return int(self._handle.tell())

    def read(self, *args: object) -> object:
        """
        Read bytes after asserting the prefix is not touched.

        :param args: Read arguments.
        :returns: Wrapped ``read`` return value.
        """
        self._assert_not_prefix_read()
        return self._handle.read(*args)

    def readline(self, *args: object) -> object:
        """
        Read one line after asserting the prefix is not touched.

        :param args: Readline arguments.
        :returns: Wrapped ``readline`` return value.
        """
        self._assert_not_prefix_read()
        return self._handle.readline(*args)

    def _assert_not_prefix_read(self) -> None:
        """
        Raise if the wrapped file is positioned before the saved cursor.

        :returns: None.
        """
        if self.tell() < self._min_read_offset:
            raise AssertionError("byte-offset reader touched the JSONL prefix")


def _fail_if_path_reads_before_offset(
    monkeypatch: pytest.MonkeyPatch,
    path: Path,
    min_read_offset: int,
) -> None:
    """
    Patch :class:`Path.open` so reads before ``min_read_offset`` fail.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param path: Path whose reads should be tracked.
    :param min_read_offset: Smallest byte offset that may be read.
    :returns: None.
    """
    real_open = Path.open

    def _open_with_tracking(self: Path, *args: Any, **kwargs: Any) -> Any:
        """
        Wrap reads for the tracked path and delegate all other opens.

        :param self: Path being opened.
        :param args: Positional ``Path.open`` arguments.
        :param kwargs: Keyword ``Path.open`` arguments.
        :returns: File object or tracking wrapper.
        """
        handle = real_open(self, *args, **kwargs)
        if self == path:
            return _NoPrefixReadFile(handle, min_read_offset)
        return handle

    monkeypatch.setattr(Path, "open", _open_with_tracking)


def test_prepare_bridge_dir_preserves_token_and_updates_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge setup is stable across wrapper re-runs for one session.

    If this regresses, reattaching ``omnigent claude`` can rotate the
    bearer token while the already-running MCP server still expects the
    old token.
    """
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)

    first = prepare_bridge_dir(
        "conv_abc",
        workspace=tmp_path,
    )
    first_config = json.loads((first / "bridge.json").read_text(encoding="utf-8"))
    second = prepare_bridge_dir(
        "conv_abc",
        workspace=tmp_path / "later",
    )
    second_config = json.loads((second / "bridge.json").read_text(encoding="utf-8"))

    assert first == second
    assert second_config["token"] == first_config["token"]
    assert second_config["workspace"] == str(tmp_path / "later")
    assert "headers" not in second_config


def test_prepare_bridge_dir_preserves_permission_hook_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Re-preparing a bridge keeps the permission command hook's Omnigent URL.

    The claude-native ``PermissionRequest`` command hook reads
    ``permission_hook.json`` at hook time to learn which Omnigent server to
    POST to (the URL is not baked into Claude's launch args). A
    rebind/reattach that re-runs ``prepare_bridge_dir`` must NOT wipe
    that file — if it does, the permission subprocess bails with "AP
    server URL missing" and approval prompts silently stop reaching the
    web UI (a regression we guard against).
    """
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)

    bridge_dir = prepare_bridge_dir("conv_abc", workspace=tmp_path)
    # ``augment_claude_args`` is the production path that writes
    # permission_hook.json (via ``build_hook_settings``) on cold launch.
    augment_claude_args(
        (),
        bridge_dir=bridge_dir,
        ap_server_url="http://127.0.0.1:8787",
        ap_auth_headers={"Authorization": "Bearer xyz"},
    )
    assert (bridge_dir / "permission_hook.json").exists()

    # Simulate a wrapper rebind/reattach that re-prepares the bridge
    # without relaunching the terminal (so build_hook_settings does not
    # run again to recreate the file).
    prepare_bridge_dir("conv_abc", workspace=tmp_path / "later")

    config = read_permission_hook_config(bridge_dir)
    assert config["ap_server_url"] == "http://127.0.0.1:8787"
    assert config["ap_auth_headers"] == {"Authorization": "Bearer xyz"}


def test_prepare_bridge_dir_restricts_filesystem_permissions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Bridge directory and bearer token are not world-readable.

    `/tmp` is shared with other Unix users on the host. The bridge dir
    holds a bearer token the local MCP server uses; if its perms drift
    to a default 0o755 (dir) / 0o644 (file) other users on the box can
    read it and impersonate the runner against the MCP server. Per the
    design doc §12 the dir must be 0o700 and bearer files 0o600. A
    regression here would be invisible without an explicit stat assertion.
    """
    import stat

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "claude-native")

    bridge_dir = prepare_bridge_dir("conv_abc", workspace=tmp_path)
    bridge_json = bridge_dir / "bridge.json"

    dir_mode = stat.S_IMODE(bridge_dir.stat().st_mode)
    bearer_mode = stat.S_IMODE(bridge_json.stat().st_mode)

    assert dir_mode == 0o700, (
        f"bridge dir at {bridge_dir} has mode {oct(dir_mode)}; "
        "expected 0o700 so other host users cannot enter it"
    )
    assert bearer_mode == 0o600, (
        f"bearer file {bridge_json} has mode {oct(bearer_mode)}; "
        "expected 0o600 so other host users cannot read the token"
    )


def test_prepare_bridge_dir_refuses_symlinked_ancestor(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Pre-created symlinked bridge ancestor is refused, not silently followed.

    Without `_ensure_secure_dir`, ``mkdir(parents=True, exist_ok=True)``
    happily walks through a symlinked intermediate dir, redirecting the
    bridge tree (including ``bridge.json``'s bearer token) to a path an
    attacker controls. A regression that removes the validation — or
    swaps it back to plain mkdir — would let this attack succeed.
    """
    # Layout: tmp_path is the trusted parent. Place a "claude-native"
    # symlink that points at a separate attacker-controlled directory
    # before any prepare_bridge_dir() call runs.
    attacker_dir = tmp_path / "attacker-controlled"
    attacker_dir.mkdir()
    symlink = tmp_path / "claude-native"
    symlink.symlink_to(attacker_dir, target_is_directory=True)

    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", symlink)

    with pytest.raises(RuntimeError, match="symlink"):
        prepare_bridge_dir("conv_abc", workspace=tmp_path)

    # Confirm the bearer token did NOT land in the attacker-controlled
    # directory — the refusal happened before any file write.
    assert not (attacker_dir / "bridge.json").exists()


def test_trusted_parent_accepts_qwen_native_bridge_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The relay's bridge-root allowlist accepts qwen-native bridge dirs.

    The comment relay (``start_tool_relay`` → ``_ensure_secure_dir`` →
    ``_trusted_parent_for_bridge_dir``) writes its JSON file under the
    harness's bridge dir, validating it lives below a known bridge root.
    qwen-native reuses this relay but keeps files under its own root
    (``$TMPDIR/omnigent-<uid>/qwen-native``); if that root is missing from the
    allowlist, every qwen-native session raises ``not under an allowed bridge
    root`` and the relay never starts (observed in a live runner log). This
    pins the qwen-native branch so the regression can't return.
    """
    from omnigent import qwen_native_bridge

    # Distinct claude root so the qwen target can't match the claude branch
    # first (the autouse fixture points the claude root at ``tmp_path``).
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "claude-native")
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    # qwen root mirrors production shape: <uid-scoped temp>/qwen-native.
    qwen_root = tmp_path / "omnigent-test" / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", qwen_root)

    target = claude_native_bridge._absolute_syntactic_path(qwen_root / "abc123")
    trusted = claude_native_bridge._trusted_parent_for_bridge_dir(target)

    # Same anchor as cursor-native: the uid-scoped temp dir's parent.
    assert trusted == claude_native_bridge._absolute_syntactic_path(qwen_root.parent.parent)


def test_trusted_parent_rejects_path_outside_all_roots_and_names_qwen(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A path under no known root is refused, and the error names the qwen root."""
    from omnigent import qwen_native_bridge

    # Distinct claude root so ``outside`` below isn't swept under it (the autouse
    # fixture points the claude root at ``tmp_path``).
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", tmp_path / "claude-native")
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    qwen_root = tmp_path / "omnigent-test" / "qwen-native"
    monkeypatch.setattr(qwen_native_bridge, "_BRIDGE_ROOT", qwen_root)

    outside = claude_native_bridge._absolute_syntactic_path(tmp_path / "somewhere-else" / "x")
    with pytest.raises(RuntimeError, match="not under an allowed bridge root") as exc:
        claude_native_bridge._trusted_parent_for_bridge_dir(outside)
    assert "qwen-native" in str(exc.value)


def test_record_hook_event_updates_transcript_state(tmp_path: Path) -> None:
    """
    Hook records expose Claude's JSONL transcript path to the executor.

    This fails if the harness would send channel messages but then have
    no transcript cursor to read Claude's response from.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "claude-session",
            "transcript_path": str(transcript_path),
        },
    )

    assert read_transcript_path(bridge_dir) == transcript_path
    assert count_hook_events(bridge_dir) == 1


def test_read_assistant_text_since_parses_claude_jsonl(tmp_path: Path) -> None:
    """
    Transcript parsing returns only assistant text after the cursor.

    The parser must not echo channel/user records back into the
    Omnigent assistant stream.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps({"message": {"role": "user", "content": "hi"}}),
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "first"}],
                        }
                    }
                ),
                json.dumps(
                    {
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "..."},
                                {"type": "text", "text": "second"},
                            ],
                        }
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cursor, chunks = read_assistant_text_since(transcript_path, 1)

    assert cursor == 3, "cursor should advance past all three complete transcript records"
    assert chunks == ["first", "second"]


def test_read_transcript_items_since_parses_claude_visible_events(tmp_path: Path) -> None:
    """
    Transcript parsing preserves user text, tools, results, and output.

    This covers the real Claude JSONL shapes observed in native CLI
    runs: user message strings, assistant ``tool_use`` blocks, user
    ``tool_result`` blocks, assistant text, and metadata records that
    must not become chat items.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps({"type": "permission-mode", "mode": "default"}),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-1",
                        "message": {"role": "user", "content": "please inspect TODO.md"},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-tool-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {"type": "thinking", "thinking": "redacted"},
                                {
                                    "type": "tool_use",
                                    "id": "toolu_read_1",
                                    "name": "Read",
                                    "input": {"file_path": "TODO.md"},
                                },
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "tool-result-1",
                        "parentUuid": "assistant-tool-1",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_read_1",
                                    "content": "TODO contents",
                                    "is_error": False,
                                }
                            ],
                        },
                        "toolUseResult": {
                            "type": "text",
                            "file": {"filePath": "TODO.md", "content": "TODO contents"},
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "attachment",
                        "uuid": "metadata-attachment",
                        "attachment": {"type": "task_reminder", "content": [], "itemCount": 0},
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-text-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cursor, current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert cursor == 6, "cursor should include metadata records even when they emit no items"
    assert [item.item_type for item in items] == [
        "message",
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[0].data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "please inspect TODO.md"}],
    }
    tool_call = items[1]
    assert tool_call.data["name"] == "Read"
    assert json.loads(tool_call.data["arguments"]) == {"file_path": "TODO.md"}
    assert tool_call.data["call_id"] == "toolu_read_1"
    assert items[2].response_id == tool_call.response_id
    assert items[2].data == {"call_id": "toolu_read_1", "output": "TODO contents"}
    assert items[3].response_id == tool_call.response_id
    assert items[3].data == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "Done."}],
    }
    assert current_response_id == tool_call.response_id


@pytest.mark.parametrize(
    "raw_text",
    [
        "Prompt is too long",
        "prompt is too long: 210000 tokens > 200000 maximum",
        "Prompt is too long\n",
    ],
)
def test_read_transcript_rewrites_prompt_too_long(tmp_path: Path, raw_text: str) -> None:
    """
    When Claude Code writes "Prompt is too long" to the transcript, the
    bridge rewrites it to actionable guidance so the web UI shows
    something useful instead of the raw API error.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "assistant",
                "uuid": "err-1",
                "message": {"role": "assistant", "content": raw_text},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _, _, items = read_transcript_items_since(transcript_path, 0, agent_name="claude-native-ui")

    assert len(items) == 1
    text = items[0].data["content"][0]["text"]
    assert "Context limit reached" in text
    assert "/compact" in text
    assert "/clear" in text


def test_read_transcript_items_from_offset_skips_existing_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Byte-offset transcript reads do not reparse old JSONL prefixes.

    A poll after a large existing transcript should seek directly to
    the saved byte offset. The patched file wrapper raises if reads
    touch the old prefix, so this test fails under the prior full-file
    rescan behavior.
    """
    transcript_path = tmp_path / "session.jsonl"
    prefix = "".join(
        json.dumps(
            {
                "type": "user",
                "uuid": f"prefix-{index}",
                "message": {"role": "user", "content": f"prefix {index}"},
            }
        )
        + "\n"
        for index in range(100)
    )
    transcript_path.write_text(
        prefix
        + json.dumps(
            {
                "type": "assistant",
                "uuid": "assistant-new",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "new output"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    prefix_offset = len(prefix.encode("utf-8"))
    _fail_if_path_reads_before_offset(monkeypatch, transcript_path, prefix_offset)

    result = read_transcript_items_from_offset(
        transcript_path,
        prefix_offset,
        start_line=100,
        agent_name="claude-native-ui",
    )

    assert result.line_cursor == 101
    assert result.byte_offset == transcript_path.stat().st_size
    assert [item.item_type for item in result.items] == ["message"]
    assert result.items[0].data == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "new output"}],
    }


def test_read_transcript_items_from_offset_preserves_partial_trailing_line(
    tmp_path: Path,
) -> None:
    """
    A partial trailing transcript JSON record is retried after completion.

    Claude writes JSONL append-only, and a poll can land after bytes
    are written but before the final newline. The reader must not
    advance the byte offset in that case or the completed record will
    be skipped forever.
    """
    transcript_path = tmp_path / "session.jsonl"
    partial = (
        '{"type":"assistant","uuid":"assistant-partial","message":{"role":"assistant",'
        '"content":[{"type":"text","text":"eventually complete"}]}'
    )
    transcript_path.write_text(partial, encoding="utf-8")

    first = read_transcript_items_from_offset(
        transcript_path,
        0,
        start_line=0,
        agent_name="claude-native-ui",
    )

    assert first.line_cursor == 0
    assert first.byte_offset == 0
    assert first.items == []

    with transcript_path.open("a", encoding="utf-8") as handle:
        handle.write("}\n")
    second = read_transcript_items_from_offset(
        transcript_path,
        first.byte_offset,
        start_line=first.line_cursor,
        agent_name="claude-native-ui",
    )

    assert second.line_cursor == 1
    assert second.byte_offset == transcript_path.stat().st_size
    assert [item.data for item in second.items] == [
        {
            "role": "assistant",
            "agent": "claude-native-ui",
            "content": [{"type": "output_text", "text": "eventually complete"}],
        }
    ]


def test_read_transcript_line_cursor_migration_preserves_legacy_source_ids(
    tmp_path: Path,
) -> None:
    """
    Line-cursor migration keeps UUID-less fallback ids stable.

    Older forwarder state can contain ``line-N`` source ids in
    ``seen_source_ids``. The compatibility reader must keep producing
    that legacy id shape for the migration poll so already-posted
    UUID-less items are not duplicated before future byte-offset polls
    switch to byte-based fallbacks.
    """
    transcript_path = tmp_path / "session.jsonl"
    first_record = (
        json.dumps(
            {
                "type": "user",
                "message": {"role": "user", "content": "already forwarded"},
            }
        )
        + "\n"
    )
    transcript_path.write_text(
        first_record
        + json.dumps(
            {
                "type": "assistant",
                "message": {
                    "role": "assistant",
                    "content": [{"type": "text", "text": "new output"}],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    migrated = claude_native_bridge.read_transcript_items_since_with_position(
        transcript_path,
        1,
        agent_name="claude-native-ui",
    )
    byte_offset = len(first_record.encode("utf-8"))
    byte_reader = read_transcript_items_from_offset(
        transcript_path,
        byte_offset,
        start_line=1,
        agent_name="claude-native-ui",
    )

    assert [item.source_id for item in migrated.items] == ["line-2:0:message"]
    assert [item.source_id for item in byte_reader.items] == [f"byte-{byte_offset}:0:message"]


def test_read_transcript_items_since_ignores_observed_status_records(tmp_path: Path) -> None:
    """
    Non-conversation Claude JSONL records do not become Omnigent items.

    The local transcript audit found status/UI records such as
    ``queue-operation``, ``branch-update``, ``progress``,
    ``pr-link``, and non-prompt attachments alongside normal
    message records. They must not show up as chat items or reset
    the active response id for following assistant output.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-tool-1",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_bash_1",
                                    "name": "Bash",
                                    "input": {"command": "sleep 1"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps({"type": "queue-operation", "operation": "enqueue", "content": "STOP"}),
                json.dumps({"type": "branch-update", "gitBranch": "feature/native-claude"}),
                json.dumps(
                    {
                        "type": "progress",
                        "uuid": "progress-1",
                        "toolUseID": "toolu_bash_1",
                        "data": {"type": "hook_progress", "hookEvent": "PostToolUse"},
                    }
                ),
                json.dumps(
                    {
                        "type": "pr-link",
                        "prNumber": 123,
                        "prRepository": "example/repo",
                        "prUrl": "https://example.invalid/pr/123",
                    }
                ),
                json.dumps(
                    {
                        "type": "attachment",
                        "uuid": "file-attachment",
                        "attachment": {
                            "type": "file",
                            "filename": "TODO.md",
                            "displayPath": "TODO.md",
                            "content": [],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "tool-result-1",
                        "parentUuid": "assistant-tool-1",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_bash_1",
                                    "content": "(No output)",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-text-1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cursor, current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert cursor == 8, "cursor should advance past ignored status/UI records"
    assert [item.item_type for item in items] == [
        "function_call",
        "function_call_output",
        "message",
    ]
    assert items[0].data["call_id"] == "toolu_bash_1"
    assert items[1].data == {"call_id": "toolu_bash_1", "output": "(No output)"}
    assert items[2].data == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "Done."}],
    }
    assert items[0].response_id == items[1].response_id == items[2].response_id
    assert current_response_id == items[0].response_id


def test_read_transcript_items_since_parses_interruption_queued_prompt(tmp_path: Path) -> None:
    """
    Queued prompt attachments become user messages in the transcript.

    Claude records text entered while a tool/assistant turn is still
    busy as ``attachment.type == "queued_command"``. This test uses
    that real interruption shape so a regression would drop the
    user's ``STOP`` message and incorrectly group the assistant's
    post-interrupt response under the pre-interrupt response id.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "user-count",
                        "message": {
                            "role": "user",
                            "content": "count to 10 unless I tell you to stop",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-three",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "3"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-sleep",
                        "message": {
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "toolu_sleep_1",
                                    "name": "Bash",
                                    "input": {"command": "sleep 1"},
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "queue-operation",
                        "operation": "enqueue",
                        "content": "STOP",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "sleep-result",
                        "parentUuid": "assistant-sleep",
                        "message": {
                            "role": "user",
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "toolu_sleep_1",
                                    "content": "(Bash completed with no output)",
                                }
                            ],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "attachment",
                        "uuid": "queued-stop",
                        "attachment": {
                            "type": "queued_command",
                            "prompt": "STOP",
                            "commandMode": "prompt",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-stopped",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Stopped at 3."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cursor, current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert cursor == 7, "cursor should include the queued-command attachment record"
    assert [item.item_type for item in items] == [
        "message",
        "message",
        "function_call",
        "function_call_output",
        "message",
        "message",
    ]
    assert items[4].source_id == "queued-stop:0:message"
    assert items[4].data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "STOP"}],
    }
    assert items[5].data == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "Stopped at 3."}],
    }
    assert items[3].response_id == items[2].response_id
    assert items[4].response_id != items[3].response_id
    assert items[5].response_id != items[3].response_id
    assert current_response_id == items[5].response_id


def test_read_hook_events_from_offset_skips_existing_prefix(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Byte-offset hook reads parse only hook records appended after the cursor.

    This guards the status forwarder hot path: many old hook records
    must not be rescanned on every poll once a byte cursor exists.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    hooks_path = bridge_dir / "hooks.jsonl"
    prefix = "".join(
        json.dumps({"payload": {"hook_event_name": "SessionStart"}}) + "\n" for _index in range(50)
    )
    hooks_path.write_text(
        prefix + json.dumps({"payload": {"hook_event_name": "Stop"}}) + "\n",
        encoding="utf-8",
    )
    prefix_offset = len(prefix.encode("utf-8"))
    _fail_if_path_reads_before_offset(monkeypatch, hooks_path, prefix_offset)

    result = read_hook_events_from_offset(
        bridge_dir,
        prefix_offset,
        start_event_count=50,
    )

    assert result.event_cursor == 51
    assert result.byte_offset == hooks_path.stat().st_size
    assert [record.event_name for record in result.records] == ["Stop"]


def test_read_hook_events_from_offset_preserves_partial_trailing_line(tmp_path: Path) -> None:
    """
    A partial trailing hook JSON record is not skipped.

    Hook processes append status edges concurrently with the forwarder
    poll loop. If the reader advanced past a partial line, the web UI
    could miss the matching ``running`` or ``idle`` transition.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    hooks_path = bridge_dir / "hooks.jsonl"
    hooks_path.write_text('{"payload":{"hook_event_name":"Stop"', encoding="utf-8")

    first = read_hook_events_from_offset(bridge_dir, 0, start_event_count=0)

    assert first.event_cursor == 0
    assert first.byte_offset == 0
    assert first.records == []

    with hooks_path.open("a", encoding="utf-8") as handle:
        handle.write("}}\n")
    second = read_hook_events_from_offset(
        bridge_dir,
        first.byte_offset,
        start_event_count=first.event_cursor,
    )

    assert second.event_cursor == 1
    assert second.byte_offset == hooks_path.stat().st_size
    assert [record.event_name for record in second.records] == ["Stop"]


def test_read_transcript_items_since_surfaces_skill_as_slash_command(
    tmp_path: Path,
) -> None:
    """
    A Skill marker record becomes one ``slash_command`` item, not
    a user bubble (the raw-markup regression) and not a drop
    (the over-correction). The subsequent assistant turn
    still surfaces.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "slash-skill",
                        "message": {
                            "role": "user",
                            "content": (
                                "<command-name>/dev-productivity:simplify</command-name>\n"
                                "            <command-message>simplify</command-message>\n"
                                "            <command-args></command-args>"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-after",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert cursor == 2
    assert [item.item_type for item in items] == ["slash_command", "message"]
    # Leading ``/`` stripped, args empty, ``output`` omitted via exclude_none.
    # ``kind`` is ``"skill"`` because the name isn't in the surfaced-CLI
    # set (``effort``, ``clear``, ``compact``, ``model``, ``ultrareview``).
    assert items[0].data == {
        "agent": "claude-native-ui",
        "kind": "skill",
        "name": "dev-productivity:simplify",
        "arguments": "",
    }
    assert items[1].data == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "Done."}],
    }
    # Slash command + the assistant turn it triggered must cluster in
    # one bubble — see test_slash_command_does_not_inherit_prior_turn_response_id.
    assert items[0].response_id == items[1].response_id


def test_slash_command_does_not_inherit_prior_turn_response_id(tmp_path: Path) -> None:
    """
    Slash command opens a new logical turn. With a prior assistant
    record establishing ``current_response_id``, the slash command +
    the assistant text it triggers must share their own
    ``response_id`` (distinct from the prior turn) so the web UI
    clusters them in one bubble instead of folding the follow-up
    text back into the older bubble.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "prior-assistant",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Earlier reply."}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "slash-skill",
                        "message": {
                            "role": "user",
                            "content": (
                                "<command-name>/dev-productivity:simplify</command-name>\n"
                                "            <command-message>simplify</command-message>\n"
                                "            <command-args></command-args>"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-after-skill",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Simplified."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert [item.item_type for item in items] == ["message", "slash_command", "message"]
    prior_id, slash_id, after_id = (item.response_id for item in items)
    assert prior_id != slash_id
    assert slash_id == after_id


def test_read_transcript_items_since_drops_cli_builtin_slash_commands(
    tmp_path: Path,
) -> None:
    """
    A real ``/login`` invocation generates three sibling
    ``role=user`` JSONL records: ``isMeta=true`` caveat, the
    ``<command-name>`` echo, and a follow-up
    ``<local-command-stdout>`` carrying the CLI's reply. All three
    must drop. Legitimate user messages around them still surface.

    Uses ``/login`` because it's in the DROPPED set; commands in the
    SURFACED set (``/effort``, ``/clear``, ``/compact``, ``/model``,
    ``/ultrareview``) deliberately emit a ``slash_command`` item now,
    so they'd fail this assertion — see the surfaces-cli-commands
    parametric test below.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "real-user-msg",
                        "message": {"role": "user", "content": "hi"},
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "caveat-meta",
                        "message": {
                            "role": "user",
                            "content": (
                                "<local-command-caveat>Caveat: ...</local-command-caveat>"
                            ),
                        },
                        "isMeta": True,
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "slash-login",
                        "message": {
                            "role": "user",
                            "content": (
                                "<command-name>/login</command-name>\n"
                                "            <command-message>login</command-message>\n"
                                "            <command-args></command-args>"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "login-stdout",
                        "message": {
                            "role": "user",
                            "content": (
                                "<local-command-stdout>Opening browser…</local-command-stdout>"
                            ),
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-after",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    # Five lines consumed; only the real user msg + final assistant text
    # become items. The stdout record on its own line is the leak that
    # rendered as a user bubble before this fix.
    assert cursor == 5
    assert [item.item_type for item in items] == ["message", "message"]
    assert items[0].data == {
        "role": "user",
        "content": [{"type": "input_text", "text": "hi"}],
    }
    assert items[1].data == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "Done."}],
    }


@pytest.mark.parametrize(
    "marker_content",
    [
        "<local-command-stdout>Set effort level to max</local-command-stdout>",
        "<local-command-stderr>command failed</local-command-stderr>",
        # Standalone command-message / command-args records (4 of these
        # appear in the real transcripts without a preceding command-name).
        "<command-message>effort</command-message>",
        "<command-args>max</command-args>",
    ],
)
def test_read_transcript_items_since_drops_standalone_cli_scaffolding_records(
    tmp_path: Path, marker_content: str
) -> None:
    """
    Any ``role=user`` record whose content starts with a known CLI
    scaffolding marker is CLI bookkeeping, not user content. Each
    must drop instead of rendering markup as a user bubble.

    Note: ``<bash-input>``, ``<bash-stdout>``, and ``<bash-stderr>`` are
    intentionally excluded here — they are surfaced as ``terminal_command``
    items (see ``test_read_transcript_items_since_surfaces_terminal_command_*``).
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "scaffolding",
                "message": {"role": "user", "content": marker_content},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert items == [], (
        f"CLI scaffolding ({marker_content[:40]}...) must drop; "
        f"bridge emitted {[item.item_type for item in items]}"
    )


@pytest.mark.parametrize(
    "content",
    [
        # String form (how Claude Code ships it today), both variants.
        "[Request interrupted by user]",
        "[Request interrupted by user for tool use]",
        # List form — defensive: Claude's JSONL shape isn't under our control.
        [{"type": "text", "text": "[Request interrupted by user]"}],
    ],
)
def test_read_transcript_items_since_keeps_interrupt_marker(
    tmp_path: Path, content: object
) -> None:
    """
    Claude's ``[Request interrupted by user]`` record is mirrored, not dropped.

    We deliberately keep it in history (it mirrors Claude's own session); the
    web UI re-classifies it as a muted "System: Interrupted" marker via
    ``parseSystemMessage`` rather than a raw user bubble. Guards against
    re-adding a bridge-side drop filter, which would starve the UI of the
    marker and leave a reload with no sign the turn was interrupted.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "interrupt",
                "message": {"role": "user", "content": content},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert [item.item_type for item in items] == ["message"], (
        f"Interrupt marker must be mirrored as a message item for the UI to "
        f"re-classify; bridge emitted {[item.item_type for item in items]}"
    )


def test_read_transcript_items_since_surfaces_bash_input_as_terminal_command(
    tmp_path: Path,
) -> None:
    """
    A ``role=user`` record starting with ``<bash-input>`` is emitted by
    Claude Code when the user types ``!cmd``. It must surface as a
    ``terminal_command`` item with ``kind="input"`` instead of being
    dropped or rendered as raw markup.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "bash-in",
                "message": {"role": "user", "content": "<bash-input>pwd</bash-input>"},
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _response_id, items = read_transcript_items_since(
        transcript_path, 0, agent_name="claude-native-ui"
    )

    assert len(items) == 1
    assert items[0].item_type == "terminal_command"
    assert items[0].data == {"kind": "input", "input": "pwd"}
    assert isinstance(items[0].response_id, str)
    assert items[0].response_id.startswith("resp_claude_")


def test_read_transcript_items_since_surfaces_bash_output_as_terminal_command(
    tmp_path: Path,
) -> None:
    """
    The sibling ``role=user`` record carrying ``<bash-stdout>`` and
    ``<bash-stderr>`` must surface as a ``terminal_command`` item with
    ``kind="output"`` so stdout/stderr are visible in the server UI.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "bash-out",
                "message": {
                    "role": "user",
                    "content": "<bash-stdout>/home/user</bash-stdout><bash-stderr></bash-stderr>",
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _response_id, items = read_transcript_items_since(
        transcript_path, 0, agent_name="claude-native-ui"
    )

    assert len(items) == 1
    assert items[0].item_type == "terminal_command"
    assert items[0].data == {"kind": "output", "stdout": "/home/user", "stderr": ""}
    assert isinstance(items[0].response_id, str)
    assert items[0].response_id.startswith("resp_claude_")


def test_read_transcript_items_since_surfaces_top_level_local_command(
    tmp_path: Path,
) -> None:
    """
    Top-level ``local_command`` shell records become terminal items.

    Claude Code can record ``!cmd`` as ``subtype="local_command"``
    with a top-level ``content`` field instead of a ``role=user``
    message. This test fails if the parser only looks under
    ``message`` and silently drops shell commands before the
    forwarder can POST them to AP.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "subtype": "local_command",
                        "uuid": "bash-in-local",
                        "content": "<bash-input>pwd</bash-input>",
                    }
                ),
                json.dumps(
                    {
                        "type": "user",
                        "subtype": "local_command",
                        "uuid": "bash-out-local",
                        "content": (
                            "<bash-stdout>/home/user</bash-stdout><bash-stderr></bash-stderr>"
                        ),
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _response_id, items = read_transcript_items_since(
        transcript_path, 0, agent_name="claude-native-ui"
    )

    assert [item.item_type for item in items] == ["terminal_command", "terminal_command"]
    assert [item.data for item in items] == [
        {"kind": "input", "input": "pwd"},
        {"kind": "output", "stdout": "/home/user", "stderr": ""},
    ]
    assert items[0].response_id == items[1].response_id
    assert items[0].response_id.startswith("resp_claude_")


def test_read_transcript_items_since_surfaces_combined_shell_record(
    tmp_path: Path,
) -> None:
    """
    One transcript record can carry both shell input and output.

    A regression that returns after the input tag would persist only
    the invocation and lose the result, recreating the broken web
    transcript that shows a local shell command with no output.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "subtype": "local_command",
                "uuid": "bash-combined",
                "content": ("<bash-input>printf hi</bash-input><bash-stdout>hi</bash-stdout>"),
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _response_id, items = read_transcript_items_since(
        transcript_path, 0, agent_name="claude-native-ui"
    )

    assert [item.item_type for item in items] == ["terminal_command", "terminal_command"]
    assert [item.data for item in items] == [
        {"kind": "input", "input": "printf hi"},
        {"kind": "output", "stdout": "hi", "stderr": None},
    ]
    assert items[0].response_id == items[1].response_id


def test_read_transcript_items_since_surfaces_skill_when_command_name_is_not_first_tag(
    tmp_path: Path,
) -> None:
    """
    Skill invocations with args ship the tag order
    ``<command-message>…<command-name>…<command-args>…`` (real shape
    observed in transcripts for e.g. ``/supervisor-agent-e2e-test use this skill``).
    The dispatcher must detect ``<command-name>`` anywhere in the
    content, not just at the start — otherwise the record falls into
    the scaffolding-drop branch because ``<command-message>`` IS in
    ``_CLI_SCAFFOLDING_MARKERS`` and the skill silently disappears.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "slash-skill-with-args",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-message>supervisor-agent-e2e-test</command-message>\n"
                        "<command-name>/supervisor-agent-e2e-test</command-name>\n"
                        "<command-args>use this skill</command-args>"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert len(items) == 1
    assert items[0].item_type == "slash_command"
    assert items[0].data == {
        "agent": "claude-native-ui",
        "kind": "skill",
        "name": "supervisor-agent-e2e-test",
        "arguments": "use this skill",
    }


def test_read_transcript_items_since_carries_stdout_for_skill_with_output(
    tmp_path: Path,
) -> None:
    """
    Inline ``<local-command-stdout>`` is threaded into ``output``
    so the renderer can show it in the expanded panel. Regex bugs
    that grab the wrong group surface here.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "slash-skill-with-stdout",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-name>/oncall</command-name>\n"
                        "<command-message>oncall</command-message>\n"
                        "<command-args>file-bug</command-args>\n"
                        "<local-command-stdout>oncall: file-bug subcommand "
                        "started</local-command-stdout>"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert len(items) == 1
    assert items[0].item_type == "slash_command"
    assert items[0].data == {
        "agent": "claude-native-ui",
        "kind": "skill",
        "name": "oncall",
        "arguments": "file-bug",
        "output": "oncall: file-bug subcommand started",
    }


@pytest.mark.parametrize(
    "builtin_name",
    [
        "add-dir",
        "plugin",
        "terminal-setup",
    ],
)
def test_read_transcript_items_since_drops_recently_added_cli_builtins(
    tmp_path: Path, builtin_name: str
) -> None:
    """
    Names in the CLI built-in DROPPED set drop end-to-end — not
    surface as fake Skill rows. Drives each through the full bridge
    pipeline (set membership alone wouldn't catch a name that's in
    the set but mishandled downstream).
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": f"slash-{builtin_name}",
                "message": {
                    "role": "user",
                    "content": (
                        f"<command-name>/{builtin_name}</command-name>\n"
                        f"            <command-message>{builtin_name}</command-message>\n"
                        "            <command-args></command-args>"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert items == [], (
        f"/{builtin_name} must drop; bridge emitted {[item.item_type for item in items]}"
    )


@pytest.mark.parametrize(
    ("command_name", "args"),
    [
        ("effort", "high"),
        ("clear", ""),
        ("compact", ""),
        ("model", "claude-opus-4-7"),
        ("ultrareview", ""),
    ],
)
def test_read_transcript_items_since_surfaces_cli_commands_with_kind_command(
    tmp_path: Path, command_name: str, args: str
) -> None:
    """
    Surfaced CLI built-ins (``/effort``, ``/clear``, ``/compact``,
    ``/model``, ``/ultrareview``) emit a ``slash_command`` item with
    ``kind="command"`` — the renderer uses that to switch the prefix
    label from "Skill" to "Command".

    These commands change conversation-visible state (effort level,
    context reset, compaction, model swap, kicking off a review). A
    web observer must see them; otherwise the next assistant turn
    appears to shift unprompted. If a regression promotes any of
    these names back into the dropped set, items would be empty and
    this test fails.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": f"slash-{command_name}",
                "message": {
                    "role": "user",
                    "content": (
                        f"<command-name>/{command_name}</command-name>\n"
                        f"            <command-message>{command_name}</command-message>\n"
                        f"            <command-args>{args}</command-args>"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert len(items) == 1
    assert items[0].item_type == "slash_command"
    assert items[0].data == {
        "agent": "claude-native-ui",
        "kind": "command",
        "name": command_name,
        "arguments": args,
    }


def test_read_transcript_items_since_drops_malformed_slash_command_record(
    tmp_path: Path,
) -> None:
    """
    A truncated slash-command record (open tag, no close) drops
    instead of falling through to the user-bubble path that would
    render the markup verbatim. Subsequent legitimate
    records still surface — one bad line must not kill the poll loop.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "user",
                        "uuid": "slash-truncated",
                        "message": {
                            "role": "user",
                            # Start tag with no closing tag — pathological shape.
                            "content": "<command-name>/oncall",
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "assistant-after",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "Done."}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    assert [item.item_type for item in items] == ["message"]
    assert items[0].data == {
        "role": "assistant",
        "agent": "claude-native-ui",
        "content": [{"type": "output_text", "text": "Done."}],
    }


def test_read_transcript_items_since_drops_list_form_slash_command_text_block(
    tmp_path: Path,
) -> None:
    """
    Slash-command markup inside a list-form ``content`` text block
    drops instead of leaking as a user bubble. Sibling tool_result
    blocks in the same record still surface.

    Today Claude Code emits slash-command records as string content
    (covered by the surfaces/drops/malformed tests above). If that
    serialization ever changes to list form, the string-branch guard
    no longer fires — without the list-branch filter this regresses
    to the raw-markup-as-user-bubble bug.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "list-form-slash",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "<command-name>/dev-productivity:simplify"
                                "</command-name>\n"
                                "            <command-args></command-args>"
                            ),
                        },
                        {
                            "type": "tool_result",
                            "tool_use_id": "call_legit",
                            "content": "tool finished",
                        },
                    ],
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    _cursor, _current_response_id, items = read_transcript_items_since(
        transcript_path,
        0,
        agent_name="claude-native-ui",
    )

    # The text block holding ``<command-name>…`` is dropped; only the
    # legitimate tool_result survives. If the list-branch filter
    # regresses, items would contain a "message" with the raw markup
    # as ``input_text``.
    assert [item.item_type for item in items] == ["function_call_output"]
    assert items[0].data["call_id"] == "call_legit"


def test_read_transcript_items_from_offset_surfaces_skill_as_slash_command(
    tmp_path: Path,
) -> None:
    """
    Byte-offset transcript reads emit ``slash_command`` items, not
    raw user bubbles, for Claude Code Skill records.

    Both transcript readers (line-cursor ``read_transcript_items_since``
    and byte-offset ``read_transcript_items_from_offset``) flow through
    the same ``_user_transcript_items_from_entry`` parser. The line-
    cursor path is covered above; this pins the byte-offset path so a
    future change to the byte-offset wrapper that drops ``agent_name``
    or ``record_offset`` from the parser call fails loud here.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps(
            {
                "type": "user",
                "uuid": "byte-offset-skill",
                "message": {
                    "role": "user",
                    "content": (
                        "<command-name>/dev-productivity:simplify</command-name>\n"
                        "            <command-args></command-args>"
                    ),
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )

    result = read_transcript_items_from_offset(
        transcript_path,
        0,
        start_line=0,
        agent_name="claude-native-ui",
    )

    assert result.byte_offset == transcript_path.stat().st_size
    assert [item.item_type for item in result.items] == ["slash_command"]
    assert result.items[0].data == {
        "agent": "claude-native-ui",
        "kind": "skill",
        "name": "dev-productivity:simplify",
        "arguments": "",
    }


def test_augment_claude_args_injects_mcp_and_hooks(tmp_path: Path) -> None:
    """
    Claude receives the Omnigent MCP server and hook settings in one launch.

    This fails if the terminal starts Claude without the bridge pieces
    needed for tool dispatch / transcript discovery. Also asserts the
    org-blocked Channels flag is not present.
    """
    args = augment_claude_args(
        ("--resume", "abc"),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
    )

    assert args[:2] == ["--resume", "abc"]
    mcp_config = json.loads(args[args.index("--mcp-config") + 1])
    server = mcp_config["mcpServers"]["omnigent"]
    assert server["command"] == "/venv/bin/python"
    assert server["args"][-2:] == ["--bridge-dir", str(tmp_path)]
    # Web-UI input does not flow through Claude Channels — that
    # capability is blocked at the org level. The wrapper must not
    # pass the development-channels flag.
    assert "--dangerously-load-development-channels" not in args
    settings = json.loads(args[args.index("--settings") + 1])
    assert "omnigent.claude_native_hook" in settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    # ``PreCompact`` must be wired so the forwarder can surface
    # ``response.compaction.in_progress`` while Claude compacts in the
    # terminal. Missing it = no "Compacting…" spinner for claude-native.
    assert "PreCompact" in settings["hooks"], (
        f"PreCompact hook must be registered; got hooks {sorted(settings['hooks'])!r}."
    )
    assert (
        "omnigent.claude_native_hook" in settings["hooks"]["PreCompact"][0]["hooks"][0]["command"]
    )
    # No built-in tools are disabled anymore: ``AskUserQuestion``
    # routes through its dedicated PreToolUse hook (answers injected
    # via ``updatedInput.answers``) and ``ExitPlanMode`` surfaces
    # through the standard PermissionRequest elicitation card, so the
    # wrapper must not inject a ``--disallowedTools`` flag of its own.
    assert "--disallowedTools" not in args


def test_augment_claude_args_merges_user_disallowed_tools(tmp_path: Path) -> None:
    """
    A user-supplied ``--disallowedTools`` passes through unchanged.

    With no wrapper-disabled tools, the user's flag must survive
    verbatim — exactly one flag, exactly their value. This fails if the
    wrapper appends a duplicate flag or mutates the user's disables.
    """
    args = augment_claude_args(
        ("--disallowedTools", "Bash,Edit"),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
    )

    flag_indices = [i for i, arg in enumerate(args) if arg == "--disallowedTools"]
    assert len(flag_indices) == 1
    disallowed = args[flag_indices[0] + 1].split(",")
    assert disallowed == ["Bash", "Edit"]


def test_augment_claude_args_injects_plugin_dir_for_bundle_with_skills(
    tmp_path: Path,
) -> None:
    """
    A bundle that ships ``skills/`` is exposed to Claude Code via
    ``--plugin-dir <bundle>``, with a plugin manifest written so the
    skills label as ``<agent>:<skill>``.

    This is the claude-native parity for the SDK executor's plugin
    wiring. It fails if a deployed agent's bundled skills never reach the
    real ``claude`` CLI (the gap before this change — native ignores the
    harness ``tools``/skill plumbing, so ``--plugin-dir`` is the only
    surface). ``skills_filter`` defaults to ``"all"``, so host skills use
    the CLI's default sources and no ``--setting-sources`` is emitted.
    """
    bundle = tmp_path / "bundle"
    (bundle / "skills" / "authoring").mkdir(parents=True)
    (bundle / "skills" / "authoring" / "SKILL.md").write_text("# authoring\n")

    args = augment_claude_args(
        (),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
        bundle_dir=bundle,
        agent_name="researcher",
    )

    assert "--plugin-dir" in args
    # The plugin path is the bundle root (Claude discovers
    # <bundle>/skills/<name>/SKILL.md under the plugin convention).
    assert args[args.index("--plugin-dir") + 1] == str(bundle)
    # "all" → host skills via the CLI default; no explicit override.
    assert "--setting-sources" not in args
    # The manifest gives the plugin a stable name for clean skill labels.
    manifest = bundle / ".claude-plugin" / "plugin.json"
    assert manifest.exists()
    assert json.loads(manifest.read_text())["name"] == "researcher"
    # The skill args are appended alongside the MCP/hook injection, not in
    # place of it — both must reach the final launch command.
    assert "--mcp-config" in args


def test_augment_claude_args_omits_permission_hook_without_omnigent_server(
    tmp_path: Path,
) -> None:
    """
    No ``PermissionRequest`` hook is registered when the wrapper has
    no Omnigent server URL to point Claude at.

    This guards the default path: a call site that forgets to plumb
    the Omnigent server URL must NOT silently fall through to Claude's TUI
    prompt for every tool — but it also must not register an HTTP hook
    against an undefined URL. The expected behaviour is "no hook at
    all", which means Claude uses its built-in permission flow.
    """
    args = augment_claude_args(
        (),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
    )
    settings = json.loads(args[args.index("--settings") + 1])
    assert "PermissionRequest" not in settings["hooks"]


def test_augment_claude_args_registers_permission_command_hook(
    tmp_path: Path,
) -> None:
    """
    Passing ``ap_server_url`` registers Claude's
    ``PermissionRequest`` hook as a command hook that resolves the
    active Omnigent session at hook time.

    If this regresses, Claude Code's built-in TUI permission prompt
    appears every time the user is supposed to approve from the web
    UI instead — silently breaking the blocking-UI-hook contract.
    """
    args = augment_claude_args(
        (),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
        ap_server_url="http://127.0.0.1:8787/",
        ap_auth_headers={"Authorization": "Bearer xyz"},
    )
    settings = json.loads(args[args.index("--settings") + 1])
    permission = settings["hooks"]["PermissionRequest"]
    assert len(permission) == 1
    hooks = permission[0]["hooks"]
    assert len(hooks) == 1
    hook = hooks[0]
    assert hook["type"] == "command"
    # The command hook must carry a day-long timeout. Claude Code's
    # default command-hook timeout (~60s) would otherwise kill the
    # hook subprocess before the user answers the permission prompt in
    # the web UI, flipping the card to "Resolved elsewhere" while the
    # terminal prompt is still open. A failure here (missing key or a
    # short value) means that premature auto-resolve has regressed.
    assert hook["timeout"] == 86400
    assert "omnigent.claude_native_hook permission-request" in hook["command"]
    assert "--bridge-dir" in hook["command"]
    assert "Bearer xyz" not in hook["command"]
    permission_config = json.loads((tmp_path / "permission_hook.json").read_text(encoding="utf-8"))
    assert permission_config["ap_server_url"] == "http://127.0.0.1:8787/"
    assert permission_config["ap_auth_headers"] == {"Authorization": "Bearer xyz"}
    session_start_command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "--conversation-url" not in session_start_command
    assert "conv_abc" not in session_start_command
    assert "companyAnnouncements" not in settings
    # statusLine is now intentionally injected (it's the only place
    # Claude Code surfaces ``context_window`` on stdin); ensure it
    # points at our wrapper module rather than something arbitrary.
    assert "omnigent.claude_native_status" in settings["statusLine"]["command"]


def test_augment_claude_args_registers_user_prompt_submit_policy_hook(
    tmp_path: Path,
) -> None:
    """
    Passing ``ap_server_url`` wires the evaluate-policy hook onto
    ``UserPromptSubmit`` alongside the transcript forwarder's status hook.

    For native sessions the server-level ``_evaluate_input_policy`` skips
    message events, so this hook is the sole REQUEST-phase gate (covering
    both web-UI-injected and direct-terminal prompts). If it regressed,
    native prompts would reach the model with no request-phase policy. The
    forwarder's own UserPromptSubmit hook (status → running) must survive,
    so the policy hook is appended, not substituted.
    """
    args = augment_claude_args(
        (),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
        ap_server_url="http://127.0.0.1:8787/",
        ap_auth_headers={"Authorization": "Bearer xyz"},
    )
    settings = json.loads(args[args.index("--settings") + 1])
    entries = settings["hooks"]["UserPromptSubmit"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    # The forwarder status hook stays; the policy hook is appended.
    assert any("evaluate-policy" in command for command in commands), (
        f"UserPromptSubmit must carry the evaluate-policy hook; got {commands!r}."
    )
    assert any("evaluate-policy" not in command for command in commands), (
        "The transcript forwarder's UserPromptSubmit hook must not be replaced."
    )


def test_augment_claude_args_omits_user_prompt_submit_policy_hook_without_server(
    tmp_path: Path,
) -> None:
    """
    Without ``ap_server_url`` the UserPromptSubmit policy hook is not wired.

    Policy hooks only make sense when an Omnigent server is configured to
    evaluate against; the forwarder's status hook still registers, but no
    evaluate-policy command should appear (mirrors PreToolUse/PostToolUse,
    which are also gated behind ``ap_server_url``).
    """
    args = augment_claude_args(
        (),
        bridge_dir=tmp_path,
        python_executable="/venv/bin/python",
    )
    settings = json.loads(args[args.index("--settings") + 1])
    entries = settings["hooks"]["UserPromptSubmit"]
    commands = [h["command"] for entry in entries for h in entry["hooks"]]
    assert all("evaluate-policy" not in command for command in commands)


def test_augment_claude_args_keeps_permission_hook_without_launch_session_id(
    tmp_path: Path,
) -> None:
    """
    Permission routing no longer depends on a launch-time conversation id.

    The command hook reads the active session from bridge config at
    hook time, so launch-time conversation ids are not part of the
    hook settings contract.
    """
    args = augment_claude_args(
        (),
        bridge_dir=tmp_path,
        ap_server_url="http://127.0.0.1:8787",
    )
    settings = json.loads(args[args.index("--settings") + 1])
    assert settings["hooks"]["PermissionRequest"][0]["hooks"][0]["type"] == "command"
    session_start_command = settings["hooks"]["SessionStart"][0]["hooks"][0]["command"]
    assert "--conversation-url" not in session_start_command
    assert "companyAnnouncements" not in settings
    assert "omnigent.claude_native_status" in settings["statusLine"]["command"]


def test_mcp_server_initialize_omits_blocked_channel_capability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subprocess_bridge_root: Path,
) -> None:
    """
    The MCP server does not advertise the org-blocked Channels capability.

    This fails if a future change re-enables Claude Channels: Claude
    Code would refuse to start with that capability advertised under
    org policy, breaking the native wrapper.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", Path("/tmp"))
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", subprocess_bridge_root)
    bridge_dir = prepare_bridge_dir("conv_abc", workspace=tmp_path)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        proc.stdin.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {},
                }
            )
            + "\n"
        )
        proc.stdin.flush()
        initialize = _read_json_line(proc.stdout, timeout_s=5.0)
        assert initialize["id"] == 1
        capabilities = initialize["result"]["capabilities"]
        # Tools/list_changed is still needed for the active-turn relay.
        assert capabilities["tools"] == {"listChanged": True}
        # The experimental Claude/channel capability must NOT appear:
        # advertising it triggers org-policy refusal in Claude Code.
        assert "experimental" not in capabilities or "claude/channel" not in capabilities.get(
            "experimental", {}
        )
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


def test_write_tmux_target_persists_socket_and_target(tmp_path: Path) -> None:
    """
    The runner advertisement file is shaped so the harness can find it.

    This catches mismatches between runner-side writes and the
    harness-side ``inject_user_message`` reader, which would cause
    the web UI to time out with "tmux target not advertised yet".
    """
    bridge_dir = tmp_path / "bridge"
    before = time.time()

    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
        pid=12345,
    )
    after = time.time()

    payload = json.loads((bridge_dir / "tmux.json").read_text(encoding="utf-8"))
    assert payload["socket_path"] == "/tmp/example/tmux.sock"
    assert payload["tmux_target"] == "claude:0.0"
    assert payload["pid"] == 12345
    assert before <= payload["updated_at"] <= after


@pytest.mark.parametrize(
    "content,expected_payload",
    [
        # Plain single line: trailing CR appended (newline -> 0x0d).
        ("hello world", b"hello world\r"),
        # Multi-line: interior newline rides as CR inside the paste so
        # Claude's TUI keeps it as data instead of submitting per line.
        ("line one\nline two", b"line one\rline two\r"),
        # Trailing "\" escapes the appended paste CR, not the submit Enter.
        ("deploy to prod\\", b"deploy to prod\\\r"),
        # CRLF line endings coalesce to a single CR (no doubled blank line).
        ("a\r\nb", b"a\rb\r"),
        # A stray ESC in the content is dropped so it can't prematurely
        # close the bracketed-paste sequence on Claude's side.
        ("a\x1bb", b"ab\r"),
        # Large payload (a PR diff in a sub-agent dispatch). Must ride the
        # load-buffer file — per-byte send-keys argv tripped tmux's ~16KB
        # client→server command cap with "command too long".
        ("x" * 100_000, b"x" * 100_000 + b"\r"),
    ],
    ids=["plain", "multiline", "trailing-backslash", "crlf", "embedded-esc", "large"],
)
def test_inject_user_message_pastes_content_then_submits(
    content: str,
    expected_payload: bytes,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Every message is delivered as one bracketed paste, then a separate Enter.

    The paste goes via ``load-buffer`` (from a temp file, so size is
    unbounded — tmux caps a single command at ~16KB, which a PR-diff
    dispatch exceeded as "command too long") then ``paste-buffer -p``,
    with interior newlines encoded as CR so Claude Code's TUI keeps
    multi-line input as a single editable block instead of submitting on
    each newline (anthropics/claude-code#52126). Fails if the harness
    regresses to send-keys argv delivery, drops the trailing Enter, or
    stops clearing the stale buffer.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    captured: list[list[str]] = []
    loaded_payloads: list[bytes] = []
    # Simulates Claude Code's input-box state machine: empty before the
    # paste, draft (collapsed-paste placeholder) after the paste, empty
    # again once Enter submits. The paste-committed and submit-verified
    # gates both poll capture-pane, so a static pane would either stall
    # the paste gate (draft never appears) or fail verification.
    tui = {"pane": "❯ "}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Record tmux calls; simulate the TUI's input-box state.

        ``capture-pane`` calls (the readiness / paste-committed /
        submit-verified polls) return the current simulated pane and
        are not recorded — the assertions below count only the
        delivery invocations. ``load-buffer`` calls read the temp
        file's bytes at call time (the harness unlinks it after the
        paste, so asserting later would race the cleanup).
        ``paste-buffer`` puts the draft into the simulated input box;
        ``Enter`` clears it (submit).

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (capture_output, text, etc.).
        :returns: A fake CompletedProcess with rc=0.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "load-buffer" in cmd:
            loaded_payloads.append(Path(cmd[-1]).read_bytes())
        if "paste-buffer" in cmd:
            tui["pane"] = "❯ [Pasted text #1 +2 lines]"
        if cmd[-1] == "Enter":
            tui["pane"] = "❯ "
        captured.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message(bridge_dir, content=content)

    # C-a, C-k (clear), load-buffer, paste-buffer, Enter — fewer than 5
    # means a delivery step was dropped.
    assert len(captured) == 5, (
        f"Expected 5 tmux calls (C-a, C-k, load-buffer, paste-buffer, Enter), got {len(captured)}."
    )
    clear_home, clear_kill, load, paste, submit = captured
    assert clear_home[-1] == "C-a"
    assert clear_kill[-1] == "C-k"
    # The buffer file carried the normalized content + trailing CR. A
    # missing trailing CR is the trailing-CR regression; a newline that stayed
    # \n (not CR) is the anthropics/claude-code#52126 multi-line collapse.
    assert loaded_payloads == [expected_payload]
    assert load[:6] == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "load-buffer",
        "-b",
        "omnigent-paste",
    ]
    # -p (bracketed-paste markers) dropped = newlines submit per-line in
    # the TUI; -d dropped = stale buffer copies accumulate server-side.
    assert paste == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "paste-buffer",
        "-p",
        "-d",
        "-b",
        "omnigent-paste",
        "-t",
        "claude:0.0",
    ]
    assert submit == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "send-keys",
        "-t",
        "claude:0.0",
        "Enter",
    ]


def test_inject_user_message_raises_when_tmux_target_never_published(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The injection helper fails loud when the runner has not written tmux.json.

    Failing silently would let the Omnigent turn complete with no user
    message ever reaching Claude — the executor needs the
    RuntimeError so it can surface an ExecutorError.
    """
    with pytest.raises(RuntimeError, match="tmux target is not advertised"):
        inject_user_message(tmp_path / "bridge", content="hi", timeout_s=0.0)


def test_inject_user_message_raises_on_tmux_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-zero tmux exit propagates as a RuntimeError with stderr context.

    Without this, a broken tmux socket (server died, permission
    issue) would be reported to the web UI as a successful turn
    with no Claude response.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Pass the readiness gate, then fail the send-keys call.

        ``capture-pane`` returns a ready pane (rc=0) so the test
        exercises the send-keys failure path specifically, not the
        readiness timeout. All other tmux calls return rc=1 to
        simulate a dead tmux server.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: A fake CompletedProcess (rc=0 for capture-pane,
            rc=1 otherwise).
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout="❯ ", stderr="")
        return SimpleNamespace(
            returncode=1,
            stdout="",
            stderr="no server running on /tmp/example/tmux.sock",
        )

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="no server running"):
        inject_user_message(bridge_dir, content="hi")


def test_inject_user_message_waits_for_claude_prompt_before_typing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Injection polls for Claude's prompt before sending any keystrokes.

    On a freshly-created session ``tmux.json`` is advertised before
    Claude Code's input box mounts. If we typed immediately the first
    message would be dropped into the still-booting TUI. This pins that
    no send-keys is issued until ``capture-pane`` shows the prompt
    glyph, and that injection proceeds once it does.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    send_keys: list[list[str]] = []
    # Prompt is absent for the first two polls (TUI still booting),
    # then renders. A wrong gate would send keys during the empty
    # polls; a correct gate waits for the third capture. After boot the
    # fake behaves like the live input box: the paste deposits the
    # draft, Enter clears it (so the submit-verification gate passes).
    boot_panes = ["", "", "❯ "]
    capture_calls = {"n": 0}
    tui = {"pane": "❯ "}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Serve a not-ready-then-ready pane; record send-keys calls.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the next
            boot pane until the prompt renders, then the simulated
            input-box state; send-keys returns rc=0.
        """
        del kwargs
        if "capture-pane" in cmd:
            idx = min(capture_calls["n"], len(boot_panes) - 1)
            capture_calls["n"] += 1
            # No keystrokes may have been sent before the prompt shows.
            if boot_panes[idx] == "":
                assert send_keys == [], "typed before Claude prompt rendered"
                return SimpleNamespace(returncode=0, stdout="", stderr="")
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "❯ hello"
        if cmd[-1] == "Enter":
            tui["pane"] = "❯ "
        send_keys.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message(bridge_dir, content="hello")

    # Gate polled until the third capture (prompt present), then the
    # five delivery calls (C-a, C-k, load-buffer, paste-buffer, Enter)
    # fired.
    assert capture_calls["n"] >= 3, (
        f"Expected >=3 capture-pane polls before the prompt rendered, got {capture_calls['n']}."
    )
    assert len(send_keys) == 5, (
        f"Expected 5 tmux calls (C-a, C-k, load-buffer, paste-buffer, Enter), "
        f"got {len(send_keys)}."
    )
    clear_home, clear_kill, load, paste, submit = send_keys
    assert clear_home[-1] == "C-a"
    assert clear_kill[-1] == "C-k"
    # The paste fires after the gate via the buffer path. The exact
    # payload/flag assertions live in the dedicated paste test; here the
    # gate ordering is the claim.
    assert load[3] == "load-buffer"
    assert paste[3] == "paste-buffer"
    assert submit[-1] == "Enter"


def test_inject_user_message_raises_when_prompt_never_renders(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Injection fails loud if Claude's prompt never renders (boot failed).

    Without this the turn would complete with the message dropped and
    the web UI stuck on "Working…". The RuntimeError surfaces as an
    ExecutorError instead. No keystrokes must be sent on this path.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    send_keys: list[list[str]] = []

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Always report an empty (never-ready) pane.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns "".
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout="", stderr="")
        send_keys.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="did not become ready"):
        inject_user_message(bridge_dir, content="hi", timeout_s=0.3)
    assert send_keys == [], "no keystrokes should be sent when the prompt never renders"


def test_inject_user_message_ignores_prompt_glyph_in_scrollback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    The readiness gate only trusts the prompt glyph in the pane tail.

    Claude echoes prior user input (which may contain ``❯``) into
    scrollback. If the gate matched anywhere in the pane it would
    falsely pass while the live input box is still booting. Here ``❯``
    appears only in an early line, with later non-empty lines lacking
    it, so the gate must NOT treat the pane as ready.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )
    # `❯` only on an early line; the last several non-empty lines (the
    # tail the gate scans) do not contain it.
    scrollback = "\n".join(
        [
            "❯ old prompt echo",
            "output line 1",
            "output line 2",
            "output line 3",
            "output line 4",
            "output line 5",
        ]
    )

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Return a pane with the glyph only in scrollback.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the
            scrollback pane.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=scrollback, stderr="")
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="did not become ready"):
        inject_user_message(bridge_dir, content="hi", timeout_s=0.3)


def test_inject_user_message_resends_enter_when_first_submit_swallowed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A submit Enter swallowed by the TUI's paste burst is retried.

    Claude Code coalesces rapid stdin bursts into a paste; an Enter
    that lands inside that window becomes a newline in the draft
    instead of a submit, and the message sits unsent — the "typed but
    never sent" bug. The helper must observe (via capture-pane) that
    the draft is still in the input box after Enter and re-send Enter
    until it clears. Here the fake TUI swallows the first Enter
    (draft stays put) and submits on the second; a regression to
    fire-and-forget Enter would send exactly one and return "success"
    with the message undelivered.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    # Shrink the polling cadence so the retry happens in milliseconds —
    # the production defaults (1s retry spacing) would make this test slow.
    monkeypatch.setattr("omnigent.claude_native_bridge._CLAUDE_READY_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr("omnigent.claude_native_bridge._SUBMIT_RETRY_INTERVAL_S", 0.02)
    monkeypatch.setattr("omnigent.claude_native_bridge._PASTE_SETTLE_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    enters: list[list[str]] = []
    # Input-box state machine: the paste deposits the draft; the FIRST
    # Enter is swallowed (folded into the paste burst — draft stays);
    # the second Enter submits and clears the box.
    tui = {"pane": "❯ ", "swallowed_enters": 0}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Simulate a TUI that swallows the first submit Enter.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the
            simulated input-box pane, other calls return rc=0.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "❯ fix the flaky test"
        if cmd[-1] == "Enter":
            enters.append(cmd)
            if tui["swallowed_enters"] == 0:
                tui["swallowed_enters"] = 1  # folded into the paste — draft stays
            else:
                tui["pane"] = "❯ "  # submitted — input box clears
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_user_message(bridge_dir, content="fix the flaky test")

    # Exactly two Enters: the swallowed one plus one retry. One means
    # the verify-retry loop regressed to fire-and-forget (the bug);
    # three+ means retries keep firing after the draft cleared.
    assert len(enters) == 2, (
        f"Expected the swallowed Enter to be retried exactly once, got {len(enters)} Enter(s)."
    )


def test_inject_user_message_raises_when_draft_never_submits(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Injection fails loud when the draft never leaves the input box.

    If every submit Enter is swallowed (e.g. the pane is wedged in a
    dialog), returning success would complete the Omnigent turn with
    the message still sitting unsent in Claude's input box. The
    RuntimeError surfaces as an ExecutorError instead.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    monkeypatch.setattr("omnigent.claude_native_bridge._CLAUDE_READY_POLL_INTERVAL_S", 0.01)
    monkeypatch.setattr("omnigent.claude_native_bridge._SUBMIT_RETRY_INTERVAL_S", 0.02)
    monkeypatch.setattr("omnigent.claude_native_bridge._SUBMIT_VERIFY_TIMEOUT_S", 0.2)
    monkeypatch.setattr("omnigent.claude_native_bridge._PASTE_SETTLE_S", 0.0)
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    tui = {"pane": "❯ "}

    def _fake_run(cmd: list[str], **kwargs: object) -> SimpleNamespace:
        """
        Simulate a TUI where the draft never submits.

        The paste deposits the draft and every Enter is ignored, so
        the input box never clears.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: Fake CompletedProcess; capture-pane returns the
            simulated input-box pane, other calls return rc=0.
        """
        del kwargs
        if "capture-pane" in cmd:
            return SimpleNamespace(returncode=0, stdout=tui["pane"], stderr="")
        if "paste-buffer" in cmd:
            tui["pane"] = "❯ fix the flaky test"
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="message was not delivered"):
        inject_user_message(bridge_dir, content="fix the flaky test")


def test_inject_interrupt_sends_escape_keystroke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    inject_interrupt issues ``tmux send-keys ... Escape`` on the pane.

    Without the ``-l`` flag, tmux interprets ``Escape`` as the key
    name (the single ASCII byte 0x1b). If the flag leaks in or the
    keyword changes, Claude won't see a cancel and the Omnigent stop
    button silently degrades back to a no-op.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    captured: list[list[str]] = []

    class _FakeCompleted:
        """Fake CompletedProcess returned by the patched subprocess.run."""

        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        """
        Record the tmux command argv list.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: A fake CompletedProcess with rc=0.
        """
        del kwargs
        captured.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr("subprocess.run", _fake_run)
    inject_interrupt(bridge_dir)

    # One tmux call: send Escape (no literal flag). If 2+, a stray
    # Enter or extra key was appended; if 0, the call was skipped.
    assert len(captured) == 1, f"Expected 1 tmux send-keys call, got {len(captured)}."
    assert captured[0] == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "send-keys",
        "-t",
        "claude:0.0",
        "Escape",
    ]


def test_inject_interrupt_raises_when_tmux_target_never_published(
    tmp_path: Path,
) -> None:
    """
    inject_interrupt fails loud if tmux.json hasn't been written.

    The runner route catches RuntimeError and returns 503 so the
    Omnigent server falls back to the DBOS cancel path. Swallowing this
    silently would make the stop button appear to work while
    actually doing nothing.
    """
    with pytest.raises(RuntimeError, match="tmux target is not advertised"):
        inject_interrupt(tmp_path / "bridge", timeout_s=0.0)


def test_inject_interrupt_raises_on_tmux_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-zero tmux exit propagates as RuntimeError with stderr context.

    Mirrors :func:`inject_user_message` so the runner route can
    distinguish a bridge-not-ready error from a transport failure
    via the message.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    class _FakeCompleted:
        """Fake CompletedProcess returning a non-zero exit code."""

        returncode = 1
        stdout = ""
        stderr = "no server running on /tmp/example/tmux.sock"

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        """
        Return a fake non-zero CompletedProcess.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: A fake CompletedProcess with rc=1.
        """
        del cmd, kwargs
        return _FakeCompleted()

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="no server running"):
        inject_interrupt(bridge_dir)


def test_kill_session_issues_kill_session_on_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    kill_session issues exactly ``tmux kill-session -t <target>``.

    This is the hard-stop the web UI's "Stop session" affordance
    relies on. If it regressed to ``send-keys`` (the interrupt path)
    the session would merely have its current response cancelled and
    stay alive, so the stop button would silently fail to terminate
    the session. A single kill-session call on the advertised target
    is the only thing that ends the ``claude`` process.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="main",
    )

    captured: list[list[str]] = []

    class _FakeCompleted:
        """Fake CompletedProcess returned by the patched subprocess.run."""

        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        """
        Record the tmux command argv list.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: A fake CompletedProcess with rc=0.
        """
        del kwargs
        captured.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr("subprocess.run", _fake_run)
    kill_session(bridge_dir)

    # Exactly one tmux call: kill-session on the advertised target.
    # If 0, the kill was skipped; if it carried send-keys/-l/a key
    # name, it's the interrupt path and wouldn't terminate the pane.
    assert len(captured) == 1, f"Expected 1 tmux kill-session call, got {len(captured)}."
    assert captured[0] == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "kill-session",
        "-t",
        "main",
    ]


def test_kill_session_raises_when_tmux_target_never_published(
    tmp_path: Path,
) -> None:
    """
    kill_session fails loud if tmux.json was never written.

    The runner handler catches RuntimeError and returns 503 (best
    effort — a missing target means there is no live session to
    kill). Swallowing it silently would make the stop button appear
    to work while doing nothing.
    """
    with pytest.raises(RuntimeError, match="tmux target is not advertised"):
        kill_session(tmp_path / "bridge", timeout_s=0.0)


def test_kill_session_raises_on_tmux_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Non-zero ``tmux kill-session`` exit propagates as a RuntimeError.

    The runner handler maps this to a 503 so a wedged tmux server
    surfaces as a failed stop rather than a silent success.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="main",
    )

    class _FakeCompleted:
        """Fake CompletedProcess returning a non-zero exit code."""

        returncode = 1
        stdout = ""
        stderr = "no server running on /tmp/example/tmux.sock"

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        """
        Return a fake non-zero CompletedProcess.

        :param cmd: Argv list passed to subprocess.run.
        :param kwargs: Subprocess kwargs (ignored).
        :returns: A fake CompletedProcess with rc=1.
        """
        del cmd, kwargs
        return _FakeCompleted()

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(RuntimeError, match="no server running"):
        kill_session(bridge_dir)


def test_inject_slash_command_clears_draft_pastes_literal_then_enter(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Slash commands flow via C-u + literal paste + Enter.

    C-u kills any draft the user is mid-typing — otherwise the paste
    concatenates and Enter submits ``<draft>/effort high``. ``-l`` is
    required so tmux pastes ``/`` and spaces literally; Enter submits.
    """
    bridge_dir = tmp_path / "bridge"
    write_tmux_target(
        bridge_dir,
        socket_path=Path("/tmp/example/tmux.sock"),
        tmux_target="claude:0.0",
    )

    captured: list[list[str]] = []

    class _FakeCompleted:
        """Fake CompletedProcess returned by the patched subprocess.run."""

        returncode = 0
        stdout = ""
        stderr = ""

    def _fake_run(cmd: list[str], **kwargs: object) -> _FakeCompleted:
        """Record one tmux invocation; return rc=0."""
        del kwargs
        captured.append(cmd)
        return _FakeCompleted()

    monkeypatch.setattr("subprocess.run", _fake_run)
    claude_native_bridge.inject_slash_command(bridge_dir, command="/effort high")

    # Three tmux calls in order: C-u (clear), literal paste, Enter.
    # 2 = C-u was dropped (draft can concatenate); 4+ = duplicated send.
    assert len(captured) == 3, (
        f"Expected 3 tmux send-keys calls (C-u + paste + Enter), got {len(captured)}."
    )
    clear, paste, submit = captured
    assert clear == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "send-keys",
        "-t",
        "claude:0.0",
        "C-u",
    ]
    assert paste == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "send-keys",
        "-l",
        "-t",
        "claude:0.0",
        "/effort high",
    ]
    assert submit == [
        "tmux",
        "-S",
        "/tmp/example/tmux.sock",
        "send-keys",
        "-t",
        "claude:0.0",
        "Enter",
    ]


@pytest.mark.parametrize(
    "bad_command",
    [
        "",
        "effort high",  # missing leading slash
        "/effort high\nrm -rf /",  # multi-line
    ],
)
def test_inject_slash_command_rejects_invalid_commands(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    bad_command: str,
) -> None:
    """
    Malformed slash commands raise ValueError before touching tmux.

    No leading ``/`` would type as plain text; a stray newline would
    chain a second command. Validation must fire before the tmux call
    so the route returns 4xx instead of silently appearing to succeed.
    """
    # No tmux.json published — validation must reject before the
    # wait-for-tmux path, proving the check is up front.
    bridge_dir = tmp_path / "bridge"

    def _fake_run(cmd: list[str], **kwargs: object) -> object:
        """Fail the test if tmux is invoked for an invalid command."""
        del cmd, kwargs
        raise AssertionError("subprocess.run must not be called for invalid commands")

    monkeypatch.setattr("subprocess.run", _fake_run)
    with pytest.raises(ValueError):
        claude_native_bridge.inject_slash_command(bridge_dir, command=bad_command)


def test_inject_slash_command_raises_when_tmux_target_never_published(
    tmp_path: Path,
) -> None:
    """
    Missing tmux.json surfaces RuntimeError, mirroring inject_user_message.

    The runner route catches and returns 503, so AP's PATCH still
    succeeds (effort persisted) and the next spawn picks it up.
    """
    with pytest.raises(RuntimeError, match="tmux target is not advertised"):
        claude_native_bridge.inject_slash_command(
            tmp_path / "bridge",
            command="/effort high",
            timeout_s=0.0,
        )


@pytest.mark.asyncio
async def test_channel_server_relays_active_omnigent_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subprocess_bridge_root: Path,
) -> None:
    """
    Active turn tools are advertised to Claude and dispatched through AP.

    This fails if Claude Code can receive web-channel inputs but cannot
    call the Omnigent tools made available to the server-side agent.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", Path("/tmp"))
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", subprocess_bridge_root)
    bridge_dir = prepare_bridge_dir("conv_tools", workspace=tmp_path)
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    calls: list[dict[str, object]] = []

    async def tool_executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        """
        Capture one relayed tool call.

        :param name: Tool name from Claude's MCP request, e.g.
            ``"sys_custom"``.
        :param arguments: Tool arguments from Claude.
        :returns: Structured tool result.
        """
        calls.append({"name": name, "arguments": arguments})
        return {"echo": arguments}

    relay = None
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        _write_json_line(
            proc.stdin,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        initialize = await asyncio.to_thread(_read_json_line, proc.stdout, timeout_s=5.0)
        assert initialize["id"] == 1

        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[
                {
                    "name": "sys_custom",
                    "description": "Test-only active Omnigent tool.",
                    "parameters": {
                        "type": "object",
                        "properties": {"value": {"type": "string"}},
                    },
                }
            ],
            tool_executor=tool_executor,
            loop=asyncio.get_running_loop(),
        )
        post_tools_changed(bridge_dir)
        changed = await asyncio.to_thread(_read_json_line, proc.stdout, timeout_s=5.0)
        assert changed["method"] == "notifications/tools/list_changed"

        _write_json_line(
            proc.stdin,
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
        )
        listed = await asyncio.to_thread(_read_json_line, proc.stdout, timeout_s=5.0)
        listed_tools = listed["result"]["tools"]
        custom_tools = [tool for tool in listed_tools if tool["name"] == "sys_custom"]
        assert custom_tools == [
            {
                "name": "sys_custom",
                "description": "Test-only active Omnigent tool.",
                "inputSchema": {
                    "type": "object",
                    "properties": {"value": {"type": "string"}},
                },
            }
        ]

        _write_json_line(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "sys_custom",
                    "arguments": {"value": "hello"},
                },
            },
        )
        tool_result = await asyncio.to_thread(_read_json_line, proc.stdout, timeout_s=5.0)
        assert tool_result["id"] == 3
        text = tool_result["result"]["content"][0]["text"]
        assert json.loads(text) == {"echo": {"value": "hello"}}
        assert calls == [{"name": "sys_custom", "arguments": {"value": "hello"}}]
    finally:
        if relay is not None:
            relay.close()
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


def test_call_relay_tool_returns_mcp_error_on_read_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A relay POST that times out mid-read returns an MCP error, not a raise.

    Regression test: ``request.urlopen`` raises a bare ``socket.timeout`` /
    ``TimeoutError`` (an ``OSError`` subclass that is NOT a ``URLError``) when
    the relay server accepts the connection but never responds. The previous
    ``except error.URLError`` did not catch it, so the exception propagated up
    through ``_call_mcp_tool`` → ``_stdio_jsonrpc_loop`` and killed the MCP
    server — surfacing to Claude Code as ``-32000: Connection closed``. This
    asserts the call instead yields an ``isError`` MCP result so the stdio
    loop can keep serving.
    """
    started = threading.Event()
    release = threading.Event()

    class _HangingHandler(BaseHTTPRequestHandler):
        """HTTP handler that accepts the request then stalls past the timeout."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Silence the default per-request stderr logging.

            :param format: Unused format string.
            :param args: Unused format args.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """
            Read the request body, then block without replying.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            started.set()
            # Hold the connection open with no response so the client's read
            # times out. Released in the test's finally so the thread exits.
            release.wait(timeout=10.0)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _HangingHandler)
    host, port = httpd.server_address
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()

    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    claude_native_bridge._write_json_file(
        bridge_dir / claude_native_bridge._TOOL_RELAY_FILE,
        {"url": f"http://{host}:{port}", "token": "relay-token", "tools": []},
    )
    # Force a sub-second read timeout so the test is fast; the production
    # value is minutes. The point under test is the exception TYPE handling,
    # not the duration.
    monkeypatch.setattr(claude_native_bridge, "_TOOL_RELAY_POST_TIMEOUT_S", 0.3)

    try:
        result = claude_native_bridge._call_relay_tool(
            bridge_dir, "sys_os_shell", {"command": "echo hi"}
        )
    finally:
        release.set()
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=5.0)

    assert started.is_set(), "relay server never received the POST"
    # The result is a well-formed MCP error result, NOT a raised exception.
    assert result["isError"] is True
    error_text = json.loads(result["content"][0]["text"])["error"]
    assert "failed to call Omnigent tool relay" in error_text


@pytest.mark.asyncio
async def test_serve_mcp_survives_handler_exception_and_keeps_serving(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subprocess_bridge_root: Path,
) -> None:
    """
    An exception handling one request becomes -32603 and the server lives.

    Regression test for the bridge crash: an *uncaught* exception while
    handling ONE ``tools/call`` must not tear down the long-lived stdio MCP
    server. The relay backend returns invalid UTF-8 bytes, so
    ``_call_relay_tool``'s ``resp.read().decode("utf-8")`` raises
    ``UnicodeDecodeError`` — a ``ValueError`` that is NOT an ``OSError``, so
    it escapes the ``except OSError`` clause and propagates out of
    ``_handle_mcp_request``. The loop's per-request guard must convert it into
    a JSON-RPC ``-32603`` error for the offending call, and a SUBSEQUENT
    ``tools/list`` on the same process must still succeed — proving the server
    did not exit (which Claude would otherwise see as
    ``-32000: Connection closed``). Without the guard, the decode error kills
    ``_serve_mcp`` and the ``tools/list`` read below times out.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", Path("/tmp"))
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", subprocess_bridge_root)
    bridge_dir = prepare_bridge_dir("conv_crash", workspace=tmp_path)

    # A relay server that returns HTTP 200 with invalid UTF-8 bytes. This
    # makes _call_relay_tool's resp.read().decode("utf-8") raise
    # UnicodeDecodeError (a ValueError, NOT an OSError), which the function's
    # except OSError does not catch — so the exception propagates up to the
    # stdio loop and exercises the per-request guard under test. The relay
    # tool name must appear in tool_relay.json so _call_mcp_tool routes to
    # _call_relay_tool.
    class _InvalidUtf8Handler(BaseHTTPRequestHandler):
        """HTTP handler that replies 200 with an undecodable body."""

        def log_message(self, format: str, *args: Any) -> None:
            """
            Silence the default per-request stderr logging.

            :param format: Unused format string.
            :param args: Unused format args.
            :returns: None.
            """
            del format, args

        def do_POST(self) -> None:
            """
            Reply with bytes that are not valid UTF-8.

            :returns: None.
            """
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            # 0xFF/0xFE are never valid UTF-8 lead bytes → decode() raises.
            body = b"\xff\xfe\xfa"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _InvalidUtf8Handler)
    host, port = httpd.server_address
    server_thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    server_thread.start()
    claude_native_bridge._write_json_file(
        bridge_dir / claude_native_bridge._TOOL_RELAY_FILE,
        {
            "url": f"http://{host}:{port}",
            "token": "relay-token",
            "tools": [
                {
                    "name": "sys_flaky",
                    "description": "Relay tool whose backend returns undecodable bytes.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
        },
    )

    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "omnigent.claude_native_bridge",
            "serve-mcp",
            "--bridge-dir",
            str(bridge_dir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        assert proc.stdin is not None
        assert proc.stdout is not None
        _write_json_line(
            proc.stdin,
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
        )
        initialize = await asyncio.to_thread(_read_json_line, proc.stdout, timeout_s=5.0)
        assert initialize["id"] == 1

        # Call the flaky relay tool. Decoding the undecodable response raises
        # UnicodeDecodeError, which escapes _call_relay_tool's except OSError
        # and is caught by the loop's per-request guard.
        _write_json_line(
            proc.stdin,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "sys_flaky", "arguments": {}},
            },
        )
        call_result = await asyncio.to_thread(_read_json_line, proc.stdout, timeout_s=5.0)
        assert call_result["id"] == 2
        # The guard reports the uncaught exception as a JSON-RPC -32603
        # ("Internal error") response — NOT a normal MCP error result. This is
        # the assertion that fails if the guard is removed (the call would get
        # no response because the server would have exited).
        assert call_result["error"]["code"] == -32603
        assert "result" not in call_result

        # The decisive assertion: the server is STILL ALIVE and answers the
        # next request. Before the per-request loop guard, the exception above
        # would have exited the process and this read would time out.
        _write_json_line(
            proc.stdin,
            {"jsonrpc": "2.0", "id": 3, "method": "tools/list", "params": {}},
        )
        listed = await asyncio.to_thread(_read_json_line, proc.stdout, timeout_s=5.0)
        assert listed["id"] == 3
        assert "tools" in listed["result"]
    finally:
        httpd.shutdown()
        httpd.server_close()
        server_thread.join(timeout=5.0)
        proc.terminate()
        try:
            proc.wait(timeout=5.0)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5.0)


@pytest.mark.asyncio
async def test_start_tool_relay_accepts_codex_native_bridge_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Relay startup accepts Codex-native's persistent bridge root.

    Codex-native reuses the Claude MCP relay but stores bridge files in
    ``~/.omnigent/codex-native`` instead of Claude's ``/tmp`` bridge
    root. A regression here logs "Failed to start comment relay" and
    leaves Codex without comment/session tools.

    :param tmp_path: Pytest temp directory used as an isolated user
        state parent.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent import codex_native_bridge

    codex_root = tmp_path / ".omnigent" / "codex-native"
    monkeypatch.setattr("omnigent.codex_native_bridge._BRIDGE_ROOT", codex_root)
    bridge_dir = codex_native_bridge.prepare_bridge_dir("conv_codex")
    relay_file = bridge_dir / claude_native_bridge._TOOL_RELAY_FILE

    async def _executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        """
        Return an empty result for the unused relay tool callback.

        :param name: Tool name, e.g. ``"list_comments"``.
        :param arguments: Tool arguments, e.g. ``{"status": "pending"}``.
        :returns: Empty tool result.
        """
        del name, arguments
        return {}

    relay = None
    try:
        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[
                {
                    "name": "list_comments",
                    "description": "List comments.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            tool_executor=_executor,
            loop=asyncio.get_running_loop(),
        )

        assert relay_file.exists(), (
            "Codex-native relay did not write tool_relay.json under the persistent bridge root"
        )
        relay_info = json.loads(relay_file.read_text(encoding="utf-8"))
        assert relay_info["tools"][0]["name"] == "list_comments"
    finally:
        if relay is not None:
            relay.close()


@pytest.mark.asyncio
async def test_start_tool_relay_accepts_antigravity_native_bridge_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Relay startup accepts Antigravity-native's persistent bridge root (#1194).

    Antigravity-native reuses the Claude MCP relay but stores bridge files in
    ``~/.omnigent/antigravity-native`` (the same ``$HOME/.omnigent/<harness>``
    shape codex uses). A regression in :func:`_trusted_parent_for_bridge_dir`
    would reject the bridge dir, the relay would fail to write
    ``tool_relay.json``, and the wrapped agy would get no ``sys_*`` tools.

    :param tmp_path: Pytest temp directory used as an isolated user
        state parent.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent import antigravity_native_bridge

    antigravity_root = tmp_path / ".omnigent" / "antigravity-native"
    monkeypatch.setattr("omnigent.antigravity_native_bridge._BRIDGE_ROOT", antigravity_root)
    bridge_dir = antigravity_native_bridge.prepare_bridge_dir("conv_agy")
    relay_file = bridge_dir / claude_native_bridge._TOOL_RELAY_FILE

    async def _executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        """
        Return an empty result for the unused relay tool callback.

        :param name: Tool name, e.g. ``"sys_session_list"``.
        :param arguments: Tool arguments.
        :returns: Empty tool result.
        """
        del name, arguments
        return {}

    relay = None
    try:
        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[
                {
                    "name": "sys_session_create",
                    "description": "Spawn an Omnigent sub-agent session.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            tool_executor=_executor,
            loop=asyncio.get_running_loop(),
        )

        assert relay_file.exists(), (
            "Antigravity-native relay did not write tool_relay.json under the "
            "persistent bridge root"
        )
        relay_info = json.loads(relay_file.read_text(encoding="utf-8"))
        assert relay_info["tools"][0]["name"] == "sys_session_create"
    finally:
        if relay is not None:
            relay.close()


@pytest.mark.asyncio
async def test_start_tool_relay_accepts_opencode_native_bridge_root(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Relay startup accepts OpenCode-native's persistent bridge root.

    opencode-native reuses the Claude MCP relay but stores bridge files in
    ``~/.omnigent/opencode-native`` (the same ``$HOME/.omnigent/<harness>``
    shape codex/antigravity use). The missing allowlist entry made ``serve-mcp``
    crash on startup (``_ensure_secure_dir`` → "not under an allowed bridge
    root"), which opencode surfaced as ``MCP error -32000: Connection closed``
    and the wrapped opencode got no ``sys_*`` tools. Guards the regression.

    :param tmp_path: Pytest temp directory used as an isolated user state parent.
    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    from omnigent import opencode_native_bridge

    opencode_root = tmp_path / ".omnigent" / "opencode-native"
    monkeypatch.setattr("omnigent.opencode_native_bridge._BRIDGE_ROOT", opencode_root)
    bridge_dir = opencode_native_bridge.prepare_bridge_dir("conv_oc")
    relay_file = bridge_dir / claude_native_bridge._TOOL_RELAY_FILE

    async def _executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        """Return an empty result for the unused relay tool callback."""
        del name, arguments
        return {}

    relay = None
    try:
        relay = start_tool_relay(
            bridge_dir=bridge_dir,
            tools=[
                {
                    "name": "sys_session_list",
                    "description": "List Omnigent sessions.",
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            tool_executor=_executor,
            loop=asyncio.get_running_loop(),
        )
        assert relay_file.exists(), (
            "OpenCode-native relay did not write tool_relay.json under the persistent bridge root"
        )
        relay_info = json.loads(relay_file.read_text(encoding="utf-8"))
        assert relay_info["tools"][0]["name"] == "sys_session_list"
    finally:
        if relay is not None:
            relay.close()


@pytest.mark.asyncio
async def test_relay_close_keeps_advertisement_owned_by_newer_relay(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    subprocess_bridge_root: Path,
) -> None:
    """
    Closing a superseded relay must not delete a newer relay's advertisement.

    Fork/clear/resume sessions keep the same ``bridge_id`` — hence the same
    bridge dir and ``tool_relay.json``. When a newer session starts its relay
    it overwrites the file with its own token; closing the older session's
    relay (e.g. on session delete) must recognise it no longer owns the file
    and leave it in place. Unconditional unlinking here would erase the
    still-active newer session's ``list_comments`` / ``update_comment``.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", Path("/tmp"))
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", subprocess_bridge_root)
    bridge_dir = prepare_bridge_dir("conv_shared_bridge", workspace=tmp_path)
    relay_file = bridge_dir / claude_native_bridge._TOOL_RELAY_FILE

    async def _executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        """
        Unused executor — this test only exercises advertisement lifecycle.

        :param name: Tool name (unused).
        :param arguments: Tool arguments (unused).
        :returns: Empty result dict.
        """
        del name, arguments
        return {}

    tools = [
        {
            "name": "list_comments",
            "description": "",
            "parameters": {"type": "object", "properties": {}},
        }
    ]
    loop = asyncio.get_running_loop()

    old_relay = None
    new_relay = None
    try:
        old_relay = start_tool_relay(
            bridge_dir=bridge_dir, tools=tools, tool_executor=_executor, loop=loop
        )
        old_token = json.loads(relay_file.read_text())["token"]

        # A second relay on the same bridge dir overwrites the advertisement
        # with its own token — the fork/clear/resume reuse of one bridge_id.
        new_relay = start_tool_relay(
            bridge_dir=bridge_dir, tools=tools, tool_executor=_executor, loop=loop
        )
        new_token = json.loads(relay_file.read_text())["token"]
        # Tokens are random per relay; equality would mean start_tool_relay did
        # not rebind, collapsing the two-relay premise of this test.
        assert new_token != old_token, "second relay did not rewrite the token"

        # Closing the OLD relay must NOT remove the file the NEW relay owns.
        old_relay.close()
        assert relay_file.exists(), (
            "old relay.close() deleted tool_relay.json now owned by the newer "
            "relay — the still-active session would lose its comment tools"
        )
        # The file still advertises the NEW relay, untouched by the old close.
        assert json.loads(relay_file.read_text())["token"] == new_token, (
            "old relay.close() mutated the newer relay's advertisement"
        )

        # The owning relay's close() does remove its own advertisement.
        new_relay.close()
        assert not relay_file.exists(), (
            "new relay.close() left a stale tool_relay.json after the owning relay shut down"
        )
    finally:
        # close() is safe to call twice (shutdown is idempotent once the
        # server has stopped); guarantees both sockets are released on failure.
        if old_relay is not None:
            old_relay.close()
        if new_relay is not None:
            new_relay.close()


def _write_json_line(handle: TextIO, payload: dict[str, object]) -> None:
    """
    Write one JSON-RPC line to a subprocess stdin stream.

    :param handle: Text stdin handle returned by
        :class:`subprocess.Popen`.
    :param payload: JSON-compatible object to write.
    :returns: None.
    """
    handle.write(json.dumps(payload) + "\n")
    handle.flush()


def _read_json_line(handle: TextIO, *, timeout_s: float) -> dict[str, object]:
    """
    Read one JSON line from a subprocess stdout stream.

    :param handle: Text stdout handle returned by
        :class:`subprocess.Popen`.
    :param timeout_s: Seconds to wait, e.g. ``5.0``.
    :returns: Parsed JSON object.
    :raises TimeoutError: If no line is available before the
        timeout.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        ready, _write, _error = select.select([handle], [], [], 0.05)
        if not ready:
            continue
        line = handle.readline()
        if line:
            payload = json.loads(line)
            assert isinstance(payload, dict)
            return payload
    raise TimeoutError("subprocess did not emit a JSON line")


def test_usage_from_transcript_entry_sums_context_tokens() -> None:
    """
    Context-token count must sum the three input-side fields.

    The "context tokens" exposed to web's input-composer ring is
    ``input_tokens + cache_creation_input_tokens +
    cache_read_input_tokens``. ``output_tokens`` is generated within
    the same call and does NOT count toward the next prompt's size,
    so it must be reported separately, not folded into the sum.
    """
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 6,
                "cache_creation_input_tokens": 22967,
                "cache_read_input_tokens": 21595,
                "output_tokens": 554,
            },
        },
    }

    usage = claude_native_bridge._usage_from_transcript_entry(entry)

    assert usage == {
        "context_tokens": 6 + 22967 + 21595,
        "input_tokens": 6,
        "output_tokens": 554,
        "cache_creation_input_tokens": 22967,
        "cache_read_input_tokens": 21595,
    }


def test_usage_from_transcript_entry_returns_none_for_non_assistant() -> None:
    """User-role entries do not carry ``message.usage``; helper returns ``None``."""
    assert (
        claude_native_bridge._usage_from_transcript_entry(
            {"type": "user", "message": {"role": "user", "content": "hi"}}
        )
        is None
    )


def test_usage_from_transcript_entry_tolerates_missing_cache_fields() -> None:
    """
    Old transcript shapes without cache_creation/cache_read still parse.

    Treats absent cache fields as zero so the helper degrades gracefully
    on transcripts written by Claude versions predating prompt caching.
    """
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {"input_tokens": 100, "output_tokens": 20},
        },
    }
    usage = claude_native_bridge._usage_from_transcript_entry(entry)
    assert usage == {"context_tokens": 100, "input_tokens": 100, "output_tokens": 20}


def test_usage_from_transcript_entry_forwards_cache_keys_for_cost() -> None:
    """
    Cache-read and cache-creation token counts must appear in the
    returned dict so ``compute_llm_cost`` can bill them at their
    cheaper per-token rates.

    Without this, cached tokens are invisible to cost computation
    and get implicitly billed at the full input rate (or not at all,
    depending on how ``input_tokens`` was populated upstream).
    """
    entry = {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "text", "text": "ok"}],
            "usage": {
                "input_tokens": 50,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 8000,
                "output_tokens": 200,
            },
        },
    }
    usage = claude_native_bridge._usage_from_transcript_entry(entry)
    # cache_read present, cache_creation absent (zero is omitted).
    assert usage is not None
    assert usage["cache_read_input_tokens"] == 8000
    assert "cache_creation_input_tokens" not in usage
    assert usage["input_tokens"] == 50
    assert usage["output_tokens"] == 200
    assert usage["context_tokens"] == 50 + 8000


def test_read_transcript_items_from_offset_returns_latest_usage(tmp_path: Path) -> None:
    """
    The transcript reader exposes the most recent assistant ``usage``.

    Two assistant entries are written; the last one's usage must win
    so the input-composer ring tracks the *current* prompt size, not a
    stale earlier value.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "one"}],
                            "usage": {
                                "input_tokens": 10,
                                "cache_creation_input_tokens": 100,
                                "cache_read_input_tokens": 0,
                                "output_tokens": 5,
                            },
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a2",
                        "message": {
                            "role": "assistant",
                            "content": [{"type": "text", "text": "two"}],
                            "usage": {
                                "input_tokens": 20,
                                "cache_creation_input_tokens": 50,
                                "cache_read_input_tokens": 200,
                                "output_tokens": 7,
                            },
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = read_transcript_items_from_offset(
        transcript_path, 0, start_line=0, agent_name="claude-native-ui"
    )

    assert result.latest_usage == {
        "context_tokens": 20 + 50 + 200,
        "input_tokens": 20,
        "output_tokens": 7,
        "cache_creation_input_tokens": 50,
        "cache_read_input_tokens": 200,
    }


def test_read_transcript_items_from_offset_latest_usage_none_when_user_only(
    tmp_path: Path,
) -> None:
    """A batch of user-only entries leaves ``latest_usage`` / ``latest_model`` as ``None``."""
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        json.dumps({"type": "user", "message": {"role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8",
    )

    result = read_transcript_items_from_offset(
        transcript_path, 0, start_line=0, agent_name="claude-native-ui"
    )

    assert result.latest_usage is None
    assert result.latest_model is None


def test_read_transcript_items_from_offset_returns_latest_model(
    tmp_path: Path,
) -> None:
    """
    The transcript reader exposes ``message.model`` from the latest assistant turn.

    Surfaced for diagnostics / future use; the ring's denominator now
    comes from the statusLine stdin (see
    :func:`read_claude_context_state`), not a model lookup.
    """
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a1",
                        "message": {
                            "role": "assistant",
                            "model": "claude-sonnet-4-6",
                            "content": [{"type": "text", "text": "one"}],
                        },
                    }
                ),
                json.dumps(
                    {
                        "type": "assistant",
                        "uuid": "a2",
                        "message": {
                            "role": "assistant",
                            "model": "claude-opus-4-7",
                            "content": [{"type": "text", "text": "two"}],
                        },
                    }
                ),
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    result = read_transcript_items_from_offset(
        transcript_path, 0, start_line=0, agent_name="claude-native-ui"
    )

    assert result.latest_model == "claude-opus-4-7"


def test_read_claude_context_state_returns_parsed_payload(tmp_path: Path) -> None:
    """
    ``context.json`` round-trips into a dict with both fields.

    The statusLine wrapper writes this file every Claude Code render
    tick; the forwarder reads it on each poll to drive the ring.
    Drift here silently breaks the ring on every claude-native chat.
    """
    bridge_dir = tmp_path
    payload = {
        "context_window_size": 1_000_000,
        "current_usage": {
            "input_tokens": 6,
            "cache_creation_input_tokens": 22967,
            "cache_read_input_tokens": 21595,
            "output_tokens": 554,
        },
    }
    (bridge_dir / "context.json").write_text(json.dumps(payload), encoding="utf-8")

    state = claude_native_bridge.read_claude_context_state(bridge_dir)

    assert state is not None
    assert state["context_window_size"] == 1_000_000
    assert state["current_usage"]["input_tokens"] == 6


def test_read_claude_context_state_returns_none_when_missing(tmp_path: Path) -> None:
    """A missing context.json must not raise — forwarder treats as no update."""
    assert claude_native_bridge.read_claude_context_state(tmp_path) is None


def test_read_claude_context_state_rejects_non_positive_window(tmp_path: Path) -> None:
    """
    A non-positive ``context_window_size`` is dropped.

    Defends the ring's denominator: zero would NaN the math, negative
    would render a backwards arc. Either is worse than no update.
    """
    bridge_dir = tmp_path
    (bridge_dir / "context.json").write_text(
        json.dumps({"context_window_size": 0}), encoding="utf-8"
    )
    assert claude_native_bridge.read_claude_context_state(bridge_dir) is None


def test_read_user_status_line_command_returns_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    The user's global statusLine.command is returned for chaining.

    The wrapper invokes it post-capture so claude-hud / the user's
    custom status bar still renders for omnigent-launched sessions.
    """
    fake_home = tmp_path
    settings = fake_home / ".claude" / "settings.json"
    settings.parent.mkdir(parents=True)
    settings.write_text(
        json.dumps({"statusLine": {"type": "command", "command": "bun run hud"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_native_bridge, "_USER_CLAUDE_SETTINGS_PATH", settings)

    assert claude_native_bridge.read_user_status_line_command() == "bun run hud"


def test_read_user_status_line_command_returns_none_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No global statusLine → chain is omitted; wrapper prints nothing extra."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    monkeypatch.setattr(claude_native_bridge, "_USER_CLAUDE_SETTINGS_PATH", settings)
    assert claude_native_bridge.read_user_status_line_command() is None


@pytest.mark.parametrize("effort", sorted(CLAUDE_EFFORTS))
def test_read_user_effort_level_returns_configured_level(
    effort: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A recognized ``effortLevel`` is returned, to stamp on the session row."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"effortLevel": effort}), encoding="utf-8")
    monkeypatch.setattr(claude_native_bridge, "_USER_CLAUDE_SETTINGS_PATH", settings)
    assert claude_native_bridge.read_user_effort_level() == effort


def test_read_user_effort_level_returns_none_when_unset(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No ``effortLevel`` → None, so creation omits reasoning_effort entirely."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"model": "opus"}), encoding="utf-8")
    monkeypatch.setattr(claude_native_bridge, "_USER_CLAUDE_SETTINGS_PATH", settings)
    assert claude_native_bridge.read_user_effort_level() is None


def test_read_user_effort_level_rejects_unrecognized_value(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unrecognized value is treated as unset (fail-soft, so launch never 400s)."""
    settings = tmp_path / "settings.json"
    settings.write_text(json.dumps({"effortLevel": "ultra"}), encoding="utf-8")
    monkeypatch.setattr(claude_native_bridge, "_USER_CLAUDE_SETTINGS_PATH", settings)
    assert claude_native_bridge.read_user_effort_level() is None


def test_read_user_effort_level_returns_none_when_settings_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Missing settings.json → None; absence of config must not raise."""
    missing = tmp_path / "does-not-exist" / "settings.json"
    monkeypatch.setattr(claude_native_bridge, "_USER_CLAUDE_SETTINGS_PATH", missing)
    assert claude_native_bridge.read_user_effort_level() is None


# ---------------------------------------------------------------------------
# launch_model storage and retrieval
# ---------------------------------------------------------------------------


def test_prepare_bridge_dir_stores_launch_model(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launch_model`` is persisted in bridge.json when provided."""
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)

    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        workspace=tmp_path,
        launch_model="databricks-claude-opus-4-7",
    )
    config = json.loads((bridge_dir / "bridge.json").read_text(encoding="utf-8"))
    assert config["launch_model"] == "databricks-claude-opus-4-7"


def test_prepare_bridge_dir_omits_launch_model_when_none(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``launch_model`` key is absent from bridge.json when not provided."""
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)

    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        workspace=tmp_path,
    )
    config = json.loads((bridge_dir / "bridge.json").read_text(encoding="utf-8"))
    assert "launch_model" not in config


def test_read_launch_model_returns_stored_value(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_launch_model`` round-trips the value stored by ``prepare_bridge_dir``."""
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)

    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        workspace=tmp_path,
        launch_model="databricks-claude-sonnet-4-6",
    )
    assert read_launch_model(bridge_dir) == "databricks-claude-sonnet-4-6"


def test_read_launch_model_returns_none_when_absent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``read_launch_model`` returns ``None`` when no launch model was stored."""
    root = tmp_path / "root"
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)

    bridge_dir = prepare_bridge_dir(
        "conv_abc",
        workspace=tmp_path,
    )
    assert read_launch_model(bridge_dir) is None


def test_read_launch_model_returns_none_for_missing_bridge_dir(
    tmp_path: Path,
) -> None:
    """``read_launch_model`` returns ``None`` for a nonexistent bridge dir."""
    assert read_launch_model(tmp_path / "nonexistent") is None


# ── _hook_record_from_jsonl_record: task/todo event parsing ──────────────────


def _make_jsonl_record(
    payload: dict[str, Any],
    *,
    line_number: int = 1,
) -> _JsonlRecord:
    """
    Build a ``_JsonlRecord`` wrapping ``payload`` in the hook JSONL envelope.

    :param payload: Hook payload dict (written under the ``payload`` key).
    :param line_number: Synthetic line number for the record.
    :returns: A ``_JsonlRecord`` with correct byte offsets and encoded text.
    """
    text = json.dumps({"payload": payload}) + "\n"
    return _JsonlRecord(
        line_number=line_number,
        byte_offset=0,
        next_byte_offset=len(text.encode()),
        text=text,
    )


def test_hook_record_parses_todo_write_todos() -> None:
    """
    ``PostToolUse/TodoWrite`` → ``record.todos`` is the items list.

    This fails if the ``tool_input.todos`` extraction path is broken or
    the dict-only filter is removed (non-dict entries would leak through).
    """
    record = _hook_record_from_jsonl_record(
        _make_jsonl_record(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "TodoWrite",
                "tool_input": {
                    "todos": [
                        {
                            "content": "Fix bug",
                            "status": "in_progress",
                            "activeForm": "Fixing bug",
                        },
                        "not-a-dict",  # must be filtered out
                    ],
                },
            }
        )
    )
    assert record.event_name == "PostToolUse"
    # Only the dict entry survives; the string is dropped.
    assert record.todos == [
        {"content": "Fix bug", "status": "in_progress", "activeForm": "Fixing bug"}
    ]
    assert record.task_id is None
    assert record.task_status is None


def test_hook_record_parses_task_update() -> None:
    """
    ``PostToolUse/TaskUpdate`` → ``record.task_id`` and ``record.task_status``.

    This fails if the ``taskId``/``status`` extraction from
    ``tool_input`` is broken or if the field names change.
    """
    record = _hook_record_from_jsonl_record(
        _make_jsonl_record(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "TaskUpdate",
                "tool_input": {"taskId": "42", "status": "in_progress"},
            }
        )
    )
    assert record.event_name == "PostToolUse"
    assert record.task_id == "42"
    assert record.task_status == "in_progress"
    assert record.todos is None


def test_hook_record_parses_task_created() -> None:
    """
    ``TaskCreated`` → ``task_id``, ``task_subject``, ``task_status == "pending"``.

    This fails if the field names change or the hardcoded "pending"
    assignment is removed.
    """
    record = _hook_record_from_jsonl_record(
        _make_jsonl_record(
            {
                "hook_event_name": "TaskCreated",
                "task_id": "7",
                "task_subject": "Write integration tests",
            }
        )
    )
    assert record.event_name == "TaskCreated"
    assert record.task_id == "7"
    assert record.task_subject == "Write integration tests"
    # TaskCreated always sets status to "pending" regardless of payload.
    assert record.task_status == "pending"
    assert record.todos is None


def test_hook_record_parses_task_completed() -> None:
    """
    ``TaskCompleted`` → ``task_id``, ``task_status == "completed"``.

    This fails if the hardcoded "completed" assignment is removed or if
    task_id extraction from the payload breaks.
    """
    record = _hook_record_from_jsonl_record(
        _make_jsonl_record(
            {
                "hook_event_name": "TaskCompleted",
                "task_id": "7",
            }
        )
    )
    assert record.event_name == "TaskCompleted"
    assert record.task_id == "7"
    assert record.task_status == "completed"
    assert record.task_subject is None


def test_hook_record_task_created_with_int_task_id_gives_none() -> None:
    """
    Malformed ``TaskCreated`` with a numeric ``task_id`` → ``task_id`` is ``None``.

    Ensures the ``isinstance(raw_task_id, str)`` guard prevents integer ids
    from leaking into the accumulation maps (which key by str).
    """
    record = _hook_record_from_jsonl_record(
        _make_jsonl_record(
            {
                "hook_event_name": "TaskCreated",
                "task_id": 99,  # int, not str
                "task_subject": "Some task",
            }
        )
    )
    assert record.task_id is None
    assert record.task_subject == "Some task"
    # task_status is still set even when task_id is absent.
    assert record.task_status == "pending"


def test_hook_record_task_update_with_missing_task_id_gives_none() -> None:
    """
    ``PostToolUse/TaskUpdate`` with no ``taskId`` → ``task_id`` is ``None``.

    Ensures the forwarder safely ignores updates for tasks whose id
    cannot be extracted (they would silently create orphaned map entries
    if the None guard were absent).
    """
    record = _hook_record_from_jsonl_record(
        _make_jsonl_record(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "TaskUpdate",
                "tool_input": {"status": "completed"},  # taskId missing
            }
        )
    )
    assert record.task_id is None
    assert record.task_status == "completed"


def test_hook_record_todo_write_with_non_list_todos_gives_none() -> None:
    """
    ``PostToolUse/TodoWrite`` with ``tool_input.todos`` as a non-list → ``todos`` is ``None``.

    Protects against forwarder crashing when a malformed hook payload
    arrives with an unexpected ``todos`` shape.
    """
    record = _hook_record_from_jsonl_record(
        _make_jsonl_record(
            {
                "hook_event_name": "PostToolUse",
                "tool_name": "TodoWrite",
                "tool_input": {"todos": "not-a-list"},
            }
        )
    )
    assert record.todos is None


# ── stop_hook_seen_since: subagent filtering ─────────────────────────


def test_stop_hook_seen_since_ignores_subagent_stop(
    tmp_path: Path,
) -> None:
    """
    ``stop_hook_seen_since`` must skip subagent ``Stop`` events.

    When a Claude Code subagent (spawned via the Agent tool) finishes,
    its ``Stop`` hook lands in the same ``hooks.jsonl`` as the parent's.
    The subagent's ``transcript_path`` contains a ``subagents/``
    component — ``stop_hook_seen_since`` uses this to distinguish it
    from a parent stop. Without this filter, the tmux message injection
    path prematurely considers the parent turn complete.
    """
    bridge_dir = tmp_path / "bridge"
    transcript_path = tmp_path / "session.jsonl"
    transcript_path.write_text("", encoding="utf-8")
    subagent_transcript = tmp_path / "session" / "subagents" / "agent-abc.jsonl"
    subagent_transcript.parent.mkdir(parents=True, exist_ok=True)
    subagent_transcript.write_text("", encoding="utf-8")

    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent",
            "transcript_path": str(transcript_path),
        },
    )
    cursor = 1
    # Subagent Stop — must not be detected.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "subagent",
            "transcript_path": str(subagent_transcript),
        },
    )
    assert not stop_hook_seen_since(bridge_dir, cursor)

    # Parent Stop — must be detected.
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "parent",
            "transcript_path": str(transcript_path),
        },
    )
    assert stop_hook_seen_since(bridge_dir, cursor)


def test_stop_hook_seen_since_detects_stop_without_transcript_path(
    tmp_path: Path,
) -> None:
    """
    ``stop_hook_seen_since`` treats ``Stop`` without a transcript path as a parent event.

    If a hook payload omits ``transcript_path`` (unusual but possible),
    the conservative default is to treat it as a parent event so the
    turn-complete signal is not lost.
    """
    bridge_dir = tmp_path / "bridge"
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "SessionStart",
            "session_id": "parent",
        },
    )
    record_hook_event(
        bridge_dir,
        {
            "hook_event_name": "Stop",
            "session_id": "parent",
        },
    )
    assert stop_hook_seen_since(bridge_dir, 1)


# ── ensure_claude_workspace_trusted ────────────────────


def _redirect_home(monkeypatch: pytest.MonkeyPatch, home: Path) -> Path:
    """
    Point ``Path.home()`` (and thus ``~/.claude.json``) at a temp dir.

    ``Path.home()`` resolves ``~`` via ``$HOME`` on POSIX, so setting the
    env var redirects the helper's reads/writes to *home* without
    patching any production internals.

    :param monkeypatch: Pytest monkeypatch fixture.
    :param home: Temp directory to use as the fake home, e.g.
        ``tmp_path / "home"``.
    :returns: The ``.claude.json`` path inside *home*.
    """
    home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("HOME", str(home))
    assert Path.home() == home  # guards against env-resolution surprises
    return home / ".claude.json"


def test_ensure_trusted_creates_config_when_missing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    A fresh host (no ``~/.claude.json``) gets both first-run gates set.

    This is the core workspace-trust case: an ``omnigent host`` machine that
    has never run Claude interactively. Both the global onboarding gate
    and the per-workspace trust gate must be pre-accepted so Claude does
    not block on its TUI prompts.
    """
    config_path = _redirect_home(monkeypatch, tmp_path / "home")
    workspace = tmp_path / "worktrees" / "feature-x"
    workspace.mkdir(parents=True)

    ensure_claude_workspace_trusted(workspace)

    data = json.loads(config_path.read_text())
    # Global onboarding gate — without this Claude shows the theme/login
    # flow on a never-onboarded machine.
    assert data["hasCompletedOnboarding"] is True
    # Per-directory trust gate, keyed by the RESOLVED absolute path —
    # without this Claude shows "Do you trust the files in this folder?".
    assert data["projects"][str(workspace.resolve())]["hasTrustDialogAccepted"] is True


def test_ensure_trusted_preserves_existing_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Seeding adds only the two gates; all other config is preserved.

    The file holds the user's OAuth account, MCP config, and per-project
    history. Clobbering any of it would be a regression, so this asserts
    an unrelated top-level key and a sibling project entry survive
    untouched while the new workspace's trust gate is added.
    """
    config_path = _redirect_home(monkeypatch, tmp_path / "home")
    other_workspace = tmp_path / "repo"
    other_workspace.mkdir()
    existing = {
        "oauthAccount": {"emailAddress": "user@example.com"},
        "hasCompletedOnboarding": True,
        "projects": {
            str(other_workspace.resolve()): {
                "hasTrustDialogAccepted": True,
                "lastSessionId": "sess_existing",
            },
        },
    }
    config_path.write_text(json.dumps(existing))

    workspace = tmp_path / "worktrees" / "feature-y"
    workspace.mkdir(parents=True)
    ensure_claude_workspace_trusted(workspace)

    data = json.loads(config_path.read_text())
    # Unrelated top-level state survives — proves we merge, not overwrite.
    assert data["oauthAccount"] == {"emailAddress": "user@example.com"}
    # The pre-existing sibling project is untouched, including its own
    # non-trust keys (a naive ``projects = {key: {...}}`` would drop it).
    assert data["projects"][str(other_workspace.resolve())] == {
        "hasTrustDialogAccepted": True,
        "lastSessionId": "sess_existing",
    }
    # The new workspace's trust gate was added alongside it.
    assert data["projects"][str(workspace.resolve())]["hasTrustDialogAccepted"] is True


def test_ensure_trusted_idempotent_does_not_rewrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    When both gates are already set, the file is left byte-for-byte alone.

    The seed file is written in compact form; the helper's writer uses
    two-space indentation. So if the ``if not changed: return`` short-
    circuit were removed, the file would be reformatted and the bytes
    would differ — this test would then fail, proving it guards the
    no-op path (not merely that the values end up correct).
    """
    config_path = _redirect_home(monkeypatch, tmp_path / "home")
    workspace = tmp_path / "worktrees" / "feature-z"
    workspace.mkdir(parents=True)
    already = {
        "hasCompletedOnboarding": True,
        "projects": {str(workspace.resolve()): {"hasTrustDialogAccepted": True}},
    }
    # Compact, no indentation — distinct from the helper's indent=2 output.
    config_path.write_text(json.dumps(already, separators=(",", ":")))
    before = config_path.read_bytes()

    ensure_claude_workspace_trusted(workspace)

    # Byte-identical → the helper detected no change and never wrote.
    assert config_path.read_bytes() == before


@pytest.mark.parametrize(
    "raw,expected_exc",
    [
        # Invalid JSON — a half-written or corrupt file. Must fail loud,
        # never be silently replaced (losing the user's real config).
        ("{not valid json", json.JSONDecodeError),
        # Valid JSON but not an object (e.g. a stray list). Claude never
        # writes this shape, so refuse rather than coerce it.
        ("[1, 2, 3]", ValueError),
    ],
)
def test_ensure_trusted_refuses_malformed_config(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    raw: str,
    expected_exc: type[Exception],
) -> None:
    """
    A malformed ``~/.claude.json`` raises and is left untouched.

    Fail-loud per project conventions: silently overwriting would
    destroy whatever the user actually had on disk.
    """
    config_path = _redirect_home(monkeypatch, tmp_path / "home")
    config_path.write_text(raw)
    workspace = tmp_path / "ws"
    workspace.mkdir()

    with pytest.raises(expected_exc):
        ensure_claude_workspace_trusted(workspace)

    # The original (malformed) bytes are preserved — no clobber occurred.
    assert config_path.read_text() == raw


def test_display_cost_approval_popup_builds_detached_tmux_command(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    Fires a detached ``display-popup`` at the advertised pane running the
    cost-popup module with all resolve inputs.

    Proves the modal targets the right tmux socket + pane + attached
    client (``-c``), launches :mod:`omnigent.native_cost_popup`, and
    forwards the session/elicitation/message plus THIS bridge's
    ``permission_hook.json`` (where the popup reads the Omnigent url/token). A
    failure means native approval would render at the wrong pane/client,
    run the wrong program, or omit an input the resolve POST needs — i.e.
    it silently wouldn't work.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "tmux.json").write_text(
        json.dumps({"socket_path": "/tmp/x.sock", "tmux_target": "claude:0.0"}),
        encoding="utf-8",
    )
    # One client attached — display-popup must target it via ``-c`` since
    # the runner invoking the helper is not itself a tmux client.
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda _s, _t: ["/dev/pts/9"])

    captured: dict[str, Any] = {}

    class _FakePopen:
        """Records the argv/kwargs; deliberately has no ``wait()`` so the
        test fails loud if the helper ever blocks on the popup."""

        def __init__(self, args: list[str], **kwargs: Any) -> None:
            captured["args"] = args
            captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    display_cost_approval_popup(
        bridge_dir,
        session_id="conv_abc123",
        elicitation_id="elicit_deadbeef",
        message="Cost $0.12 crossed the $0.10 checkpoint. Continue?",
        timeout_s=1.0,
    )

    args = captured["args"]
    # Targets the advertised socket + pane via display-popup -E (so the
    # popup closes when the script exits).
    assert args[:5] == ["tmux", "-S", "/tmp/x.sock", "display-popup", "-E"]
    # Must target the attached client explicitly; without -c the runner
    # (not a tmux client) gets "no current client" and nothing renders.
    assert args[args.index("-c") + 1] == "/dev/pts/9"
    # The pane comes from tmux.json; a wrong target would render nowhere.
    assert args[args.index("-t") + 1] == "claude:0.0"
    # Inner command runs the popup module with every resolve input.
    inner = shlex.split(args[-1])
    assert "omnigent.native_cost_popup" in inner
    assert "conv_abc123" in inner  # --session-id value
    assert "elicit_deadbeef" in inner  # --elicitation-id value
    assert "Cost $0.12 crossed the $0.10 checkpoint. Continue?" in inner  # --message
    # AP-routing config must point at THIS bridge's permission_hook.json so
    # the popup reads the matching ap_server_url + auth headers.
    cfg = inner[inner.index("--config-file") + 1]
    assert cfg.endswith("permission_hook.json")
    assert str(bridge_dir) in cfg
    # Detached, non-interactive stdio — the runner handler must not inherit
    # a tty or block; DEVNULL on all three proves fire-and-forget.
    assert captured["kwargs"]["stdin"] == subprocess.DEVNULL
    assert captured["kwargs"]["stdout"] == subprocess.DEVNULL
    assert captured["kwargs"]["stderr"] == subprocess.DEVNULL


def test_display_cost_approval_popup_skips_when_no_client_attached(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    With the pane advertised but NO client attached, no popup is fired.

    A ``display-popup`` needs an attached client to render on; with none
    (e.g. the web Terminal tab closed) tmux would error "no current
    client". The helper must skip cleanly — NOT spawn a doomed ``tmux`` —
    leaving the web ApprovalCard as the answer surface. A regression here
    (spawning anyway) means noisy failed popups and, worse, masks that the
    real surface is the web card.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    (bridge_dir / "tmux.json").write_text(
        json.dumps({"socket_path": "/tmp/x.sock", "tmux_target": "claude:0.0"}),
        encoding="utf-8",
    )
    # No client attached.
    monkeypatch.setattr(native_cost_popup, "_list_tmux_clients", lambda _s, _t: [])

    spawned: list[list[str]] = []

    class _FakePopen:
        def __init__(self, args: list[str], **kwargs: Any) -> None:
            spawned.append(args)

    monkeypatch.setattr(subprocess, "Popen", _FakePopen)

    display_cost_approval_popup(
        bridge_dir,
        session_id="conv_abc123",
        elicitation_id="elicit_x",
        message="msg",
        timeout_s=1.0,
    )

    # No popup spawned when there is no client to render it on.
    assert spawned == []


def test_display_cost_approval_popup_raises_when_pane_not_advertised(
    tmp_path: Path,
) -> None:
    """
    With no ``tmux.json`` (pane not attached), the helper raises.

    The runner handler catches this as a best-effort miss so the web
    ApprovalCard stays the answer surface; it must NOT fire a popup at a
    nonexistent pane. A pass-through (no raise) would mean the runner
    couldn't tell the pane was absent.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    with pytest.raises(RuntimeError):
        display_cost_approval_popup(
            bridge_dir,
            session_id="conv_abc123",
            elicitation_id="elicit_x",
            message="msg",
            timeout_s=0.05,
        )


def test_claude_prompt_rendered_sees_prompt_above_default_footer() -> None:
    """
    The readiness scan reaches the prompt glyph above Claude's footer.

    Claude Code 2.1.x renders a footer below the input box (the box's
    closing rule line, the cwd/status line, the model+effort line, and
    the permission-mode hint), so the live ``❯`` row is the 5th
    non-empty line from the bottom — NOT the last. The prior 4-line scan
    window never reached it and the web-UI readiness gate timed out with
    "did not become ready" even though the box was mounted. A failure
    here means the scan window regressed below 5 and the first web
    message would be dropped.
    """
    pane = "\n".join(
        [
            "────────────────────────────────────────",  # input box top rule
            "❯ ",  # the live prompt row (5th non-empty line from bottom)
            "────────────────────────────────────────",  # box closing rule
            "  alice: /home/alice/proj   Remote Control failed",  # status line
            "  Opus 4.8 (1M context) | effort:high",  # model + effort line
            "  ⏵⏵ don't ask on (shift+tab to cycle) · ← for agents",  # hint line
        ]
    )
    # ``❯`` is 5 non-empty lines above the bottom (rule + status + model
    # + hint sit below it), so only a scan window of >= 5 reaches it.
    assert _claude_prompt_rendered(pane) is True


def _write_deltas_lines(bridge_dir: Path, lines: list[str]) -> None:
    """
    Append raw JSONL lines to the bridge deltas file.

    :param bridge_dir: Bridge directory.
    :param lines: Already-serialized JSON strings (no trailing newline);
        each is written as its own newline-terminated record.
    :returns: None.
    """
    bridge_dir.mkdir(parents=True, exist_ok=True)
    with (bridge_dir / "message_deltas.jsonl").open("a", encoding="utf-8") as handle:
        for line in lines:
            handle.write(line + "\n")


def test_read_message_deltas_parses_and_advances_offset(tmp_path: Path) -> None:
    """
    Complete delta records parse in order and the offset reaches EOF.

    Fails if the reader drops fields or stops short of the last complete
    record — either would lose streamed chunks the forwarder must POST.
    """
    bridge_dir = tmp_path / "bridge"
    _write_deltas_lines(
        bridge_dir,
        [
            json.dumps({"message_id": "m1", "index": 0, "final": False, "delta": "Hello "}),
            json.dumps({"message_id": "m1", "index": 1, "final": True, "delta": "world"}),
        ],
    )
    result = read_message_deltas_from_offset(bridge_dir, 0)
    assert [(d.message_id, d.index, d.final, d.delta) for d in result.deltas] == [
        ("m1", 0, False, "Hello "),
        ("m1", 1, True, "world"),
    ]
    # Offset reached EOF, so a follow-up read sees nothing new.
    assert result.byte_offset == os.path.getsize(bridge_dir / "message_deltas.jsonl")
    assert read_message_deltas_from_offset(bridge_dir, result.byte_offset).deltas == []


def test_read_message_deltas_resumes_from_offset(tmp_path: Path) -> None:
    """
    A second read from the prior offset returns only newly appended records.

    Fails if the byte cursor is ignored (re-reading old chunks → the
    forwarder re-POSTs and the UI double-renders) or over-advances
    (skipping a new chunk → a gap in the live text).
    """
    bridge_dir = tmp_path / "bridge"
    _write_deltas_lines(
        bridge_dir, [json.dumps({"message_id": "m1", "index": 0, "final": True, "delta": "one"})]
    )
    first = read_message_deltas_from_offset(bridge_dir, 0)
    _write_deltas_lines(
        bridge_dir, [json.dumps({"message_id": "m2", "index": 0, "final": True, "delta": "two"})]
    )
    second = read_message_deltas_from_offset(bridge_dir, first.byte_offset)
    assert [(d.message_id, d.delta) for d in second.deltas] == [("m2", "two")]


def test_read_message_deltas_skips_partial_trailing_line(tmp_path: Path) -> None:
    """
    A half-written final line is not consumed until its newline lands.

    The hook writes each line with O_APPEND, but the reader can observe a
    record mid-write; it must leave the offset before the partial line so
    the next poll retries it. Fails if a torn line is parsed (data loss)
    or advances the offset past unterminated bytes (permanent loss).
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    complete = json.dumps({"message_id": "m1", "index": 0, "final": False, "delta": "done"}) + "\n"
    partial = json.dumps({"message_id": "m1", "index": 1, "final": True, "delta": "half"})  # no \n
    (bridge_dir / "message_deltas.jsonl").write_text(complete + partial, encoding="utf-8")

    result = read_message_deltas_from_offset(bridge_dir, 0)
    # Only the newline-terminated record is returned...
    assert [d.delta for d in result.deltas] == ["done"]
    # ...and the offset sits at the start of the partial line, so once
    # the hook finishes that line a re-read picks it up.
    assert result.byte_offset == len(complete.encode("utf-8"))


def test_read_message_deltas_skips_malformed_records(tmp_path: Path) -> None:
    """
    Lines that aren't well-formed deltas are dropped, not fatal.

    A line missing ``message_id``, with a non-string ``delta``, with a
    boolean ``index`` (``bool`` is an ``int`` subclass), or that isn't
    JSON must be skipped while still advancing past it — one bad line
    must never wedge the live tail. Fails if a malformed record is
    surfaced (→ a malformed SSE event) or halts the read.
    """
    bridge_dir = tmp_path / "bridge"
    _write_deltas_lines(
        bridge_dir,
        [
            "{not json",
            json.dumps({"index": 0, "delta": "no id"}),
            json.dumps({"message_id": "m1", "index": 0, "delta": 123}),
            json.dumps({"message_id": "m1", "index": True, "delta": "bool index"}),
            json.dumps({"message_id": "m1", "index": 2, "final": True, "delta": "good"}),
        ],
    )
    result = read_message_deltas_from_offset(bridge_dir, 0)
    # Only the one well-formed record survives; the four bad lines are
    # skipped but the offset still reached EOF (they were consumed).
    assert [(d.message_id, d.index, d.delta) for d in result.deltas] == [("m1", 2, "good")]
    assert result.byte_offset == os.path.getsize(bridge_dir / "message_deltas.jsonl")


def test_read_message_deltas_missing_file_is_empty(tmp_path: Path) -> None:
    """
    A read before the hook has written anything returns no deltas.

    Fails if a missing file raises instead of being treated as "no
    chunks yet" — the forwarder polls this every tick before streaming
    has started.
    """
    bridge_dir = tmp_path / "bridge"
    bridge_dir.mkdir()
    result = read_message_deltas_from_offset(bridge_dir, 0)
    assert result.deltas == []
    assert result.byte_offset == 0


# ── compute_transcript_cumulative_cost ────────────────────────────────


def _write_transcript_jsonl(path: Path, entries: list[dict[str, Any]]) -> None:
    """
    Write transcript records as newline-terminated JSONL.

    :param path: Destination JSONL path.
    :param entries: Decoded transcript records to serialize, one per
        line, e.g. an assistant message with a ``usage`` block.
    :returns: None.
    """
    path.write_text(
        "".join(json.dumps(entry) + "\n" for entry in entries),
        encoding="utf-8",
    )


def _assistant_entry(
    *,
    model: str,
    input_tokens: int,
    output_tokens: int,
    is_sidechain: bool = False,
    request_id: str | None = None,
) -> dict[str, Any]:
    """
    Build one assistant transcript record with a usage block.

    :param model: ``message.model`` to stamp, e.g. ``"test-model"``.
    :param input_tokens: Non-cached input tokens for the usage block.
    :param output_tokens: Output tokens for the usage block.
    :param is_sidechain: When ``True`` mark the record ``isSidechain``
        (how a sub-agent message is inlined into a parent transcript).
    :param request_id: Top-level ``requestId`` to stamp, e.g.
        ``"req_A"``. ``None`` omits it (records without a ``requestId``
        are each billed once, never collapsed together).
    :returns: A decoded transcript record dict.
    """
    entry: dict[str, Any] = {
        "message": {
            "role": "assistant",
            "model": model,
            "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
        }
    }
    if is_sidechain:
        entry["isSidechain"] = True
    if request_id is not None:
        entry["requestId"] = request_id
    return entry


def test_compute_transcript_cumulative_cost_dedupes_by_request_id(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Records sharing a ``requestId`` are billed once, not once per record.

    Claude writes the same API response as multiple transcript records (a
    streamed partial plus the final record), each carrying that response's
    full ``usage`` under one ``requestId``. Summing every record
    double-bills (observed ~2x inflation, which leaked into the parent
    badge and the cost-budget gate). The cost must dedupe by ``requestId``.
    """
    from omnigent.llms.context_window import ModelPricing

    pricing = ModelPricing(input_per_token=10.0, output_per_token=20.0)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: pricing)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    path = tmp_path / "transcript.jsonl"
    _write_transcript_jsonl(
        path,
        [
            # Two records, SAME requestId = one billed response (2*10 + 3*20 = 80).
            _assistant_entry(model="m", input_tokens=2, output_tokens=3, request_id="req_A"),
            _assistant_entry(model="m", input_tokens=2, output_tokens=3, request_id="req_A"),
            # Distinct requestId = a second billed response (1*10 + 1*20 = 30).
            _assistant_entry(model="m", input_tokens=1, output_tokens=1, request_id="req_B"),
        ],
    )
    cost = claude_native_bridge.compute_transcript_cumulative_cost(path, include_sidechains=True)
    # 80 + 30 = 110. Without dedup the duplicate req_A record adds another
    # 80 → 190, the ~2x over-report this dedup fixes.
    assert cost == pytest.approx(110.0)


def test_compute_transcript_cumulative_cost_sums_priced_messages(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Cost is the sum over every priced assistant message in the transcript.

    Asserts the exact USD total (real :func:`compute_llm_cost` math), so
    a regression in per-message summation or token pricing fails loudly —
    not just "some positive number".
    """
    from omnigent.llms.context_window import ModelPricing

    pricing = ModelPricing(input_per_token=10.0, output_per_token=20.0)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: pricing)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    path = tmp_path / "transcript.jsonl"
    _write_transcript_jsonl(
        path,
        [
            _assistant_entry(model="m", input_tokens=2, output_tokens=3),  # 2*10 + 3*20 = 80
            {"message": {"role": "user", "content": "hi"}},  # no usage → skipped
            _assistant_entry(model="m", input_tokens=1, output_tokens=1),  # 1*10 + 1*20 = 30
        ],
    )
    cost = claude_native_bridge.compute_transcript_cumulative_cost(path, include_sidechains=True)
    assert cost == pytest.approx(110.0)


def test_compute_transcript_cumulative_cost_excludes_parent_sidechains(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    ``include_sidechains=False`` skips inlined sub-agent records.

    The parent transcript inlines a sub-agent's messages as
    ``isSidechain: true``; counting them there AND via the sub-agent's
    own transcript would double-bill, so the parent path must drop them.
    The sub-agent path (``include_sidechains=True``) counts everything.
    """
    from omnigent.llms.context_window import ModelPricing

    pricing = ModelPricing(input_per_token=10.0, output_per_token=0.0)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: pricing)
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    path = tmp_path / "parent.jsonl"
    _write_transcript_jsonl(
        path,
        [
            _assistant_entry(model="m", input_tokens=5, output_tokens=0),  # own → 50
            _assistant_entry(  # inlined sub-agent → 1000
                model="m", input_tokens=100, output_tokens=0, is_sidechain=True
            ),
        ],
    )
    assert claude_native_bridge.compute_transcript_cumulative_cost(
        path, include_sidechains=False
    ) == pytest.approx(50.0)
    assert claude_native_bridge.compute_transcript_cumulative_cost(
        path, include_sidechains=True
    ) == pytest.approx(1050.0)


def test_compute_transcript_cumulative_cost_none_when_nothing_priceable(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """
    Returns ``None`` (not ``0.0``) when no message can be priced.

    Covers a missing file, a transcript with no assistant usage, and a
    model with no available pricing — each must yield ``None`` so the
    forwarder treats it as "no estimate" rather than "$0 spent".
    """
    from omnigent.llms.context_window import ModelPricing

    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    # Missing file.
    assert (
        claude_native_bridge.compute_transcript_cumulative_cost(
            tmp_path / "missing.jsonl", include_sidechains=True
        )
        is None
    )
    # File with no assistant usage.
    pricing = ModelPricing(input_per_token=10.0, output_per_token=20.0)
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: pricing)
    no_usage = tmp_path / "no_usage.jsonl"
    _write_transcript_jsonl(no_usage, [{"message": {"role": "user", "content": "hi"}}])
    assert (
        claude_native_bridge.compute_transcript_cumulative_cost(no_usage, include_sidechains=True)
        is None
    )
    # Pricing unavailable for the model.
    claude_native_bridge._TRANSCRIPT_PRICING_CACHE.clear()
    monkeypatch.setattr("omnigent.llms.context_window.fetch_model_pricing", lambda model: None)
    priced = tmp_path / "priced.jsonl"
    _write_transcript_jsonl(priced, [_assistant_entry(model="m", input_tokens=5, output_tokens=5)])
    assert (
        claude_native_bridge.compute_transcript_cumulative_cost(priced, include_sidechains=True)
        is None
    )


def test_format_terminal_failure_tail_returns_empty_for_blank_pane() -> None:
    """
    A pane with no visible text yields no tail block.

    :returns: None.
    """
    assert claude_native_bridge._format_terminal_failure_tail("   \n\n  ") == ""


def test_format_terminal_failure_tail_includes_recent_error_lines() -> None:
    """
    The tail block carries the pane's trailing non-blank lines, so a
    startup crash surfaces in the readiness-timeout error.

    :returns: None.
    """
    pane = "ERROR  JSON Parse error: Unrecognized token '<'\n  at <parse> (:0)\n"
    tail = claude_native_bridge._format_terminal_failure_tail(pane)
    assert tail.startswith(" Last terminal output:\n")
    assert "JSON Parse error: Unrecognized token '<'" in tail


def test_format_terminal_failure_tail_caps_length(monkeypatch: pytest.MonkeyPatch) -> None:
    """
    A very long pane is truncated to the configured character cap.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    monkeypatch.setattr("omnigent.claude_native_bridge._TERMINAL_FAILURE_TAIL_CHARS", 50)
    pane = "\n".join(f"line {i}" for i in range(100))
    tail = claude_native_bridge._format_terminal_failure_tail(pane)
    body = tail.split("\n", 1)[1]
    assert body.startswith("…")
    # Leading ellipsis marker plus at most the configured character cap.
    assert len(body) <= 51


def test_wait_for_claude_prompt_ready_surfaces_terminal_output_on_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """
    On a readiness timeout, the RuntimeError carries Claude Code's own
    terminal output (e.g. a startup ``JSON Parse error``) so the cause
    surfaces in the web UI error banner, not only in the terminal.

    Without the tail, the user sees a generic "terminal did not become
    ready" timeout in the UI while the actual crash sits unread in the
    terminal pane.

    :param monkeypatch: Pytest monkeypatch fixture.
    :returns: None.
    """
    crash_pane = (
        "ERROR  JSON Parse error: Unrecognized token '<'\n"
        "  at <parse> (:0)\n"
        "  at parse (unknown)\n"
    )
    monkeypatch.setattr(
        "omnigent.claude_native_bridge._capture_pane",
        lambda socket_path, tmux_target: crash_pane,
    )
    with pytest.raises(RuntimeError) as excinfo:
        claude_native_bridge._wait_for_claude_prompt_ready(
            "/tmp/example/tmux.sock",
            "claude:0.0",
            timeout_s=0.0,
        )
    message = str(excinfo.value)
    assert "did not become ready" in message
    assert "Last terminal output:" in message
    assert "JSON Parse error: Unrecognized token '<'" in message


def test_prepare_bridge_dir_writes_owner_pid_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """prepare_bridge_dir records the creating pid for owner-pid-gated
    pruning (O6)."""
    import os

    from omnigent.claude_native_bridge import prepare_bridge_dir

    monkeypatch.setattr(
        "omnigent.claude_native_bridge._BRIDGE_ROOT",
        tmp_path / "claude-native",
    )
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = prepare_bridge_dir("conv_owner", workspace=tmp_path)
    assert (bridge_dir / "owner.pid").read_text(encoding="utf-8").strip() == str(os.getpid())


def test_cleanup_bridge_dir_removes_the_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cleanup_bridge_dir rmtrees the per-session dir (O6)."""
    from omnigent.claude_native_bridge import cleanup_bridge_dir, prepare_bridge_dir

    monkeypatch.setattr(
        "omnigent.claude_native_bridge._BRIDGE_ROOT",
        tmp_path / "claude-native",
    )
    monkeypatch.setattr("omnigent.claude_native_bridge._TRUSTED_PARENT", tmp_path)
    bridge_dir = prepare_bridge_dir("conv_cleanup", workspace=tmp_path)
    assert bridge_dir.exists()

    assert cleanup_bridge_dir("conv_cleanup") is True
    assert not bridge_dir.exists()
    assert cleanup_bridge_dir("conv_cleanup") is False


def test_prune_orphaned_bridge_dirs_only_dead_owners(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Prune removes only provably-dead-owner dirs; live and unmarked
    survive (owner-pid invariant, O6)."""
    import os
    import subprocess
    import sys

    from omnigent.claude_native_bridge import prune_orphaned_bridge_dirs

    root = tmp_path / "claude-native"
    root.mkdir(parents=True)
    monkeypatch.setattr("omnigent.claude_native_bridge._BRIDGE_ROOT", root)

    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    dead_dir = root / "deadowner"
    dead_dir.mkdir()
    (dead_dir / "owner.pid").write_text(str(dead.pid), encoding="utf-8")

    live_dir = root / "liveowner"
    live_dir.mkdir()
    (live_dir / "owner.pid").write_text(str(os.getpid()), encoding="utf-8")

    unmarked_dir = root / "unmarked"
    unmarked_dir.mkdir()

    pruned = prune_orphaned_bridge_dirs()

    assert pruned == 1
    assert not dead_dir.exists()
    assert live_dir.exists()
    assert unmarked_dir.exists()
