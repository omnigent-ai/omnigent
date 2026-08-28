import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { setOmnigentHostConfig } from "@/lib/host";
import type { AnyBlock } from "@/lib/blocks";

import { conversationRegistry } from "./conversationRegistry";
import { markSessionCreated, resetInteractionTelemetryForTests } from "./interactionTelemetry";

// The projector subscribes to `conversationRegistry` on import (above) and derives
// interaction-phase events from committed conversation state. We drive real
// registry entries and assert what it emits to the host analytics sink.

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

// Minimal block fixtures — only the fields the projector reads. Cast once here so
// the tests stay readable; the projector only ever inspects `type` (+ tool ids).
function ctx() {
  return { agent: null, depth: 0, turn: 0, timestamp: 0, responseId: "resp", itemId: null };
}
const textChunk = { type: "text_chunk", ctx: ctx(), text: "hi" };
const errorBlock = { type: "error", ctx: ctx(), message: "boom", source: "llm", code: "x" };
const userEcho = { type: "user_message", ctx: ctx(), content: [] };
function toolGroup(callId: string, name = "shell") {
  return {
    type: "tool_group",
    ctx: ctx(),
    iteration: 0,
    executions: [
      { name, arguments: {}, argsSummary: "", callId, agentName: "a", source: "client" },
    ],
  };
}
function toolResult(callId: string, name = "shell") {
  return { type: "tool_result", ctx: ctx(), name, callId, agentName: "a", output: "ok" };
}
function blocks(...bs: unknown[]): AnyBlock[] {
  return bs as AnyBlock[];
}

function set(id: string, patch: Record<string, unknown>) {
  conversationRegistry.acquire(id).setState(patch);
}

/** interaction_phase events emitted for a given interactionId, in order. */
function phasesFor(interactionId: string) {
  return analytics.mock.calls
    .map(
      (c) =>
        c[0] as {
          type: string;
          interactionId: string;
          phase: string;
          status?: string;
          interactionKind: string;
        },
    )
    .filter((e) => e.type === "interaction_phase" && e.interactionId === interactionId);
}

describe("interaction telemetry projector", () => {
  describe("create_session", () => {
    it("completes on the first assistant text (first AI message)", () => {
      markSessionCreated("s1", "sandbox");
      set("s1", { blocks: blocks(userEcho, textChunk) });

      const p = phasesFor("s1");
      expect(p[0]).toMatchObject({ interactionKind: "create_session_sandbox", phase: "start" });
      expect(p.at(-1)).toMatchObject({ phase: "complete", status: "success" });
    });

    it("completes on a tool call as first AI activity", () => {
      markSessionCreated("s2", "computer");
      set("s2", { blocks: blocks(toolGroup("c1")) });

      expect(phasesFor("s2").at(-1)).toMatchObject({
        interactionKind: "create_session_computer",
        phase: "complete",
        status: "success",
      });
    });

    it("does NOT complete on an error block, and fails when the first turn ends without activity", () => {
      markSessionCreated("s3", "sandbox");
      // error block precedes the terminal — must not be read as a success.
      set("s3", {
        blocks: blocks(errorBlock),
        activeResponse: { responseId: "r", state: "failed", error: "e" },
      });

      const p = phasesFor("s3");
      expect(p.at(-1)).toMatchObject({ phase: "complete", status: "failure" });
      expect(p.some((e) => e.phase === "complete" && e.status === "success")).toBe(false);
    });

    it("does NOT complete on the user_message echo of a slash/skill prompt", () => {
      markSessionCreated("s4", "computer");
      set("s4", { blocks: blocks(userEcho) });

      expect(phasesFor("s4").some((e) => e.phase === "complete")).toBe(false);
    });

    it("bounds the span to the first turn: a later turn's activity cannot complete a stale span", () => {
      markSessionCreated("s5", "sandbox");
      // First response streams then terminates with no activity → fail.
      set("s5", { activeResponse: { responseId: "r1", state: "streaming", error: null } });
      set("s5", { activeResponse: { responseId: "r1", state: "failed", error: "e" } });
      // A second turn produces content — must NOT flip the settled span to success.
      set("s5", {
        blocks: blocks(textChunk),
        activeResponse: { responseId: "r2", state: "streaming", error: null },
      });

      const p = phasesFor("s5");
      expect(p.filter((e) => e.phase === "complete")).toHaveLength(1);
      expect(p.at(-1)).toMatchObject({ phase: "complete", status: "failure" });
    });
  });

  describe("agent_run", () => {
    it("starts on streaming and completes with the mapped outcome", () => {
      set("a1", { activeResponse: { responseId: "run1", state: "streaming", error: null } });
      set("a1", { activeResponse: { responseId: "run1", state: "completed", error: null } });

      const p = phasesFor("run1");
      expect(p[0]).toMatchObject({ interactionKind: "agent_run", phase: "start" });
      expect(p.at(-1)).toMatchObject({ phase: "complete", status: "success" });
    });

    it("maps a failed response to a failure outcome", () => {
      set("a2", { activeResponse: { responseId: "run2", state: "streaming", error: null } });
      set("a2", { activeResponse: { responseId: "run2", state: "failed", error: "e" } });

      expect(phasesFor("run2").at(-1)).toMatchObject({ phase: "complete", status: "failure" });
    });
  });

  describe("tool_call", () => {
    it("starts on tool_group and completes on the matching tool_result", () => {
      set("t1", { blocks: blocks(toolGroup("call_9", "web_search")) });
      set("t1", {
        blocks: blocks(toolGroup("call_9", "web_search"), toolResult("call_9", "web_search")),
      });

      const p = phasesFor("call_9");
      expect(p[0]).toMatchObject({
        interactionKind: "tool_call",
        phase: "start",
        name: "web_search",
      });
      expect(p.at(-1)).toMatchObject({ phase: "complete" });
    });
  });
});
