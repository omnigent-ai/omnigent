"""Resume rollouts tests for Codex session."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import click
import httpx
import pytest

from omnigent.harnesses.codex_native import forwarder as codex_native_forwarder
from omnigent.harnesses.codex_native import main as codex_native
from omnigent.inner.native_attachments import attachment_cache_dir
from tests.harnesses.codex_native.session._support import (
    _write_source_rollout,
)


def test_clone_codex_rollout_rewrites_id_and_structural_cwd_into_clone_home(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Cloning rewrites only the thread id and structural cwd in the clone home.

    Proves the surgical rewrite: ``session_meta.id`` becomes the target
    id, ``session_meta.cwd`` and ``turn_context.cwd`` become the clone
    workspace, the rollout lands in the CLONE's CODEX_HOME under the
    target id, and record order is preserved.
    """
    from omnigent.harnesses.codex_native.bridge import (
        bridge_dir_for_bridge_id,
        codex_home_for_bridge_dir,
    )

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    target_thread = "019eaa11-1111-7222-8333-444455556666"
    source_cwd = "/repo/worktree-source"
    clone_cwd = tmp_path / "worktree-clone"

    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    _write_source_rollout(codex_home=source_home, thread_id=source_thread, source_cwd=source_cwd)
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id=target_thread,
        clone_codex_home=clone_home,
        clone_workspace=clone_cwd,
    )

    assert result is not None
    # Lands in the CLONE's home, under the target id, preserving the
    # source's date-partitioned layout.
    assert clone_home in result.parents, f"{result} not under clone home {clone_home}"
    assert source_home not in result.parents
    assert result.name == f"rollout-2026-06-05T15-23-07-{target_thread}.jsonl"

    records = [json.loads(line) for line in result.read_text().splitlines()]
    # Order preserved: session_meta, turn_context, message, function_call_output.
    assert [r["type"] for r in records] == [
        "session_meta",
        "turn_context",
        "response_item",
        "response_item",
    ]
    meta = records[0]["payload"]
    assert meta["id"] == target_thread, "session_meta.id must be rewritten to the target thread id"
    assert meta["cwd"] == str(clone_cwd), (
        "session_meta.cwd must be rewritten to the clone workspace"
    )
    assert records[1]["payload"]["cwd"] == str(clone_cwd), "turn_context.cwd must be rewritten"


def test_clone_codex_rollout_leaves_historical_cwd_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Cloning never rewrites cwd inside message / tool-output bodies.

    The historical ``cwd`` mentions (a developer message and a
    function_call_output) record what actually happened in the source
    workspace; rewriting them would fabricate history. Only the two
    structural fields move.
    """
    from omnigent.harnesses.codex_native.bridge import (
        bridge_dir_for_bridge_id,
        codex_home_for_bridge_dir,
    )

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    target_thread = "019eaa11-1111-7222-8333-444455556666"
    source_cwd = "/repo/worktree-source"
    clone_cwd = tmp_path / "worktree-clone"

    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    source_rollout = _write_source_rollout(
        codex_home=source_home, thread_id=source_thread, source_cwd=source_cwd
    )
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id=target_thread,
        clone_codex_home=clone_home,
        clone_workspace=clone_cwd,
    )

    assert result is not None
    records = [json.loads(line) for line in result.read_text().splitlines()]
    developer_text = records[2]["payload"]["content"][0]["text"]
    tool_output = records[3]["payload"]["output"]
    # Both historical bodies still reference the SOURCE cwd verbatim.
    assert source_cwd in developer_text, "historical message body must be preserved"
    assert source_cwd in tool_output, "historical tool output must be preserved"
    assert str(clone_cwd) not in developer_text, "clone cwd must not leak into message history"
    assert str(clone_cwd) not in tool_output, "clone cwd must not leak into tool output"
    # Historical lines (indices 2-3) must be copied byte-for-byte. JSON
    # re-serialization would lose the fixture's spaces and fail this check.
    source_lines = source_rollout.read_text().splitlines()
    clone_lines = result.read_text().splitlines()
    assert clone_lines[2] == source_lines[2], "historical message line must be byte-identical"
    assert clone_lines[3] == source_lines[3], "historical tool-output line must be byte-identical"


def test_clone_codex_rollout_leaves_source_untouched(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The source rollout is read-only — cloning never mutates it."""
    from omnigent.harnesses.codex_native.bridge import (
        bridge_dir_for_bridge_id,
        codex_home_for_bridge_dir,
    )

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    source_cwd = "/repo/worktree-source"

    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    source_rollout = _write_source_rollout(
        codex_home=source_home, thread_id=source_thread, source_cwd=source_cwd
    )
    before = source_rollout.read_bytes()
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id="019eaa11-1111-7222-8333-444455556666",
        clone_codex_home=clone_home,
        clone_workspace=tmp_path / "clone",
    )

    assert source_rollout.read_bytes() == before, (
        "source rollout must be byte-identical after clone"
    )


def test_clone_codex_rollout_returns_none_when_source_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    Cloning returns None when the source rollout isn't on this host.

    The caller treats None as "launch fresh" — a fork to a host without
    the source rollout must not strand the clone pointing at a missing
    thread.
    """
    from omnigent.harnesses.codex_native.bridge import (
        bridge_dir_for_bridge_id,
        codex_home_for_bridge_dir,
    )

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_missing",
        source_thread_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
        target_thread_id="019eaa11-1111-7222-8333-444455556666",
        clone_codex_home=clone_home,
        clone_workspace=tmp_path / "clone",
    )

    assert result is None


def test_clone_codex_rollout_returns_none_for_unsafe_target_id(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """
    An unsafe target id is rejected before any filesystem write.

    Guards against path traversal via the minted id being interpolated
    into the rollout filename.
    """
    from omnigent.harnesses.codex_native.bridge import (
        bridge_dir_for_bridge_id,
        codex_home_for_bridge_dir,
    )

    monkeypatch.setattr(
        "omnigent.harnesses.codex_native.bridge._BRIDGE_ROOT", tmp_path / "bridges"
    )
    source_thread = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    source_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_source"))
    _write_source_rollout(codex_home=source_home, thread_id=source_thread, source_cwd="/repo/src")
    clone_home = codex_home_for_bridge_dir(bridge_dir_for_bridge_id("conv_clone"))

    result = codex_native._clone_codex_rollout(
        source_session_id="conv_source",
        source_thread_id=source_thread,
        target_thread_id="../../etc/passwd",
        clone_codex_home=clone_home,
        clone_workspace=tmp_path / "clone",
    )

    assert result is None


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_replays_before_history_fetch(
    tmp_path: Path,
) -> None:
    """A successful dead-letter replay is included in the rebuilt snapshot."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    replay_data = {
        "item_type": "message",
        "item_data": {
            "role": "assistant",
            "content": [{"type": "output_text", "text": "recovered reply"}],
        },
        "response_id": "turn_recovered",
        "source_id": "codex:item:recovered",
    }
    codex_native_forwarder.append_dead_letter(
        tmp_path,
        session_id="conv_codex",
        event_type="external_conversation_item",
        payload=replay_data,
        reason="http 503",
        http_status=503,
    )
    request_order: list[str] = []
    server_items: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        request_order.append(request.method)
        if request.method == "POST":
            body = json.loads(request.content)
            assert body == {"type": "external_conversation_item", "data": replay_data}
            server_items.append(
                {
                    "id": "msg_recovered",
                    "response_id": replay_data["response_id"],
                    "type": "message",
                    **replay_data["item_data"],
                }
            )
            return httpx.Response(200)
        assert request.url.path == "/v1/sessions/conv_codex/items"
        return httpx.Response(200, json={"data": server_items, "has_more": False})

    async with httpx.AsyncClient(
        transport=httpx.MockTransport(handler), base_url="http://test"
    ) as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=tmp_path.resolve(),
            model_provider="omnigent_databricks",
            codex_path=None,
        )

    records = [json.loads(line) for line in rollout.read_text().splitlines()]
    assert request_order == ["POST", "GET"]
    assert not (tmp_path / "dead_letter.jsonl").exists()
    assert any(
        record["type"] == "response_item" and record["payload"].get("id") == "msg_recovered"
        for record in records
    )


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_restores_a_zip_outside_the_workspace(
    tmp_path: Path,
) -> None:
    """A cold rollout rebuild downloads ZIP files without changing the checkout."""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = tmp_path / "bridge" / "codex-home"
    zip_bytes = b"PK\x03\x04 resumed zip"

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/resources/files/file_zip/content"):
            return httpx.Response(200, content=zip_bytes)
        if path.endswith("/resources/files/file_zip"):
            return httpx.Response(
                200,
                json={"id": "file_zip", "name": "bundle.zip", "content_type": "application/zip"},
            )
        item = {
            "id": "msg_user_1",
            "response_id": "codex_turn_1",
            "type": "message",
            "role": "user",
            "content": [
                {"type": "input_file", "file_id": "file_zip", "filename": "bundle.zip"},
                {"type": "input_text", "text": "unpack this"},
            ],
        }
        return httpx.Response(200, json={"data": [item], "has_more": False})

    transport = httpx.MockTransport(handler)
    expected = attachment_cache_dir(codex_home.parent) / "bundle.zip"
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for attempt in range(2):
            rollout = await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
                codex_home=codex_home,
                workspace=workspace,
                model_provider="omnigent_databricks",
                codex_path=None,
            )
            assert expected.read_bytes() == zip_bytes
            assert list(workspace.iterdir()) == []
            if attempt == 0:
                expected.unlink()

    assert expected.read_bytes() == zip_bytes
    records = [json.loads(line) for line in rollout.read_text(encoding="utf-8").splitlines()]
    user_item = next(r["payload"] for r in records if r["type"] == "response_item")
    assert user_item["content"] == [
        {"type": "input_text", "text": f"[Attached: {expected}]"},
        {"type": "input_text", "text": "unpack this"},
    ]


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_synthesizes_omnigent_history(
    tmp_path: Path,
) -> None:
    """
    Cross-machine Codex resume rebuilds a local rollout from Omnigent history.

    The server can know the Omnigent conversation and Codex thread id while the
    current host has no ``$CODEX_HOME/sessions/.../rollout-*-<thread>.jsonl``.
    This helper must fetch committed Omnigent items, follow pagination, and write
    the response items before ``codex resume <thread>`` launches. If it only
    checked for local rollout state, this test would leave no file to read.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME`` and cwd.
    """
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    codex_home = tmp_path / "codex-home"
    requested_urls: list[str] = []
    first_page = [
        {
            "id": "msg_user_1",
            "response_id": "codex_turn_1",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "open TODO.md"}],
        },
        {
            "id": "fc_shell_1",
            "response_id": "codex_turn_1",
            "type": "function_call",
            "name": "shell",
            "arguments": '{"command":"cat TODO.md"}',
            "call_id": "call_shell_1",
        },
    ]
    second_page = [
        {
            "id": "fco_shell_1",
            "response_id": "codex_turn_1",
            "type": "function_call_output",
            "call_id": "call_shell_1",
            "output": "contents",
        },
        {
            "id": "msg_assistant_1",
            "response_id": "codex_turn_1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "TODO.md says contents"}],
        },
        {
            "id": "msg_user_interrupted",
            "response_id": "codex_turn_interrupted",
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "apply risky change"}],
        },
        {
            "id": "msg_assistant_interrupted",
            "response_id": "codex_turn_interrupted",
            "type": "message",
            "role": "assistant",
            "interrupted": True,
            "content": [{"type": "output_text", "text": "partially applied"}],
        },
    ]

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve two chronological item pages.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent response.
        """
        requested_urls.append(str(request.url))
        assert request.url.path == "/v1/sessions/conv_codex/items"
        after = request.url.params.get("after")
        if after is None:
            return httpx.Response(
                200,
                json={"data": first_page, "has_more": True, "last_id": "fc_shell_1"},
            )
        assert after == "fc_shell_1"
        return httpx.Response(200, json={"data": second_page, "has_more": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=workspace.resolve(),
            model_provider="omnigent_databricks",
            codex_path=None,
        )

    assert rollout is not None
    assert codex_home in rollout.parents
    assert rollout.name.endswith(f"-{thread_id}.jsonl")
    records = [
        json.loads(line)
        for line in rollout.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [record["type"] for record in records] == [
        "session_meta",
        "turn_context",
        "response_item",
        "event_msg",
        "response_item",
        "response_item",
        "response_item",
        "event_msg",
    ]
    assert records[0]["payload"]["id"] == thread_id
    assert records[0]["payload"]["cwd"] == str(workspace.resolve())
    # codex >= 0.133 refuses to parse a rollout whose session_meta lacks
    # timestamp/cli_version, and its thread-store backfill breaks resume
    # when model_provider is absent (verified against codex 0.136.0).
    assert records[0]["payload"]["timestamp"] == records[0]["timestamp"]
    assert records[0]["payload"]["cli_version"] == "0.0.0"
    assert records[0]["payload"]["model_provider"] == "omnigent_databricks"
    assert records[1]["payload"]["turn_id"] == "turn_1"
    assert records[1]["payload"]["cwd"] == str(workspace.resolve())
    assert records[2]["payload"] == {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": "open TODO.md"}],
        "id": "msg_user_1",
    }
    # Visible-turn reconstruction reads event_msg mirrors, not
    # response_item history: without them codex resumes an empty thread.
    assert records[3]["payload"] == {
        "type": "user_message",
        "message": "open TODO.md",
        "images": [],
        "local_images": [],
        "text_elements": [],
    }
    assert records[4]["payload"] == {
        "type": "function_call",
        "name": "shell",
        "arguments": '{"command":"cat TODO.md"}',
        "call_id": "call_shell_1",
        "id": "fc_shell_1",
    }
    assert records[5]["payload"] == {
        "type": "function_call_output",
        "call_id": "call_shell_1",
        "output": "contents",
        "id": "fco_shell_1",
    }
    assert records[6]["payload"] == {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": "TODO.md says contents"}],
        "id": "msg_assistant_1",
    }
    assert records[7]["payload"] == {
        "type": "agent_message",
        "message": "TODO.md says contents",
        "phase": "final_answer",
        "memory_citation": None,
    }
    restored_payloads = [
        record["payload"] for record in records if record["type"] == "response_item"
    ]
    assert all(payload.get("id") != "msg_user_interrupted" for payload in restored_payloads)
    assert all(payload.get("id") != "msg_assistant_interrupted" for payload in restored_payloads)
    restored_text = json.dumps(restored_payloads)
    assert "apply risky change" not in restored_text
    assert "partially applied" not in restored_text
    assert any("after=fc_shell_1" in url for url in requested_urls), (
        f"history pagination was not followed; requests were {requested_urls!r}"
    )


def _replayed_function_calls(arguments: str) -> list[dict[str, Any]]:
    """Rebuild a rollout holding one call with ``arguments`` and its output."""
    records = codex_native._codex_rollout_records_from_session_items(
        [
            {
                "id": "fc_patch",
                "response_id": "codex_turn_1",
                "type": "function_call",
                "name": "apply_patch",
                "arguments": arguments,
                "call_id": "call_patch",
            },
            {
                "id": "fco_patch",
                "response_id": "codex_turn_1",
                "type": "function_call_output",
                "call_id": "call_patch",
                "output": "delete /workspace/fixtures/big.json",
            },
        ],
        session_id="conv_codex",
        external_session_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
        cwd=Path("/workspace"),
        model_provider="omnigent_databricks",
        cli_version="0.154.0",
    )
    return [
        record["payload"]
        for record in records
        if record["type"] == "response_item"
        and record["payload"]["type"] in ("function_call", "function_call_output")
    ]


def test_rollout_records_replay_oversized_function_call_arguments_as_a_stub() -> None:
    """
    A call too large for the Responses API replays as a stub, paired with its output.

    The apply_patch mirror carries a deleted file's whole content, so a rebuilt
    thread would resend arguments over the API's 1,048,576-character limit and
    every later request in the session would fail.
    """
    arguments = json.dumps(
        {
            "changes": [
                {
                    "path": "/workspace/fixtures/big.json",
                    "kind": {"type": "delete"},
                    "diff": "x" * 1_200_000,
                }
            ]
        }
    )

    call, output = _replayed_function_calls(arguments)

    assert call["name"] == "apply_patch"
    assert call["call_id"] == "call_patch"
    assert json.loads(call["arguments"]) == {
        "omitted": f"{len(arguments)} characters of arguments, too large to replay"
    }
    assert output["type"] == "function_call_output"
    assert output["call_id"] == "call_patch"


def test_rollout_records_replay_arguments_at_the_limit_verbatim() -> None:
    """Arguments exactly at the replay cap are kept as written."""
    prefix = '{"command":"'
    suffix = '"}'
    filler = "a" * (codex_native._MAX_REPLAYED_ARGUMENTS_CHARS - len(prefix) - len(suffix))
    arguments = prefix + filler + suffix
    assert len(arguments) == codex_native._MAX_REPLAYED_ARGUMENTS_CHARS

    call, _output = _replayed_function_calls(arguments)

    assert call["arguments"] == arguments


def test_rollout_records_keep_function_call_namespace() -> None:
    """Replay keeps a tool call's namespace; default-namespace calls stay bare."""
    records = codex_native._codex_rollout_records_from_session_items(
        [
            {
                "id": "fc_sleep",
                "response_id": "codex_turn_1",
                "type": "function_call",
                "name": "sleep",
                "namespace": "container",
                "arguments": '{"seconds":2}',
                "call_id": "call_sleep",
            },
            {
                "id": "fco_sleep",
                "response_id": "codex_turn_1",
                "type": "function_call_output",
                "call_id": "call_sleep",
                "output": "slept",
            },
            {
                "id": "fc_shell",
                "response_id": "codex_turn_1",
                "type": "function_call",
                "name": "shell",
                "arguments": '{"command":"ls"}',
                "call_id": "call_shell",
            },
        ],
        session_id="conv_codex",
        external_session_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
        cwd=Path("/workspace"),
        model_provider="omnigent_databricks",
        cli_version="0.154.0",
    )

    calls = [
        record["payload"]
        for record in records
        if record["type"] == "response_item" and record["payload"]["type"] == "function_call"
    ]
    assert calls == [
        {
            "type": "function_call",
            "name": "sleep",
            "namespace": "container",
            "arguments": '{"seconds":2}',
            "call_id": "call_sleep",
            "id": "fc_sleep",
        },
        {
            "type": "function_call",
            "name": "shell",
            "arguments": '{"command":"ls"}',
            "call_id": "call_shell",
            "id": "fc_shell",
        },
    ]


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_refreshes_existing_from_server(
    tmp_path: Path,
) -> None:
    """
    Codex cold resume refreshes an existing rollout from server history.

    The server transcript is authoritative on cold resume. A stale local
    rollout must be atomically replaced with the committed Omnigent items
    instead of silently preserving divergent history.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME``.
    """
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    existing = _write_source_rollout(
        codex_home=codex_home,
        thread_id=thread_id,
        source_cwd="/stale/cwd",
    )
    before = existing.read_bytes()
    workspace = (tmp_path / "workspace").resolve()
    requested = False

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve the authoritative Omnigent history.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent item page.
        """
        nonlocal requested
        requested = True
        assert request.url.path == "/v1/sessions/conv_codex/items"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "msg_server",
                        "response_id": "codex_turn_server",
                        "type": "message",
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "authoritative server history"}
                        ],
                    }
                ],
                "has_more": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=workspace,
            model_provider="omnigent_databricks",
            codex_path=None,
        )

    assert requested
    assert rollout == existing
    assert existing.read_bytes() != before
    records = [json.loads(line) for line in existing.read_text().splitlines()]
    assert records[0]["payload"]["cwd"] == str(workspace)
    assert "authoritative server history" in json.dumps(records)
    assert "/stale/cwd" not in json.dumps(records)


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_empty_server_history_wins(
    tmp_path: Path,
) -> None:
    """A successful empty server history replaces divergent local-only records."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    existing = _write_source_rollout(
        codex_home=codex_home,
        thread_id=thread_id,
        source_cwd="/local/only",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_codex/items"
        return httpx.Response(200, json={"data": [], "has_more": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=(tmp_path / "workspace").resolve(),
            model_provider="omnigent_databricks",
            codex_path=None,
        )

    assert rollout == existing
    records = [json.loads(line) for line in existing.read_text().splitlines()]
    assert [record["type"] for record in records] == ["session_meta"]
    assert "/local/only" not in json.dumps(records)


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_uses_unique_atomic_temp_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Separate cold-resume writers never share a temporary rollout path."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    replaced_from: list[Path] = []
    real_replace = os.replace

    def recording_replace(source: os.PathLike[str], target: os.PathLike[str]) -> None:
        replaced_from.append(Path(source))
        real_replace(source, target)

    monkeypatch.setattr(codex_native.os, "replace", recording_replace)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v1/sessions/conv_codex/items"
        return httpx.Response(
            200,
            json={
                "data": [
                    {
                        "id": "msg_server",
                        "response_id": "codex_turn_server",
                        "type": "message",
                        "role": "user",
                        "content": [{"type": "input_text", "text": "server history"}],
                    }
                ],
                "has_more": False,
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        for _ in range(2):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id=thread_id,
                codex_home=codex_home,
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )

    assert len(replaced_from) == 2
    assert replaced_from[0] != replaced_from[1]
    assert all(path.suffix == ".tmp" for path in replaced_from)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["server", "transport"])
async def test_ensure_local_codex_resume_rollout_falls_back_when_server_unavailable(
    tmp_path: Path,
    failure: str,
) -> None:
    """A transient server failure falls back to a valid local rollout."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    existing = _write_source_rollout(
        codex_home=codex_home,
        thread_id=thread_id,
        source_cwd="/local/fallback",
    )
    before = existing.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        if failure == "transport":
            raise httpx.ReadError("connection dropped", request=request)
        return httpx.Response(503, json={"error": {"code": "unavailable"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rollout = await codex_native._ensure_local_codex_resume_rollout(
            client,
            session_id="conv_codex",
            external_session_id=thread_id,
            codex_home=codex_home,
            workspace=(tmp_path / "workspace").resolve(),
            model_provider="omnigent_databricks",
            codex_path=None,
        )

    assert rollout == existing
    assert existing.read_bytes() == before


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_does_not_fallback_on_4xx(
    tmp_path: Path,
) -> None:
    """A server contract rejection cannot revive a local Codex rollout."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    existing = _write_source_rollout(
        codex_home=codex_home,
        thread_id=thread_id,
        source_cwd="/local/fallback",
    )
    before = existing.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(404, json={"error": {"code": "not_found"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="Failed to fetch history"):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id=thread_id,
                codex_home=codex_home,
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )

    assert existing.read_bytes() == before


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_rejects_invalid_local_fallback(
    tmp_path: Path,
) -> None:
    """An unavailable server cannot fall back to a malformed local rollout."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    invalid = (
        codex_home
        / "sessions"
        / "2026"
        / "09"
        / "11"
        / f"rollout-2026-09-11T00-00-00-{thread_id}.jsonl"
    )
    invalid.parent.mkdir(parents=True)
    invalid.write_text("not json\n", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(503, json={"error": {"code": "unavailable"}})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="Failed to fetch history"):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id=thread_id,
                codex_home=codex_home,
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_item", [None, "not an object", 42])
async def test_ensure_local_codex_resume_rollout_rejects_non_object_server_item(
    tmp_path: Path,
    bad_item: object,
) -> None:
    """Malformed entries in a successful server page fail closed."""
    thread_id = "019e96aa-0be2-7343-8d3b-6f914d60936b"
    codex_home = tmp_path / "codex-home"
    existing = _write_source_rollout(
        codex_home=codex_home,
        thread_id=thread_id,
        source_cwd="/local/fallback",
    )
    before = existing.read_bytes()

    def handler(request: httpx.Request) -> httpx.Response:
        del request
        return httpx.Response(200, json={"data": [bad_item], "has_more": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="non-object item at index 0"):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id=thread_id,
                codex_home=codex_home,
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )

    assert existing.read_bytes() == before


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("bad_item", "message"),
    [
        (
            {
                "id": "fc_bad",
                "response_id": "codex_turn_1",
                "type": "function_call",
                "name": "",
                "call_id": "call_shell_1",
                "arguments": "{}",
            },
            "function_call 'fc_bad' has an invalid name",
        ),
        (
            {
                "id": "fc_bad",
                "response_id": "codex_turn_1",
                "type": "function_call",
                "name": "shell",
                "call_id": "",
                "arguments": "{}",
            },
            "function_call 'fc_bad' has an invalid call_id",
        ),
        (
            {
                "id": "fc_bad",
                "response_id": "codex_turn_1",
                "type": "function_call",
                "name": "shell",
                "call_id": "call_shell_1",
            },
            "function_call 'fc_bad' has non-string arguments",
        ),
        (
            {
                "id": "fco_bad",
                "response_id": "codex_turn_1",
                "type": "function_call_output",
                "call_id": "",
                "output": "done",
            },
            "function_call_output 'fco_bad' has an invalid call_id",
        ),
        (
            {
                "id": "fco_bad",
                "response_id": "codex_turn_1",
                "type": "function_call_output",
                "call_id": "call_shell_1",
            },
            "function_call_output 'fco_bad' has non-string output",
        ),
    ],
)
async def test_ensure_local_codex_resume_rollout_rejects_malformed_tool_history(
    tmp_path: Path,
    bad_item: dict[str, Any],
    message: str,
) -> None:
    """
    Codex rollout synthesis fails loudly for corrupt Omnigent tool history.

    Tool call ``arguments`` and tool ``output`` are required Omnigent string
    fields. Missing values must not be invented as ``{}`` or ``""``,
    because Codex would then resume a tool history that never happened.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME``.
    :param bad_item: Malformed Omnigent item to serve.
    :param message: Expected diagnostic substring.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Serve malformed Omnigent history.

        :param request: Incoming mock HTTP request.
        :returns: Mock Omnigent item page.
        """
        assert request.url.path == "/v1/sessions/conv_codex/items"
        return httpx.Response(200, json={"data": [bad_item], "has_more": False})

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match=message):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id="019e96aa-0be2-7343-8d3b-6f914d60936b",
                codex_home=tmp_path / "codex-home",
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )


@pytest.mark.asyncio
async def test_ensure_local_codex_resume_rollout_rejects_unsafe_thread_id(
    tmp_path: Path,
) -> None:
    """
    Codex cold resume fails loudly for an unsafe persisted thread id.

    The caller relies on this helper to make ``codex resume <thread>`` viable.
    Returning ``None`` would silently launch Codex without the guaranteed
    rollout this path is responsible for preparing.

    :param tmp_path: Temporary directory for isolated ``CODEX_HOME``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """
        Fail if Omnigent history is fetched for an unsafe thread id.

        :param request: Incoming mock HTTP request.
        :returns: Never returns.
        """
        del request
        raise AssertionError("unsafe thread id should be rejected before Omnigent fetch")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        with pytest.raises(click.ClickException, match="not a safe Codex rollout id"):
            await codex_native._ensure_local_codex_resume_rollout(
                client,
                session_id="conv_codex",
                external_session_id="../../etc/passwd",
                codex_home=tmp_path / "codex-home",
                workspace=(tmp_path / "workspace").resolve(),
                model_provider="omnigent_databricks",
                codex_path=None,
            )


def test_mint_codex_thread_id_is_uuidv7() -> None:
    """
    Minted thread ids are valid UUIDv7 strings.

    Codex thread ids are UUIDv7; the clone resumes via
    ``codex resume <minted_id>``, so the format must match what Codex
    accepts (verified end-to-end by the opt-in fork e2e).
    """
    import uuid as _uuid

    minted = codex_native._mint_codex_thread_id()
    parsed = _uuid.UUID(minted)
    assert parsed.version == 7
    assert codex_native._CODEX_THREAD_ID_RE.fullmatch(minted)
