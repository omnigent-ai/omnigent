"""Unit tests for block types and BlockContext."""

from __future__ import annotations

from omnigent_client._blocks import (
    BlockContext,
    CompactionBlock,
    ErrorBlock,
    FileBlock,
    NativeToolBlock,
    ReasoningBlock,
    ReasoningStartBlock,
    ResponseEndBlock,
    ResponseStartBlock,
    RetryBlock,
    TextChunk,
    TextDone,
    ToolExecution,
    ToolGroup,
)


def test_block_context_defaults() -> None:
    """BlockContext has sensible defaults."""
    ctx = BlockContext()
    assert ctx.agent is None
    assert ctx.depth == 0
    assert ctx.turn == 0
    assert ctx.timestamp > 0


def test_block_context_depth_from_dotted_agent() -> None:
    """Depth is derived from dots in agent name by the renderer,
    but the dataclass itself just stores the value."""
    ctx = BlockContext(agent="coder.researcher", depth=1)
    assert ctx.depth == 1
    assert ctx.agent == "coder.researcher"


def test_all_blocks_have_context() -> None:
    """Every block type carries a BlockContext."""
    blocks = [
        ResponseStartBlock(model="test", response_id="r1"),
        ReasoningStartBlock(),
        ReasoningBlock(reasoning_text="thinking", summary_text="summary"),
        ToolGroup(executions=[]),
        NativeToolBlock(tool_type="web_search_call", label="search", data={}),
        TextChunk(text="hello"),
        TextDone(full_text="hello world", has_code_blocks=False),
        ErrorBlock(message="oops", source="llm"),
        RetryBlock(source="tool", attempt=2, max_attempts=3, delay_seconds=1.0),
        CompactionBlock(),
        FileBlock(file_id="f1", filename="test.png"),
        ResponseEndBlock(status="completed"),
    ]
    for block in blocks:
        assert hasattr(block, "ctx"), f"{type(block).__name__} missing ctx"
        assert isinstance(block.ctx, BlockContext)


def test_text_done_code_block_detection() -> None:
    """TextDone.has_code_blocks is set correctly."""
    no_code = TextDone(full_text="just text", has_code_blocks=False)
    assert not no_code.has_code_blocks

    with_code = TextDone(full_text="```python\nprint('hi')\n```", has_code_blocks=True)
    assert with_code.has_code_blocks


def test_tool_execution_fields() -> None:
    """ToolExecution carries all expected fields."""
    ex = ToolExecution(
        name="Read",
        arguments={"file_path": "/tmp/test.py"},
        args_summary="test.py",
        call_id="call_123",
        agent_name="coder",
        executed_by="client",
        output="file content here",
    )
    assert ex.name == "Read"
    assert ex.arguments["file_path"] == "/tmp/test.py"
    assert ex.output == "file content here"
    assert ex.executed_by == "client"


def test_tool_group_multiple_executions() -> None:
    """ToolGroup can hold multiple executions."""
    group = ToolGroup(
        executions=[
            ToolExecution(
                name="Glob",
                arguments={},
                args_summary="*",
                call_id="c1",
                agent_name="coder",
                executed_by="server",
                output="a.py\nb.py",
            ),
            ToolExecution(
                name="Read",
                arguments={},
                args_summary="a.py",
                call_id="c2",
                agent_name="coder",
                executed_by="client",
                output="content",
            ),
        ],
    )
    assert len(group.executions) == 2
    assert group.executions[0].name == "Glob"
    assert group.executions[1].output == "content"
