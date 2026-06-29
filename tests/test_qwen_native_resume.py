"""Tests for qwen-native fork/resume recording synthesis.

Covers converting Omnigent items into qwen chat-recording records and writing
the recording + discovery sidecars (``runtime.json`` / ``meta.json``) that qwen
needs to resolve ``--resume``. An optional, opt-in end-to-end test confirms a
real ``qwen --resume`` loads the synthesized recording.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest

from omnigent import qwen_native_bridge as qnb


def _user_item(text: str, *, response_id: str | None = None) -> dict:
    item: dict = {
        "type": "message",
        "role": "user",
        "content": [{"type": "input_text", "text": text}],
    }
    if response_id is not None:
        item["response_id"] = response_id
    return item


def _assistant_item(
    text: str,
    *,
    model: str | None = None,
    interrupted: bool = False,
    response_id: str | None = None,
) -> dict:
    item: dict = {
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }
    if model is not None:
        item["model"] = model
    if interrupted:
        item["interrupted"] = True
    if response_id is not None:
        item["response_id"] = response_id
    return item


def test_records_map_user_and_assistant_turns(tmp_path: Path) -> None:
    items = [
        _user_item("hello"),
        _assistant_item("hi there", model="qwen-max"),
    ]
    records = qnb.qwen_session_records_from_session_items(
        items, qwen_session_id="sess-1", cwd=tmp_path, timestamp="2026-01-01T00:00:00.000Z"
    )
    assert [r["type"] for r in records] == ["user", "assistant"]

    user, asst = records
    assert user["message"] == {"role": "user", "parts": [{"text": "hello"}]}
    assert user["sessionId"] == "sess-1"
    assert user["parentUuid"] is None
    assert user["cwd"] == os.path.realpath(str(tmp_path))

    assert asst["message"] == {"role": "model", "parts": [{"text": "hi there"}]}
    assert asst["model"] == "qwen-max"
    # Records form a linked list chained by uuid/parentUuid.
    assert asst["parentUuid"] == user["uuid"]
    assert "contextWindowSize" in asst and "usageMetadata" in asst


def test_records_skip_interrupted_response_group() -> None:
    items = [
        _user_item("keep me"),
        _assistant_item("kept reply"),
        _user_item("interrupted question", response_id="resp-x"),
        _assistant_item("partial cancelled", interrupted=True, response_id="resp-x"),
    ]
    records = qnb.qwen_session_records_from_session_items(
        items, qwen_session_id="sess-2", cwd="/tmp/x"
    )
    texts = [r["message"]["parts"][0]["text"] for r in records]
    assert texts == ["keep me", "kept reply"]


def test_records_skip_empty_and_non_message_items() -> None:
    items = [
        {"type": "function_call", "name": "ls", "call_id": "c1", "arguments": "{}"},
        _user_item(""),  # empty text → dropped
        _user_item("real"),
        _assistant_item("reply"),  # so "real" isn't a trailing unanswered prompt
    ]
    records = qnb.qwen_session_records_from_session_items(
        items, qwen_session_id="sess-3", cwd="/tmp/x"
    )
    assert [r["message"]["parts"][0]["text"] for r in records] == ["real", "reply"]


def test_records_drop_trailing_unanswered_user_prompt() -> None:
    # A qwen-native source stamps a distinct per-event response_id and never sets
    # `interrupted`, so a cancelled last turn leaves a dangling user prompt the
    # response-group skip can't catch. It must be dropped from the rebuild.
    items = [
        _user_item("q1", response_id="qwen:a"),
        _assistant_item("a1", response_id="qwen:b"),
        _user_item("cancelled prompt", response_id="qwen:c"),  # no assistant reply
    ]
    records = qnb.qwen_session_records_from_session_items(items, qwen_session_id="s", cwd="/tmp/x")
    texts = [r["message"]["parts"][0]["text"] for r in records]
    assert texts == ["q1", "a1"]


def test_records_drop_all_trailing_user_prompts() -> None:
    # Multiple consecutive dangling user messages all get dropped.
    items = [
        _user_item("q1"),
        _assistant_item("a1"),
        _user_item("dangling 1"),
        _user_item("dangling 2"),
    ]
    records = qnb.qwen_session_records_from_session_items(items, qwen_session_id="s", cwd="/tmp/x")
    assert [r["type"] for r in records] == ["user", "assistant"]


def test_records_default_model_used_when_item_has_none() -> None:
    records = qnb.qwen_session_records_from_session_items(
        [_assistant_item("a")], qwen_session_id="s", cwd="/tmp/x", model="fallback-model"
    )
    assert records[0]["model"] == "fallback-model"


def test_record_uuids_are_deterministic() -> None:
    items = [_user_item("hi"), _assistant_item("yo")]
    a = qnb.qwen_session_records_from_session_items(items, qwen_session_id="s", cwd="/tmp/x")
    b = qnb.qwen_session_records_from_session_items(items, qwen_session_id="s", cwd="/tmp/x")
    assert [r["uuid"] for r in a] == [r["uuid"] for r in b]


def test_write_recording_creates_jsonl_and_sidecars(tmp_path: Path, monkeypatch) -> None:
    # Point ~/.qwen at a temp HOME so we don't touch the real recording store.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))

    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = str(uuid.uuid4())
    records = qnb.qwen_session_records_from_session_items(
        [_user_item("remember X"), _assistant_item("ok")],
        qwen_session_id=session_id,
        cwd=workspace,
    )

    recording = qnb.write_qwen_session_recording(session_id, workspace, records)

    # The recording exists where the resume-gate looks for it.
    assert recording.is_file()
    assert qnb.qwen_session_recording_exists(session_id, workspace)
    assert recording == qnb.qwen_session_recording_path(session_id, workspace)

    lines = recording.read_text().strip().splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["type"] == "user"

    runtime = json.loads((recording.parent / f"{session_id}.runtime.json").read_text())
    assert runtime["session_id"] == session_id
    assert runtime["work_dir"] == os.path.realpath(str(workspace))
    assert isinstance(runtime["started_at"], float)
    assert runtime["qwen_version"]

    meta = json.loads((recording.parent.parent / "meta.json").read_text())
    assert meta["version"] == 1


def test_write_recording_preserves_existing_meta(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = str(uuid.uuid4())
    records = qnb.qwen_session_records_from_session_items(
        [_user_item("hi")], qwen_session_id=session_id, cwd=workspace
    )
    # Pre-create meta.json with a sentinel createdAt the writer must not clobber.
    recording_path = qnb.qwen_session_recording_path(session_id, workspace)
    meta_path = recording_path.parent.parent / "meta.json"
    meta_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path.write_text(json.dumps({"version": 1, "createdAt": "SENTINEL", "updatedAt": "x"}))

    qnb.write_qwen_session_recording(session_id, workspace, records)

    assert json.loads(meta_path.read_text())["createdAt"] == "SENTINEL"


def test_write_recording_sidecar_failure_leaves_no_gate_jsonl(tmp_path: Path, monkeypatch) -> None:
    # The resume gate keys on the .jsonl. If a sidecar write fails, the .jsonl
    # must NOT exist — otherwise the gate would pick --resume onto a session with
    # no runtime.json and land on qwen's blocking "No saved session" screen (B1).
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    workspace = tmp_path / "ws"
    workspace.mkdir()
    session_id = str(uuid.uuid4())
    records = qnb.qwen_session_records_from_session_items(
        [_user_item("hi"), _assistant_item("ok")], qwen_session_id=session_id, cwd=workspace
    )

    # Fail the runtime.json sidecar write (it's written before the .jsonl).
    real_atomic = qnb._atomic_write_text

    def _boom(target: Path, text: str) -> None:
        if target.name.endswith(".runtime.json"):
            raise RuntimeError("disk full")
        real_atomic(target, text)

    monkeypatch.setattr(qnb, "_atomic_write_text", _boom)

    with pytest.raises(RuntimeError):
        qnb.write_qwen_session_recording(session_id, workspace, records)

    # The gate file was never committed → a clean fresh launch, not the blocking screen.
    assert not qnb.qwen_session_recording_exists(session_id, workspace)


@pytest.mark.skipif(
    shutil.which("qwen") is None or os.environ.get("OMNIGENT_QWEN_E2E") != "1",
    reason="needs the qwen CLI + configured auth; opt in with OMNIGENT_QWEN_E2E=1",
)
def test_synthesized_recording_loads_on_resume(tmp_path: Path) -> None:
    """A real ``qwen --resume`` loads the synthesized recording and recalls the fact.

    Network + auth dependent — skipped unless OMNIGENT_QWEN_E2E=1 and qwen is on
    PATH. This is the regression guard for the on-disk format (records + the
    runtime/meta sidecars) that the resume gate depends on. Uses the real
    ``~/.qwen`` for auth; the tmp workspace gives a unique project slug so the
    recording lands in its own dir and never collides with a real session.
    """
    workspace = tmp_path / "ws"
    workspace.mkdir()
    secret = "PURPLE-PENGUIN-42"
    session_id = str(uuid.uuid4())
    records = qnb.qwen_session_records_from_session_items(
        [
            _user_item(f"Remember: the secret code is {secret}."),
            _assistant_item(f"Acknowledged. The secret code is {secret}."),
        ],
        qwen_session_id=session_id,
        cwd=workspace,
    )
    qnb.write_qwen_session_recording(session_id, workspace, records)

    proc = subprocess.run(
        [
            "qwen",
            "--resume",
            session_id,
            "-p",
            "What is the secret code? Reply with ONLY the code.",
        ],
        cwd=str(workspace),
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    assert secret in proc.stdout
