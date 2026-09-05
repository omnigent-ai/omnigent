import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setOmnigentHostConfig } from "@/lib/host";
import type { AnyBlock } from "@/lib/blocks";

import { conversationRegistry } from "./conversationRegistry";
import {
  markSessionCreated,
  onLiveBlock,
  onResponseEnd,
  onResponseStart,
  resetInteractionTelemetryForTests,
} from "./interactionTelemetry";

// The telemetry functions are called by the pump's live reduce-loop. Here we
// call them directly and assert what they emit to the host analytics sink —
// covering the paths the committed-state projector kept mismeasuring.

const analytics = vi.fn();

beforeEach(() => {
  resetInteractionTelemetryForTests();
  analytics.mockClear();
  setOmnigentHostConfig({ analytics });
});

afterEach(() => {
  conversationRegistry.clear();
  setOmnigentHostConfig({});
});

// Minimal block fixtures — only the fields the functions read. `block` widens
// through `unknown` so the literals aren't excess-property-checked against the
// full block interfaces.
function ctx() {
  return { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp", itemId: null };
}
function block(b: unknown): AnyBlock {
  return b as AnyBlock;
}
const textDone = block({ type: "text_done", ctx: ctx(), fullText: "hi", hasCodeBlocks: false });
const errorBlock = block({ type: "error", ctx: ctx(), message: "boom", source: "llm", code: "x" });
const userEcho = block({ type: "user_message", ctx: ctx(), content: [] });
function toolGroup(callId: string, name = "shell"): AnyBlock {
  return block({
    type: "tool_group",
    ctx: ctx(),
    iteration: 0,
    executions: [
      { name, arguments: {}, argsSummary: "", callId, agentName: "a", source: "client" },
    ],
  });
}
function toolResult(callId: string, name = "shell"): AnyBlock {
  return block({ type: "tool_result", ctx: ctx(), name, callId, agentName: "a", output: "ok" });
}

/** interaction_phase events emitted for a given interactionId, in order. */
function phasesFor(interactionId: string) {
  return analytics.mock.calls
    .map((c) => c[0] as Record<string, unknown>)
    .filter((e) => e.type === "interaction_phase" && e.interactionId === interactionId);
}

describe("interaction telemetry", () => {
  describe("create_session", () => {
    it("completes on the first assistant text (text_done on the native path)", () => {
      markSessionCreated("s1", "sandbox");
      onLiveBlock("s1", userEcho);
      onLiveBlock("s1", textDone);

      const p = phasesFor("s1");
      expect(p[0]).toMatchObject({ interactionKind: "create_session_sandbox", phase: "start" });
      expect(p.at(-1)).toMatchObject({ phase: "complete", status: "success" });
    });

    it("completes on a tool call as first activity", () => {
      markSessionCreated("s2", "computer");
      onLiveBlock("s2", toolGroup("c1"));

      expect(phasesFor("s2").at(-1)).toMatchObject({
        interactionKind: "create_session_computer",
        phase: "complete",
        status: "success",
      });
    });

    it("does NOT complete on a user echo or an error block", () => {
      markSessionCreated("s3", "sandbox");
      onLiveBlock("s3", userEcho);
      onLiveBlock("s3", errorBlock);

      expect(phasesFor("s3").some((e) => e.phase === "complete")).toBe(false);
    });

    it("fails on a genuine failure terminal with no activity", () => {
      markSessionCreated("s4", "sandbox");
      onResponseEnd("s4", "r1", "failed");

      expect(phasesFor("s4").at(-1)).toMatchObject({ phase: "complete", status: "failure" });
    });

    it("does NOT fail on a stray `completed` terminal that revive can reopen (B2)", () => {
      markSessionCreated("s5", "computer");
      onResponseStart("s5", "r1");
      onResponseEnd("s5", "r1", "completed"); // stray idle edge — no activity yet
      // reviveStrayCompletedResponse reopens the turn (no block); late content lands:
      onLiveBlock("s5", textDone);

      const completes = phasesFor("s5").filter((e) => e.phase === "complete");
      expect(completes).toHaveLength(1);
      expect(completes[0]).toMatchObject({ phase: "complete", status: "success" });
    });
  });

  describe("agent_run", () => {
    it("starts on response_start and completes with the mapped outcome", () => {
      onResponseStart("a1", "run1");
      onResponseEnd("a1", "run1", "completed");

      const p = phasesFor("run1");
      expect(p[0]).toMatchObject({ interactionKind: "agent_run", phase: "start" });
      expect(p.at(-1)).toMatchObject({ phase: "complete", status: "success" });
    });

    it("maps failed / incomplete terminals", () => {
      onResponseStart("a2", "run2");
      onResponseEnd("a2", "run2", "failed");
      onResponseStart("a3", "run3");
      onResponseEnd("a3", "run3", "incomplete");

      expect(phasesFor("run2").at(-1)).toMatchObject({ status: "failure" });
      expect(phasesFor("run3").at(-1)).toMatchObject({ status: "cancelled" });
    });

    it("does not double-start on a re-delivered response.created", () => {
      onResponseStart("a4", "run4");
      onResponseStart("a4", "run4"); // reconnect re-delivery
      onResponseEnd("a4", "run4", "completed");

      const starts = phasesFor("run4").filter((e) => e.phase === "start");
      expect(starts).toHaveLength(1);
    });

    it("emits exactly one span across a revive (stray completed → reopen → real end) (B3)", () => {
      onResponseStart("a5", "run5");
      onResponseEnd("a5", "run5", "completed"); // stray terminal
      // revive emits no response_start; the real terminal arrives later for the same id:
      onResponseEnd("a5", "run5", "completed");

      const p = phasesFor("run5");
      expect(p.filter((e) => e.phase === "start")).toHaveLength(1);
      expect(p.filter((e) => e.phase === "complete")).toHaveLength(1);
    });
  });

  describe("tool_call", () => {
    it("starts on tool_group and completes on the result — with NO status (B4)", () => {
      onLiveBlock("t1", toolGroup("call_9", "web_search"));
      onLiveBlock("t1", toolResult("call_9", "web_search"));

      const p = phasesFor("call_9");
      expect(p[0]).toMatchObject({
        interactionKind: "tool_call",
        phase: "start",
        name: "web_search",
      });
      const complete = p.at(-1)!;
      expect(complete.phase).toBe("complete");
      expect(complete).not.toHaveProperty("status");
    });

    it("closes an unresulted tool (no status) when its run ends", () => {
      onResponseStart("t2", "run_t2");
      onLiveBlock("t2", toolGroup("call_x"));
      onResponseEnd("t2", "run_t2", "failed");

      const complete = phasesFor("call_x").find((e) => e.phase === "complete");
      expect(complete).toBeDefined();
      expect(complete).not.toHaveProperty("status");
    });
  });

  describe("disposal (B1)", () => {
    it("settles open spans when the conversation is disposed", () => {
      markSessionCreated("d1", "sandbox");
      onResponseStart("d1", "run_d1");
      conversationRegistry.acquire("d1");
      conversationRegistry.release("d1"); // fires the dispose notification

      expect(phasesFor("d1").at(-1)).toMatchObject({ phase: "complete", status: "cancelled" });
      expect(phasesFor("run_d1").at(-1)).toMatchObject({ phase: "complete", status: "cancelled" });
    });
  });
});
