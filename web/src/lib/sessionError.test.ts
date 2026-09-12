import { describe, expect, it } from "vitest";
import type { AnyBlock, BlockContext, ErrorBlock, TextDone } from "./blocks";
import { latestActivityIsError } from "./sessionError";

const ctx: BlockContext = {
  agent: null,
  depth: 0,
  turn: 0,
  timestamp: 0,
  responseId: "r1",
  itemId: "i1",
};
const error: ErrorBlock = {
  type: "error",
  ctx,
  source: "execution",
  code: "rate_limit_exceeded",
  message: "Rate limited",
};
const text = (fullText: string): TextDone => ({
  type: "text_done",
  ctx,
  fullText,
  hasCodeBlocks: false,
});

describe("latestActivityIsError", () => {
  it("recognizes structured errors and ignores info-level notices", () => {
    expect(latestActivityIsError([error])).toBe(true);
    expect(latestActivityIsError([{ ...error, level: "info" }])).toBe(false);
  });

  it("recognizes the native idle-session API rejection text", () => {
    expect(
      latestActivityIsError([
        text(
          "API Error: Request rejected (429) · REQUEST_LIMIT_EXCEEDED: Exceeded workspace input tokens per minute rate limit.",
        ),
      ]),
    ).toBe(true);
  });

  it.each([
    "The API Error: Request rejected (429) has been fixed.",
    "Here is an example:\nAPI Error: Request rejected (429)",
    "```\nAPI Error: Request rejected (429)\n```",
    "All done.",
  ])("does not flag normal assistant prose: %s", (message) => {
    expect(latestActivityIsError([text(message)])).toBe(false);
  });

  it.each([
    text("Recovered."),
    { type: "user_message", ctx, content: [{ type: "input_text", text: "API Error: please fix" }] },
    { type: "tool_result", ctx, callId: "call1", name: "test", output: "done", agentName: "" },
    { type: "policy_denied", ctx, reason: "Approval required", phase: "request" },
    { type: "routing_decision", ctx, model: "test-model", applied: true, rationale: "Selected" },
    {
      type: "elicitation",
      ctx,
      elicitationId: "approval1",
      message: "Continue?",
      phase: "tool_call",
      policyName: "approval",
      contentPreview: "",
      requestedSchema: {},
      status: "responded",
      response: { action: "accept" },
    },
    { ...error, level: "info" },
  ] satisfies AnyBlock[])("clears an old error after newer $type activity", (newer) => {
    expect(latestActivityIsError([error, newer])).toBe(false);
  });

  it("does not let a completed lifecycle marker hide the latest error", () => {
    expect(
      latestActivityIsError([
        error,
        { type: "response_end", ctx, status: "completed", response: null },
      ]),
    ).toBe(true);
  });

  it("detects failed response lifecycle markers", () => {
    expect(
      latestActivityIsError([
        text("Partial reply"),
        { type: "response_end", ctx, status: "failed", response: null },
      ]),
    ).toBe(true);
  });

  it("leaves an empty window unknown", () => {
    expect(latestActivityIsError([])).toBeUndefined();
  });

  it.each([
    { type: "compaction", ctx },
    { type: "compaction_loading", ctx },
    { type: "retry", ctx, source: "llm", attempt: 1, maxAttempts: 2, delaySeconds: 1 },
  ] satisfies AnyBlock[])("skips $type bookkeeping without hiding an earlier error", (metadata) => {
    expect(latestActivityIsError([metadata])).toBeUndefined();
    expect(latestActivityIsError([error, metadata])).toBe(true);
  });
});
