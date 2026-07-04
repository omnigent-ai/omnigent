# tests/inner/test_opencode_translate.py
from omnigent.inner.executor import (
    ReasoningChunk,
    TextChunk,
    ToolCallComplete,
    ToolCallRequest,
    ToolCallStatus,
)
from omnigent.inner.opencode_executor import (
    _PartTracker,
    _tokens_to_usage,
    _translate_part_event,
)


def test_text_delta_emits_only_suffix():
    tracker = _PartTracker()
    p = {"id": "p1", "type": "text", "text": "Hello"}
    out1 = _translate_part_event(p, tracker, emit_reasoning=False)
    assert [e.text for e in out1 if isinstance(e, TextChunk)] == ["Hello"]
    p2 = {"id": "p1", "type": "text", "text": "Hello world"}
    out2 = _translate_part_event(p2, tracker, emit_reasoning=False)
    assert [e.text for e in out2 if isinstance(e, TextChunk)] == [" world"]


def test_reasoning_gated():
    tracker = _PartTracker()
    p = {"id": "r1", "type": "reasoning", "text": "thinking"}
    assert _translate_part_event(p, tracker, emit_reasoning=False) == []
    tracker2 = _PartTracker()
    out = _translate_part_event(p, tracker2, emit_reasoning=True)
    assert any(isinstance(e, ReasoningChunk) for e in out)


def test_tool_completed_emits_request_and_complete():
    tracker = _PartTracker()
    p = {
        "id": "t1",
        "type": "tool",
        "tool": "bash",
        "callID": "c1",
        "state": {
            "status": "completed",
            "input": {"command": "ls"},
            "output": "file.txt",
            "title": "ls",
            "metadata": {},
        },
    }
    out = _translate_part_event(p, tracker, emit_reasoning=False)
    req = [e for e in out if isinstance(e, ToolCallRequest)]
    comp = [e for e in out if isinstance(e, ToolCallComplete)]
    assert req and req[0].name == "bash" and req[0].args == {"command": "ls"}
    assert comp and comp[0].status == ToolCallStatus.SUCCESS
    assert comp[0].result == "file.txt"


def test_tool_running_emits_request_only():
    tracker = _PartTracker()
    p = {
        "id": "t2",
        "type": "tool",
        "tool": "edit",
        "callID": "c2",
        "state": {"status": "running", "input": {"path": "x"}},
    }
    out = _translate_part_event(p, tracker, emit_reasoning=False)
    assert [type(e).__name__ for e in out] == ["ToolCallRequest"]
    # Re-emitting the same running part again must not duplicate the request.
    assert _translate_part_event(p, tracker, emit_reasoning=False) == []


def test_tool_error_status():
    tracker = _PartTracker()
    p = {
        "id": "t3",
        "type": "tool",
        "tool": "bash",
        "callID": "c3",
        "state": {"status": "error", "error": "boom", "input": {}},
    }
    out = _translate_part_event(p, tracker, emit_reasoning=False)
    comp = [e for e in out if isinstance(e, ToolCallComplete)]
    assert comp and comp[0].status == ToolCallStatus.ERROR and comp[0].error == "boom"


def test_tokens_to_usage():
    tokens = {"input": 100, "output": 50, "reasoning": 10, "cache": {"read": 20, "write": 5}}
    assert _tokens_to_usage(tokens) == {
        "input_tokens": 100,
        "output_tokens": 50,
        "total_tokens": 150,
        "cache_read_input_tokens": 20,
        "cache_creation_input_tokens": 5,
    }


def test_tool_pending_then_completed_emits_request_with_real_args():
    tracker = _PartTracker()
    pid = "t-pending"
    pending = {
        "id": pid,
        "type": "tool",
        "tool": "bash",
        "callID": "c9",
        "state": {"status": "pending"},
    }
    running = {
        "id": pid,
        "type": "tool",
        "tool": "bash",
        "callID": "c9",
        "state": {"status": "running", "input": {"command": "ls -la"}},
    }
    completed = {
        "id": pid,
        "type": "tool",
        "tool": "bash",
        "callID": "c9",
        "state": {"status": "completed", "input": {"command": "ls -la"}, "output": "ok"},
    }
    out_pending = _translate_part_event(pending, tracker, emit_reasoning=False)
    out_running = _translate_part_event(running, tracker, emit_reasoning=False)
    out_completed = _translate_part_event(completed, tracker, emit_reasoning=False)
    # The pending update emits nothing.
    assert out_pending == []
    reqs = [e for e in out_running + out_completed if isinstance(e, ToolCallRequest)]
    comps = [e for e in out_running + out_completed if isinstance(e, ToolCallComplete)]
    assert len(reqs) == 1
    assert reqs[0].args == {"command": "ls -la"}  # real args, not {}
    assert len(comps) == 1 and comps[0].result == "ok"
