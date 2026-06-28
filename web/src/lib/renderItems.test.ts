// Vitest cases for the bubble walker. Hand-built block sequences →
// `buildBubbles` → assert on the resulting `Bubble[]`.
//
// Pins the GROUPING and JOINING semantics for the renderer; the
// streaming reducer's behavior is tested separately in
// `blockStream.test.ts`.

import { describe, expect, it } from "vitest";
import type { AnyBlock, BlockContext, MessageContentBlock, ToolExecution } from "./blocks";
import { BlockStream } from "./blockStream";
import type { ConversationItem } from "./conversationItems";
import type { StreamEvent } from "./events";
import { itemsToBlocks } from "./itemsToBlocks";
import {
  type Bubble,
  type RenderItem,
  buildBubbles,
  bubblesEqual,
  createBubbleCache,
} from "./renderItems";
import type { ActiveResponse } from "@/store/types";

function ctx(opts?: {
  itemId?: string | null;
  responseId?: string;
  agent?: string | null;
  timestamp?: number;
  createdBy?: string;
}): BlockContext {
  return {
    agent: opts?.agent ?? "test",
    depth: 0,
    turn: 0,
    timestamp: opts?.timestamp ?? 0,
    responseId: opts?.responseId ?? "resp_1",
    itemId: opts?.itemId === undefined ? null : opts.itemId,
    ...(opts?.createdBy !== undefined ? { createdBy: opts.createdBy } : {}),
  };
}

function mkExec(name: string, callId: string): ToolExecution {
  return {
    name,
    arguments: {},
    argsSummary: "",
    callId,
    agentName: "test",
    executedBy: "server",
    output: null,
  };
}

describe("buildBubbles — bubble grouping", () => {
  it("UserMessageBlock + TextDone in same response → [user, assistant{ items: [text] }]", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "resp_1" }),
        content: [{ type: "input_text", text: "Hello" }],
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
        fullText: "Hi!",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.length).toBe(2);
    expect(bubbles[0]!.kind).toBe("user");
    expect((bubbles[0] as Extract<Bubble, { kind: "user" }>).itemId).toBe("u1");
    expect(bubbles[1]!.kind).toBe("assistant");
    const asst = bubbles[1] as Extract<Bubble, { kind: "assistant" }>;
    expect(asst.responseId).toBe("resp_1");
    expect(asst.items.length).toBe(1);
    expect(asst.items[0]!.kind).toBe("text");
    expect((asst.items[0] as Extract<RenderItem, { kind: "text" }>).text).toBe("Hi!");
  });

  it("propagates ctx.createdBy onto the user bubble", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "resp_1", createdBy: "alice@example.com" }),
        content: [{ type: "input_text", text: "Hello" }],
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const user = bubbles[0] as Extract<Bubble, { kind: "user" }>;
    expect(user.createdBy).toBe("alice@example.com");
  });

  it("leaves user bubble createdBy undefined when ctx omits it", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "resp_1" }),
        content: [{ type: "input_text", text: "Hello" }],
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const user = bubbles[0] as Extract<Bubble, { kind: "user" }>;
    expect(user.createdBy).toBeUndefined();
  });

  it("propagates a user_message block's stableKey onto its bubble", () => {
    // A block promoted from an optimistic bubble on session.input.consumed
    // carries stableKey = the optimistic temp id; buildBubbles must surface
    // it so bubbleKey can hold the React key steady across the swap (no
    // remount/flink). Plain history blocks have no stableKey.
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "msg_server_1", responseId: "resp_1" }),
        content: [{ type: "input_text", text: "hi" }],
        stableKey: "pend_1",
      },
    ];
    const bubble = buildBubbles(blocks, null)[0] as Extract<Bubble, { kind: "user" }>;
    // itemId stays the server id (dedup/nav); stableKey carries the temp id.
    expect(bubble.itemId).toBe("msg_server_1");
    expect(bubble.stableKey).toBe("pend_1");
  });

  it("leaves stableKey undefined for a history-hydrated user_message", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "msg_hist", responseId: "resp_1" }),
        content: [{ type: "input_text", text: "hi" }],
      },
    ];
    const bubble = buildBubbles(blocks, null)[0] as Extract<Bubble, { kind: "user" }>;
    expect(bubble.stableKey).toBeUndefined();
  });

  it("a REQUEST-phase elicitation with its own response id is a standalone bubble", () => {
    // The blockStream stamps a unique response id on REQUEST-phase
    // elicitations precisely so they do NOT fold into the previous turn's
    // assistant bubble. With that distinct id, the card is its own
    // elicitation-only bubble, which is what `isRequestElicitationBubble`
    // (ChatPage) keys on to lift the prompt above it.
    const blocks: AnyBlock[] = [
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_prev" }),
        fullText: "Previous answer.",
        hasCodeBlocks: false,
      },
      {
        type: "elicitation",
        ctx: ctx({ itemId: null, responseId: "elicit_elic_req" }),
        elicitationId: "elic_req",
        message: "Continue?",
        phase: "request",
        policyName: "session_cost_budget",
        contentPreview: "{}",
        requestedSchema: {},
        status: "pending",
        response: null,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.length).toBe(2);
    const answer = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(answer.items.map((i) => i.kind)).toEqual(["text"]);
    const card = bubbles[1] as Extract<Bubble, { kind: "assistant" }>;
    expect(card.items.map((i) => i.kind)).toEqual(["elicitation"]);
  });

  it("two response_ids produce two assistant bubbles in order", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "resp_1" }),
        content: [{ type: "input_text", text: "First" }],
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
        fullText: "Reply 1",
        hasCodeBlocks: false,
      },
      {
        type: "user_message",
        ctx: ctx({ itemId: "u2", responseId: "resp_2" }),
        content: [{ type: "input_text", text: "Second" }],
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a2", responseId: "resp_2" }),
        fullText: "Reply 2",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["user", "assistant", "user", "assistant"]);
    expect((bubbles[1] as Extract<Bubble, { kind: "assistant" }>).responseId).toBe("resp_1");
    expect((bubbles[3] as Extract<Bubble, { kind: "assistant" }>).responseId).toBe("resp_2");
  });

  it("history-hydrated error blocks render inside an assistant bubble", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "msg_retry", responseId: "resp_failed" }),
        content: [{ type: "input_text", text: "try again" }],
      },
      {
        type: "error",
        ctx: ctx({ itemId: "err_failed", responseId: "resp_failed" }),
        source: "execution",
        code: "native_terminal_start_failed",
        message: "Native Codex requires the 'codex' CLI on PATH.",
      },
    ];

    const bubbles = buildBubbles(blocks, null);

    expect(bubbles.map((b) => b.kind)).toEqual(["user", "assistant"]);
    const asst = bubbles[1] as Extract<Bubble, { kind: "assistant" }>;
    expect(asst.items).toEqual([
      {
        kind: "error",
        itemId: "err_failed",
        source: "execution",
        code: "native_terminal_start_failed",
        message: "Native Codex requires the 'codex' CLI on PATH.",
      },
    ]);
  });

  it("compaction block becomes a standalone compaction bubble", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "resp_1" }),
        content: [{ type: "input_text", text: "First" }],
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
        fullText: "Reply 1",
        hasCodeBlocks: false,
      },
      {
        type: "compaction",
        ctx: ctx({ itemId: "comp_1", responseId: "resp_compact" }),
      },
      {
        type: "user_message",
        ctx: ctx({ itemId: "u2", responseId: "resp_2" }),
        content: [{ type: "input_text", text: "Second" }],
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["user", "assistant", "compaction", "user"]);
    expect((bubbles[2] as Extract<Bubble, { kind: "compaction" }>).itemId).toBe("comp_1");
  });

  it("UserMessageBlock with mixed content preserves attachments", () => {
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "resp_1" }),
        content: [
          { type: "input_text", text: "Look at this " },
          { type: "input_image", file_id: "file_xyz" },
          { type: "input_text", text: "carefully." },
        ],
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.length).toBe(1);
    const u = bubbles[0] as Extract<Bubble, { kind: "user" }>;
    expect(u.content).toEqual([
      { type: "input_text", text: "Look at this " },
      { type: "input_image", file_id: "file_xyz" },
      { type: "input_text", text: "carefully." },
    ]);
  });

  it("queued user messages mid-response split into multiple assistant bubbles with unique stableIds", () => {
    // Queued-message scenario: while a response is streaming, each
    // session.input.consumed appends a user_message block to `blocks`
    // between the response's text_chunks. The walker splits on those
    // boundaries — the user wants them rendered interleaved. The
    // resulting assistant sub-bubbles all share a `responseId`, so
    // `stableId` must disambiguate them; otherwise the React keys
    // collide and sibling user bubbles get dropped during reconciliation.
    const blocks: AnyBlock[] = [
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "" }),
        content: [{ type: "input_text", text: "first" }],
      },
      {
        type: "text_chunk",
        ctx: ctx({ itemId: null, responseId: "resp_1" }),
        text: "Working on it",
      },
      {
        type: "user_message",
        ctx: ctx({ itemId: "u2", responseId: "" }),
        content: [{ type: "input_text", text: "second" }],
      },
      {
        type: "text_chunk",
        ctx: ctx({ itemId: null, responseId: "resp_1" }),
        text: " — almost done",
      },
      {
        type: "user_message",
        ctx: ctx({ itemId: "u3", responseId: "" }),
        content: [{ type: "input_text", text: "third" }],
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "msg_done_final", responseId: "resp_1" }),
        fullText: "wrapped up",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual([
      "user",
      "assistant",
      "user",
      "assistant",
      "user",
      "assistant",
    ]);
    // All three assistant bubbles share `responseId`, but stableIds
    // must differ so React keys are unique.
    const asst0 = bubbles[1] as Extract<Bubble, { kind: "assistant" }>;
    const asst1 = bubbles[3] as Extract<Bubble, { kind: "assistant" }>;
    const asst2 = bubbles[5] as Extract<Bubble, { kind: "assistant" }>;
    expect(asst0.responseId).toBe("resp_1");
    expect(asst1.responseId).toBe("resp_1");
    expect(asst2.responseId).toBe("resp_1");
    expect(new Set([asst0.stableId, asst1.stableId, asst2.stableId]).size).toBe(3);
    // The third bubble has a text_done, so its stableId pins to the
    // canonical item id (stable across streaming → committed transition).
    expect(asst2.stableId).toBe("msg_done_final");
    // The first two have no item id yet → responseId-suffixed fallback.
    expect(asst0.stableId).toBe("resp_1:0");
    expect(asst1.stableId).toBe("resp_1:1");
  });

  it("response_start / response_end blocks are skipped (lifecycle markers, not content)", () => {
    const blocks: AnyBlock[] = [
      {
        type: "response_start",
        ctx: ctx({ responseId: "resp_1" }),
        model: "x",
        responseId: "resp_1",
        conversationId: null,
      },
      {
        type: "user_message",
        ctx: ctx({ itemId: "u1", responseId: "resp_1" }),
        content: [{ type: "input_text", text: "hi" }],
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
        fullText: "hello",
        hasCodeBlocks: false,
      },
      {
        type: "response_end",
        ctx: ctx({ responseId: "resp_1" }),
        status: "completed",
        response: null,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.length).toBe(2);
    expect(bubbles.map((b) => b.kind)).toEqual(["user", "assistant"]);
  });
});

describe("buildBubbles — text grouping", () => {
  it("text_chunk + text_done collapse to one final text item with the canonical fullText", () => {
    const blocks: AnyBlock[] = [
      { type: "text_chunk", ctx: ctx(), text: "Hello " },
      { type: "text_chunk", ctx: ctx(), text: "world!" },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1" }),
        fullText: "Hello world!",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    expect(items.length).toBe(1);
    const t = items[0] as Extract<RenderItem, { kind: "text" }>;
    expect(t.text).toBe("Hello world!");
    expect(t.final).toBe(true);
    expect(t.itemId).toBe("a1");
  });

  it("trailing-empty text_done in same response is dropped when a non-empty one exists", () => {
    // The server emits a real-text + empty trailing message item per
    // response. Without dedup, the empty bubble would render as a
    // blank line.
    const blocks: AnyBlock[] = [
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1" }),
        fullText: "Real reply",
        hasCodeBlocks: false,
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a2" }),
        fullText: "",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    expect(items.length).toBe(1);
    expect((items[0] as Extract<RenderItem, { kind: "text" }>).text).toBe("Real reply");
  });

  it("two non-empty text_dones in the same response BOTH render", () => {
    const blocks: AnyBlock[] = [
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1" }),
        fullText: "First",
        hasCodeBlocks: false,
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a2" }),
        fullText: "Second",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    expect(items.length).toBe(2);
    expect((items[0] as Extract<RenderItem, { kind: "text" }>).text).toBe("First");
    expect((items[1] as Extract<RenderItem, { kind: "text" }>).text).toBe("Second");
  });

  it("text_chunks without a text_done produce a non-final text item (in-progress tail)", () => {
    const blocks: AnyBlock[] = [
      { type: "text_chunk", ctx: ctx(), text: "still " },
      { type: "text_chunk", ctx: ctx(), text: "streaming" },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    expect(items.length).toBe(1);
    const t = items[0] as Extract<RenderItem, { kind: "text" }>;
    expect(t.text).toBe("still streaming");
    expect(t.final).toBe(false);
  });
});

describe("buildBubbles — tool joining", () => {
  it("tool_group + matching tool_result by callId → tool item with output", () => {
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", timestamp: 10 }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
      {
        type: "tool_result",
        ctx: ctx({ itemId: "fco_1", timestamp: 12.25 }),
        name: "Read",
        callId: "c1",
        agentName: "test",
        output: "file content",
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    expect(items.length).toBe(1);
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;
    expect(t.kind).toBe("tool");
    expect(t.execution.callId).toBe("c1");
    expect(t.output).toBe("file content");
    expect(t.state).toBe("output-available");
    expect(t.itemId).toBe("fc_1");
    expect(t.startedAt).toBe(10);
    expect(t.duration).toBe(2.25);
  });

  it("tool_group without matching result, lifecycle streaming → state input-available", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
    ];
    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;
    expect(t.state).toBe("input-available");
  });

  it("settles an older result-less tool once the streaming turn moves past it", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_ls", responseId: "resp_1" }),
        executions: [mkExec("ls", "call_ls")],
        iteration: 0,
      },
      {
        type: "text_chunk",
        ctx: ctx({ responseId: "resp_1" }),
        text: "Continuing after ls.\n",
      },
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_sleep", responseId: "resp_1" }),
        executions: [mkExec("sleep", "call_sleep")],
        iteration: 0,
      },
    ];

    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const tools = items.filter((item): item is Extract<RenderItem, { kind: "tool" }> => {
      return item.kind === "tool";
    });

    expect(tools.map((tool) => [tool.execution.name, tool.state])).toEqual([
      ["ls", "no-output"],
      ["sleep", "input-available"],
    ]);
  });

  it("keeps all unresolved tools in the trailing streaming tool phase active", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_read", responseId: "resp_1", timestamp: 10 }),
        executions: [mkExec("Read", "call_read")],
        iteration: 0,
      },
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_grep", responseId: "resp_1", timestamp: 11 }),
        executions: [mkExec("Grep", "call_grep")],
        iteration: 0,
      },
    ];

    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const tools = items.filter((item): item is Extract<RenderItem, { kind: "tool" }> => {
      return item.kind === "tool";
    });

    expect(tools.map((tool) => [tool.execution.name, tool.state])).toEqual([
      ["Read", "input-available"],
      ["Grep", "input-available"],
    ]);
  });

  it("uses output attached directly to the tool execution as a completed result", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
        executions: [{ ...mkExec("Read", "c1"), output: "inline file content" }],
        iteration: 0,
      },
    ];

    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;

    expect(t.output).toBe("inline file content");
    expect(t.state).toBe("output-available");
  });

  it("tool_group without matching result preserves start time for live elapsed display", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1", timestamp: 42 }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
    ];
    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;
    expect(t.startedAt).toBe(42);
    expect(t.duration).toBeUndefined();
  });

  it("tool_group without matching result, lifecycle cancelled → state cancelled", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "cancelled", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
    ];
    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;
    expect(t.state).toBe("cancelled");
  });

  // Regression test: a result-less tool on a finished turn must never
  // show the live spinner — it resolves to "no-output", not "input-available".
  it("tool_group without matching result, lifecycle completed → state no-output", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "completed", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
    ];
    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;
    expect(t.state).toBe("no-output");
  });

  it("tool_group without matching result, lifecycle incomplete → state no-output", () => {
    const active: ActiveResponse = { responseId: "resp_1", state: "incomplete", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
    ];
    const bubbles = buildBubbles(blocks, active);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;
    expect(t.state).toBe("no-output");
  });

  it("tool_group without matching result on a historical bubble → state no-output (not spinner)", () => {
    // Historical bubbles default to "completed", so a reloaded dangling
    // tool must also resolve to no-output, not a perpetual spinner.
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const t = items[0] as Extract<RenderItem, { kind: "tool" }>;
    expect(t.state).toBe("no-output");
  });
});

describe("buildBubbles — cross-bubble tool_result pairing", () => {
  function resultBlock(
    callId: string,
    output: string,
    opts?: { itemId?: string; responseId?: string },
  ): AnyBlock {
    return {
      type: "tool_result",
      ctx: ctx({ itemId: opts?.itemId ?? null, responseId: opts?.responseId ?? "resp_1" }),
      // Empty name mirrors a bare result (itemsToBlocks / reducer with no
      // call metadata) — pairing happens by callId.
      name: "",
      callId,
      agentName: "test",
      output,
    };
  }

  function toolOf(bubble: Bubble): Extract<RenderItem, { kind: "tool" }> {
    const asst = bubble as Extract<Bubble, { kind: "assistant" }>;
    const tool = asst.items.find(
      (item): item is Extract<RenderItem, { kind: "tool" }> => item.kind === "tool",
    );
    expect(tool).toBeDefined();
    return tool!;
  }

  it("folds a backdated result into the prior turn's card without splitting the live bubble", () => {
    // Live out-of-band shape: turn A's spawn call, turn B streaming, the
    // child's result arrives mid-B backdated to A.
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_A" }),
        executions: [mkExec("spawn_agent", "c1")],
        iteration: 0,
      },
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "Synthesizing " },
      resultBlock("c1", "child output", { itemId: "fco_1", responseId: "resp_A" }),
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "results." },
    ];
    const active: ActiveResponse = { responseId: "resp_B", state: "streaming", error: null };
    const bubbles = buildBubbles(blocks, active);

    // Exactly two bubbles: the absorbed result must not split B's
    // narration (three bubbles = the detached-bubble bug).
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "assistant"]);
    const a = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(a.responseId).toBe("resp_A");
    const tool = toolOf(a);
    // The original card shows the late output (was "No output was
    // recorded" until a manual reload).
    expect(tool.output).toBe("child output");
    expect(tool.state).toBe("output-available");

    const b = bubbles[1] as Extract<Bubble, { kind: "assistant" }>;
    expect(b.responseId).toBe("resp_B");
    // ONE continuous text item — the absorbed result must not split the
    // text run into two paragraphs either.
    expect(b.items).toEqual([
      { kind: "text", itemId: null, text: "Synthesizing results.", final: false },
    ]);
  });

  it("absorbing a backdated result mid-stream keeps the live bubble's stableId", () => {
    // ChatPage keys assistant bubbles on stableId — adopting the absorbed
    // result's itemId would remount (and flash) the streaming bubble.
    const active: ActiveResponse = { responseId: "resp_B", state: "streaming", error: null };
    const before: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_A" }),
        executions: [mkExec("spawn_agent", "c1")],
        iteration: 0,
      },
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "Synthesizing " },
    ];
    const liveId = (buildBubbles(before, active)[1] as Extract<Bubble, { kind: "assistant" }>)
      .stableId;
    const after: AnyBlock[] = [
      ...before,
      resultBlock("c1", "child output", { itemId: "fco_1", responseId: "resp_A" }),
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "results." },
    ];
    const bubble = buildBubbles(after, active)[1] as Extract<Bubble, { kind: "assistant" }>;
    expect(bubble.stableId).not.toBe("fco_1");
    expect(bubble.stableId).toBe(liveId);
  });

  it("reload: a persisted late output folds into the original card with no orphan bubble", () => {
    // Persisted order is arrival order: the backdated output sits inside
    // the NEXT turn's item run with the ORIGINAL turn's response_id.
    const items: ConversationItem[] = [
      {
        id: "u1",
        response_id: "resp_T1",
        type: "message",
        role: "user",
        status: "completed",
        content: [{ type: "input_text", text: "review the PR" }],
      },
      {
        id: "fc_c1",
        response_id: "resp_T1",
        type: "function_call",
        status: "completed",
        name: "spawn_agent",
        arguments: JSON.stringify({ title: "reviewer" }),
        call_id: "c1",
      },
      {
        id: "msg_t2a",
        response_id: "resp_T2",
        type: "message",
        role: "assistant",
        status: "completed",
        model: "nessie",
        content: [{ type: "output_text", text: "Synthesizing the review." }],
      },
      {
        id: "fco_c1",
        response_id: "resp_T1",
        type: "function_call_output",
        status: "completed",
        call_id: "c1",
        output: "reviewer findings",
      },
      {
        id: "msg_t2b",
        response_id: "resp_T2",
        type: "message",
        role: "assistant",
        status: "completed",
        model: "nessie",
        content: [{ type: "output_text", text: "Done." }],
      },
    ];
    const bubbles = buildBubbles(itemsToBlocks(items), null);

    // No empty orphan bubble for the consumed result, and T2 stays one
    // bubble (before: [user, assistant, assistant, EMPTY, assistant]).
    expect(bubbles.map((b) => b.kind)).toEqual(["user", "assistant", "assistant"]);
    const t1 = bubbles[1] as Extract<Bubble, { kind: "assistant" }>;
    expect(t1.responseId).toBe("resp_T1");
    const tool = toolOf(t1);
    expect(tool.execution.name).toBe("spawn_agent");
    // The cross-bubble fold: before the fix this was null → the card
    // showed "No output was recorded for this tool call." after reload.
    expect(tool.output).toBe("reviewer findings");
    expect(tool.state).toBe("output-available");

    const t2 = bubbles[2] as Extract<Bubble, { kind: "assistant" }>;
    expect(t2.responseId).toBe("resp_T2");
    expect(t2.items.map((item) => (item as Extract<RenderItem, { kind: "text" }>).text)).toEqual([
      "Synthesizing the review.",
      "Done.",
    ]);
  });

  it("live output for a history-hydrated call renders after a fresh pump", () => {
    // Reload mid-turn: the call card comes from itemsToBlocks; the
    // output then arrives on the freshly-bound stream where the
    // reducer has no metadata for it.
    const history: ConversationItem[] = [
      {
        id: "u1",
        response_id: "resp_T1",
        type: "message",
        role: "user",
        status: "completed",
        content: [{ type: "input_text", text: "spawn the reviewer" }],
      },
      {
        id: "fc_c1",
        response_id: "resp_T1",
        type: "function_call",
        status: "completed",
        name: "spawn_agent",
        arguments: JSON.stringify({ title: "reviewer" }),
        call_id: "c1",
      },
    ];
    const liveBlocks = new BlockStream().reduceSync([
      {
        type: "tool_result",
        callId: "c1",
        output: "spawn results",
        itemId: "fco_c1",
        responseId: "resp_T1",
      },
    ]);
    const bubbles = buildBubbles([...itemsToBlocks(history), ...liveBlocks], null);

    expect(bubbles.map((b) => b.kind)).toEqual(["user", "assistant"]);
    const tool = toolOf(bubbles[1]!);
    // Before the fix the reducer dropped the event outright (no block,
    // no output) and the card showed "No output was recorded".
    expect(tool.output).toBe("spawn results");
    expect(tool.state).toBe("output-available");
  });

  it("a result-only group folds into its card instead of painting an empty bubble", () => {
    // A queued user message lands between the call and its late result,
    // so the result would otherwise OPEN its own (empty) assistant bubble.
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
        executions: [mkExec("Read", "c1")],
        iteration: 0,
      },
      {
        type: "user_message",
        ctx: ctx({ itemId: "u2", responseId: "" }),
        content: [{ type: "input_text", text: "queued question" }],
      },
      resultBlock("c1", "file body", { itemId: "fco_1", responseId: "resp_1" }),
    ];
    const bubbles = buildBubbles(blocks, null);

    // No trailing empty assistant bubble for the consumed result.
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "user"]);
    expect(toolOf(bubbles[0]!).output).toBe("file body");
  });

  it("a callId reused across turns keeps each card paired with its own turn's result", () => {
    // The SDK can legitimately reuse a call id across tasks; each card
    // must keep ITS OWN turn's output, not the globally-last one.
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_a", responseId: "resp_1" }),
        executions: [mkExec("Read", "shared")],
        iteration: 0,
      },
      resultBlock("shared", "first output", { itemId: "fco_a", responseId: "resp_1" }),
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_b", responseId: "resp_2" }),
        executions: [mkExec("Read", "shared")],
        iteration: 0,
      },
      resultBlock("shared", "second output", { itemId: "fco_b", responseId: "resp_2" }),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "assistant"]);
    // If results were ever indexed by bare callId (last-wins), the
    // first card would wrongly show "second output".
    expect(toolOf(bubbles[0]!).output).toBe("first output");
    expect(toolOf(bubbles[1]!).output).toBe("second output");
  });

  it("a dangling call does not adopt a later turn's output when its callId is reused", () => {
    // Turn 1's call never resolved; turn 2 reuses the callId AND resolves.
    // The relay backdates a delayed result to the ORIGINAL call's rid, so
    // a cross-bubble fold whose rid doesn't match is cross-pollination.
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_a", responseId: "resp_1" }),
        executions: [mkExec("Read", "shared")],
        iteration: 0,
      },
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_b", responseId: "resp_2" }),
        executions: [mkExec("Read", "shared")],
        iteration: 0,
      },
      resultBlock("shared", "second output", { itemId: "fco_b", responseId: "resp_2" }),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "assistant"]);
    const dangling = toolOf(bubbles[0]!);
    // The unresolved card keeps its honest no-output state.
    expect(dangling.output).toBeNull();
    expect(dangling.state).toBe("no-output");
    expect(toolOf(bubbles[1]!).output).toBe("second output");
  });

  it("cache: a late result targeting a finalized bubble invalidates reuse, then reuse resumes", () => {
    const cache = createBubbleCache();
    const streaming: ActiveResponse = { responseId: "resp_B", state: "streaming", error: null };
    const base: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_A" }),
        executions: [mkExec("spawn_agent", "c1")],
        iteration: 0,
      },
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "Synthesizing " },
    ];
    const first = buildBubbles(base, streaming, cache);
    expect(toolOf(first[0]!).output).toBeNull();

    // The late result appends while bubble A is finalized in the cache.
    const withResult = [
      ...base,
      resultBlock("c1", "late child output", { itemId: "fco_1", responseId: "resp_A" }),
    ];
    const second = buildBubbles(withResult, streaming, cache);
    // The finalized card must repaint with the output — serving the
    // cached prefix here is exactly the stale-card bug.
    expect(toolOf(second[0]!).output).toBe("late child output");
    expect(toolOf(second[0]!).state).toBe("output-available");
    // Incremental output must equal a from-scratch rebuild (no cache).
    expect(second).toEqual(buildBubbles(withResult, streaming));

    // Streaming continues: the one-off rebuild must not permanently
    // disable prefix reuse (the cache's whole reason to exist).
    const withMore = [
      ...withResult,
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "results." } as AnyBlock,
    ];
    const third = buildBubbles(withMore, streaming, cache);
    expect(third[0]).toBe(second[0]); // reused by reference again
    expect(third).toEqual(buildBubbles(withMore, streaming));
  });

  it("an out-of-band result for a reused callId folds into its original card, not the live turn's", () => {
    // Full pipeline (reducer → buildBubbles) for the callId-reuse race:
    // resp_A's delayed result lands while resp_B streams its OWN tool
    // under the same callId, then resp_B's real result arrives.
    const throughOutOfBand: StreamEvent[] = [
      {
        type: "response_in_progress",
        response: { id: "resp_A", status: "in_progress", model: "nessie", conversation: null },
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
      {
        type: "response_completed",
        response: { id: "resp_A", status: "completed", model: "nessie", conversation: null },
      },
      {
        type: "response_in_progress",
        response: { id: "resp_B", status: "in_progress", model: "nessie", conversation: null },
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
      {
        type: "tool_result",
        callId: "shared",
        output: "A-output",
        itemId: "fco_a",
        responseId: "resp_A",
      },
    ];
    const streaming: ActiveResponse = { responseId: "resp_B", state: "streaming", error: null };
    const mid = buildBubbles(new BlockStream().reduceSync(throughOutOfBand), streaming);
    expect(mid.map((b) => b.kind)).toEqual(["assistant", "assistant"]);
    // The delayed output paints resp_A's card mid-stream...
    expect(toolOf(mid[0]!).output).toBe("A-output");
    expect(toolOf(mid[0]!).state).toBe("output-available");
    // ...while resp_B's same-callId tool keeps spinning instead of
    // adopting resp_A's output.
    expect(toolOf(mid[1]!).output).toBeNull();
    expect(toolOf(mid[1]!).state).toBe("input-available");

    const blocks = new BlockStream().reduceSync([
      ...throughOutOfBand,
      // resp_B's real result: runner-emitted, no rid → current turn.
      {
        type: "tool_result",
        callId: "shared",
        output: "B-output",
        itemId: "fco_b",
        responseId: "",
      },
      {
        type: "response_completed",
        response: { id: "resp_B", status: "completed", model: "nessie", conversation: null },
      },
    ]);
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "assistant"]);
    // Each turn's card carries its own output — cross-pollinating in
    // either direction is the reused-callId bug.
    expect(toolOf(bubbles[0]!).output).toBe("A-output");
    expect(toolOf(bubbles[1]!).output).toBe("B-output");
  });
});

describe("buildBubbles — lifecycle from activeResponse", () => {
  it("matching responseId → bubble lifecycle copies state and error", () => {
    const active: ActiveResponse = {
      responseId: "resp_1",
      state: "failed",
      error: "boom",
    };
    const blocks: AnyBlock[] = [
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
        fullText: "partial",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, active);
    const a = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(a.lifecycle).toBe("failed");
    expect(a.error).toBe("boom");
  });

  it("non-matching responseId → bubble lifecycle is completed", () => {
    const active: ActiveResponse = {
      responseId: "resp_2",
      state: "streaming",
      error: null,
    };
    const blocks: AnyBlock[] = [
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
        fullText: "old",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, active);
    const a = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(a.lifecycle).toBe("completed");
  });

  it("activeResponse=null → all bubbles completed", () => {
    const blocks: AnyBlock[] = [
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1" }),
        fullText: "hi",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const a = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(a.lifecycle).toBe("completed");
  });

  it("persisted interrupted TextDone marks rehydrated bubble cancelled", () => {
    const blocks: AnyBlock[] = [
      {
        type: "text_done",
        ctx: ctx({ itemId: "msg_interrupted", responseId: "codex_turn_123" }),
        fullText: "partial answer",
        hasCodeBlocks: false,
        interrupted: true,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const a = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(a.lifecycle).toBe("cancelled");
    expect(a.items).toEqual([
      {
        kind: "text",
        itemId: "msg_interrupted",
        text: "partial answer",
        final: true,
      },
    ]);
  });
});

describe("buildBubbles — reasoning", () => {
  it("reasoning_chunks concatenate into one reasoning item", () => {
    const blocks: AnyBlock[] = [
      { type: "reasoning_start", ctx: ctx() },
      { type: "reasoning_chunk", ctx: ctx(), text: "Let me " },
      { type: "reasoning_chunk", ctx: ctx(), text: "think...\n" },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    expect(items.length).toBe(1);
    const r = items[0] as Extract<RenderItem, { kind: "reasoning" }>;
    expect(r.text).toBe("Let me think...\n");
  });

  it("duration is the span between the first and last block in the run", () => {
    const blocks: AnyBlock[] = [
      { type: "reasoning_start", ctx: ctx({ timestamp: 100 }) },
      { type: "reasoning_chunk", ctx: ctx({ timestamp: 101.5 }), text: "a" },
      { type: "reasoning_chunk", ctx: ctx({ timestamp: 102.5 }), text: "b" },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const r = items[0] as Extract<RenderItem, { kind: "reasoning" }>;
    expect(r.duration).toBe(2.5);
  });

  it("duration is undefined for historical blocks (timestamp=0)", () => {
    const blocks: AnyBlock[] = [
      { type: "reasoning_start", ctx: ctx({ timestamp: 0 }) },
      { type: "reasoning_chunk", ctx: ctx({ timestamp: 0 }), text: "loaded" },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const r = items[0] as Extract<RenderItem, { kind: "reasoning" }>;
    expect(r.duration).toBeUndefined();
  });
});

describe("buildBubbles — slash_command items", () => {
  it("slash_command block becomes a slash_command RenderItem inside its bubble", () => {
    const blocks: AnyBlock[] = [
      {
        type: "slash_command",
        ctx: ctx({ itemId: "sc_1", responseId: "resp_slash" }),
        kind: "skill",
        name: "dev-productivity:simplify",
        arguments: "",
        output: null,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.length).toBe(1);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    expect(items.length).toBe(1);
    const slash = items[0] as Extract<RenderItem, { kind: "slash_command" }>;
    expect(slash.slashKind).toBe("skill");
    expect(slash.name).toBe("dev-productivity:simplify");
    expect(slash.arguments).toBe("");
    expect(slash.output).toBeNull();
    expect(slash.itemId).toBe("sc_1");
  });

  it("propagates kind='command' onto the RenderItem as slashKind", () => {
    const blocks: AnyBlock[] = [
      {
        type: "slash_command",
        ctx: ctx({ itemId: "sc_cmd", responseId: "resp_cmd" }),
        kind: "command",
        name: "effort",
        arguments: "high",
        output: null,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    const items = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items;
    const slash = items[0] as Extract<RenderItem, { kind: "slash_command" }>;
    expect(slash.slashKind).toBe("command");
    expect(slash.name).toBe("effort");
  });
});

describe("buildBubbles — routing_decision (intelligent model router) chip", () => {
  it("routing_decision block becomes a standalone routing_decision bubble, not folded into an assistant bubble", () => {
    const blocks: AnyBlock[] = [
      {
        type: "routing_decision",
        ctx: ctx({ itemId: "rd_1", responseId: "routing_1" }),
        model: "databricks-claude-opus-4-8",
        tier: "expensive",
        applied: true,
        rationale: "multi-file refactor needs deep reasoning",
      },
      // An assistant turn under a different responseId follows.
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
        fullText: "Done.",
        hasCodeBlocks: false,
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    // The chip is its own top-level bubble BEFORE the assistant bubble —
    // if it were folded into the assistant group, the kinds would be just
    // ["assistant"] and the chip would render inside the answer.
    expect(bubbles.map((b) => b.kind)).toEqual(["routing_decision", "assistant"]);
    const chip = bubbles[0] as Extract<Bubble, { kind: "routing_decision" }>;
    expect(chip.itemId).toBe("rd_1");
    expect(chip.model).toBe("databricks-claude-opus-4-8");
    expect(chip.tier).toBe("expensive");
    expect(chip.applied).toBe(true);
    expect(chip.rationale).toBe("multi-file refactor needs deep reasoning");
  });

  it("carries applied=false for a shadow verdict (would-have-picked)", () => {
    const blocks: AnyBlock[] = [
      {
        type: "routing_decision",
        ctx: ctx({ itemId: "rd_shadow", responseId: "routing_2" }),
        model: "databricks-claude-haiku-4-5",
        tier: "cheap",
        applied: false,
        rationale: "trivial question",
      },
    ];
    const chip = buildBubbles(blocks, null)[0] as Extract<Bubble, { kind: "routing_decision" }>;
    // applied=false drives the "would have picked" copy — a flip to true
    // would falsely claim the brain ran on the router's pick.
    expect(chip.applied).toBe(false);
    expect(chip.model).toBe("databricks-claude-haiku-4-5");
  });

  it("reload funnel: a routing_decision item maps through itemsToBlocks to the same bubble", () => {
    const items: ConversationItem[] = [
      {
        id: "rd_reload",
        type: "routing_decision",
        response_id: "routing_3",
        status: "completed",
        model: "databricks-claude-sonnet-4-6",
        tier: "medium",
        applied: true,
        rationale: "moderate knowledge work",
      } as unknown as ConversationItem,
    ];
    const blocks = itemsToBlocks(items);
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.length).toBe(1);
    const chip = bubbles[0] as Extract<Bubble, { kind: "routing_decision" }>;
    // Reload path produces the same chip the live path does — id carried
    // from the persisted item so both funnels dedup by ctx.itemId.
    expect(chip.kind).toBe("routing_decision");
    expect(chip.itemId).toBe("rd_reload");
    expect(chip.model).toBe("databricks-claude-sonnet-4-6");
    expect(chip.tier).toBe("medium");
  });

  it("live funnel: a response.output_item.done routing_decision reduces to the same bubble", () => {
    const events: StreamEvent[] = [
      {
        type: "routing_decision",
        model: "databricks-claude-opus-4-8",
        tier: "expensive",
        applied: true,
        rationale: "hard turn",
        itemId: "rd_live",
        responseId: "routing_live",
      },
    ];
    const blocks = new BlockStream().reduceSync(events);
    const bubbles = buildBubbles(blocks, null);
    // The live reducer produces the same standalone chip the reload path
    // does — a missing case here would silently drop the live chip.
    expect(bubbles.length).toBe(1);
    const chip = bubbles[0] as Extract<Bubble, { kind: "routing_decision" }>;
    expect(chip.kind).toBe("routing_decision");
    expect(chip.itemId).toBe("rd_live");
    expect(chip.applied).toBe(true);
    expect(chip.model).toBe("databricks-claude-opus-4-8");
  });
});

describe("bubblesEqual — React.memo comparator", () => {
  const baseBlocks = (): AnyBlock[] => [
    {
      type: "user_message",
      ctx: ctx({ itemId: "u1", responseId: "resp_1" }),
      content: [{ type: "input_text", text: "Hello" }],
    },
    {
      type: "text_done",
      ctx: ctx({ itemId: "a1", responseId: "resp_1" }),
      fullText: "Hi there",
      hasCodeBlocks: false,
    },
  ];

  function assistant(text: string, lifecycle: ActiveResponse["state"]): Bubble {
    return {
      kind: "assistant",
      responseId: "resp_1",
      stableId: "a1",
      lifecycle,
      error: null,
      items: [{ kind: "text", itemId: "a1", text, final: lifecycle === "completed" }],
    };
  }

  it("treats unchanged bubbles as equal across a rebuild (the memo win)", () => {
    const blocks = baseBlocks();
    const first = buildBubbles(blocks, null);
    // A new turn arrives: buildBubbles re-runs over an extended block list
    // and produces brand-new Bubble objects for the prior turn.
    const second = buildBubbles(
      [
        ...blocks,
        {
          type: "user_message",
          ctx: ctx({ itemId: "u2", responseId: "resp_2" }),
          content: [{ type: "input_text", text: "Again" }],
        },
      ],
      null,
    );
    // New object identities — proves buildBubbles rebuilt them, so a plain
    // React.memo (reference compare) would NOT skip these.
    expect(second[0]).not.toBe(first[0]);
    expect(second[1]).not.toBe(first[1]);
    // ...but the comparator sees them as equal, so memo skips the re-render.
    // If either returned false, every prior message would re-render (and
    // re-run markdown/syntax-highlighting) on each streaming delta — the bug.
    expect(bubblesEqual(first[0]!, second[0]!)).toBe(true);
    expect(bubblesEqual(first[1]!, second[1]!)).toBe(true);
  });

  it("reports not-equal when a streaming text run grows", () => {
    // "Hel" -> "Hello": the active bubble's content changed, so it must
    // re-render. Equal text must stay equal (no spurious re-render).
    expect(bubblesEqual(assistant("Hel", "streaming"), assistant("Hello", "streaming"))).toBe(
      false,
    );
    expect(bubblesEqual(assistant("Hello", "streaming"), assistant("Hello", "streaming"))).toBe(
      true,
    );
  });

  it("reports not-equal across a lifecycle transition", () => {
    // Same text, but streaming -> completed flips affordances (copy button,
    // markers), so the bubble must re-render.
    expect(bubblesEqual(assistant("Done", "streaming"), assistant("Done", "completed"))).toBe(
      false,
    );
  });

  it("reports not-equal when the item count changes", () => {
    const oneItem = assistant("Hi", "completed");
    const twoItems: Bubble = {
      ...(oneItem as Extract<Bubble, { kind: "assistant" }>),
      items: [
        ...(oneItem as Extract<Bubble, { kind: "assistant" }>).items,
        { kind: "text", itemId: "a2", text: "more", final: true },
      ],
    };
    expect(bubblesEqual(oneItem, twoItems)).toBe(false);
  });

  it("different bubble kinds are never equal", () => {
    const bubbles = buildBubbles(baseBlocks(), null);
    // bubbles[0] is the user bubble, bubbles[1] the assistant bubble.
    expect(bubblesEqual(bubbles[0]!, bubbles[1]!)).toBe(false);
  });

  it("reports not-equal when a user bubble's createdBy differs", () => {
    // Author attribution feeds the bubble's rendered label, so a changed
    // author must re-render. Without the createdBy compare in bubblesEqual,
    // a hydrated author would not repaint over an optimistic unattributed
    // bubble of the same itemId/content.
    const content: MessageContentBlock[] = [{ type: "input_text", text: "Hello" }];
    const alice: Bubble = { kind: "user", itemId: "u1", content, createdBy: "alice@example.com" };
    const bob: Bubble = { kind: "user", itemId: "u1", content, createdBy: "bob@example.com" };
    const none: Bubble = { kind: "user", itemId: "u1", content };
    expect(bubblesEqual(alice, bob)).toBe(false);
    expect(bubblesEqual(none, alice)).toBe(false);
    expect(bubblesEqual(alice, alice)).toBe(true);
  });
});

describe("buildBubbles — incremental reuse cache", () => {
  function userBlock(itemId: string, responseId: string): AnyBlock {
    return {
      type: "user_message",
      ctx: ctx({ itemId, responseId }),
      content: [{ type: "input_text", text: `q ${itemId}` }],
    };
  }
  function doneBlock(itemId: string, responseId: string, text: string): AnyBlock {
    return {
      type: "text_done",
      ctx: ctx({ itemId, responseId }),
      fullText: text,
      hasCodeBlocks: false,
    };
  }
  function chunk(responseId: string, text: string): AnyBlock {
    return { type: "text_chunk", ctx: ctx({ itemId: null, responseId }), text };
  }

  it("reuses finalized bubbles by reference while the active bubble grows", () => {
    const cache = createBubbleCache();
    // A finished turn (resp_1) followed by a streaming turn (resp_2).
    const finished = [userBlock("u1", "resp_1"), doneBlock("a1", "resp_1", "done one")];
    const streaming = { responseId: "resp_2", state: "streaming" as const, error: null };

    const blocks2 = [...finished, chunk("resp_2", "Hel")];
    const first = buildBubbles(blocks2, streaming, cache);
    // [user, assistant(resp_1, finalized), assistant(resp_2, streaming)].
    expect(first.map((b) => b.kind)).toEqual(["user", "assistant", "assistant"]);

    // A streaming delta grows the active bubble — append-only extension.
    const blocks3 = [...blocks2, chunk("resp_2", "lo")];
    const second = buildBubbles(blocks3, streaming, cache);

    // The finalized prefix is reused BY REFERENCE — no new objects, so a
    // plain React.memo (===) skips re-rendering + re-markdown of prior
    // turns. If reuse broke, these would be fresh objects every delta.
    expect(second[0]).toBe(first[0]); // user bubble
    expect(second[1]).toBe(first[1]); // finalized assistant(resp_1)
    // The active bubble IS rebuilt (its content changed Hel → Hello).
    expect(second[2]).not.toBe(first[2]);
    const active = second[2] as Extract<Bubble, { kind: "assistant" }>;
    expect(active.items).toEqual([{ kind: "text", itemId: null, text: "Hello", final: false }]);

    // Incremental output must equal a from-scratch rebuild (no cache).
    expect(second).toEqual(buildBubbles(blocks3, streaming));
  });

  it("falls back to a full rebuild when blocks are not an append-only extension", () => {
    const cache = createBubbleCache();
    const sessionA = [userBlock("u1", "resp_1"), doneBlock("a1", "resp_1", "A answer")];
    const built = buildBubbles(sessionA, null, cache);

    // Session switch: a brand-new block list that does not extend the
    // cached one. Reuse must NOT leak the prior session's bubbles.
    const sessionB = [userBlock("u9", "resp_9"), doneBlock("a9", "resp_9", "B answer")];
    const rebuilt = buildBubbles(sessionB, null, cache);
    expect(rebuilt).toEqual(buildBubbles(sessionB, null));
    expect(rebuilt[0]).not.toBe(built[0]);
    const u = rebuilt[0] as Extract<Bubble, { kind: "user" }>;
    expect(u.itemId).toBe("u9");
  });

  it("rebuilds the active bubble when only activeResponse changes (e.g. cancel)", () => {
    const cache = createBubbleCache();
    const blocks = [userBlock("u1", "resp_1"), chunk("resp_1", "partial")];
    const streaming = { responseId: "resp_1", state: "streaming" as const, error: null };
    const before = buildBubbles(blocks, streaming, cache);
    expect((before[1] as Extract<Bubble, { kind: "assistant" }>).lifecycle).toBe("streaming");

    // Same blocks, but the response was cancelled — the active bubble's
    // lifecycle must update even though no block changed.
    const cancelled = { responseId: "resp_1", state: "cancelled" as const, error: null };
    const after = buildBubbles(blocks, cancelled, cache);
    expect((after[1] as Extract<Bubble, { kind: "assistant" }>).lifecycle).toBe("cancelled");
    expect(after).toEqual(buildBubbles(blocks, cancelled));
  });

  it("marks interrupted response ids cancelled after activeResponse clears", () => {
    const blocks = [doneBlock("a1", "codex_turn_123", "partial answer")];
    const completed = { responseId: "codex_turn_123", state: "completed" as const, error: null };

    const bubbles = buildBubbles(blocks, completed, undefined, ["codex_turn_123"]);

    expect(bubbles).toHaveLength(1);
    expect((bubbles[0] as Extract<Bubble, { kind: "assistant" }>).lifecycle).toBe("cancelled");
  });
});
