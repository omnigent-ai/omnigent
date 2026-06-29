// Mirrors tests/frontends/sdk/test_stream.py.
//
// Hand-built event sequences → BlockStream → assert on emitted blocks.
// When a Python test changes (or a new one is added there), mirror it
// here. See web/README.md "Reducer parity" for the workflow.

import { describe, expect, it } from "vitest";
import type {
  AnyBlock,
  ElicitationBlock,
  ReasoningBlock,
  ReasoningChunk,
  ResponseEndBlock,
  SlashCommandBlock,
  TextChunk,
  TextDone,
  ToolGroup,
  ToolResultBlock,
  UserMessageBlock,
} from "./blocks";
import { BlockStream } from "./blockStream";
import type { StreamEvent } from "./events";
import type { Response } from "./types";

function makeResponse(opts?: {
  responseId?: string;
  status?: string;
  model?: string;
  conversationId?: string | null;
}): Response {
  return {
    id: opts?.responseId ?? "resp_1",
    status: opts?.status ?? "completed",
    model: opts?.model ?? "test-agent",
    conversation: opts?.conversationId ? { id: opts.conversationId } : null,
  };
}

function reduce(events: StreamEvent[], opts?: { textFlushThreshold?: number }): AnyBlock[] {
  return new BlockStream({ textFlushThreshold: opts?.textFlushThreshold ?? 10 }).reduceSync(events);
}

function blockTypes(blocks: AnyBlock[]): string[] {
  return blocks.map((b) => b.type);
}

describe("BlockStream — response_start", () => {
  it("populates conversationId from response.created", () => {
    const blocks = reduce([
      {
        type: "response_created",
        response: makeResponse({ conversationId: "conv_xyz" }),
      },
      { type: "response_completed", response: makeResponse() },
    ]);
    const start = blocks.find((b) => b.type === "response_start");
    expect(start).toBeDefined();
    if (start && start.type === "response_start") {
      expect(start.conversationId).toBe("conv_xyz");
    }
  });

  it("conversationId is null when response.conversation is missing", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() }, // no conversationId
      { type: "response_completed", response: makeResponse() },
    ]);
    const start = blocks.find((b) => b.type === "response_start");
    if (start && start.type === "response_start") {
      expect(start.conversationId).toBeNull();
    }
  });
});

describe("BlockStream — block ctx carries response_id and item_id", () => {
  it("every block ctx.responseId is the active response id from response.created", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_xyz" }) },
      { type: "text_delta", delta: "hi" },
      {
        type: "tool_call",
        name: "Read",
        arguments: {},
        callId: "c1",
        status: "completed",
        agentName: "agent",
        itemId: "fc_item_1",
        responseId: "resp_xyz",
      },
      {
        type: "tool_result",
        callId: "c1",
        output: "ok",
        itemId: "fco_item_1",
        responseId: "resp_xyz",
      },
      { type: "message_done", content: [], itemId: "msg_item_1", responseId: "resp_xyz" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_xyz" }) },
    ]);

    for (const b of blocks) {
      expect(b.ctx.responseId).toBe("resp_xyz");
    }
  });

  it("tool, tool_result, native_tool, and text_done blocks carry their item id", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      { type: "text_delta", delta: "before tool" },
      {
        type: "tool_call",
        name: "Read",
        arguments: {},
        callId: "c1",
        status: "completed",
        agentName: "agent",
        itemId: "fc_item_1",
        responseId: "resp_1",
      },
      {
        type: "tool_result",
        callId: "c1",
        output: "ok",
        itemId: "fco_item_1",
        responseId: "resp_1",
      },
      { type: "text_delta", delta: "after" },
      {
        type: "native_tool_call",
        toolType: "web_search_call",
        data: { action: { type: "search", query: "foo" } },
        itemId: "nt_item_1",
        responseId: "resp_1",
      },
      { type: "message_done", content: [], itemId: "msg_item_1", responseId: "resp_1" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_1" }) },
    ]);

    const group = blocks.find((b): b is ToolGroup => b.type === "tool_group");
    expect(group?.ctx.itemId).toBe("fc_item_1");

    const result = blocks.find((b): b is ToolResultBlock => b.type === "tool_result");
    expect(result?.ctx.itemId).toBe("fco_item_1");

    const native = blocks.find((b) => b.type === "native_tool");
    expect(native?.ctx.itemId).toBe("nt_item_1");

    // The "before tool" text gets closed by the tool_call branch (no
    // item id available there) → first TextDone has itemId === null.
    // The "after" text gets closed by message_done, which DOES carry
    // the canonical item id → second TextDone stamps it.
    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones.length).toBe(2);
    expect(textDones[0]!.ctx.itemId).toBeNull();
    expect(textDones[1]!.ctx.itemId).toBe("msg_item_1");

    // Text chunks are pre-finalization; their item id stays null
    // because the canonical message id only arrives with message_done.
    const chunks = blocks.filter((b): b is TextChunk => b.type === "text_chunk");
    for (const c of chunks) {
      expect(c.ctx.itemId).toBeNull();
    }
  });

  it("slash_command kind='skill' emits a user-echo bubble before the SlashCommandBlock", () => {
    const blocks = reduce([
      {
        type: "slash_command",
        kind: "skill",
        name: "dev-productivity:simplify",
        arguments: "src/foo.ts",
        output: null,
        agentName: "claude-native-ui",
        createdBy: "alice@example.com",
        itemId: "sc_1",
        responseId: "resp_slash",
      },
    ]);
    // Pin the exact sequence: the typed message must render as a user
    // bubble AND the Skill indicator must still follow it (the receipt
    // is the only transcript record of the user's send).
    expect(blockTypes(blocks)).toEqual(["user_message", "slash_command"]);
    const echo = blocks[0] as UserMessageBlock;
    // Echo reconstructs the literal composer text — if this is empty or
    // missing the args, the user's message visually vanished again.
    expect(echo.content).toEqual([
      { type: "input_text", text: "/dev-productivity:simplify src/foo.ts" },
    ]);
    // Derived id (`<receipt id>:user`) — must match itemsToBlocks' echo
    // so live and snapshot copies dedupe on reconcile.
    expect(echo.ctx.itemId).toBe("sc_1:user");
    // Echo inherits the receipt's response id (stamped via
    // applyOutputItemResponseId) so it groups with the indicator.
    expect(echo.ctx.responseId).toBe("resp_slash");
    // Authorship threads through for shared-session labels.
    expect(echo.ctx.createdBy).toBe("alice@example.com");
    const slash = blocks[1] as SlashCommandBlock;
    expect(slash.kind).toBe("skill");
    expect(slash.name).toBe("dev-productivity:simplify");
    expect(slash.arguments).toBe("src/foo.ts");
    expect(slash.output).toBeNull();
    expect(slash.ctx.itemId).toBe("sc_1");
    expect(slash.ctx.responseId).toBe("resp_slash");
  });

  it("skill echo with no args omits the trailing space", () => {
    const blocks = reduce([
      {
        type: "slash_command",
        kind: "skill",
        name: "oncall",
        arguments: "",
        output: null,
        agentName: "claude-native-ui",
        itemId: "sc_noargs",
        responseId: "resp_slash",
      },
    ]);
    const echo = blocks[0] as UserMessageBlock;
    // "/oncall " (trailing space) would be a reconstruction artifact the
    // user never typed.
    expect(echo.content).toEqual([{ type: "input_text", text: "/oncall" }]);
    expect(echo.ctx.responseId).toBe("resp_slash");
    // No author on the event → none invented on the echo.
    expect(echo.ctx.createdBy).toBeUndefined();
  });

  it("slash_command kind='command' stays indicator-only (no user echo)", () => {
    const blocks = reduce([
      {
        type: "slash_command",
        kind: "command",
        name: "effort",
        arguments: "high",
        output: null,
        agentName: "claude-native-ui",
        itemId: "sc_cmd",
        responseId: "resp_cmd",
      },
    ]);
    // Surfaced CLI built-ins are state changes, not prose — a synthetic
    // user bubble here would double-render every /effort, /model, etc.
    expect(blockTypes(blocks)).toEqual(["slash_command"]);
    const slash = blocks[0] as SlashCommandBlock;
    expect(slash.kind).toBe("command");
    expect(slash.name).toBe("effort");
  });

  it("output_item.done tool blocks carry responseId without response.created", () => {
    const blocks = reduce([
      {
        type: "tool_call",
        name: "Read",
        arguments: { file_path: "TODO.md" },
        callId: "toolu_read_1",
        status: "completed",
        agentName: "claude-native-ui",
        itemId: "fc_terminal_1",
        responseId: "resp_terminal_1",
      },
      {
        type: "tool_result",
        callId: "toolu_read_1",
        output: "todo contents",
        itemId: "fco_terminal_1",
        responseId: "resp_terminal_1",
      },
    ]);

    const group = blocks.find((b): b is ToolGroup => b.type === "tool_group");
    expect(group?.ctx.responseId).toBe("resp_terminal_1");
    const result = blocks.find((b): b is ToolResultBlock => b.type === "tool_result");
    expect(result?.ctx.responseId).toBe("resp_terminal_1");
    expect(result?.output).toBe("todo contents");
  });
});

describe("BlockStream — text", () => {
  it("message_done content renders without preceding text deltas", () => {
    const blocks = reduce([
      {
        type: "message_done",
        content: [{ type: "output_text", text: "Hello from transcript" }],
        itemId: "msg_terminal_1",
        responseId: "resp_terminal_1",
      },
    ]);

    expect(blockTypes(blocks)).toEqual(["text_done"]);
    const textDone = blocks[0] as TextDone;
    expect(textDone.ctx.itemId).toBe("msg_terminal_1");
    expect(textDone.ctx.responseId).toBe("resp_terminal_1");
    expect(textDone.fullText).toBe("Hello from transcript");
  });

  it("simple text response → start, text, done, end", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "response_in_progress", response: makeResponse({ status: "in_progress" }) },
      { type: "text_delta", delta: "Hello " },
      { type: "text_delta", delta: "world!" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const types = blockTypes(blocks);
    expect(types).toContain("response_start");
    expect(types).toContain("text_done");
    expect(types).toContain("response_end");

    const textDone = blocks.find((b): b is TextDone => b.type === "text_done");
    expect(textDone).toBeDefined();
    expect(textDone!.fullText).toBe("Hello world!");
    expect(textDone!.hasCodeBlocks).toBe(false);
  });

  it("code fences → hasCodeBlocks=true", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "text_delta", delta: "```python\nprint('hi')\n```" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const textDone = blocks.find((b): b is TextDone => b.type === "text_done");
    expect(textDone?.hasCodeBlocks).toBe(true);
  });

  it("text chunks flush on newline boundaries", () => {
    // threshold=10; the input has a newline early so the first chunk
    // should be "short\n" (newline-flushed) regardless of threshold.
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "text_delta",
        delta: "short\nline two is longer than threshold characters",
      },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const chunks = blocks.filter((b): b is TextChunk => b.type === "text_chunk");
    expect(chunks.length).toBeGreaterThanOrEqual(1);
    expect(chunks[0]!.text).toBe("short\n");
  });
});

describe("BlockStream — reasoning", () => {
  it("reasoning deltas surface as live chunks; trailing block is suppressed", () => {
    // Both ReasoningDelta and ReasoningSummaryDelta should reach the
    // consumer as ReasoningChunk. When chunks fire, the trailing
    // ReasoningBlock is suppressed — emitting both would render the
    // same text twice (once streaming, once as a panel).
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "reasoning_started" },
      { type: "reasoning_delta", delta: "Let me think...\n" },
      { type: "reasoning_summary_delta", delta: "Summary here\n" },
      { type: "text_delta", delta: "Answer" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const types = blockTypes(blocks);
    expect(types).toContain("reasoning_start");

    const chunks = blocks.filter((b): b is ReasoningChunk => b.type === "reasoning_chunk");
    expect(chunks.length).toBeGreaterThan(0);
    const joined = chunks.map((c) => c.text).join("");
    expect(joined).toContain("Let me think");
    expect(joined).toContain("Summary here");

    expect(types).not.toContain("reasoning_block");
  });

  it("reasoning started but no deltas → empty ReasoningBlock fires", () => {
    // Edge case: ReasoningStarted arrives but no deltas follow before
    // the section closes. With no chunks to stream, the ReasoningBlock
    // must still fire so non-streaming renderers know reasoning
    // happened (even if empty).
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "reasoning_started" },
      // No deltas — straight to text.
      { type: "text_delta", delta: "Direct answer" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const types = blockTypes(blocks);
    expect(types).toContain("reasoning_start");
    expect(types).not.toContain("reasoning_chunk");
    expect(types).toContain("reasoning_block");

    const block = blocks.find((b): b is ReasoningBlock => b.type === "reasoning_block")!;
    expect(block.reasoningText).toBe("");
    expect(block.summaryText).toBe("");
  });

  it("reasoning delta without started → implicit start before first chunk", () => {
    // Codex events arrive as bridged ReasoningSummaryDelta with no
    // preceding ReasoningStarted. The block stream must synthesize a
    // ReasoningStartBlock on the first delta so the formatter still
    // gets its "thinking…" anchor.
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      // No reasoning_started — straight into a delta.
      { type: "reasoning_summary_delta", delta: "$ ls /tmp\n" },
      { type: "text_delta", delta: "Result" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const startIdx = blocks.findIndex((b) => b.type === "reasoning_start");
    const chunkIdx = blocks.findIndex((b) => b.type === "reasoning_chunk");
    expect(startIdx).toBeGreaterThanOrEqual(0);
    expect(chunkIdx).toBeGreaterThanOrEqual(0);
    expect(startIdx).toBeLessThan(chunkIdx);
  });

  it("interleaved text→reasoning→text closes each text section (no orphan, no concatenation)", () => {
    // think→speak→think→speak in one response: reasoning must close text
    // or the pre-reasoning text orphans and the final text_done concatenates.
    const t1 = "First answer here.";
    const t2 = "Second answer here.";
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "reasoning_started" },
      { type: "reasoning_delta", delta: "plan it" },
      { type: "text_delta", delta: t1 },
      { type: "reasoning_started" },
      { type: "reasoning_delta", delta: "continue" },
      { type: "text_delta", delta: t2 },
      { type: "response_completed", response: makeResponse() },
    ]);

    const dones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(dones.map((d) => d.fullText)).toEqual([t1, t2]);

    // Reasoning-start for the second think block must land BETWEEN the
    // two closed text sections, not after both.
    const firstDone = blocks.indexOf(dones[0]!);
    const secondDone = blocks.indexOf(dones[1]!);
    const reasoningStarts = blocks
      .map((b, i) => (b.type === "reasoning_start" ? i : -1))
      .filter((i) => i >= 0);
    expect(reasoningStarts.some((i) => i > firstDone && i < secondDone)).toBe(true);
  });

  it("consecutive reasoning blocks are separated, not run together", () => {
    // Summarized thinking arrives as several thinking blocks (each a fresh
    // reasoning_started). Without a separator the renderer joins chunks
    // "…item one.Item two"; assert a break is inserted and no tail is lost.
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "reasoning_started" },
      { type: "reasoning_delta", delta: "First thought." },
      { type: "reasoning_started" },
      { type: "reasoning_delta", delta: "Second thought." },
      { type: "text_delta", delta: "Answer" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const joined = blocks
      .filter((b): b is ReasoningChunk => b.type === "reasoning_chunk")
      .map((c) => c.text)
      .join("");
    expect(joined).toContain("First thought.\n\nSecond thought.");
    expect(joined).not.toContain("First thought.Second thought.");
  });
});

describe("BlockStream — multi-response (session-lifetime reducer)", () => {
  // Background: with /v1/responses, web built a new BlockStream
  // per POST so the reducer only ever saw one response.created. After
  // the /v1/sessions migration, a single reducer spans every task in
  // the session — multiple response.created events arrive on the same
  // instance. These tests pin the boundaries that matter.

  it("emits response_start on EVERY response.created (one bubble per task)", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      { type: "response_completed", response: makeResponse({ responseId: "resp_1" }) },
      { type: "response_created", response: makeResponse({ responseId: "resp_2" }) },
      { type: "response_completed", response: makeResponse({ responseId: "resp_2" }) },
    ]);

    const starts = blocks.filter((b) => b.type === "response_start");
    expect(starts).toHaveLength(2);
    expect(starts.map((b) => (b as { responseId: string }).responseId)).toEqual([
      "resp_1",
      "resp_2",
    ]);
    expect(starts.map((b) => b.ctx.responseId)).toEqual(["resp_1", "resp_2"]);
  });

  it("stamps task-2 blocks with the task-2 responseId (state.responseId is bumped on response.created)", () => {
    const blocks = reduce(
      [
        { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
        { type: "text_delta", delta: "first" },
        { type: "message_done", content: [], itemId: "msg_1", responseId: "resp_1" },
        { type: "response_completed", response: makeResponse({ responseId: "resp_1" }) },
        { type: "response_created", response: makeResponse({ responseId: "resp_2" }) },
        { type: "text_delta", delta: "second" },
        { type: "message_done", content: [], itemId: "msg_2", responseId: "resp_2" },
        { type: "response_completed", response: makeResponse({ responseId: "resp_2" }) },
      ],
      { textFlushThreshold: 1 },
    );

    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones).toHaveLength(2);
    expect(textDones[0]!.fullText).toBe("first");
    expect(textDones[0]!.ctx.responseId).toBe("resp_1");
    expect(textDones[1]!.fullText).toBe("second");
    expect(textDones[1]!.ctx.responseId).toBe("resp_2");
  });

  it("resets seenCallIds across tasks — same callId in two tasks renders TWO tool cards", () => {
    // The claude-sdk MCP dedup guards an intra-task double-emit
    // (inline observed + post-stream action_required). Across task
    // boundaries the SDK can legitimately reuse a tool_use_id-shaped
    // call id; a session-wide dedup would silently drop the second
    // turn's call. Clearing the set per response.created keeps the
    // invariant pinned to "render at most once per task".
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      {
        type: "tool_call",
        name: "Read",
        arguments: { file_path: "/a" },
        callId: "shared_call_id",
        status: "completed",
        agentName: "coder",
        itemId: "",
        responseId: "resp_1",
      },
      {
        type: "tool_result",
        callId: "shared_call_id",
        output: "first task output",
        itemId: "",
        responseId: "resp_1",
      },
      { type: "response_completed", response: makeResponse({ responseId: "resp_1" }) },
      { type: "response_created", response: makeResponse({ responseId: "resp_2" }) },
      {
        type: "tool_call",
        name: "Read",
        arguments: { file_path: "/b" },
        callId: "shared_call_id",
        status: "completed",
        agentName: "coder",
        itemId: "",
        responseId: "resp_2",
      },
      {
        type: "tool_result",
        callId: "shared_call_id",
        output: "second task output",
        itemId: "",
        responseId: "resp_2",
      },
      { type: "response_completed", response: makeResponse({ responseId: "resp_2" }) },
    ]);

    const groups = blocks.filter((b): b is ToolGroup => b.type === "tool_group");
    expect(groups).toHaveLength(2);
    expect(groups[0]!.executions[0]!.arguments).toEqual({ file_path: "/a" });
    expect(groups[0]!.ctx.responseId).toBe("resp_1");
    expect(groups[1]!.executions[0]!.arguments).toEqual({ file_path: "/b" });
    expect(groups[1]!.ctx.responseId).toBe("resp_2");

    const results = blocks.filter((b): b is ToolResultBlock => b.type === "tool_result");
    expect(results).toHaveLength(2);
    expect(results[0]!.output).toBe("first task output");
    expect(results[1]!.output).toBe("second task output");
  });

  it("no stale text/reasoning bleeds across the task boundary", () => {
    // Reasoning + text sections close on response.completed via the
    // terminal-event branch (closeReasoning + closeText). The
    // implicit reset is what keeps task-1 reasoning out of task-2's
    // first emission. Pin it so a future refactor that breaks the
    // close doesn't silently leak. Deltas are short and space-free
    // so they sit in the accumulator and flush as a single chunk on
    // close, making per-task content easy to assert on.
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      { type: "reasoning_started" },
      { type: "reasoning_delta", delta: "t1think" },
      { type: "text_delta", delta: "t1ans" },
      { type: "message_done", content: [], itemId: "msg_1", responseId: "resp_1" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_1" }) },
      { type: "response_created", response: makeResponse({ responseId: "resp_2" }) },
      { type: "reasoning_started" },
      { type: "reasoning_delta", delta: "t2think" },
      { type: "text_delta", delta: "t2ans" },
      { type: "message_done", content: [], itemId: "msg_2", responseId: "resp_2" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_2" }) },
    ]);

    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones).toHaveLength(2);
    expect(textDones[0]!.fullText).toBe("t1ans");
    expect(textDones[0]!.ctx.responseId).toBe("resp_1");
    expect(textDones[1]!.fullText).toBe("t2ans");
    expect(textDones[1]!.ctx.responseId).toBe("resp_2");

    // Reasoning accumulator flushes as one chunk per task on close.
    // Their texts must not concatenate across the boundary.
    const reasoningChunks = blocks.filter((b): b is ReasoningChunk => b.type === "reasoning_chunk");
    expect(reasoningChunks).toHaveLength(2);
    expect(reasoningChunks[0]!.text).toBe("t1think");
    expect(reasoningChunks[0]!.ctx.responseId).toBe("resp_1");
    expect(reasoningChunks[1]!.text).toBe("t2think");
    expect(reasoningChunks[1]!.ctx.responseId).toBe("resp_2");
  });

  it("no stale text bleeds when the cancelled turn's terminal event was dropped (fenced Stop)", () => {
    // The server fences a Stopped turn and drops its response.cancelled, so
    // the client never gets a terminal event for task 1 and closeText never
    // fires on the cancel. Task 2's response_created must finalize task 1's
    // partial under resp_1 so it doesn't concatenate into resp_2's bubble.
    // Without the close-on-created guard there is ONE text_done whose
    // fullText is "t1partialt2ans" (the leak the web UI showed). Deltas are
    // space-free + under the flush threshold so they sit in the accumulator
    // and flush as one chunk on close, making per-task content assertable.
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      { type: "text_delta", delta: "t1partial" },
      // No response_cancelled / response_completed for resp_1: the server
      // fence dropped it. The next response_created is the only signal.
      { type: "response_created", response: makeResponse({ responseId: "resp_2" }) },
      { type: "text_delta", delta: "t2ans" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_2" }) },
    ]);

    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones).toHaveLength(2);
    // Task 1's abandoned partial stays in its own bubble.
    expect(textDones[0]!.fullText).toBe("t1partial");
    expect(textDones[0]!.ctx.responseId).toBe("resp_1");
    // Task 2 is clean — NOT "t1partialt2ans" (the leak).
    expect(textDones[1]!.fullText).toBe("t2ans");
    expect(textDones[1]!.ctx.responseId).toBe("resp_2");
  });

  it("closes prior text under old responseId before stamping new responseId on message_done", () => {
    // Drive deltas under resp_A, then a no-delta message_done for resp_B
    // (the transcript-mirrored shape that exposed the ordering bug).
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_A" }) },
      { type: "text_delta", delta: "text from A" },
      {
        type: "message_done",
        content: [{ type: "output_text", text: "text from B" }],
        itemId: "msg_B",
        responseId: "resp_B",
      },
    ]);

    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones).toHaveLength(2);
    expect(textDones[0]!.fullText).toBe("text from A");
    expect(textDones[0]!.ctx.responseId).toBe("resp_A");
    expect(textDones[0]!.ctx.itemId).toBeNull();
    expect(textDones[1]!.fullText).toBe("text from B");
    expect(textDones[1]!.ctx.responseId).toBe("resp_B");
    expect(textDones[1]!.ctx.itemId).toBe("msg_B");
  });

  it("dedupes message_done with matching content even when responseId appears switched", () => {
    // Models the session-stream race where ``response.created`` was lost
    // (subscribe registered after the workflow already emitted it). The
    // reducer never saw response_created, so state.responseId stays empty
    // through the deltas. When message_done arrives with a non-empty
    // responseId, the existing isResponseSwitch logic would emit a second
    // text_done from event.content — duplicating the assistant message in
    // the UI. The content-equality dedup must catch this.
    const blocks = reduce([
      { type: "text_delta", delta: "Hi! " },
      { type: "text_delta", delta: "👋" },
      {
        type: "message_done",
        content: [{ type: "output_text", text: "Hi! 👋" }],
        itemId: "msg_X",
        responseId: "resp_X",
      },
    ]);

    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones).toHaveLength(1);
    expect(textDones[0]!.fullText).toBe("Hi! 👋");
  });

  it("response_in_progress sets the responseId when response.created is absent", () => {
    // in_progress is the only turn header when created is suppressed —
    // adopting its id keeps the whole turn in one bubble.
    const blocks = reduce([
      { type: "response_in_progress", response: makeResponse({ responseId: "resp_X" }) },
      { type: "text_delta", delta: "opening text" },
      {
        type: "tool_call",
        name: "Bash",
        arguments: { command: "ls" },
        callId: "call_1",
        status: "completed",
        itemId: "fc_1",
        responseId: "resp_X",
        agentName: "nessie",
      },
      { type: "response_completed", response: makeResponse({ responseId: "resp_X" }) },
    ]);

    const start = blocks.find((b) => b.type === "response_start");
    expect(start && start.type === "response_start" ? start.responseId : null).toBe("resp_X");
    // Every emitted block carries resp_X — so the renderer groups the whole
    // turn into ONE bubble instead of splitting at the first tool.
    const ids = new Set(blocks.map((b) => b.ctx.responseId));
    expect([...ids]).toEqual(["resp_X"]);
  });

  it("response_in_progress is idempotent when response.created already began the turn", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_X" }) },
      { type: "response_in_progress", response: makeResponse({ responseId: "resp_X" }) },
      { type: "text_delta", delta: "hi" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_X" }) },
    ]);
    // Only ONE response_start — in_progress with the same id is a no-op.
    expect(blocks.filter((b) => b.type === "response_start")).toHaveLength(1);
  });
});

describe("BlockStream — tool calls", () => {
  it("ToolCall + ToolResult → ToolGroup with output", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      {
        type: "tool_call",
        name: "Read",
        arguments: { file_path: "/tmp/f" },
        callId: "c1",
        status: "completed",
        agentName: "coder",
        itemId: "",
        responseId: "",
      },
      { type: "response_completed", response: makeResponse({ responseId: "resp_1" }) },
      // Client SDK yields ToolResult between iterations:
      { type: "tool_result", callId: "c1", output: "file content", itemId: "", responseId: "" },
      // Next iteration:
      { type: "response_created", response: makeResponse({ responseId: "resp_2" }) },
      { type: "text_delta", delta: "Done" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_2" }) },
    ]);

    // First ToolGroup: emitted immediately with output=null (call line).
    const groups = blocks.filter((b): b is ToolGroup => b.type === "tool_group");
    expect(groups.length).toBeGreaterThanOrEqual(1);
    expect(groups[0]!.executions[0]!.name).toBe("Read");

    const results = blocks.filter((b): b is ToolResultBlock => b.type === "tool_result");
    expect(results.length).toBe(1);
    expect(results[0]!.output).toBe("file content");
  });

  it("two ToolCalls with same callId yield ONE ToolGroup (claude-sdk MCP dedup)", () => {
    // Why this matters: under the claude-sdk harness's MCP path, a
    // single logical tool call surfaces as TWO ToolCall events with
    // correlated call ids — an inline observed event
    // (status="completed") emitted as the inner SDK parses the
    // tool_use block, and a post-stream action_required event emitted
    // when the SDK invokes the MCP-server handler. They share callId;
    // this dedup is what keeps the renderer from drawing the call
    // line twice.
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      // Inline observed event — fires as the inner SDK parses the
      // tool_use block, BEFORE the SDK invokes the MCP handler.
      {
        type: "tool_call",
        name: "sys_terminal_launch",
        arguments: { terminal: "shell", session: "probe" },
        callId: "tool_use_xyz",
        status: "completed",
        agentName: "agent",
        itemId: "",
        responseId: "",
      },
      // Post-stream action_required event — fires when the SDK's MCP
      // handler chains through ctx.dispatch_tool. Same callId
      // (correlated via the adapter's _pending_mcp_call_ids queue).
      {
        type: "tool_call",
        name: "sys_terminal_launch",
        arguments: { terminal: "shell", session: "probe" },
        callId: "tool_use_xyz",
        status: "action_required",
        agentName: "agent",
        itemId: "",
        responseId: "",
      },
      {
        type: "tool_result",
        callId: "tool_use_xyz",
        output: '{"ok": true}',
        itemId: "",
        responseId: "",
      },
      { type: "response_completed", response: makeResponse() },
    ]);

    const groups = blocks.filter((b): b is ToolGroup => b.type === "tool_group");
    expect(groups.length).toBe(1);
    expect(groups[0]!.executions[0]!.name).toBe("sys_terminal_launch");
    expect(groups[0]!.executions[0]!.callId).toBe("tool_use_xyz");
  });

  it("two ToolCalls with distinct callIds yield TWO ToolGroups", () => {
    // The dedup is keyed on callId, not on (name, args). An LLM can
    // legitimately invoke the same tool twice with the same arguments
    // (e.g. retrying a transient failure); each invocation is a
    // distinct logical call with its own callId.
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "tool_call",
        name: "Read",
        arguments: { path: "/tmp/x" },
        callId: "call_a",
        status: "completed",
        agentName: "agent",
        itemId: "",
        responseId: "",
      },
      {
        type: "tool_call",
        name: "Read",
        arguments: { path: "/tmp/x" },
        callId: "call_b",
        status: "completed",
        agentName: "agent",
        itemId: "",
        responseId: "",
      },
      { type: "response_completed", response: makeResponse() },
    ]);

    const groups = blocks.filter((b): b is ToolGroup => b.type === "tool_group");
    expect(groups.length).toBe(2);
  });

  it("text_delta between tool call and result flushes pending tool result inline", () => {
    // A tool that finishes between text_deltas should surface its
    // result before the next text chunk so multi-tool turns don't bunch
    // results at end-of-turn. Specifically: the tool result is emitted
    // by the tool_result branch when it arrives. Text between events
    // does NOT itself emit results — the pending sweep only flushes
    // results that already exist on pendingTools (output != null).
    // This test pins the result-emission ordering.
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "tool_call",
        name: "Read",
        arguments: {},
        callId: "c1",
        status: "completed",
        agentName: "agent",
        itemId: "",
        responseId: "",
      },
      { type: "tool_result", callId: "c1", output: "out1", itemId: "", responseId: "" },
      { type: "text_delta", delta: "After tool" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse() },
    ]);

    const types = blockTypes(blocks);
    const groupIdx = types.indexOf("tool_group");
    const resultIdx = types.indexOf("tool_result");
    const textChunkIdx = types.findIndex((t, i) => t === "text_chunk" && i > resultIdx);
    expect(groupIdx).toBeGreaterThanOrEqual(0);
    expect(resultIdx).toBeGreaterThan(groupIdx);
    // Text-related blocks (text_chunk on flush, text_done on
    // message_done) must come AFTER the tool_result, not before.
    expect(types.indexOf("text_done")).toBeGreaterThan(resultIdx);
    if (textChunkIdx >= 0) expect(textChunkIdx).toBeGreaterThan(resultIdx);
  });

  it("renders a delayed tool_result after text clears pending tool metadata", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      {
        type: "tool_call",
        name: "Read",
        arguments: { file_path: "/tmp/f" },
        callId: "c1",
        status: "completed",
        agentName: "coder",
        itemId: "fc_1",
        responseId: "resp_1",
      },
      { type: "text_delta", delta: "Continuing while the tool is still running." },
      {
        type: "tool_result",
        callId: "c1",
        output: "late file content",
        itemId: "fco_1",
        responseId: "resp_1",
      },
      { type: "response_completed", response: makeResponse({ responseId: "resp_1" }) },
    ]);

    const groups = blocks.filter((b): b is ToolGroup => b.type === "tool_group");
    // One call card: the later text delta must not synthesize a duplicate call.
    expect(groups).toHaveLength(1);
    expect(groups[0]!.executions[0]!.name).toBe("Read");

    const results = blocks.filter((b): b is ToolResultBlock => b.type === "tool_result");
    // One result panel: before metadata retention this was zero.
    expect(results).toHaveLength(1);
    expect(results[0]!.callId).toBe("c1");
    expect(results[0]!.name).toBe("Read");
    expect(results[0]!.output).toBe("late file content");
  });
});

describe("BlockStream — out-of-band response ids", () => {
  it("a backdated tool_result mid-turn renders under its original id without hijacking the reducer", () => {
    // The relay backdates a delayed function_call_output to the turn of
    // its ORIGINAL call, so it can arrive after the next
    // response.in_progress. The reducer must render it (Bug B: the
    // metadata maps were reset at the turn boundary) under the original
    // id, while the CURRENT turn keeps streaming under its own id
    // (Bug A: adopting the stale id re-stamped the rest of the turn).
    const blocks = reduce([
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_A", status: "in_progress" }),
      },
      {
        type: "tool_call",
        name: "spawn_agent",
        arguments: { title: "reviewer" },
        callId: "c1",
        status: "completed",
        agentName: "nessie",
        itemId: "fc_1",
        responseId: "resp_A",
      },
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_B", status: "in_progress" }),
      },
      // Turn B's own MCP call: inline observed event...
      {
        type: "tool_call",
        name: "sys_session_send",
        arguments: { purpose: "status" },
        callId: "c2",
        status: "completed",
        agentName: "nessie",
        itemId: "fc_2",
        responseId: "resp_B",
      },
      // ...the child's delayed result lands mid-turn, backdated to A...
      {
        type: "tool_result",
        callId: "c1",
        output: "child B output",
        itemId: "fco_1",
        responseId: "resp_A",
      },
      // ...then the action_required twin of c2 (same callId, fresh item id).
      {
        type: "tool_call",
        name: "sys_session_send",
        arguments: { purpose: "status" },
        callId: "c2",
        status: "action_required",
        agentName: "nessie",
        itemId: "fc_2b",
        responseId: "resp_B",
      },
      { type: "text_delta", delta: "Synthesizing results." },
      { type: "message_done", content: [], itemId: "msg_B", responseId: "resp_B" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_B" }) },
    ]);

    // The backdated result renders (it was silently dropped before) and
    // carries its ORIGINAL turn's id so the renderer can pair it with c1.
    const results = blocks.filter((b): b is ToolResultBlock => b.type === "tool_result");
    expect(results).toHaveLength(1);
    expect(results[0]!.callId).toBe("c1");
    expect(results[0]!.output).toBe("child B output");
    expect(results[0]!.ctx.responseId).toBe("resp_A");
    expect(results[0]!.ctx.itemId).toBe("fco_1");

    // c2's action_required twin still dedups: the out-of-band result must
    // not wipe seenCallIds. Two groups here = the duplicate tool card bug.
    const c2Groups = blocks.filter(
      (b): b is ToolGroup => b.type === "tool_group" && b.executions[0]!.callId === "c2",
    );
    expect(c2Groups).toHaveLength(1);
    expect(c2Groups[0]!.ctx.responseId).toBe("resp_B");

    // Turn B's narration stays stamped resp_B. Before the fix the result
    // flipped state.responseId to resp_A and the whole tail of turn B
    // (chunks + text_done) rendered under the wrong id, splitting the bubble.
    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones).toHaveLength(1);
    expect(textDones[0]!.fullText).toBe("Synthesizing results.");
    expect(textDones[0]!.ctx.responseId).toBe("resp_B");
    expect(textDones[0]!.ctx.itemId).toBe("msg_B");
    const chunks = blocks.filter((b): b is TextChunk => b.type === "text_chunk");
    for (const c of chunks) {
      expect(c.ctx.responseId).toBe("resp_B");
    }
  });

  it("a mid-turn skill receipt keeps its synthetic id without moving the reducer", () => {
    // Skill receipts are persisted+published under a fresh synthetic
    // `turn_<uuid>` id. The banner (and its user echo) must carry that id,
    // but the reducer must keep stamping the CURRENT turn's id on the
    // deltas that follow.
    const blocks = reduce([
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_B", status: "in_progress" }),
      },
      {
        type: "slash_command",
        kind: "skill",
        name: "oncall",
        arguments: "",
        output: null,
        agentName: "claude-native-ui",
        itemId: "sc_1",
        responseId: "turn_4f6f0c",
      },
      { type: "text_delta", delta: "Running the oncall skill now." },
      { type: "message_done", content: [], itemId: "msg_B", responseId: "resp_B" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_B" }) },
    ]);

    // Banner + echo render under the receipt's own synthetic id.
    const slash = blocks.find((b): b is SlashCommandBlock => b.type === "slash_command")!;
    expect(slash.ctx.responseId).toBe("turn_4f6f0c");
    expect(slash.ctx.itemId).toBe("sc_1");
    const echo = blocks.find((b): b is UserMessageBlock => b.type === "user_message")!;
    expect(echo.ctx.responseId).toBe("turn_4f6f0c");
    expect(echo.ctx.itemId).toBe("sc_1:user");

    // Subsequent deltas continue the current turn — before the fix they
    // were stamped turn_4f6f0c and split off into a detached bubble.
    const textDones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(textDones).toHaveLength(1);
    expect(textDones[0]!.fullText).toBe("Running the oncall skill now.");
    expect(textDones[0]!.ctx.responseId).toBe("resp_B");
  });

  it("renders a tool_result for a call hydrated from history (fresh pump, unknown callId)", () => {
    // After a reload mid-turn the tool cards live in itemsToBlocks
    // output, not in the reducer's maps. The result must still emit —
    // with the persisted-path convention (empty name; the renderer
    // pairs by callId) — instead of being silently dropped.
    const blocks = reduce([
      {
        type: "tool_result",
        callId: "c_hydrated",
        output: "late child output",
        itemId: "fco_late",
        responseId: "resp_T1",
      },
      // The redundant response.completed flush re-emission still dedups.
      {
        type: "tool_result",
        callId: "c_hydrated",
        output: "late child output",
        itemId: "fco_late",
        responseId: "resp_T1",
      },
    ]);

    // Exactly one block: emitted once (was zero before — the silent
    // drop), deduped on the second arrival by seenResultCallIds.
    expect(blocks).toHaveLength(1);
    const result = blocks[0] as ToolResultBlock;
    expect(result.type).toBe("tool_result");
    expect(result.callId).toBe("c_hydrated");
    expect(result.output).toBe("late child output");
    expect(result.name).toBe("");
    expect(result.ctx.responseId).toBe("resp_T1");
    expect(result.ctx.itemId).toBe("fco_late");
  });

  it("a delayed cross-turn result for a reused callId leaves the live turn's pending tool intact", () => {
    // The SDK can reuse a callId across turns. When resp_A's delayed
    // result lands while resp_B has its OWN pending tool under the same
    // callId, the out-of-band result must neither overwrite resp_B's
    // execution nor consume the dedup slot resp_B's real result needs.
    const throughOutOfBand: StreamEvent[] = [
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_A", status: "in_progress" }),
      },
      {
        type: "tool_call",
        name: "run_check",
        arguments: { target: "alpha" },
        callId: "shared",
        status: "completed",
        agentName: "nessie",
        itemId: "fc_a",
        responseId: "resp_A",
      },
      { type: "response_completed", response: makeResponse({ responseId: "resp_A" }) },
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_B", status: "in_progress" }),
      },
      {
        type: "tool_call",
        name: "run_check",
        arguments: { target: "beta" },
        callId: "shared",
        status: "completed",
        agentName: "nessie",
        itemId: "fc_b",
        responseId: "resp_B",
      },
      // resp_A's delayed result arrives while resp_B's same-callId tool is pending.
      {
        type: "tool_result",
        callId: "shared",
        output: "A-output",
        itemId: "fco_a",
        responseId: "resp_A",
      },
    ];

    const midStream = reduce(throughOutOfBand);
    const midGroups = midStream.filter((b): b is ToolGroup => b.type === "tool_group");
    expect(midGroups).toHaveLength(2);
    // resp_B's pending execution is untouched — before the fix the
    // out-of-band result overwrote its output with "A-output".
    expect(midGroups[1]!.ctx.responseId).toBe("resp_B");
    expect(midGroups[1]!.executions[0]!.output).toBeNull();
    const midResults = midStream.filter((b): b is ToolResultBlock => b.type === "tool_result");
    // The out-of-band result still renders, stamped with ITS turn's id
    // and WITHOUT adopting resp_B's metadata (persisted-path empty name).
    expect(midResults).toHaveLength(1);
    expect(midResults[0]!.output).toBe("A-output");
    expect(midResults[0]!.ctx.responseId).toBe("resp_A");
    expect(midResults[0]!.name).toBe("");

    // The real result follows (runner-emitted, no rid → current turn).
    const blocks = reduce([
      ...throughOutOfBand,
      {
        type: "tool_result",
        callId: "shared",
        output: "B-output",
        itemId: "fco_b",
        responseId: "",
      },
      { type: "response_completed", response: makeResponse({ responseId: "resp_B" }) },
    ]);
    const results = blocks.filter((b): b is ToolResultBlock => b.type === "tool_result");
    // Two panels — before the fix the resp_B result was dropped as a
    // "duplicate" of the resp_A one (seenResultCallIds keyed by bare callId).
    expect(results).toHaveLength(2);
    expect(results[1]!.output).toBe("B-output");
    expect(results[1]!.ctx.responseId).toBe("resp_B");
    // Current-turn pairing still works: the panel keeps the call's
    // metadata and the live execution carries the real output.
    expect(results[1]!.name).toBe("run_check");
    const groups = blocks.filter((b): b is ToolGroup => b.type === "tool_group");
    expect(groups[1]!.executions[0]!.output).toBe("B-output");
  });
});

describe("BlockStream — context propagation", () => {
  it("agent name from response.model populates ctx.agent on every block", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ model: "my-agent" }) },
      { type: "text_delta", delta: "hi" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse({ model: "my-agent" }) },
    ]);

    for (const block of blocks) {
      expect(block.ctx.agent).toBe("my-agent");
    }
  });

  it("nested agent names set ctx.depth = number of dots", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ model: "coder.researcher" }) },
      { type: "text_delta", delta: "hi" },
      { type: "message_done", content: [], itemId: "", responseId: "" },
      { type: "response_completed", response: makeResponse({ model: "coder.researcher" }) },
    ]);

    for (const block of blocks) {
      expect(block.ctx.depth).toBe(1);
    }
  });
});

describe("BlockStream — terminal lifecycles", () => {
  it("empty response → just start + end blocks", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "response_completed", response: makeResponse() },
    ]);

    expect(blockTypes(blocks)).toEqual(["response_start", "response_end"]);
  });

  it("cancellation surfaces as ResponseEnd with status='cancelled'", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "text_delta", delta: "partial" },
      {
        type: "response_cancelled",
        response: makeResponse({ status: "cancelled" }),
      },
    ]);

    const end = blocks.find((b): b is ResponseEndBlock => b.type === "response_end");
    expect(end?.status).toBe("cancelled");
  });

  it("failure surfaces as ResponseEnd with status='failed'", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "response_failed",
        response: makeResponse({ status: "failed" }),
      },
    ]);

    const end = blocks.find((b): b is ResponseEndBlock => b.type === "response_end");
    expect(end?.status).toBe("failed");
  });

  it("failure with error emits ErrorBlock before ResponseEnd", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "response_failed",
        response: {
          ...makeResponse({ status: "failed" }),
          error: { code: "policy_denied", message: "trivial task" },
        },
      },
    ]);

    const types = blockTypes(blocks);
    expect(types).toContain("error");
    expect(types).toContain("response_end");
    // ErrorBlock comes before ResponseEnd.
    expect(types.indexOf("error")).toBeLessThan(types.indexOf("response_end"));

    const errorBlock = blocks.find((b) => b.type === "error");
    expect(errorBlock).toBeDefined();
    if (errorBlock && errorBlock.type === "error") {
      expect(errorBlock.message).toBe("trivial task");
      expect(errorBlock.code).toBe("policy_denied");
    }
  });

  it("failure without error does not emit ErrorBlock", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "response_failed",
        response: makeResponse({ status: "failed" }),
      },
    ]);

    const types = blockTypes(blocks);
    expect(types).not.toContain("error");
    expect(types).toContain("response_end");
  });
});

describe("BlockStream — status events", () => {
  it("error event → ErrorBlock with both message and code", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "error",
        source: "llm",
        toolName: null,
        error: { code: "llm_auth_failed", message: "API key invalid" },
      },
      { type: "response_failed", response: makeResponse({ status: "failed" }) },
    ]);

    const err = blocks.find((b) => b.type === "error");
    expect(err).toBeDefined();
    if (err && err.type === "error") {
      expect(err.message).toBe("API key invalid");
      expect(err.code).toBe("llm_auth_failed");
      expect(err.source).toBe("llm");
    }
  });

  it("output_item.done error event preserves persisted ids", () => {
    const blocks = reduce([
      {
        type: "error",
        source: "execution",
        toolName: null,
        error: {
          code: "kiro_native_prompt_not_recorded",
          message: "Kiro did not accept this web message.",
        },
        itemId: "err_kiro",
        responseId: "resp_kiro_failed_input",
      },
    ]);

    const err = blocks.find((b) => b.type === "error");
    expect(err).toBeDefined();
    if (err && err.type === "error") {
      expect(err.ctx.itemId).toBe("err_kiro");
      expect(err.ctx.responseId).toBe("resp_kiro_failed_input");
      expect(err.message).toBe("Kiro did not accept this web message.");
      expect(err.code).toBe("kiro_native_prompt_not_recorded");
    }
  });

  it("retry event → RetryBlock with attempt/max/delay fields", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "retry",
        source: "tool",
        toolName: "web_search",
        attempt: 2,
        maxAttempts: 5,
        delaySeconds: 1.5,
        error: { code: "transient", message: "rate limited" },
      },
      { type: "response_completed", response: makeResponse() },
    ]);

    const retry = blocks.find((b) => b.type === "retry");
    expect(retry).toBeDefined();
    if (retry && retry.type === "retry") {
      expect(retry.source).toBe("tool");
      expect(retry.attempt).toBe(2);
      expect(retry.maxAttempts).toBe(5);
      expect(retry.delaySeconds).toBe(1.5);
    }
  });

  it("compaction_in_progress event → CompactionInProgressBlock (spinner)", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "compaction_in_progress" },
      { type: "response_completed", response: makeResponse() },
    ]);

    expect(blockTypes(blocks)).toContain("compaction_loading");
    expect(blockTypes(blocks)).not.toContain("compaction");
  });

  it("compaction_completed event → CompactionBlock (done marker)", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      { type: "compaction_completed", totalTokens: null },
      { type: "response_completed", response: makeResponse() },
    ]);

    expect(blockTypes(blocks)).toContain("compaction");
    expect(blockTypes(blocks)).not.toContain("compaction_loading");
  });

  it("file output → FileBlock with id and filename", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "output_file_done",
        fileId: "file_123",
        filename: "report.csv",
        contentType: "text/csv",
      },
      { type: "response_completed", response: makeResponse() },
    ]);

    const file = blocks.find((b) => b.type === "file");
    expect(file).toBeDefined();
    if (file && file.type === "file") {
      expect(file.fileId).toBe("file_123");
      expect(file.filename).toBe("report.csv");
    }
  });

  it("native tool call → NativeToolBlock with formatted label", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "native_tool_call",
        toolType: "web_search_call",
        data: { action: { type: "search", query: "omnigent framework" } },
        itemId: "",
        responseId: "",
      },
      { type: "response_completed", response: makeResponse() },
    ]);

    const native = blocks.find((b) => b.type === "native_tool");
    expect(native).toBeDefined();
    if (native && native.type === "native_tool") {
      expect(native.toolType).toBe("web_search_call");
      expect(native.label).toBe("web search: omnigent framework");
    }
  });

  it("mcp_call native tool → label uses tool name", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse() },
      {
        type: "native_tool_call",
        toolType: "mcp_call",
        data: { name: "list_resources" },
        itemId: "",
        responseId: "",
      },
      { type: "response_completed", response: makeResponse() },
    ]);

    const native = blocks.find((b) => b.type === "native_tool");
    if (native && native.type === "native_tool") {
      expect(native.label).toBe("mcp: list_resources");
    }
  });
});

describe("BlockStream — elicitation", () => {
  it("emits an ElicitationBlock with status='pending' for response.elicitation_request", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_1" }) },
      {
        type: "elicitation_request",
        elicitationId: "elic_abc",
        message: "Approve launching a zsh terminal?",
        requestedSchema: {},
        mode: "form",
        phase: "tool_call",
        policyName: "approve_terminal_launch",
        contentPreview: '{"tool":"sys_terminal_launch","tool_args":{"terminal":"zsh"}}',
      },
    ]);

    const elic = blocks.find((b): b is ElicitationBlock => b.type === "elicitation");
    expect(elic).toBeDefined();
    expect(elic!.elicitationId).toBe("elic_abc");
    expect(elic!.message).toBe("Approve launching a zsh terminal?");
    expect(elic!.phase).toBe("tool_call");
    expect(elic!.policyName).toBe("approve_terminal_launch");
    expect(elic!.contentPreview).toContain("sys_terminal_launch");
    expect(elic!.status).toBe("pending");
    expect(elic!.response).toBeNull();
    expect(elic!.ctx.responseId).toBe("resp_1");
  });

  it("stamps a REQUEST-phase elicitation with its own response id, not the prior turn's", () => {
    // A follow-up REQUEST-phase ASK arrives BEFORE the next turn starts,
    // so `state.responseId` still holds the previous turn's id. If the
    // card inherited it, bubble grouping would fold the card into the last
    // answer's bubble. Stamping a unique id keeps it standalone so the
    // ChatPage reorder can lift the prompt above it.
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_prev" }) },
      { type: "text_delta", delta: "Previous answer." },
      {
        type: "elicitation_request",
        elicitationId: "elic_req",
        message: "Session cost passed the threshold. Continue?",
        requestedSchema: {},
        mode: "form",
        phase: "request",
        policyName: "session_cost_budget",
        contentPreview: '{"role":"user","content":[{"type":"input_text","text":"Hi again"}]}',
      },
    ]);

    const elic = blocks.find((b): b is ElicitationBlock => b.type === "elicitation");
    expect(elic).toBeDefined();
    expect(elic!.phase).toBe("request");
    expect(elic!.ctx.responseId).toBe("elicit_elic_req");
    expect(elic!.ctx.responseId).not.toBe("resp_prev");
  });

  it("stamps a no-active-turn pre_tool_use card (cursor-native) with its own id", () => {
    // cursor-native is terminal-driven: it never emits `response_created`, so
    // `state.responseId` is still "" when its `pre_tool_use` approval card
    // arrives. The card must still get its own id so it forms a standalone
    // bubble (not group under the empty id), letting the ChatPage reorder lift
    // the gated user message above it.
    const blocks = reduce([
      {
        type: "elicitation_request",
        elicitationId: "elic_cursor",
        message: "Run this command?",
        requestedSchema: {},
        mode: "form",
        phase: "pre_tool_use",
        policyName: "cursor_native_permission",
        contentPreview: "echo it-works > approval_test.txt",
      },
    ]);

    const elic = blocks.find((b): b is ElicitationBlock => b.type === "elicitation");
    expect(elic).toBeDefined();
    expect(elic!.phase).toBe("pre_tool_use");
    expect(elic!.ctx.responseId).toBe("elicit_elic_cursor");
  });

  it("keeps the active turn's id for a pre_tool_use card inside an SDK turn", () => {
    // The SDK path DOES have an active turn (response_created fired), so a
    // `pre_tool_use` card must render inline with that turn — NOT get a
    // standalone `elicit_*` id. Guards the cursor-native fix from leaking into
    // the in-turn SDK case.
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_sdk" }) },
      {
        type: "elicitation_request",
        elicitationId: "elic_sdk",
        message: "Allow tool?",
        requestedSchema: {},
        mode: "form",
        phase: "pre_tool_use",
        policyName: "approve_shell_commands",
        contentPreview: "{}",
      },
    ]);

    const elic = blocks.find((b): b is ElicitationBlock => b.type === "elicitation");
    expect(elic!.ctx.responseId).toBe("resp_sdk");
  });

  it("carries structured Codex command approval details", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_cmd" }) },
      {
        type: "elicitation_request",
        elicitationId: "elic_cmd",
        message: "Codex wants to run **date**",
        requestedSchema: {},
        mode: "form",
        phase: "codex_command_approval",
        policyName: "codex_native_command_approval",
        contentPreview: '{"threadId":"thread_123","command":"date"}',
        codexCommand: {
          command: "date",
          cwd: "/tmp/workspace",
          reason: "Run a focused test",
          execPolicyAmendment: [".venv/bin/python", "-m", "pytest"],
        },
      },
    ]);

    const elic = blocks.find((b): b is ElicitationBlock => b.type === "elicitation");
    expect(elic).toBeDefined();
    expect(elic!.codexCommand).toEqual({
      command: "date",
      cwd: "/tmp/workspace",
      reason: "Run a focused test",
      execPolicyAmendment: [".venv/bin/python", "-m", "pytest"],
    });
  });

  it("flushes pending text before an elicitation prompt", () => {
    const blocks = reduce([
      { type: "response_created", response: makeResponse({ responseId: "resp_plan" }) },
      { type: "text_delta", delta: "Draft plan without newline" },
      {
        type: "elicitation_request",
        elicitationId: "elic_plan",
        message: "Implement this plan?",
        requestedSchema: {},
        mode: "form",
        phase: "codex_request_user_input",
        policyName: "codex_native_request_user_input",
        contentPreview: "{}",
      },
    ]);

    const textDoneIndex = blocks.findIndex((b): b is TextDone => b.type === "text_done");
    const elicitationIndex = blocks.findIndex(
      (b): b is ElicitationBlock => b.type === "elicitation",
    );
    expect(textDoneIndex).toBeGreaterThanOrEqual(0);
    expect(elicitationIndex).toBeGreaterThan(textDoneIndex);
    const textDone = blocks[textDoneIndex];
    if (textDone?.type !== "text_done") throw new Error("expected text_done");
    expect(textDone.fullText).toBe("Draft plan without newline");
  });
});

describe("BlockStream — interrupt (Stop) finalizes in-flight content", () => {
  it("session_interrupted seals the dangling text into a text_done", () => {
    // Stop fences the turn (no response.cancelled), so the reducer must
    // seal the partial here, not defer to the next response_created.
    const blocks = reduce([
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_1", status: "in_progress" }),
      },
      { type: "text_delta", delta: "LeBron is a basketball player" },
      { type: "session_interrupted", requestedAt: 0 },
    ]);
    const dones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(dones).toHaveLength(1);
    expect(dones[0]!.fullText).toBe("LeBron is a basketball player");
    expect(dones[0]!.ctx.responseId).toBe("resp_1");
  });

  it("seals the interrupted turn BEFORE the next turn's content begins", () => {
    const blocks = reduce([
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_1", status: "in_progress" }),
      },
      { type: "text_delta", delta: "partial A" },
      { type: "session_interrupted", requestedAt: 0 },
      {
        type: "response_in_progress",
        response: makeResponse({ responseId: "resp_2", status: "in_progress" }),
      },
      { type: "text_delta", delta: "answer B" },
      { type: "response_completed", response: makeResponse({ responseId: "resp_2" }) },
    ]);
    const dones = blocks.filter((b): b is TextDone => b.type === "text_done");
    expect(dones.map((d) => d.fullText)).toEqual(["partial A", "answer B"]);
    // resp_1's text_done lands before resp_2's response_start — so a user
    // message appended between the interrupt and resp_2 stays below it.
    const done1 = blocks.indexOf(dones[0]!);
    const start2 = blocks.findIndex(
      (b) => b.type === "response_start" && (b as { responseId: string }).responseId === "resp_2",
    );
    expect(done1).toBeGreaterThanOrEqual(0);
    expect(start2).toBeGreaterThan(done1);
  });
});
