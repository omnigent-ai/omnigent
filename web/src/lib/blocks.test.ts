import { describe, expect, it } from "vitest";
import { hasPendingElicitation, type AnyBlock, type BlockContext } from "./blocks";

function ctx(): BlockContext {
  return { agent: "test", depth: 0, turn: 0, timestamp: 0, responseId: "resp_1", itemId: null };
}

function elicitation(status: "pending" | "answered"): AnyBlock {
  return {
    type: "elicitation",
    ctx: ctx(),
    elicitationId: "elic_1",
    message: "Continue?",
    phase: "request",
    policyName: "session_cost_budget",
    contentPreview: "{}",
    requestedSchema: {},
    status,
    response: null,
  };
}

function text(): AnyBlock {
  return { type: "text_done", ctx: ctx(), fullText: "hi", hasCodeBlocks: false };
}

describe("hasPendingElicitation", () => {
  it("is false for an empty list", () => {
    expect(hasPendingElicitation([])).toBe(false);
  });

  it("is false when no elicitation is pending", () => {
    expect(hasPendingElicitation([text(), elicitation("answered")])).toBe(false);
  });

  it("is true when any elicitation is pending", () => {
    expect(hasPendingElicitation([text(), elicitation("pending"), text()])).toBe(true);
  });

  // The result is cached against the array instance, so a new array must be
  // scanned afresh — the store replaces `blocks` on every change, which is what
  // makes the cache correct rather than stale.
  it("re-evaluates for a different array instance", () => {
    const before: AnyBlock[] = [elicitation("pending")];
    expect(hasPendingElicitation(before)).toBe(true);
    const after: AnyBlock[] = [elicitation("answered")];
    expect(hasPendingElicitation(after)).toBe(false);
  });

  it("returns the same answer for repeated calls on one instance", () => {
    const blocks: AnyBlock[] = [text(), elicitation("pending")];
    expect(hasPendingElicitation(blocks)).toBe(true);
    expect(hasPendingElicitation(blocks)).toBe(true);
  });

  it("scans each array instance only once", () => {
    // A getter on the sole element counts how many times the predicate reads
    // it: once for the first call, never again for the same instance.
    let reads = 0;
    const block = elicitation("answered");
    const spied = {
      get type() {
        reads++;
        return block.type;
      },
      status: block.status,
    } as unknown as AnyBlock;
    const blocks: AnyBlock[] = [spied];

    hasPendingElicitation(blocks);
    expect(reads).toBe(1);
    hasPendingElicitation(blocks);
    hasPendingElicitation(blocks);
    expect(reads).toBe(1);
  });
});
