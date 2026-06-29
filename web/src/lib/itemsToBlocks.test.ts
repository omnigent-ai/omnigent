// Vitest cases for the items → flat blocks translator. Hand-build item
// arrays matching the server's `ConversationItem` shape; assert on the
// resulting `AnyBlock[]`.

import { describe, expect, it } from "vitest";
import type {
  CompactionBlock,
  ErrorBlock,
  NativeToolBlock,
  SlashCommandBlock,
  TextDone,
  ToolGroup,
  ToolResultBlock,
  UserMessageBlock,
} from "./blocks";
import type { ConversationItem } from "./conversationItems";
import { itemsToBlocks } from "./itemsToBlocks";

function userMessage(responseId: string, text: string, id = "msg_user"): ConversationItem {
  return {
    id,
    response_id: responseId,
    type: "message",
    role: "user",
    status: "completed",
    content: [{ type: "input_text", text }],
  };
}

function assistantMessage(
  responseId: string,
  text: string,
  id = "msg_asst",
  model = "test-agent",
): ConversationItem {
  return {
    id,
    response_id: responseId,
    type: "message",
    role: "assistant",
    status: "completed",
    model,
    content: [{ type: "output_text", text }],
  };
}

function functionCall(
  responseId: string,
  callId: string,
  name: string,
  args: Record<string, unknown>,
  id = `fc_${callId}`,
): ConversationItem {
  return {
    id,
    response_id: responseId,
    type: "function_call",
    status: "completed",
    name,
    arguments: JSON.stringify(args),
    call_id: callId,
  };
}

function functionCallOutput(
  responseId: string,
  callId: string,
  output: string,
  id = `fco_${callId}`,
): ConversationItem {
  return {
    id,
    response_id: responseId,
    type: "function_call_output",
    status: "completed",
    call_id: callId,
    output,
  };
}

describe("itemsToBlocks — flat shape", () => {
  it("skips meta messages so hidden skill context is not rendered on reload", () => {
    const items: ConversationItem[] = [
      {
        id: "msg_meta",
        response_id: "resp_skill",
        type: "message",
        status: "completed",
        role: "user",
        is_meta: true,
        content: [{ type: "input_text", text: "<skill>hidden</skill>" }],
      },
      userMessage("resp_1", "visible"),
    ];
    const blocks = itemsToBlocks(items);
    expect(blocks).toHaveLength(1);
    expect(blocks[0]!.type).toBe("user_message");
    expect((blocks[0] as UserMessageBlock).content).toEqual([
      { type: "input_text", text: "visible" },
    ]);
  });

  it("user + assistant items produce [UserMessageBlock, TextDone] in order", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "Hello", "msg_user1"),
      assistantMessage("resp_1", "Hi there!", "msg_asst1"),
    ];
    const blocks = itemsToBlocks(items);
    expect(blocks.length).toBe(2);
    expect(blocks[0]!.type).toBe("user_message");
    expect(blocks[1]!.type).toBe("text_done");

    const user = blocks[0] as UserMessageBlock;
    expect(user.ctx.itemId).toBe("msg_user1");
    expect(user.ctx.responseId).toBe("resp_1");
    expect(user.content).toEqual([{ type: "input_text", text: "Hello" }]);

    const asst = blocks[1] as TextDone;
    expect(asst.ctx.itemId).toBe("msg_asst1");
    expect(asst.ctx.responseId).toBe("resp_1");
    expect(asst.fullText).toBe("Hi there!");
  });

  it("text containing code fences → hasCodeBlocks=true", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "Show me code"),
      assistantMessage("resp_1", "Here:\n```ts\nconst x = 1;\n```"),
    ];
    const blocks = itemsToBlocks(items);
    const td = blocks.find((b): b is TextDone => b.type === "text_done");
    expect(td?.hasCodeBlocks).toBe(true);
  });

  it("assistant interrupted marker is preserved on TextDone", () => {
    const items: ConversationItem[] = [
      {
        id: "msg_interrupted",
        response_id: "codex_turn_123",
        type: "message",
        role: "assistant",
        status: "completed",
        model: "codex-native-ui",
        interrupted: true,
        content: [{ type: "output_text", text: "partial answer" }],
      },
    ];
    const blocks = itemsToBlocks(items);
    const td = blocks.find((b): b is TextDone => b.type === "text_done");
    expect(td?.ctx.itemId).toBe("msg_interrupted");
    expect(td?.fullText).toBe("partial answer");
    expect(td?.interrupted).toBe(true);
  });

  it("error items produce ErrorBlock banners on reload", () => {
    const items: ConversationItem[] = [
      {
        id: "err_1",
        response_id: "resp_failed",
        type: "error",
        status: "completed",
        source: "execution",
        code: "native_terminal_start_failed",
        message: "Native Codex requires the 'codex' CLI on PATH.",
      },
    ];
    const blocks = itemsToBlocks(items);
    expect(blocks.length).toBe(1);
    const error = blocks[0] as ErrorBlock;
    expect(error.type).toBe("error");
    expect(error.ctx.itemId).toBe("err_1");
    expect(error.ctx.responseId).toBe("resp_failed");
    expect(error.source).toBe("execution");
    expect(error.code).toBe("native_terminal_start_failed");
    expect(error.message).toBe("Native Codex requires the 'codex' CLI on PATH.");
  });

  it("preserves input_image and input_file content on UserMessageBlock", () => {
    const items: ConversationItem[] = [
      {
        id: "msg_user_compound",
        response_id: "resp_1",
        type: "message",
        role: "user",
        status: "completed",
        content: [
          { type: "input_text", text: "Look at this " },
          { type: "input_image", file_id: "file_xyz" },
          { type: "input_text", text: "carefully." },
          { type: "input_file", file_id: "file_abc" },
        ],
      },
    ];
    const blocks = itemsToBlocks(items);
    expect(blocks.length).toBe(1);
    const user = blocks[0] as UserMessageBlock;
    expect(user.content).toEqual([
      { type: "input_text", text: "Look at this " },
      { type: "input_image", file_id: "file_xyz" },
      { type: "input_text", text: "carefully." },
      { type: "input_file", file_id: "file_abc" },
    ]);
  });
});

describe("itemsToBlocks — tool calls", () => {
  it("function_call + function_call_output produce a ToolGroup paired with a ToolResultBlock by call_id", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "What's the time?"),
      functionCall("resp_1", "c1", "get_time", { tz: "UTC" }, "fc_c1"),
      functionCallOutput("resp_1", "c1", "12:00:00 UTC", "fco_c1"),
      assistantMessage("resp_1", "It's noon UTC."),
    ];
    const blocks = itemsToBlocks(items);
    expect(blocks.map((b) => b.type)).toEqual([
      "user_message",
      "tool_group",
      "tool_result",
      "text_done",
    ]);
    const group = blocks[1] as ToolGroup;
    expect(group.ctx.itemId).toBe("fc_c1");
    expect(group.executions[0]!.callId).toBe("c1");
    expect(group.executions[0]!.arguments).toEqual({ tz: "UTC" });
    const result = blocks[2] as ToolResultBlock;
    expect(result.ctx.itemId).toBe("fco_c1");
    expect(result.callId).toBe("c1");
    expect(result.output).toBe("12:00:00 UTC");
  });

  it("multiple tool calls in one response_id stay flat (no bubble boundary)", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "Read three files"),
      functionCall("resp_1", "c1", "Read", { file_path: "/a" }),
      functionCallOutput("resp_1", "c1", "A content"),
      functionCall("resp_1", "c2", "Read", { file_path: "/b" }),
      functionCallOutput("resp_1", "c2", "B content"),
      functionCall("resp_1", "c3", "Read", { file_path: "/c" }),
      functionCallOutput("resp_1", "c3", "C content"),
      assistantMessage("resp_1", "Done."),
    ];
    const blocks = itemsToBlocks(items);
    const groups = blocks.filter((b) => b.type === "tool_group");
    const results = blocks.filter((b) => b.type === "tool_result");
    expect(groups.length).toBe(3);
    expect(results.length).toBe(3);
    // All blocks share the response_id.
    for (const b of blocks) {
      expect(b.ctx.responseId).toBe("resp_1");
    }
  });

  it("malformed JSON arguments fall back to {} without crashing", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "Call with bad args"),
      {
        id: "fc_bad",
        response_id: "resp_1",
        type: "function_call",
        status: "completed",
        name: "Bash",
        arguments: "not valid json",
        call_id: "c_bad",
      },
    ];
    const blocks = itemsToBlocks(items);
    const group = blocks.find((b): b is ToolGroup => b.type === "tool_group");
    expect(group?.executions[0]!.arguments).toEqual({});
  });
});

describe("itemsToBlocks — multiple responses", () => {
  it("items spanning two response_ids produce flat blocks with the right responseIds", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "First", "u1"),
      assistantMessage("resp_1", "Reply 1", "a1"),
      userMessage("resp_2", "Second", "u2"),
      assistantMessage("resp_2", "Reply 2", "a2"),
    ];
    const blocks = itemsToBlocks(items);
    expect(blocks.length).toBe(4);
    expect(blocks[0]!.ctx.responseId).toBe("resp_1");
    expect(blocks[1]!.ctx.responseId).toBe("resp_1");
    expect(blocks[2]!.ctx.responseId).toBe("resp_2");
    expect(blocks[3]!.ctx.responseId).toBe("resp_2");
  });

  it("items without a response_id are silently skipped", () => {
    const items: ConversationItem[] = [
      {
        id: "x",
        response_id: "",
        type: "message",
        status: "completed",
      } as unknown as ConversationItem,
      userMessage("resp_1", "Hello"),
    ];
    const blocks = itemsToBlocks(items);
    expect(blocks.length).toBe(1);
    expect(blocks[0]!.ctx.responseId).toBe("resp_1");
  });
});

describe("itemsToBlocks — native tools and compaction", () => {
  it("native tool item → NativeToolBlock with formatted label", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "Search the web"),
      {
        id: "nt_1",
        response_id: "resp_1",
        type: "web_search_call",
        status: "completed",
        action: { type: "search", query: "omnigent framework" },
      },
      assistantMessage("resp_1", "Found relevant info."),
    ];
    const blocks = itemsToBlocks(items);
    const native = blocks.find((b): b is NativeToolBlock => b.type === "native_tool");
    expect(native).toBeDefined();
    expect(native!.toolType).toBe("web_search_call");
    expect(native!.label).toBe("web search: omnigent framework");
    expect(native!.ctx.itemId).toBe("nt_1");
  });

  it("slash_command item → SlashCommandBlock with name + arguments + output", () => {
    const items: ConversationItem[] = [
      {
        id: "sc_1",
        response_id: "resp_slash",
        type: "slash_command",
        status: "completed",
        kind: "skill",
        name: "oncall",
        arguments: "file-bug",
        output: "oncall: file-bug subcommand started",
        model: "claude-native-ui",
      },
    ];
    const blocks = itemsToBlocks(items);
    const slash = blocks.find((b): b is SlashCommandBlock => b.type === "slash_command");
    expect(slash).toBeDefined();
    expect(slash!.kind).toBe("skill");
    expect(slash!.name).toBe("oncall");
    expect(slash!.arguments).toBe("file-bug");
    expect(slash!.output).toBe("oncall: file-bug subcommand started");
    expect(slash!.ctx.itemId).toBe("sc_1");
    expect(slash!.ctx.responseId).toBe("resp_slash");
  });

  it("skill receipt also hydrates a user-echo bubble before the indicator", () => {
    const items: ConversationItem[] = [
      {
        id: "sc_1",
        response_id: "resp_slash",
        type: "slash_command",
        status: "completed",
        kind: "skill",
        name: "oncall",
        arguments: "file-bug",
        model: "claude-native-ui",
        created_by: "alice@example.com",
      },
    ];
    const blocks = itemsToBlocks(items);
    // Echo precedes the indicator: the receipt is the only transcript
    // record of the user's send, so on reload the typed message must
    // re-materialize as a user bubble, not vanish behind the banner.
    expect(blocks.map((b) => b.type)).toEqual(["user_message", "slash_command"]);
    const echo = blocks[0] as UserMessageBlock;
    expect(echo.content).toEqual([{ type: "input_text", text: "/oncall file-bug" }]);
    // Derived id must match the live reducer's echo so snapshot
    // reconcile dedupes instead of double-rendering the bubble.
    expect(echo.ctx.itemId).toBe("sc_1:user");
    expect(echo.ctx.responseId).toBe("resp_slash");
    // Authorship carries over for shared-session labels.
    expect(echo.ctx.createdBy).toBe("alice@example.com");
  });

  it("kind='command' propagates through the translator (and gets no user echo)", () => {
    // Surfaced CLI built-ins (``/effort``, ``/clear``, …) flow through
    // the same SlashCommandItem path with kind="command" so the
    // renderer can switch the prefix label.
    const items: ConversationItem[] = [
      {
        id: "sc_cmd",
        response_id: "resp_cmd",
        type: "slash_command",
        status: "completed",
        kind: "command",
        name: "effort",
        arguments: "high",
        model: "claude-native-ui",
      },
    ];
    const blocks = itemsToBlocks(items);
    // Command receipts are state changes, not prose — indicator only.
    expect(blocks.map((b) => b.type)).toEqual(["slash_command"]);
    const slash = blocks.find((b): b is SlashCommandBlock => b.type === "slash_command");
    expect(slash).toBeDefined();
    expect(slash!.kind).toBe("command");
    expect(slash!.name).toBe("effort");
  });

  it("normalises a missing output field to null (typical Skill case)", () => {
    // Server omits `output` via exclude_none; translator must coerce
    // to `null` so the renderer's `output !== null` branch is sound.
    // Also: a missing ``kind`` defaults to ``"skill"`` so items
    // persisted before the field was added still render.
    const items: ConversationItem[] = [
      {
        id: "sc_2",
        response_id: "resp_slash",
        type: "slash_command",
        status: "completed",
        name: "dev-productivity:simplify",
        arguments: "",
        model: "claude-native-ui",
      },
    ];
    const blocks = itemsToBlocks(items);
    const slash = blocks.find((b): b is SlashCommandBlock => b.type === "slash_command");
    expect(slash).toBeDefined();
    expect(slash!.kind).toBe("skill");
    expect(slash!.output).toBeNull();
    expect(slash!.arguments).toBe("");
    // Default-skill items get the user echo too; no args → no trailing
    // space artifact in the reconstructed text.
    const echo = blocks.find((b): b is UserMessageBlock => b.type === "user_message");
    expect(echo).toBeDefined();
    expect(echo!.content).toEqual([{ type: "input_text", text: "/dev-productivity:simplify" }]);
  });

  it("compaction item produces a CompactionBlock inline (no synthetic turn)", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "First"),
      assistantMessage("resp_1", "Reply 1"),
      {
        id: "comp_1",
        response_id: "resp_compact",
        type: "compaction",
        status: "completed",
        summary: "Older context summarized",
      },
      userMessage("resp_2", "Continue"),
      assistantMessage("resp_2", "Sure."),
    ];
    const blocks = itemsToBlocks(items);
    const compactions = blocks.filter((b): b is CompactionBlock => b.type === "compaction");
    expect(compactions.length).toBe(1);
    expect(compactions[0]!.ctx.responseId).toBe("resp_compact");
    expect(compactions[0]!.ctx.itemId).toBe("comp_1");
  });
});

describe("itemsToBlocks — edge cases", () => {
  it("assistant message with empty content → TextDone with empty fullText", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "Say nothing"),
      {
        id: "msg_empty",
        response_id: "resp_1",
        type: "message",
        role: "assistant",
        status: "completed",
        model: "test",
        content: [],
      },
    ];
    const blocks = itemsToBlocks(items);
    const td = blocks.find((b): b is TextDone => b.type === "text_done");
    expect(td?.fullText).toBe("");
  });

  it("user-only message (no assistant response yet) → just one UserMessageBlock", () => {
    const items: ConversationItem[] = [userMessage("resp_1", "Just asking")];
    const blocks = itemsToBlocks(items);
    expect(blocks.length).toBe(1);
    expect(blocks[0]!.type).toBe("user_message");
  });

  it("unknown future item types are silently skipped", () => {
    const items: ConversationItem[] = [
      userMessage("resp_1", "Hello"),
      {
        id: "fut_1",
        response_id: "resp_1",
        type: "future_thing",
        status: "completed",
      } as ConversationItem,
      assistantMessage("resp_1", "Hi"),
    ];
    const blocks = itemsToBlocks(items);
    // The unknown item is dropped; only user_message + text_done survive.
    expect(blocks.map((b) => b.type)).toEqual(["user_message", "text_done"]);
  });

  it("hides the compaction summary message injected after /compact", () => {
    const items: ConversationItem[] = [
      userMessage("r1", "hello", "msg_1"),
      assistantMessage("r1", "hi there", "msg_2"),
      // Compaction summary message — should be hidden
      userMessage(
        "r2",
        "This session is being continued from a previous conversation that ran out of context. The summary below covers the earlier portion of the conversation.\n\nSummary:\n1. User asked hello.",
        "msg_compact_summary",
      ),
      // Normal message after compaction — should be visible
      userMessage("r3", "what next?", "msg_3"),
    ];
    const blocks = itemsToBlocks(items);
    const userBlocks = blocks.filter((b) => b.type === "user_message") as UserMessageBlock[];
    // The compaction summary should be hidden; only "hello" and "what next?" remain
    expect(userBlocks).toHaveLength(2);
    const texts = userBlocks.map((b) => b.content.map((c) => ("text" in c ? c.text : "")).join(""));
    expect(texts).toContain("hello");
    expect(texts).toContain("what next?");
    expect(texts.some((t) => t.includes("continued from a previous conversation"))).toBe(false);
  });
});
