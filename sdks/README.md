# SDKs

Python packages for integrating with omnigent.

## Structure

```
sdks/
  python-client/           # Headless HTTP/SSE client
    pyproject.toml
    omnigent_client/    # import omnigent_client
  ui/                      # Terminal UI layer (Rich + prompt_toolkit)
    pyproject.toml
    omnigent_ui_sdk/    # import omnigent_ui_sdk
      terminal/
```

Claude Code skills for SDK development live under `.claude/skills/`.

## `omnigent_client` — the headless client

Pure HTTP/SSE client. No Rich, no prompt_toolkit, no terminal
dependencies. Use this for:

- Scripts that invoke an agent and collect output.
- Web frontends, Slack bots, test harnesses — anything non-terminal.
- As the foundation layer for `omnigent_ui_sdk` below.

Three levels of abstraction are available:

1. **Raw events** — `session.send()` yields typed wire events
   (`ResponseCreated`, `TextDelta`, `ToolCallDone`, etc.). 1:1 with SSE.
2. **Semantic blocks** — `BlockStream` folds events into higher-level
   units (`TextChunk`, `ToolGroup`, `ReasoningBlock`, …). Frameworks
   consuming these don't need to reimplement the stream state machine.
3. **Composable transforms** — `pipe`, `skip_blocks`,
   `skip_intermediate_ends`, `merge_text_across_iterations`, `only_agent`.

### Install

```bash
pip install -e sdks/python-client
```

### Minimal invocation

```python
import asyncio
from omnigent_client import OmnigentClient

async def main():
    async with OmnigentClient(base_url="http://localhost:8080") as client:
        session = client.session(model="archer")
        async for event in session.send("hello"):
            print(event)

asyncio.run(main())
```

### Using semantic blocks (web, Slack, or any custom UI)

```python
from omnigent_client import (
    BlockStream, TextChunk, ToolGroup, ResponseEndBlock,
    pipe, skip_intermediate_ends,
)

async def handle(websocket, session, text):
    block_stream = BlockStream()
    async for block in pipe(
        block_stream.stream(session, text),
        skip_intermediate_ends(),
    ):
        match block:
            case TextChunk(text=t):
                await websocket.send_json({"type": "text", "chunk": t})
            case ToolGroup(executions=execs):
                await websocket.send_json({"type": "tools", "data": [
                    {"name": e.name, "output": e.output} for e in execs
                ]})
            case ResponseEndBlock(status=s):
                await websocket.send_json({"type": "done", "status": s})
```

## `omnigent_ui_sdk` — the terminal frontend

Thin layer on top of `omnigent_client` for building terminal REPLs.
Provides:

- **RichBlockFormatter** — converts `StreamBlock` values to Rich
  renderables. Subclass and override one method to customize.
- **TerminalHost** — manages prompt_toolkit: pinned input bar,
  background streaming, Escape to cancel, persistent history.

### Install

```bash
pip install -e sdks/ui
```

(Pulls in `omnigent-client` as a dependency.)

### Minimal REPL

```python
import asyncio
from omnigent_client import (
    OmnigentClient, LocalServer, BlockStream,
    pipe, skip_intermediate_ends,
)
from omnigent_ui_sdk import RichBlockFormatter, TerminalHost

async def main():
    async with LocalServer(agent_path="./my-agent/") as server:
        client = server.client
        session = client.session(model="my-agent")
        block_stream = BlockStream()
        fmt = RichBlockFormatter()
        host = TerminalHost(model_name="my agent")

        async def on_input(text):
            host.output(fmt.user_message(text))
            async for block in pipe(
                block_stream.stream(session, text),
                skip_intermediate_ends(),
            ):
                for item in fmt.format(block):
                    host.output(item)
                await asyncio.sleep(0)

        async with host:
            host.output(fmt.welcome("my agent"))
            await host.run(on_input)

asyncio.run(main())
```

### Customization

Override one formatter method:

```python
class MyFormatter(RichBlockFormatter):
    def format_tool_group(self, block):
        from rich.tree import Tree
        tree = Tree("Tools")
        for ex in block.executions:
            tree.add(f"{ex.name} → {(ex.output or '')[:50]}")
        return [tree]
```

Use transforms to reshape the block stream:

```python
from omnigent_client import pipe, skip_blocks, ReasoningBlock

stream = pipe(
    block_stream.stream(session, text),
    skip_blocks(ReasoningBlock),  # Hide thinking
)
```

## Reference Implementation

The built-in REPL at `omnigent/repl/` demonstrates all features:
streaming, tool calls, reasoning, slash commands, conversation
switching, elapsed timer. See `omnigent/repl/_repl.py`.
