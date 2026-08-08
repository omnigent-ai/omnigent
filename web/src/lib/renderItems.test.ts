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
  type RoutingDecisionWire,
  routingDecisionBlock,
  routingDecisionItem,
} from "./__fixtures__/routingDecision";
import {
  type Bubble,
  type RenderItem,
  buildBubbles,
  bubblesEqual,
  createBubbleCache,
  lastRenderableAssistantIndex,
  liveCandidateAssistantIndex,
} from "./renderItems";
import type { ActiveResponse } from "@/store/types";

function ctx(opts?: {
  itemId?: string | null;
  responseId?: string;
  agent?: string | null;
  timestamp?: number;
  createdBy?: string;
  createdAtS?: number;
}): BlockContext {
  return {
    agent: opts?.agent ?? "test",
    depth: 0,
    turn: 0,
    timestamp: opts?.timestamp ?? 0,
    responseId: opts?.responseId ?? "resp_1",
    itemId: opts?.itemId === undefined ? null : opts.itemId,
    ...(opts?.createdBy !== undefined ? { createdBy: opts.createdBy } : {}),
    ...(opts?.createdAtS !== undefined ? { createdAtS: opts.createdAtS } : {}),
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

  it("compaction_loading bubble is removed even when separated from compaction by assistant blocks", () => {
    const blocks: AnyBlock[] = [
      {
        type: "compaction_loading",
        ctx: ctx({ itemId: "cl_1", responseId: "resp_compact" }),
      },
      {
        type: "text_done",
        ctx: ctx({ itemId: "a1", responseId: "resp_compact" }),
        fullText: "Summarised",
        hasCodeBlocks: false,
      },
      {
        type: "compaction",
        ctx: ctx({ itemId: "comp_1", responseId: "resp_compact" }),
      },
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "compaction"]);
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

  it("two calls under one response_id: the tool_result-resolved call completes while the live one spins", () => {
    // The hermes-native contract: every tool call in a turn shares one
    // response_id, so the bubble lifecycle is streaming for the whole turn.
    // A finished call (resolved by its tool_result) must still render as
    // completed, not inherit the turn's spinner — only the trailing live call.
    const active: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_read", responseId: "resp_1", timestamp: 10 }),
        executions: [mkExec("Read", "call_read")],
        iteration: 0,
      },
      {
        type: "tool_result",
        ctx: ctx({ itemId: "fco_read", responseId: "resp_1", timestamp: 12 }),
        name: "Read",
        callId: "call_read",
        agentName: "test",
        output: "file content",
      },
      {
        type: "text_chunk",
        ctx: ctx({ responseId: "resp_1" }),
        text: "Now running a slow tool.\n",
      },
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_sleep", responseId: "resp_1", timestamp: 13 }),
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
      ["Read", "output-available"],
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

describe("buildBubbles — session-driven trailing tool spinner", () => {
  // claude-native has no streaming `activeResponse` — its running/idle lives
  // in `sessionStatus`. The `sessionRunning` arg spins the newest turn's
  // trailing tool phase so a dispatched-but-unresolved tool shows a spinner
  // instead of "No output", without any bubble reaching lifecycle "streaming".
  function toolState(bubbles: Bubble[]): string {
    const asst = bubbles[bubbles.length - 1] as Extract<Bubble, { kind: "assistant" }>;
    const tool = asst.items.find(
      (item): item is Extract<RenderItem, { kind: "tool" }> => item.kind === "tool",
    );
    expect(tool).toBeDefined();
    return tool!.state;
  }

  const danglingToolTurn: AnyBlock[] = [
    {
      type: "tool_group",
      ctx: ctx({ itemId: "fc_1", responseId: "resp_1" }),
      executions: [mkExec("Bash", "c1")],
      iteration: 0,
    },
  ];

  it("spins the newest turn's trailing tool while the session is running", () => {
    // No activeResponse (claude-native never opens one), sessionRunning=true.
    const bubbles = buildBubbles(danglingToolTurn, null, undefined, [], true);
    expect(toolState(bubbles)).toBe("input-available");
  });

  it("does not spin when the session is idle (dangling tool resolves to no-output)", () => {
    // The idle edge can land before the tool's result block — the tool must
    // settle, not spin forever. This is the property the never-spin tests pin,
    // now exercised through the session-driven path.
    const bubbles = buildBubbles(danglingToolTurn, null, undefined, [], false);
    expect(toolState(bubbles)).toBe("no-output");
  });

  it("a resolved tool shows its output regardless of session running", () => {
    const blocks: AnyBlock[] = [
      ...danglingToolTurn,
      {
        type: "tool_result",
        ctx: ctx({ itemId: "fco_1", responseId: "resp_1" }),
        name: "",
        callId: "c1",
        agentName: "test",
        output: "done",
      },
    ];
    const bubbles = buildBubbles(blocks, null, undefined, [], true);
    expect(toolState(bubbles)).toBe("output-available");
  });

  it("only the NEWEST turn spins — an earlier turn's dangling tool stays no-output", () => {
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_old", responseId: "resp_old" }),
        executions: [mkExec("Read", "c_old")],
        iteration: 0,
      },
      { type: "user_message", ctx: ctx({ itemId: "u1", responseId: "" }), content: [] },
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_new", responseId: "resp_new" }),
        executions: [mkExec("Bash", "c_new")],
        iteration: 0,
      },
    ];
    const bubbles = buildBubbles(blocks, null, undefined, [], true);
    const assistants = bubbles.filter(
      (b): b is Extract<Bubble, { kind: "assistant" }> => b.kind === "assistant",
    );
    const oldTool = assistants[0].items.find((item) => item.kind === "tool");
    const newTool = assistants[1].items.find((item) => item.kind === "tool");
    expect((oldTool as Extract<RenderItem, { kind: "tool" }>).state).toBe("no-output");
    expect((newTool as Extract<RenderItem, { kind: "tool" }>).state).toBe("input-available");
  });

  it("a trailing user message means no live turn — nothing spins", () => {
    // A just-sent prompt with no assistant output yet: newestAssistantTurnId
    // returns null, so the earlier turn's tool does not spin.
    const blocks: AnyBlock[] = [
      ...danglingToolTurn,
      { type: "user_message", ctx: ctx({ itemId: "u1", responseId: "" }), content: [] },
    ];
    const bubbles = buildBubbles(blocks, null, undefined, [], true);
    const asst = bubbles.find(
      (b): b is Extract<Bubble, { kind: "assistant" }> => b.kind === "assistant",
    )!;
    const tool = asst.items.find((item) => item.kind === "tool");
    expect((tool as Extract<RenderItem, { kind: "tool" }>).state).toBe("no-output");
  });

  it("the cache re-walks on a running→idle flip so a dangling tool stops spinning", () => {
    // The flip carries no block change; the cache key must still see it move.
    const cache = createBubbleCache();
    expect(toolState(buildBubbles(danglingToolTurn, null, cache, [], true))).toBe(
      "input-available",
    );
    expect(toolState(buildBubbles(danglingToolTurn, null, cache, [], false))).toBe("no-output");
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

  /** The inbox wake marker that separates a dispatch turn from its continuation. */
  function wakeMarker(itemId: string): AnyBlock {
    return {
      type: "user_message",
      ctx: ctx({ itemId, responseId: "" }),
      content: [
        {
          type: "input_text",
          text: "[System: sub-agent general-purpose/general-purpose finished (completed) — 1 result waiting in inbox.]",
        },
      ],
    };
  }

  it("folds a backdated result into the prior turn's card without splitting the live bubble", () => {
    // Live out-of-band shape: turn A's spawn call, the inbox wake, turn B
    // streaming, the child's result arrives mid-B backdated to A.
    const blocks: AnyBlock[] = [
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_1", responseId: "resp_A" }),
        executions: [mkExec("spawn_agent", "c1")],
        iteration: 0,
      },
      wakeMarker("u_wake"),
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "Synthesizing " },
      resultBlock("c1", "child output", { itemId: "fco_1", responseId: "resp_A" }),
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "results." },
    ];
    const active: ActiveResponse = { responseId: "resp_B", state: "streaming", error: null };
    const bubbles = buildBubbles(blocks, active);

    // The absorbed result must not split B's narration (an extra
    // assistant bubble = the detached-bubble bug).
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "user", "assistant"]);
    const a = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(a.responseId).toBe("resp_A");
    const tool = toolOf(a);
    // The original card shows the late output (was "No output was
    // recorded" until a manual reload).
    expect(tool.output).toBe("child output");
    expect(tool.state).toBe("output-available");

    const b = bubbles[2] as Extract<Bubble, { kind: "assistant" }>;
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
      wakeMarker("u_wake"),
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "Synthesizing " },
    ];
    const liveId = (buildBubbles(before, active)[2] as Extract<Bubble, { kind: "assistant" }>)
      .stableId;
    const after: AnyBlock[] = [
      ...before,
      resultBlock("c1", "child output", { itemId: "fco_1", responseId: "resp_A" }),
      { type: "text_chunk", ctx: ctx({ responseId: "resp_B" }), text: "results." },
    ];
    const bubble = buildBubbles(after, active)[2] as Extract<Bubble, { kind: "assistant" }>;
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
        id: "u_wake",
        response_id: "resp_T2",
        type: "message",
        role: "user",
        status: "completed",
        content: [
          {
            type: "input_text",
            text: "[System: sub-agent general-purpose/general-purpose finished (completed) — 1 result waiting in inbox.]",
          },
        ],
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
    // bubble (before: an EMPTY assistant bubble appeared for the result).
    expect(bubbles.map((b) => b.kind)).toEqual(["user", "assistant", "user", "assistant"]);
    const t1 = bubbles[1] as Extract<Bubble, { kind: "assistant" }>;
    expect(t1.responseId).toBe("resp_T1");
    const tool = toolOf(t1);
    expect(tool.execution.name).toBe("spawn_agent");
    // The cross-bubble fold: before the fix this was null → the card
    // showed "No output was recorded for this tool call." after reload.
    expect(tool.output).toBe("reviewer findings");
    expect(tool.state).toBe("output-available");

    const t2 = bubbles[3] as Extract<Bubble, { kind: "assistant" }>;
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
      wakeMarker("u_wake"),
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_b", responseId: "resp_2" }),
        executions: [mkExec("Read", "shared")],
        iteration: 0,
      },
      resultBlock("shared", "second output", { itemId: "fco_b", responseId: "resp_2" }),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "user", "assistant"]);
    // If results were ever indexed by bare callId (last-wins), the
    // first card would wrongly show "second output".
    expect(toolOf(bubbles[0]!).output).toBe("first output");
    expect(toolOf(bubbles[2]!).output).toBe("second output");
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
      wakeMarker("u_wake"),
      {
        type: "tool_group",
        ctx: ctx({ itemId: "fc_b", responseId: "resp_2" }),
        executions: [mkExec("Read", "shared")],
        iteration: 0,
      },
      resultBlock("shared", "second output", { itemId: "fco_b", responseId: "resp_2" }),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant", "user", "assistant"]);
    const dangling = toolOf(bubbles[0]!);
    // The unresolved card keeps its honest no-output state.
    expect(dangling.output).toBeNull();
    expect(dangling.state).toBe("no-output");
    expect(toolOf(bubbles[2]!).output).toBe("second output");
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
      wakeMarker("u_wake"),
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
    // No user message separates resp_A from resp_B (a retry-shaped
    // continuation), so the two responses render as ONE turn bubble;
    // pairing is still keyed by (responseId, callId) within it.
    const mid = buildBubbles(new BlockStream().reduceSync(throughOutOfBand), streaming);
    expect(mid.map((b) => b.kind)).toEqual(["assistant"]);
    const midTools = (mid[0] as Extract<Bubble, { kind: "assistant" }>).items.filter(
      (item): item is Extract<RenderItem, { kind: "tool" }> => item.kind === "tool",
    );
    expect(midTools).toHaveLength(2);
    // The delayed output paints resp_A's card mid-stream...
    expect(midTools[0]!.output).toBe("A-output");
    expect(midTools[0]!.state).toBe("output-available");
    // ...while resp_B's same-callId tool keeps spinning instead of
    // adopting resp_A's output.
    expect(midTools[1]!.output).toBeNull();
    expect(midTools[1]!.state).toBe("input-available");

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
    expect(bubbles.map((b) => b.kind)).toEqual(["assistant"]);
    const tools = (bubbles[0] as Extract<Bubble, { kind: "assistant" }>).items.filter(
      (item): item is Extract<RenderItem, { kind: "tool" }> => item.kind === "tool",
    );
    expect(tools).toHaveLength(2);
    // Each call's card carries its own output — cross-pollinating in
    // either direction is the reused-callId bug.
    expect(tools[0]!.output).toBe("A-output");
    expect(tools[1]!.output).toBe("B-output");
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
      routingDecisionBlock(ctx({ itemId: "rd_1", responseId: "routing_1" }), {
        rationale: "multi-file refactor needs deep reasoning",
      }),
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
    expect(chip.applied).toBe(true);
    expect(chip.rationale).toBe("multi-file refactor needs deep reasoning");
  });

  it("carries applied=false for a shadow verdict (would-have-picked)", () => {
    const blocks: AnyBlock[] = [
      routingDecisionBlock(ctx({ itemId: "rd_shadow", responseId: "routing_2" }), {
        model: "databricks-claude-haiku-4-5",
        applied: false,
        rationale: "trivial question",
      }),
    ];
    const chip = buildBubbles(blocks, null)[0] as Extract<Bubble, { kind: "routing_decision" }>;
    // applied=false drives the "would have picked" copy — a flip to true
    // would falsely claim the brain ran on the router's pick.
    expect(chip.applied).toBe(false);
    expect(chip.model).toBe("databricks-claude-haiku-4-5");
  });

  it("reload funnel: a routing_decision item maps through itemsToBlocks to the same bubble", () => {
    const items: ConversationItem[] = [
      routingDecisionItem({
        id: "rd_reload",
        response_id: "routing_3",
        model: "databricks-claude-sonnet-4-6",
        rationale: "moderate knowledge work",
      }),
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
  });

  it("live funnel: a response.output_item.done routing_decision reduces to the same bubble", () => {
    const events: StreamEvent[] = [
      {
        type: "routing_decision",
        model: "databricks-claude-opus-4-8",
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

// A session/turn decision is the verdict on the message that triggered it, so
// the chip renders below that message. The persisted/streamed order differs by
// harness — native paths put the decision first — so the walker defers those.
describe("buildBubbles — routing chip rendered below its user message", () => {
  function chipBlock(
    itemId: string,
    responseId: string,
    scope?: "session" | "turn" | "child_session" | "native_subagent",
  ): AnyBlock {
    return routingDecisionBlock(ctx({ itemId, responseId }), {
      ...(scope !== undefined ? { routing: { scope } } : {}),
    });
  }
  function userBlock(itemId: string, responseId: string): AnyBlock {
    return {
      type: "user_message",
      ctx: ctx({ itemId, responseId }),
      content: [{ type: "input_text", text: "refactor it" }],
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
  // claude-native applies a routed model by typing `/model <alias>` into its
  // TUI; the injection round-trips back through the transcript and lands
  // BETWEEN the decision and the user's own message.
  function modelInjectBlock(itemId: string, responseId: string, alias: string): AnyBlock {
    return {
      type: "slash_command",
      ctx: ctx({ itemId, responseId }),
      kind: "command",
      name: "model",
      arguments: alias,
      output: null,
    };
  }
  function startBlock(responseId: string): AnyBlock {
    return {
      type: "response_start",
      ctx: ctx({ responseId }),
      model: "coder",
      responseId,
      conversationId: null,
    };
  }
  function endBlock(responseId: string): AnyBlock {
    return { type: "response_end", ctx: ctx({ responseId }), status: "completed", response: null };
  }
  const kinds = (bubbles: Bubble[]): string[] => bubbles.map((b) => b.kind);
  const chipIds = (bubbles: Bubble[]): string[] =>
    bubbles.filter((b) => b.kind === "routing_decision").map((b) => b.itemId);

  /**
   * Replay *all* one block at a time, asserting at every frame that the
   * incrementally-cached build equals a from-scratch rebuild, then check the
   * settled order. A frame that reorders, duplicates, or drops a bubble
   * diverges from the rebuild and fails on that frame.
   */
  function expectFrameByFrameStable(
    all: AnyBlock[],
    settledKinds: string[],
    active: ActiveResponse | null = null,
  ): void {
    const cache = createBubbleCache();
    for (let n = 1; n <= all.length; n += 1) {
      const frame = all.slice(0, n);
      expect(buildBubbles(frame, active, cache)).toEqual(buildBubbles(frame, active));
    }
    expect(kinds(cache.bubbles)).toEqual(settledKinds);
  }

  it("defers a turn-scoped chip persisted before its message to below the message", () => {
    // Native-terminal order: routing_decision at position 1, user message at 2.
    const blocks: AnyBlock[] = [
      chipBlock("rd_1", "resp_1", "turn"),
      userBlock("u1", "resp_1"),
      doneBlock("a1", "resp_1", "Done."),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(kinds(bubbles)).toEqual(["user", "routing_decision", "assistant"]);
    // Deferring must not drop or duplicate anything else in the turn.
    expect((bubbles[0] as Extract<Bubble, { kind: "user" }>).itemId).toBe("u1");
    expect(chipIds(bubbles)).toEqual(["rd_1"]);
  });

  it("leaves a chip persisted after its message exactly where it is", () => {
    const blocks: AnyBlock[] = [
      userBlock("u1", "resp_1"),
      chipBlock("rd_1", "resp_1", "turn"),
      doneBlock("a1", "resp_1", "Done."),
    ];
    expect(kinds(buildBubbles(blocks, null))).toEqual(["user", "routing_decision", "assistant"]);
  });

  it("defers a session-scoped and a legacy scope-less decision the same way", () => {
    for (const scope of ["session", undefined] as const) {
      const blocks: AnyBlock[] = [chipBlock("rd_1", "resp_1", scope), userBlock("u1", "resp_1")];
      expect(kinds(buildBubbles(blocks, null))).toEqual(["user", "routing_decision"]);
    }
  });

  it("leaves sub-agent decisions in item order on both sides of the message", () => {
    for (const scope of ["child_session", "native_subagent"] as const) {
      // A spawn's decision belongs to the spawn, not to the user's message.
      const before: AnyBlock[] = [chipBlock("rd_sub", "resp_1", scope), userBlock("u1", "resp_1")];
      expect(kinds(buildBubbles(before, null))).toEqual(["routing_decision", "user"]);
      const after: AnyBlock[] = [userBlock("u1", "resp_1"), chipBlock("rd_sub", "resp_1", scope)];
      expect(kinds(buildBubbles(after, null))).toEqual(["user", "routing_decision"]);
    }
  });

  it("pairs per turn, so each message keeps its own chip below it", () => {
    const blocks: AnyBlock[] = [
      chipBlock("rd_1", "resp_1", "turn"),
      userBlock("u1", "resp_1"),
      doneBlock("a1", "resp_1", "one"),
      chipBlock("rd_2", "resp_2", "turn"),
      userBlock("u2", "resp_2"),
      doneBlock("a2", "resp_2", "two"),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(kinds(bubbles)).toEqual([
      "user",
      "routing_decision",
      "assistant",
      "user",
      "routing_decision",
      "assistant",
    ]);
    expect(chipIds(bubbles)).toEqual(["rd_1", "rd_2"]);
  });

  it("pairs a chip between two messages with the message it already follows", () => {
    // Ambiguity resolves backwards: rd_1 is the verdict on u1, and moving it
    // below u2 would attribute it to the wrong turn.
    const blocks: AnyBlock[] = [
      userBlock("u1", "resp_1"),
      chipBlock("rd_1", "resp_1", "turn"),
      userBlock("u2", "resp_2"),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(kinds(bubbles)).toEqual(["user", "routing_decision", "user"]);
    expect((bubbles[2] as Extract<Bubble, { kind: "user" }>).itemId).toBe("u2");
  });

  it("keeps a chip with no adjacent message in item order", () => {
    // A mid-turn decision (after assistant output, before more output) is
    // nobody's verdict-on-a-message — it stays where it happened.
    const blocks: AnyBlock[] = [
      chipBlock("rd_1", "resp_1", "turn"),
      userBlock("u1", "resp_1"),
      doneBlock("a1", "resp_1", "thinking"),
      chipBlock("rd_2", "resp_1", "turn"),
      doneBlock("a2", "resp_1", "done"),
    ];
    expect(kinds(buildBubbles(blocks, null))).toEqual([
      "user",
      "routing_decision",
      "assistant",
      "routing_decision",
      "assistant",
    ]);
  });

  describe("create-time chip the first turn repeats", () => {
    // A Smart Routing create records the pick as a `session` chip, then the
    // first turn routes again and records the same model as a `turn` chip.
    function verdictChip(
      itemId: string,
      scope: "session" | "turn",
      overrides: Record<string, unknown> = {},
    ): AnyBlock {
      return routingDecisionBlock(ctx({ itemId, responseId: `routing_${itemId}` }), {
        routing: { scope, harness: "claude-native" },
        ...overrides,
      });
    }

    it("renders one chip when the create-time and first-turn picks agree", () => {
      const blocks: AnyBlock[] = [
        verdictChip("rd_create", "session"),
        verdictChip("rd_turn", "turn"),
        userBlock("u1", "resp_1"),
        doneBlock("a1", "resp_1", "Done."),
      ];
      const bubbles = buildBubbles(blocks, null);
      expect(kinds(bubbles)).toEqual(["user", "routing_decision", "assistant"]);
      expect(chipIds(bubbles)).toEqual(["rd_turn"]);
    });

    it("tolerates the /model echo the native path injects between the two", () => {
      const blocks: AnyBlock[] = [
        verdictChip("rd_create", "session"),
        modelInjectBlock("sc_1", "resp_1", "opus"),
        verdictChip("rd_turn", "turn"),
        userBlock("u1", "resp_1"),
      ];
      expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_turn"]);
    });

    // The pair is matched by decision ORDER, not proximity: a booting session
    // writes whatever it writes between the two (the terminal it claimed, a
    // rename), and an adjacency test read every one of those as "no pair".
    it("pairs the two across anything that renders in between", () => {
      const between: AnyBlock[] = [
        doneBlock("a0", "resp_0", "Launched the terminal."),
        userBlock("u0", "resp_0"),
      ];
      for (const filler of between) {
        const blocks: AnyBlock[] = [
          verdictChip("rd_create", "session"),
          filler,
          verdictChip("rd_turn", "turn"),
          userBlock("u1", "resp_1"),
        ];
        expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_turn"]);
      }
    });

    it("pairs the two across a turn-group boundary", () => {
      // A full response lands between them, so the create chip and the turn
      // chip belong to different turn groups.
      const blocks: AnyBlock[] = [
        verdictChip("rd_create", "session"),
        startBlock("resp_0"),
        doneBlock("a0", "resp_0", "Ready."),
        endBlock("resp_0"),
        verdictChip("rd_turn", "turn"),
        userBlock("u1", "resp_1"),
      ];
      expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_turn"]);
    });

    it("drops the create chip once the turn chip arrives mid-stream", () => {
      // The create chip is finalized into the cached prefix frames before the
      // turn chip exists, so the drop can only land by re-walking it. Without
      // that the owner saw both chips for the rest of the session.
      expectFrameByFrameStable(
        [
          verdictChip("rd_create", "session"),
          doneBlock("a0", "resp_0", "Launched the terminal."),
          verdictChip("rd_turn", "turn"),
          userBlock("u1", "resp_1"),
          startBlock("resp_1"),
          doneBlock("a1", "resp_1", "Done."),
          endBlock("resp_1"),
        ],
        ["assistant", "user", "routing_decision", "assistant"],
      );
    });

    it("keeps both when the turn changes the model, harness, or applied flag", () => {
      const differing: Record<string, unknown>[] = [
        { model: "databricks-claude-sonnet-5" },
        { routing: { scope: "turn", harness: "codex-native" } },
        { applied: false },
      ];
      for (const override of differing) {
        const blocks: AnyBlock[] = [
          verdictChip("rd_create", "session"),
          verdictChip("rd_turn", "turn", override),
          userBlock("u1", "resp_1"),
        ];
        expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_create", "rd_turn"]);
      }
    });

    it("keeps a failed create-time route next to the turn that recovered", () => {
      const blocks: AnyBlock[] = [
        verdictChip("rd_create", "session", { model: "unavailable", applied: false }),
        verdictChip("rd_turn", "turn"),
        userBlock("u1", "resp_1"),
      ];
      expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_create", "rd_turn"]);
    });

    it("keeps a later identical turn chip — a real second turn still gets one", () => {
      const blocks: AnyBlock[] = [
        verdictChip("rd_create", "session"),
        verdictChip("rd_turn1", "turn"),
        userBlock("u1", "resp_1"),
        doneBlock("a1", "resp_1", "one"),
        verdictChip("rd_turn2", "turn"),
        userBlock("u2", "resp_2"),
      ];
      expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_turn1", "rd_turn2"]);
    });

    it("stays stable frame by frame as the pair streams in", () => {
      expectFrameByFrameStable(
        [
          verdictChip("rd_create", "session"),
          verdictChip("rd_turn", "turn"),
          userBlock("u1", "resp_1"),
          startBlock("resp_1"),
          doneBlock("a1", "resp_1", "Done."),
          endBlock("resp_1"),
        ],
        ["user", "routing_decision", "assistant"],
      );
    });

    // The rows as a Smart-Routing-harness create actually persists them: the
    // create-time pick, then the first turn's, then the spawn's own decision.
    it("static reload: one session chip survives the itemsToBlocks funnel", () => {
      const items: ConversationItem[] = [
        routingDecisionItem({
          id: "rd_create",
          response_id: "routing_create",
          model: "databricks-claude-opus-4-8",
          rationale: "Routed to claude-opus-4-8 because [prompt_short] holds.",
          harness: "claude-native",
          scope: "session",
          decision_id: "68857ae9-374d-4a9f-8448-958c021bec04",
        }),
        routingDecisionItem({
          id: "rd_turn",
          response_id: "routing_turn",
          model: "databricks-claude-opus-4-8",
          rationale: "Routed to claude-opus-4-8 because [low_ambiguity] holds.",
          harness: "claude-native",
          scope: "turn",
          decision_id: "ac50ed71-8fa9-4faa-aea2-90a6c901156f",
        }),
        {
          id: "u1",
          type: "message",
          role: "user",
          response_id: "resp_1",
          status: "completed",
          content: [{ type: "input_text", text: "refactor it" }],
        } as unknown as ConversationItem,
        // A spawn's own pick is a different scope — always its own chip.
        routingDecisionItem({
          id: "rd_spawn",
          response_id: "routing_spawn",
          model: "databricks-gpt-5-6-sol",
          rationale: "Routed to gpt-5-6-sol.",
          agent: "Explore",
          harness: "codex-native",
          scope: "native_subagent",
        }),
      ];
      const bubbles = buildBubbles(itemsToBlocks(items), null);
      expect(kinds(bubbles)).toEqual(["user", "routing_decision", "routing_decision"]);
      expect(chipIds(bubbles)).toEqual(["rd_turn", "rd_spawn"]);
    });

    // The rows an auto-harness codex session actually persisted, in order:
    // the create-time pick, the resource_event for the terminal it launched,
    // the first turn's identical pick, then the user's message. One verdict,
    // so one chip — the resource_event between them changes nothing.
    it("static reload: a resource_event between the two does not split them", () => {
      const luna = "databricks-gpt-5-6-luna";
      const items: ConversationItem[] = [
        routingDecisionItem({
          id: "rd_create",
          response_id: "routing_create",
          model: luna,
          rationale: "Routed to gpt-5-6-luna because [prompt_short] holds.",
          harness: "codex-native",
          scope: "session",
        }),
        {
          id: "re_1",
          type: "resource_event",
          response_id: "resp_boot",
          status: "completed",
          action: "created",
          resource_id: "term_1",
          resource_type: "terminal",
        } as unknown as ConversationItem,
        routingDecisionItem({
          id: "rd_turn",
          response_id: "routing_turn",
          model: luna,
          rationale: "Routed to gpt-5-6-luna because [low_ambiguity] holds.",
          harness: "codex-native",
          scope: "turn",
        }),
        {
          id: "u1",
          type: "message",
          role: "user",
          response_id: "resp_1",
          status: "completed",
          content: [{ type: "input_text", text: "audit the routing" }],
        } as unknown as ConversationItem,
      ];
      const bubbles = buildBubbles(itemsToBlocks(items), null);
      expect(chipIds(bubbles)).toEqual(["rd_turn"]);
      expect(kinds(bubbles)).toEqual(["user", "routing_decision"]);
    });

    it("keeps a spawn's deny-then-honor pair as two decisions", () => {
      // Two real verdicts on one spawn, not one verdict recorded twice — and
      // never the supersessor of a create-time chip either.
      for (const scope of ["native_subagent", "child_session"] as const) {
        const blocks: AnyBlock[] = [
          verdictChip("rd_create", "session"),
          verdictChip("rd_spawn_deny", "turn", {
            routing: { scope, harness: "claude-native" },
            applied: false,
          }),
          verdictChip("rd_spawn_honor", "turn", {
            routing: { scope, harness: "claude-native" },
          }),
          userBlock("u1", "resp_1"),
        ];
        expect(chipIds(buildBubbles(blocks, null))).toEqual([
          "rd_create",
          "rd_spawn_deny",
          "rd_spawn_honor",
        ]);
      }
    });
  });

  describe("spawn-gate chip the child session repeats", () => {
    // One spawn, two decisions: the in-harness gate sizes the task, then the
    // child session it created routes its own first message. The rows below are
    // the ones a live auto-harness codex session actually persisted.
    const ESCALATE =
      "Routed to claude-opus-4-8 because [not_crosscutting AND prompt_short AND " +
      "low_ambiguity] all hold -> escalate up to claude-opus-4-8.";
    const TRIVIAL =
      "Routed to gpt-5-6-luna because trivial task (prompt<300, no errors/refs, " +
      "llm easy) -> cheapest arm gpt-5-6-luna; never escalate.";

    function gateItem(overrides: RoutingDecisionWire = {}): ConversationItem {
      return routingDecisionItem({
        id: "rd_gate",
        response_id: "routing_gate",
        model: "databricks-claude-opus-4-8",
        rationale: ESCALATE,
        harness: "claude-native",
        scope: "native_subagent",
        decision_id: "48fc9aad-a5a8-4902-ab90-ce85d8d9db1b",
        ...overrides,
      });
    }

    function childItem(overrides: RoutingDecisionWire = {}): ConversationItem {
      return routingDecisionItem({
        id: "rd_child",
        response_id: "routing_child",
        // The gate's own arm is not servable here, so the child ran the best
        // model of that tier and the pick is preserved as ``raw_model``.
        model: "databricks-claude-opus-5",
        raw_model: "claude-opus-4-8",
        rationale: ESCALATE,
        agent: "claude-native-ui",
        harness: "auto",
        scope: "child_session",
        decision_id: "5106aef9-595b-46a7-a99e-5a3a65d5b8ac",
        ...overrides,
      });
    }

    it("static reload: one chip per spawn, and the child row is the one kept", () => {
      // Every decision the live session recorded, in order: its create-time
      // pick, the first turn's repeat of it, one trivial spawn, then the
      // escalated spawn's gate verdict and the child session it produced.
      const items: ConversationItem[] = [
        routingDecisionItem({
          id: "rd_create",
          response_id: "routing_create",
          model: "databricks-gpt-5-6-sol",
          rationale:
            "Routed to gpt-5-6-sol because [not_crosscutting AND prompt_short AND " +
            "low_ambiguity] not all hold -> default gpt-5-6-sol.",
          harness: "codex-native",
          scope: "session",
        }),
        routingDecisionItem({
          id: "rd_turn",
          response_id: "routing_turn",
          model: "databricks-gpt-5-6-sol",
          rationale:
            "Routed to gpt-5-6-sol because [not_crosscutting AND prompt_short] " +
            "not all hold -> default gpt-5-6-sol.",
          harness: "codex-native",
          scope: "turn",
        }),
        {
          id: "u1",
          type: "message",
          role: "user",
          response_id: "resp_1",
          status: "completed",
          content: [{ type: "input_text", text: "spawn subagents to do the work" }],
        } as unknown as ConversationItem,
        routingDecisionItem({
          id: "rd_gate_trivial",
          response_id: "routing_gate_trivial",
          model: "databricks-gpt-5-6-luna",
          rationale: TRIVIAL,
          harness: "codex-native",
          scope: "native_subagent",
        }),
        gateItem(),
        childItem(),
      ];
      const bubbles = buildBubbles(itemsToBlocks(items), null);
      // Three spawns' worth of decisions collapse to one chip each: the turn
      // chip (standing for the create), the trivial spawn, and the escalated
      // spawn — represented by its child row, not its gate row.
      expect(chipIds(bubbles)).toEqual(["rd_turn", "rd_gate_trivial", "rd_child"]);
      const kept = bubbles.find(
        (b) => b.kind === "routing_decision" && b.itemId === "rd_child",
      ) as Extract<Bubble, { kind: "routing_decision" }>;
      // The surviving row names the spawned agent and the arm that ran, with
      // the gate's pick still visible as the router's raw verdict.
      expect(kept.agent).toBe("claude-native-ui");
      expect(kept.model).toBe("databricks-claude-opus-5");
      expect(kept.routing?.rawModel).toBe("claude-opus-4-8");
    });

    it("collapses the pair across anything that renders in between", () => {
      const blocks = itemsToBlocks([gateItem(), childItem()]);
      expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_child"]);
      const spread = itemsToBlocks([
        gateItem(),
        {
          id: "a0",
          type: "message",
          role: "assistant",
          response_id: "resp_0",
          status: "completed",
          content: [{ type: "output_text", text: "Delegated." }],
        } as unknown as ConversationItem,
        childItem(),
      ]);
      expect(chipIds(buildBubbles(spread, null))).toEqual(["rd_child"]);
    });

    it("keeps a deny-then-honor spawn pair as two chips", () => {
      // The gate refused to route (a router outage), and the child then
      // routed — the owner needs to see the attempt that failed.
      const blocks = itemsToBlocks([
        gateItem({ model: "unavailable", applied: false, rationale: ESCALATE }),
        childItem(),
      ]);
      expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_gate", "rd_child"]);
    });

    it("keeps two independent spawns as two chips", () => {
      // Two in-harness spawns, then one child session: the trivial gate row
      // belongs to a different spawn than the child row, so only the pair that
      // shares a verdict collapses.
      const blocks = itemsToBlocks([
        routingDecisionItem({
          id: "rd_gate_trivial",
          response_id: "routing_gate_trivial",
          model: "databricks-gpt-5-6-luna",
          rationale: TRIVIAL,
          harness: "codex-native",
          scope: "native_subagent",
        }),
        gateItem(),
        childItem(),
      ]);
      expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_gate_trivial", "rd_child"]);
    });

    it("keeps a pair whose verdicts genuinely differ as two chips", () => {
      // Same spawn shape, different outcomes: a child that ran another arm, and
      // a child whose decision was about another task, are both real news.
      const differing: RoutingDecisionWire[] = [
        { model: "databricks-claude-sonnet-5", raw_model: "claude-sonnet-5" },
        { rationale: TRIVIAL },
      ];
      for (const override of differing) {
        const blocks = itemsToBlocks([gateItem(), childItem(override)]);
        expect(chipIds(buildBubbles(blocks, null))).toEqual(["rd_gate", "rd_child"]);
      }
    });

    it("stays stable frame by frame as the pair streams in", () => {
      // The gate chip is finalized into the cached prefix before the child row
      // exists, so the drop can only land by re-walking it.
      const streamed = itemsToBlocks([gateItem(), childItem()]);
      const cache = createBubbleCache();
      for (let n = 1; n <= streamed.length; n += 1) {
        const frame = streamed.slice(0, n);
        expect(buildBubbles(frame, null, cache)).toEqual(buildBubbles(frame, null));
      }
      expect(chipIds(cache.bubbles)).toEqual(["rd_child"]);
    });
  });

  it("defers BOTH chips when two session-scoped decisions precede a message", () => {
    // Nothing of the conversation sits between them, so neither is a preamble:
    // both are verdicts on the message that follows, in transcript order.
    const blocks: AnyBlock[] = [
      chipBlock("rd_far", "resp_1", "turn"),
      chipBlock("rd_near", "resp_1", "turn"),
      userBlock("u1", "resp_1"),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(kinds(bubbles)).toEqual(["user", "routing_decision", "routing_decision"]);
    expect(chipIds(bubbles)).toEqual(["rd_far", "rd_near"]);
  });

  it("a sub-agent chip between two decisions blocks the earlier one from moving", () => {
    // The spawn chip renders standalone where it occurred; deferring the chip
    // above it past it would reorder the two.
    const blocks: AnyBlock[] = [
      chipBlock("rd_session", "resp_1", "turn"),
      chipBlock("rd_spawn", "resp_1", "native_subagent"),
      userBlock("u1", "resp_1"),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(kinds(bubbles)).toEqual(["routing_decision", "routing_decision", "user"]);
    expect(chipIds(bubbles)).toEqual(["rd_session", "rd_spawn"]);
  });

  // Rows copied from `GET /v1/sessions/<id>/items` on a session created with
  // Smart Routing as BOTH the model and the harness. The create records the
  // pick as a `session` chip before the harness even boots, the first turn
  // routes again and records a `turn` chip, and the terminal the harness
  // brought up lands a `resource_event` in between — so the session's first
  // message has two chips and a non-conversation row above it.
  describe("auto-harness create (Smart Routing picks the harness too)", () => {
    const RATIONALE =
      "Routed to gpt-5-6-luna because trivial task (prompt<300, no errors/refs, " +
      "llm easy) -> cheapest arm gpt-5-6-luna; never escalate.";

    function resourceEventItem(id: string): ConversationItem {
      return {
        id,
        type: "resource_event",
        response_id: "ef1ee84af89447898edbbd3acb64a5d2",
        status: "completed",
        event_type: "session.resource.created",
        resource_id: "terminal_codex_main",
        resource_type: "terminal",
      } as unknown as ConversationItem;
    }
    function promptItem(): ConversationItem {
      return {
        id: "msg_user",
        type: "message",
        response_id: "codex_019fd3fb",
        status: "completed",
        role: "user",
        content: [{ type: "input_text", text: "what is 2+2?" }],
      } as unknown as ConversationItem;
    }
    function replyItem(): ConversationItem {
      return {
        id: "msg_assistant",
        type: "message",
        response_id: "codex_019fd3fb",
        status: "completed",
        role: "assistant",
        content: [{ type: "output_text", text: "2 + 2 = 4." }],
      } as unknown as ConversationItem;
    }
    /** The captured transcript, with the turn chip's verdict overridable. */
    function autoItems(turn: RoutingDecisionWire = {}): ConversationItem[] {
      return [
        routingDecisionItem({
          id: "rd_create",
          response_id: "routing_d55b96265e01448a8c8cbfdb404b8ac1",
          model: "databricks-gpt-5-6-luna",
          rationale: RATIONALE,
          harness: "codex-native",
          scope: "session",
          decision_id: "628a03ea-da4b-49cb-b41a-6f7c28598381",
        }),
        resourceEventItem("re_1"),
        resourceEventItem("re_2"),
        routingDecisionItem({
          id: "rd_turn",
          response_id: "routing_52d9826e05be482785570c6e86ff41ec",
          model: "databricks-gpt-5-6-luna",
          rationale: RATIONALE,
          harness: "codex-native",
          scope: "turn",
          decision_id: "633569bb-dbfd-4c72-85db-88788583842b",
          ...turn,
        }),
        promptItem(),
        replyItem(),
      ];
    }

    it("renders the create chip BELOW the prompt when the turn re-routes", () => {
      // The turn scored the task differently, so the create pick is real news
      // and survives the collapse — and it is still a verdict on this message,
      // not a preamble to it.
      const blocks = itemsToBlocks(autoItems({ model: "databricks-gpt-5-6-sol" }));
      const bubbles = buildBubbles(blocks, null);
      expect(kinds(bubbles)).toEqual(["user", "routing_decision", "routing_decision", "assistant"]);
      expect(chipIds(bubbles)).toEqual(["rd_create", "rd_turn"]);
    });

    it("still collapses the repeated verdict to one chip below the prompt", () => {
      const bubbles = buildBubbles(itemsToBlocks(autoItems()), null);
      expect(kinds(bubbles)).toEqual(["user", "routing_decision", "assistant"]);
      expect(chipIds(bubbles)).toEqual(["rd_turn"]);
    });

    it("stays stable frame by frame as the auto-harness transcript streams in", () => {
      expectFrameByFrameStable(itemsToBlocks(autoItems({ model: "databricks-gpt-5-6-sol" })), [
        "user",
        "routing_decision",
        "routing_decision",
        "assistant",
      ]);
    });

    it("defers the lone create chip past the terminal's resource row", () => {
      // A create whose first turn never recorded its own chip — captured from
      // an auto-harness session mid-flight.
      const items = [
        routingDecisionItem({
          id: "rd_create",
          model: "databricks-gpt-5-6-sol",
          harness: "codex-native",
          scope: "session",
        }),
        resourceEventItem("re_1"),
        promptItem(),
        replyItem(),
      ];
      const bubbles = buildBubbles(itemsToBlocks(items), null);
      expect(kinds(bubbles)).toEqual(["user", "routing_decision", "assistant"]);
    });
  });

  // The rows a web create pinned to claude-native actually persisted: the
  // session-scope decision is written AT CREATE, so for a while it is the
  // session's only item and the prompt exists only as an optimistic pending
  // bubble (spliced above the chip by `mergePendingBubbles`, tested in
  // ChatPage.test.ts). These pin the walker's half: one chip throughout, and
  // it lands below the message with no second position once it is persisted.
  describe("pinned create routed before its pane launches", () => {
    function createChip(applied = true): ConversationItem {
      return routingDecisionItem({
        id: "rd_create",
        response_id: "routing_0181ee7e17fa4fa8aa8b18c2d41555e1",
        model: "databricks-claude-sonnet-5",
        applied,
        rationale: "Routed to claude-sonnet-5 because trivial task -> cheapest arm.",
        harness: "claude-native",
        scope: "session",
        decision_id: "2fc19175-929e-41e4-ab4a-6b1c07a0cbe7",
      });
    }
    function prompt(): ConversationItem {
      return {
        id: "msg_user",
        type: "message",
        role: "user",
        response_id: "resp_1",
        status: "completed",
        content: [{ type: "input_text", text: "what is 2+2?" }],
      } as unknown as ConversationItem;
    }

    it("renders the lone create chip while no message has been persisted", () => {
      const bubbles = buildBubbles(itemsToBlocks([createChip()]), null);
      expect(kinds(bubbles)).toEqual(["routing_decision"]);
      expect(chipIds(bubbles)).toEqual(["rd_create"]);
    });

    it("keeps a DECLINED create chip visible with no message", () => {
      // Routing failed and the session launched unrouted — the user still has
      // to see that a decision was attempted.
      const bubbles = buildBubbles(itemsToBlocks([createChip(false)]), null);
      expect(kinds(bubbles)).toEqual(["routing_decision"]);
      expect(bubbles[0]).toMatchObject({ kind: "routing_decision", applied: false });
    });

    it("renders the chip once, below the prompt, when the message is appended", () => {
      // Same block list plus the persisted message, through the incremental
      // cache: the held-and-released region must not paint a stale position.
      const cache = createBubbleCache();
      const f1 = itemsToBlocks([createChip()]);
      expect(kinds(buildBubbles(f1, null, cache))).toEqual(["routing_decision"]);
      const f2 = [...f1, ...itemsToBlocks([prompt()])];
      const second = buildBubbles(f2, null, cache);
      expect(kinds(second)).toEqual(["user", "routing_decision"]);
      expect(chipIds(second)).toEqual(["rd_create"]);
      expect(second).toEqual(buildBubbles(f2, null));
    });
  });

  it("static reload: the itemsToBlocks funnel defers the persisted decision too", () => {
    const items: ConversationItem[] = [
      routingDecisionItem({
        id: "rd_reload",
        response_id: "resp_1",
        model: "databricks-claude-sonnet-4-6",
        rationale: "moderate knowledge work",
        scope: "turn",
      }),
      {
        id: "u1",
        type: "message",
        role: "user",
        response_id: "resp_1",
        status: "completed",
        content: [{ type: "input_text", text: "refactor it" }],
      } as unknown as ConversationItem,
    ];
    const bubbles = buildBubbles(itemsToBlocks(items), null);
    expect(kinds(bubbles)).toEqual(["user", "routing_decision"]);
    expect(chipIds(bubbles)).toEqual(["rd_reload"]);
  });

  it("live stream (native order): the chip defers once the message arrives", () => {
    const cache = createBubbleCache();
    const streaming: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    // Frame 1: only the decision has streamed — nothing to sit below yet.
    const f1 = [chipBlock("rd_1", "resp_1", "turn")];
    expect(kinds(buildBubbles(f1, streaming, cache))).toEqual(["routing_decision"]);

    // Frame 2: the message lands and the chip moves below it, once.
    const f2 = [...f1, userBlock("u1", "resp_1")];
    const second = buildBubbles(f2, streaming, cache);
    expect(kinds(second)).toEqual(["user", "routing_decision"]);
    // Incremental output must match a from-scratch rebuild — a duplicated or
    // dropped bubble would show up here.
    expect(second).toEqual(buildBubbles(f2, streaming));

    // Frame 3+: assistant text streams in below the finalized pair.
    const f3 = [...f2, { type: "text_chunk", ctx: ctx({ responseId: "resp_1" }), text: "Wor" }];
    const third = buildBubbles(f3 as AnyBlock[], streaming, cache);
    expect(kinds(third)).toEqual(["user", "routing_decision", "assistant"]);
    expect(third).toEqual(buildBubbles(f3 as AnyBlock[], streaming));

    const f4 = [...f3, { type: "text_chunk", ctx: ctx({ responseId: "resp_1" }), text: "king" }];
    const fourth = buildBubbles(f4 as AnyBlock[], streaming, cache);
    expect(kinds(fourth)).toEqual(["user", "routing_decision", "assistant"]);
    expect(fourth).toEqual(buildBubbles(f4 as AnyBlock[], streaming));
    // The finalized pair is reused by reference, not rebuilt each frame.
    expect(fourth[0]).toBe(third[0]);
    expect(fourth[1]).toBe(third[1]);
  });

  it("live stream: a response_start marker between chip and message doesn't block the pairing", () => {
    const cache = createBubbleCache();
    const streaming: ActiveResponse = { responseId: "resp_1", state: "streaming", error: null };
    const blocks: AnyBlock[] = [
      chipBlock("rd_1", "resp_1", "turn"),
      startBlock("resp_1"),
      userBlock("u1", "resp_1"),
    ];
    const bubbles = buildBubbles(blocks, streaming, cache);
    expect(kinds(bubbles)).toEqual(["user", "routing_decision"]);
    expect(bubbles).toEqual(buildBubbles(blocks, streaming));
  });

  it("live stream: a second turn's chip pairs without disturbing the finalized first turn", () => {
    const cache = createBubbleCache();
    const done: AnyBlock[] = [
      chipBlock("rd_1", "resp_1", "turn"),
      userBlock("u1", "resp_1"),
      doneBlock("a1", "resp_1", "one"),
    ];
    buildBubbles(done, null, cache);
    const withChip = [...done, chipBlock("rd_2", "resp_2", "turn")];
    buildBubbles(withChip, null, cache);
    const next = [...withChip, userBlock("u2", "resp_2")];
    const bubbles = buildBubbles(next, null, cache);
    expect(kinds(bubbles)).toEqual([
      "user",
      "routing_decision",
      "assistant",
      "user",
      "routing_decision",
    ]);
    expect(chipIds(bubbles)).toEqual(["rd_1", "rd_2"]);
    expect(bubbles).toEqual(buildBubbles(next, null));
  });

  it("reused cache: a shorter block array than the cached one rebuilds, never reads off the end", () => {
    // A stale cache (session switch, history reload) can carry a
    // lastBubbleStart pointing past a now-shorter array. chipPendingBeforeRegion
    // must not index off the end — doing so read `undefined.type` and blanked
    // the whole page. The shorter frame must rebuild and match from-scratch.
    const cache = createBubbleCache();
    const long: AnyBlock[] = [
      chipBlock("rd_1", "resp_1", "turn"),
      userBlock("u1", "resp_1"),
      doneBlock("a1", "resp_1", "answer"),
    ];
    buildBubbles(long, null, cache);
    // Now hand it a strictly shorter array (only the chip survives).
    const shorter = [chipBlock("rd_1", "resp_1", "turn")];
    const bubbles = buildBubbles(shorter, null, cache);
    expect(kinds(bubbles)).toEqual(["routing_decision"]);
    expect(bubbles).toEqual(buildBubbles(shorter, null));
  });

  it("live stream: frame-by-frame native order always matches a from-scratch rebuild", () => {
    expectFrameByFrameStable(
      [
        chipBlock("rd_1", "resp_1", "turn"),
        userBlock("u1", "resp_1"),
        startBlock("resp_1"),
        { type: "text_chunk", ctx: ctx({ responseId: "resp_1" }), text: "one" },
        doneBlock("a1", "resp_1", "one"),
        endBlock("resp_1"),
        chipBlock("rd_2", "resp_2", "turn"),
        userBlock("u2", "resp_2"),
        { type: "text_chunk", ctx: ctx({ responseId: "resp_2" }), text: "two" },
        doneBlock("a2", "resp_2", "two"),
      ],
      ["user", "routing_decision", "assistant", "user", "routing_decision", "assistant"],
    );
  });

  it("live stream: frame-by-frame message-first order always matches a from-scratch rebuild", () => {
    expectFrameByFrameStable(
      [
        userBlock("u1", "resp_1"),
        chipBlock("rd_1", "resp_1", "turn"),
        { type: "text_chunk", ctx: ctx({ responseId: "resp_1" }), text: "one" },
        doneBlock("a1", "resp_1", "one"),
        userBlock("u2", "resp_2"),
        chipBlock("rd_2", "resp_2", "turn"),
        { type: "text_chunk", ctx: ctx({ responseId: "resp_2" }), text: "two" },
        doneBlock("a2", "resp_2", "two"),
      ],
      ["user", "routing_decision", "assistant", "user", "routing_decision", "assistant"],
    );
  });

  it("claude-native: the injected /model echo between chip and message doesn't block the pairing", () => {
    // Observed claude-native order (chat.db, sessions "claude sonnet" /
    // "claude opus"): decision, then the `/model <alias>` the apply layer types
    // into the TUI, then the user's own message. codex-native has no such
    // injection, which is why only claude rendered the chip above the message.
    const blocks: AnyBlock[] = [
      chipBlock("rd_1", "routing_1", "turn"),
      modelInjectBlock("sc_1", "resp_inject", "sonnet"),
      userBlock("u1", "resp_u"),
      doneBlock("a1", "resp_u", "Hi!"),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(kinds(bubbles)).toEqual(["assistant", "user", "routing_decision", "assistant"]);
    expect(chipIds(bubbles)).toEqual(["rd_1"]);
  });

  it("claude-native static reload: the itemsToBlocks funnel defers past the /model echo", () => {
    const items: ConversationItem[] = [
      routingDecisionItem({
        id: "rd_reload",
        model: "databricks-claude-sonnet-5",
        rationale: "trivial task",
        scope: "turn",
      }),
      {
        id: "sc_reload",
        type: "slash_command",
        response_id: "resp_inject",
        status: "completed",
        kind: "command",
        name: "model",
        arguments: "sonnet",
      } as unknown as ConversationItem,
      {
        id: "u1",
        type: "message",
        role: "user",
        response_id: "resp_u",
        status: "completed",
        content: [{ type: "input_text", text: "hi" }],
      } as unknown as ConversationItem,
    ];
    const bubbles = buildBubbles(itemsToBlocks(items), null);
    expect(kinds(bubbles)).toEqual(["assistant", "user", "routing_decision"]);
    expect(chipIds(bubbles)).toEqual(["rd_reload"]);
  });

  it("claude-native live stream: the chip drops below the message once it arrives", () => {
    expectFrameByFrameStable(
      [
        chipBlock("rd_1", "routing_1", "turn"),
        modelInjectBlock("sc_1", "resp_inject", "sonnet"),
        userBlock("u1", "resp_u"),
        { type: "text_chunk", ctx: ctx({ responseId: "resp_u" }), text: "Hi" },
        doneBlock("a1", "resp_u", "Hi!"),
      ],
      ["assistant", "user", "routing_decision", "assistant"],
    );
  });

  it("claude-native live stream: the chip+echo pattern on a SECOND turn stays stable", () => {
    // The cache bails out while the region starts at block 0, so the first turn
    // can never exercise reuse. On a later turn the chip↔message region also
    // holds the `/model` echo's own bubble — a region of THREE bubbles. Assuming
    // two left the echo bubble in the reused prefix and re-emitted it, so the
    // echo appeared twice (duplicate React keys) on every following frame.
    expectFrameByFrameStable(
      [
        userBlock("u1", "resp_1"),
        { type: "text_chunk", ctx: ctx({ responseId: "resp_1" }), text: "one" },
        doneBlock("a1", "resp_1", "one"),
        endBlock("resp_1"),
        chipBlock("rd_2", "routing_2", "turn"),
        modelInjectBlock("sc_2", "resp_inject", "opus"),
        userBlock("u2", "resp_u2"),
        startBlock("resp_u2"),
        { type: "text_chunk", ctx: ctx({ responseId: "resp_u2" }), text: "tw" },
        { type: "text_chunk", ctx: ctx({ responseId: "resp_u2" }), text: "o" },
        doneBlock("a2", "resp_u2", "two"),
      ],
      ["user", "assistant", "assistant", "user", "routing_decision", "assistant"],
    );
  });

  it("claude-native live stream: reuse after the second-turn pair drops the whole region", () => {
    // Same shape, asserted directly on the cache: the pair's region spans the
    // chip, the echo bubble, and the message, so `lastBubbleCount` is 3.
    const cache = createBubbleCache();
    const settled: AnyBlock[] = [
      userBlock("u1", "resp_1"),
      doneBlock("a1", "resp_1", "one"),
      chipBlock("rd_2", "routing_2", "turn"),
      modelInjectBlock("sc_2", "resp_inject", "opus"),
      userBlock("u2", "resp_u2"),
    ];
    const first = buildBubbles(settled, null, cache);
    expect(kinds(first)).toEqual(["user", "assistant", "assistant", "user", "routing_decision"]);
    expect(cache.lastBubbleStart).toBe(2);
    expect(cache.lastBubbleCount).toBe(3);

    // Next frame streams the answer — the reused prefix must stop before the
    // echo bubble, not after it.
    const next = [
      ...settled,
      { type: "text_chunk", ctx: ctx({ responseId: "resp_u2" }), text: "t" },
    ];
    const second = buildBubbles(next as AnyBlock[], null, cache);
    expect(kinds(second)).toEqual([
      "user",
      "assistant",
      "assistant",
      "user",
      "routing_decision",
      "assistant",
    ]);
    expect(second).toEqual(buildBubbles(next as AnyBlock[], null));
    // The first turn is still reused by reference (the point of the cache).
    expect(second[0]).toBe(first[0]);
    expect(second[1]).toBe(first[1]);
  });

  it("keeps a chip that already follows its message put, across a /model echo", () => {
    // Backwards adjacency skips the echo too, so the chip is not re-attributed
    // to the NEXT turn's message.
    const blocks: AnyBlock[] = [
      userBlock("u1", "resp_1"),
      modelInjectBlock("sc_1", "resp_inject", "opus"),
      chipBlock("rd_1", "routing_1", "turn"),
      userBlock("u2", "resp_2"),
    ];
    const bubbles = buildBubbles(blocks, null);
    expect(kinds(bubbles)).toEqual(["user", "assistant", "routing_decision", "user"]);
    expect((bubbles[3] as Extract<Bubble, { kind: "user" }>).itemId).toBe("u2");
  });

  it("streaming past an unpaired chip still reuses the finalized prefix by reference", () => {
    // The chip-before-region rebuild must stay bounded: once real assistant
    // output follows the chip it can never pair, so the cache resumes.
    const cache = createBubbleCache();
    const base: AnyBlock[] = [
      chipBlock("rd_1", "routing_1", "turn"),
      modelInjectBlock("sc_1", "resp_inject", "sonnet"),
      doneBlock("a1", "resp_1", "one"),
    ];
    const f1 = [...base, { type: "text_chunk", ctx: ctx({ responseId: "resp_2" }), text: "tw" }];
    const first = buildBubbles(f1 as AnyBlock[], null, cache);
    const f2 = [...f1, { type: "text_chunk", ctx: ctx({ responseId: "resp_2" }), text: "o" }];
    const second = buildBubbles(f2 as AnyBlock[], null, cache);
    // One assistant bubble per turn: the echo, the finished item and the
    // streaming text all sit in the same turn, so they group together.
    expect(kinds(second)).toEqual(["routing_decision", "assistant"]);
    expect(second).toEqual(buildBubbles(f2 as AnyBlock[], null));
    // The chip is still the SAME object — a full rebuild would have minted a
    // new one, so the cache resumed rather than restarting past the chip.
    expect(second[0]).toBe(first[0]);
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
    // A finished turn (resp_1), then the next turn's user message and its
    // streaming reply (resp_2).
    const finished = [userBlock("u1", "resp_1"), doneBlock("a1", "resp_1", "done one")];
    const streaming = { responseId: "resp_2", state: "streaming" as const, error: null };

    const blocks2 = [...finished, userBlock("u2", "resp_2"), chunk("resp_2", "Hel")];
    const first = buildBubbles(blocks2, streaming, cache);
    // [user, assistant(resp_1, finalized), user, assistant(resp_2, streaming)].
    expect(first.map((b) => b.kind)).toEqual(["user", "assistant", "user", "assistant"]);

    // A streaming delta grows the active bubble — append-only extension.
    const blocks3 = [...blocks2, chunk("resp_2", "lo")];
    const second = buildBubbles(blocks3, streaming, cache);

    // The finalized prefix is reused BY REFERENCE — no new objects, so a
    // plain React.memo (===) skips re-rendering + re-markdown of prior
    // turns. If reuse broke, these would be fresh objects every delta.
    expect(second[0]).toBe(first[0]); // user bubble
    expect(second[1]).toBe(first[1]); // finalized assistant(resp_1)
    // The active bubble IS rebuilt (its content changed Hel → Hello).
    expect(second[3]).not.toBe(first[3]);
    const active = second[3] as Extract<Bubble, { kind: "assistant" }>;
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

describe("buildBubbles — workedForS turn duration", () => {
  const textDone = (
    itemId: string,
    text: string,
    stamps?: { timestamp?: number; createdAtS?: number },
  ): AnyBlock => ({
    type: "text_done",
    ctx: ctx({ itemId, ...stamps }),
    fullText: text,
    hasCodeBlocks: false,
  });
  const assistantBubble = (blocks: AnyBlock[]) =>
    buildBubbles(blocks, null)[0] as Extract<Bubble, { kind: "assistant" }>;

  it("spans live block timestamps (page-relative clock)", () => {
    const bubble = assistantBubble([
      textDone("a1", "working…", { timestamp: 10.25 }),
      textDone("a2", "done", { timestamp: 116.5 }),
    ]);
    expect(bubble.workedForS).toBeCloseTo(106.25);
  });

  it("spans server created_at stamps for reloaded history", () => {
    const bubble = assistantBubble([
      textDone("a1", "working…", { createdAtS: 1_753_900_000 }),
      textDone("a2", "done", { createdAtS: 1_753_900_106 }),
    ]);
    expect(bubble.workedForS).toBe(106);
  });

  it("is undefined when stamps are missing or span different clocks", () => {
    // No stamps at all (pre-plumb history).
    expect(assistantBubble([textDone("a1", "a"), textDone("a2", "b")]).workedForS).toBeUndefined();
    // Single-block turn: no span to measure.
    expect(assistantBubble([textDone("a1", "a", { timestamp: 5 })]).workedForS).toBeUndefined();
    // Mixed clocks (page loaded mid-turn): epoch first, page-relative
    // last — never subtract across clocks.
    expect(
      assistantBubble([
        textDone("a1", "a", { createdAtS: 1_753_900_000 }),
        textDone("a2", "b", { timestamp: 42 }),
      ]).workedForS,
    ).toBeUndefined();
    // …and the reverse direction: a turn that began live but whose
    // tail was hydrated (page-relative first, epoch last). The first
    // block's clock picks the branch, so this must also bail.
    expect(
      assistantBubble([
        textDone("a1", "a", { timestamp: 42 }),
        textDone("a2", "b", { createdAtS: 1_753_900_000 }),
      ]).workedForS,
    ).toBeUndefined();
  });
});

describe("buildBubbles — continued turns (sub-agent await)", () => {
  const narrationThenTools = (rid: string): AnyBlock[] => [
    {
      type: "text_done",
      ctx: ctx({ itemId: `${rid}_t`, responseId: rid }),
      fullText: "Dispatching two sub-agents.",
      hasCodeBlocks: false,
    },
    {
      type: "tool_group",
      ctx: ctx({ itemId: `${rid}_g`, responseId: rid }),
      executions: [mkExec("Agent", `${rid}_c1`), mkExec("Agent", `${rid}_c2`)],
      iteration: 0,
    },
  ];
  const answer = (rid: string): AnyBlock => ({
    type: "text_done",
    ctx: ctx({ itemId: `${rid}_a`, responseId: rid }),
    fullText: "Both repos profiled.",
    hasCodeBlocks: false,
  });
  const userMessage = (rid: string, text: string): AnyBlock => ({
    type: "user_message",
    ctx: ctx({ itemId: `${rid}_u`, responseId: rid }),
    content: [{ type: "input_text", text }],
  });
  const assistantAt = (bubbles: Bubble[], i: number) =>
    bubbles[i] as Extract<Bubble, { kind: "assistant" }>;

  it("marks a turn continued across the [System: …] sub-agent wakes", () => {
    // The dispatch turn must END to await its sub-agents; the inbox wake
    // starts a NEW response carrying the answer. Both halves belong to one
    // logical turn, so the first is flagged continued.
    const bubbles = buildBubbles(
      [
        ...narrationThenTools("resp_1"),
        userMessage(
          "resp_2",
          "[System: sub-agent general-purpose finished (completed) — 1 result]",
        ),
        userMessage(
          "resp_3",
          "[System: sub-agent general-purpose finished (completed) — 2 result]",
        ),
        answer("resp_4"),
      ],
      null,
    );
    const assistants = bubbles.filter((b) => b.kind === "assistant");
    expect(assistants).toHaveLength(2);
    expect(assistantAt(assistants, 0).continued).toBe(true);
    // The answer bubble ends the turn — nothing continues it. Unmarked
    // bubbles keep the field unset (no needless clone), so assert falsy.
    expect(assistantAt(assistants, 1).continued).toBeFalsy();
  });

  it("does not mark a turn continued across a real user message", () => {
    const bubbles = buildBubbles(
      [...narrationThenTools("resp_1"), userMessage("resp_2", "Do it again"), answer("resp_3")],
      null,
    );
    const assistants = bubbles.filter((b) => b.kind === "assistant");
    expect(assistantAt(assistants, 0).continued).toBeFalsy();
  });

  it("leaves a lone trailing turn unmarked", () => {
    const bubbles = buildBubbles(narrationThenTools("resp_1"), null);
    const assistants = bubbles.filter((b) => b.kind === "assistant");
    expect(assistantAt(assistants, 0).continued).toBeFalsy();
  });

  it("does not mark anything while a turn is still streaming", () => {
    // Mid-turn the transcript is full of transient fragment bubbles (live
    // text previews, separate reasoning bursts) that merge away as the
    // authoritative items land — a codex turn the server records as ONE
    // response can show as several. Marking them folded and unfolded
    // fragments on every delta.
    const streaming = { responseId: "resp_2", state: "streaming" as const, error: null };
    const bubbles = buildBubbles([...narrationThenTools("resp_1"), answer("resp_2")], streaming);
    const assistants = bubbles.filter((b) => b.kind === "assistant");
    expect(assistantAt(assistants, 0).continued).toBeFalsy();
  });

  it("keeps an existing mark when a later turn starts streaming", () => {
    // Sticky: a bubble that already folded must not reopen just because
    // the next turn began. The wake marker separates the dispatch turn
    // from its continuation, as in the real inbox-wake flow.
    const blocks = [
      ...narrationThenTools("resp_1"),
      userMessage(
        "resp_wake",
        "[System: sub-agent general-purpose/general-purpose finished (completed) — 1 result waiting in inbox.]",
      ),
      answer("resp_2"),
    ];
    const settled = buildBubbles(blocks, null);
    expect(
      assistantAt(
        settled.filter((b) => b.kind === "assistant"),
        0,
      ).continued,
    ).toBe(true);

    const cache = createBubbleCache();
    buildBubbles(blocks, null, cache);
    const streaming = { responseId: "resp_3", state: "streaming" as const, error: null };
    const later = buildBubbles([...blocks, answer("resp_3")], streaming, cache);
    expect(
      assistantAt(
        later.filter((b) => b.kind === "assistant"),
        0,
      ).continued,
    ).toBe(true);
  });

  it("bubblesEqual distinguishes a bubble whose continuation just landed", () => {
    // The memo comparator must see the flip, or the fold never appears
    // when the continuation bubble arrives.
    const before = buildBubbles(narrationThenTools("resp_1"), null);
    const after = buildBubbles([...narrationThenTools("resp_1"), answer("resp_2")], null);
    expect(bubblesEqual(before[0]!, after[0]!)).toBe(false);
  });
});

describe("buildBubbles — anonymous blocks join the surrounding turn", () => {
  const textDone = (itemId: string | null, rid: string, text: string): AnyBlock => ({
    type: "text_done",
    ctx: ctx({ itemId, responseId: rid }),
    fullText: text,
    hasCodeBlocks: false,
  });
  const reasoning = (rid: string): AnyBlock => ({
    type: "reasoning_chunk",
    ctx: ctx({ itemId: null, responseId: rid }),
    text: "thinking",
  });
  const assistants = (bubbles: Bubble[]) =>
    bubbles.filter((b): b is Extract<Bubble, { kind: "assistant" }> => b.kind === "assistant");

  it("keeps a turn whole across id-less reasoning and live previews", () => {
    // Native harnesses emit reasoning before the edge that names the turn
    // (rid "") and stream text as `live:` previews. Splitting on those
    // rendered one turn as several fragment bubbles live but one bubble on
    // reload — the codex fold flicker.
    const bubbles = buildBubbles(
      [
        textDone("m1", "codex_t1", "Checking the CLI."),
        reasoning(""),
        textDone("m2", "codex_t1", "Found the subcommand."),
        textDone("live:m3", "live:m3", "Starting it now…"),
        textDone("m4", "codex_t1", "Started."),
      ],
      null,
    );
    expect(assistants(bubbles)).toHaveLength(1);
    expect(assistants(bubbles)[0]!.responseId).toBe("codex_t1");
    expect(assistants(bubbles)[0]!.items.length).toBeGreaterThanOrEqual(4);
  });

  it("adopts the first real id when the group opens on anonymous blocks", () => {
    // Codex opens reasoning ~2s before the turn is named, so the group can
    // START anonymous; it must become the turn's bubble, not its own.
    const bubbles = buildBubbles(
      [
        reasoning(""),
        textDone("live:m1", "live:m1", "Working on it…"),
        textDone("m2", "codex_t1", "Done."),
      ],
      null,
    );
    expect(assistants(bubbles)).toHaveLength(1);
    expect(assistants(bubbles)[0]!.responseId).toBe("codex_t1");
  });

  it("merges a same-turn continuation across a real response-id change", () => {
    // Codex's step-wise (goal/plan) turns publish a distinct response id
    // per step while the items carry the thread id — one user turn, so
    // one bubble (and eventually ONE "Worked for" fold, not one per
    // step).
    const bubbles = buildBubbles(
      [textDone("m1", "codex_step1", "step one"), textDone("m2", "codex_step2", "step two")],
      null,
    );
    expect(assistants(bubbles)).toHaveLength(1);
    // Lifecycle tracks the LATEST step id.
    expect(assistants(bubbles)[0]!.responseId).toBe("codex_step2");
  });

  it("splits turns at a real user message", () => {
    const bubbles = buildBubbles(
      [
        textDone("m1", "resp_a", "turn A"),
        {
          type: "user_message",
          ctx: ctx({ itemId: "u2", responseId: "resp_b" }),
          content: [{ type: "input_text", text: "next question" }],
        },
        textDone("m2", "resp_b", "turn B"),
      ],
      null,
    );
    expect(assistants(bubbles)).toHaveLength(2);
  });

  it("does not key the bubble off a transient live: preview id", () => {
    // The authoritative item replaces the preview in place; keying off the
    // preview id would remount the bubble at the swap.
    const bubbles = buildBubbles(
      [textDone("live:m1", "live:m1", "streaming…"), textDone("m2", "codex_t1", "Done.")],
      null,
    );
    expect(assistants(bubbles)[0]!.stableId).toBe("m2");
  });
});

describe("lastRenderableAssistantIndex", () => {
  const textDone = (itemId: string, rid: string, text: string): AnyBlock => ({
    type: "text_done",
    ctx: ctx({ itemId, responseId: rid }),
    fullText: text,
    hasCodeBlocks: false,
  });

  it("skips a trailing item-less assistant bubble (floated elicitation phantom)", () => {
    // A parked elicitation forms its own trailing bubble whose card
    // ChatPage floats out, leaving items:[] — it renders null. Counting
    // it as "last assistant" handed the live turn's TRACE to the fold on
    // a reload while parked.
    const bubbles = buildBubbles([textDone("m1", "codex_t", "working…")], null);
    const phantom: Bubble = {
      kind: "assistant",
      responseId: "elicit_e1",
      stableId: "elicit_e1:0",
      lifecycle: "completed",
      error: null,
      items: [],
    };
    const withPhantom = [...bubbles, phantom];
    expect(lastRenderableAssistantIndex(withPhantom)).toBe(0);
  });

  it("returns the real last assistant when it has items, and -1 when none do", () => {
    const bubbles = buildBubbles([textDone("m1", "resp_1", "answer")], null);
    expect(lastRenderableAssistantIndex(bubbles)).toBe(0);
    expect(lastRenderableAssistantIndex([])).toBe(-1);
  });
});

describe("liveCandidateAssistantIndex", () => {
  const textDone = (itemId: string, rid: string, text: string): AnyBlock => ({
    type: "text_done",
    ctx: ctx({ itemId, responseId: rid }),
    fullText: text,
    hasCodeBlocks: false,
  });
  const userMsg = (itemId: string, text: string): AnyBlock => ({
    type: "user_message",
    ctx: ctx({ itemId, responseId: "" }),
    content: [{ type: "input_text", text }],
  });

  it("returns the trailing assistant bubble (normal live-turn shape)", () => {
    const bubbles = buildBubbles(
      [userMsg("u1", "question"), textDone("m1", "r1", "working…")],
      null,
    );
    expect(liveCandidateAssistantIndex(bubbles)).toBe(1);
  });

  it("returns -1 once a real user message follows the last assistant", () => {
    // The reply-in-flight belongs to the newer input (which has no bubble
    // yet), so the settled prior bubble must not lose its fold while the
    // new turn spins up.
    const bubbles = buildBubbles(
      [userMsg("u1", "question"), textDone("m1", "r1", "done"), userMsg("u2", "follow-up")],
      null,
    );
    expect(liveCandidateAssistantIndex(bubbles)).toBe(-1);
  });

  it("ignores a trailing [System: …] wake marker — the turn may continue", () => {
    const bubbles = buildBubbles(
      [
        userMsg("u1", "question"),
        textDone("m1", "r1", "dispatching…"),
        userMsg("u2", "[System: sub-agent general-purpose finished (completed) — 1 result]"),
      ],
      null,
    );
    expect(liveCandidateAssistantIndex(bubbles)).toBe(1);
  });
});

describe("buildBubbles — lastActivityAtS", () => {
  it("carries the newest item's server stamp onto the bubble", () => {
    const textAt = (itemId: string, createdAtS: number): AnyBlock => ({
      type: "text_done",
      ctx: ctx({ itemId, responseId: "resp_1", createdAtS }),
      fullText: "x",
      hasCodeBlocks: false,
    });
    const bubbles = buildBubbles([textAt("m1", 1_753_900_000), textAt("m2", 1_753_900_030)], null);
    const asst = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(asst.lastActivityAtS).toBe(1_753_900_030);
  });

  it("is absent when no block carries a server stamp (pure live turn)", () => {
    const bubbles = buildBubbles(
      [
        {
          type: "text_done",
          ctx: ctx({ itemId: "m1", responseId: "resp_1" }),
          fullText: "x",
          hasCodeBlocks: false,
        },
      ],
      null,
    );
    const asst = bubbles[0] as Extract<Bubble, { kind: "assistant" }>;
    expect(asst.lastActivityAtS).toBeUndefined();
  });
});
