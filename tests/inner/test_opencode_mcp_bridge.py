# tests/inner/test_opencode_mcp_bridge.py
import pytest
from omnigent.inner.opencode_executor import _OmnigentToolBridge


@pytest.mark.asyncio
async def test_bridge_starts_and_reports_url():
    called = {}

    async def fake_executor(name, args):
        called["name"] = name
        called["args"] = args
        return {"ok": True}

    tools = [{"name": "echo", "description": "echo", "parameters": {
        "type": "object", "properties": {"msg": {"type": "string"}}}}]
    bridge = _OmnigentToolBridge(tools, fake_executor)
    url = await bridge.start()
    try:
        assert url.startswith("http://127.0.0.1:")
        assert url.endswith("/mcp")
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_registered_tool_advertises_schema_and_forwards_call():
    seen = {}

    async def fake_executor(name, args):
        seen["name"] = name
        seen["args"] = args
        return {"ok": True}

    schema = {"type": "object", "properties": {"msg": {"type": "string"}}, "required": ["msg"]}
    tools = [{"name": "echo", "description": "echo it", "parameters": schema}]
    bridge = _OmnigentToolBridge(tools, fake_executor)
    await bridge.start()
    try:
        # Reach into the FastMCP tool manager to verify the registered tool
        # advertises the real spec schema and that calling it round-trips
        # through the executor callback.
        tool = bridge._mcp._tool_manager._tools["echo"]
        assert tool.parameters == schema
        result = await tool.run({"msg": "hello"})
        assert seen == {"name": "echo", "args": {"msg": "hello"}}
    finally:
        await bridge.close()
