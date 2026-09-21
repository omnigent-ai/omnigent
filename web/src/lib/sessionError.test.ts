import { describe, expect, it } from "vitest";
import type { AnyBlock, BlockContext, ErrorBlock, TextDone } from "./blocks";
import {
  latestActivityErrorState,
  latestActivityErrorWindow,
  latestActivityIsError,
} from "./sessionError";

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

  it("distinguishes runner disconnects from genuine faults and host recovery", () => {
    const disconnected = { ...error, code: "runner_disconnected" };
    expect(latestActivityErrorState([disconnected], false)).toBe("disconnected");
    expect(latestActivityErrorState([disconnected], true)).toBe("recovered_disconnect");
    expect(latestActivityErrorState([error], true)).toBe("error");
  });

  it("preserves a genuine fault in a mixed fault and disconnect cascade", () => {
    const disconnected = { ...error, code: "runner_disconnected", message: "Tunnel dropped" };
    expect(latestActivityErrorState([error, disconnected], false)).toBe("error");
    expect(latestActivityErrorState([error, disconnected], true)).toBe("error");
  });

  it("reports whether a bounded disconnect window still needs older history", () => {
    const disconnected = { ...error, code: "runner_disconnected" };
    expect(latestActivityErrorWindow([disconnected], true)).toEqual({
      state: "recovered_disconnect",
      boundaryResolved: false,
    });
    expect(
      latestActivityErrorWindow(
        [
          { ...error, ctx: { ...ctx, responseId: "r0" } },
          { ...disconnected, ctx: { ...ctx, responseId: "r1" } },
        ],
        true,
      ),
    ).toEqual({ state: "recovered_disconnect", boundaryResolved: true });
  });

  it("does not reach across a causal boundary for an older genuine fault", () => {
    const older = { ...error, ctx: { ...ctx, responseId: "r0", turn: 0 } };
    const disconnected = {
      ...error,
      ctx: { ...ctx, responseId: "r1", turn: 1 },
      code: "runner_disconnected",
    };
    expect(latestActivityErrorState([older, disconnected], true)).toBe("recovered_disconnect");
  });

  it("does not reach across a newer disconnect boundary for native API error text", () => {
    const nativeError = {
      ...text("API Error: Request rejected (429)"),
      ctx: { ...ctx, responseId: "r0", turn: 0 },
    };
    const disconnected = {
      ...error,
      ctx: { ...ctx, responseId: "r1", turn: 1 },
      code: "runner_disconnected",
    };

    expect(latestActivityErrorState([nativeError, disconnected], true)).toBe(
      "recovered_disconnect",
    );
    expect(latestActivityErrorState([nativeError, disconnected], false)).toBe("disconnected");
  });

  it("preserves same-response native API error text before a disconnect", () => {
    const nativeError = text("API Error: Request rejected (429)");
    const disconnected = { ...error, code: "runner_disconnected" };
    expect(latestActivityErrorState([nativeError, disconnected], true)).toBe("error");
  });

  it("lets a recovered disconnect override its generic failed lifecycle marker", () => {
    const disconnected = { ...error, code: "runner_disconnected" };
    expect(
      latestActivityErrorState(
        [disconnected, { type: "response_end", ctx, status: "failed", response: null }],
        true,
      ),
    ).toBe("recovered_disconnect");
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
