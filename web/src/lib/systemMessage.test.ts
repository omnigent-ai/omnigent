import { describe, expect, it } from "vitest";
import { parseSystemMessage } from "./systemMessage";

describe("parseSystemMessage", () => {
  it("returns null for plain user text", () => {
    expect(parseSystemMessage("hello world")).toBeNull();
    expect(parseSystemMessage("[note: not a system message]")).toBeNull();
  });

  it("parses sub-agent completion with body", () => {
    const r = parseSystemMessage("[System: task t_abc (sub_agent) completed]\nfinal answer here");
    expect(r).toEqual({
      kind: "task_completed",
      label: "Sub-agent t_abc completed",
      body: "final answer here",
    });
  });

  it("parses tool completion with multi-line body", () => {
    const r = parseSystemMessage("[System: task t_x (tool) completed]\nline one\nline two");
    expect(r).toEqual({
      kind: "task_completed",
      label: "Tool t_x completed",
      body: "line one\nline two",
    });
  });

  it("parses client_tool completion", () => {
    const r = parseSystemMessage("[System: task t_99 (client_tool) completed]\nresult");
    expect(r?.kind).toBe("task_completed");
    expect(r?.label).toBe("Client tool t_99 completed");
  });

  it("parses task failure with message + traceback", () => {
    const r = parseSystemMessage("[System: task t_abc (tool) failed]\nBoom\nTraceback ...");
    expect(r).toEqual({
      kind: "task_failed",
      label: "Tool t_abc failed",
      body: "Boom\nTraceback ...",
    });
  });

  it("parses task cancellation (no body)", () => {
    const r = parseSystemMessage("[System: task t_abc (sub_agent) cancelled]");
    expect(r).toEqual({
      kind: "task_cancelled",
      label: "Sub-agent t_abc cancelled",
      body: "",
    });
  });

  it("parses bare timer firing (no note)", () => {
    const r = parseSystemMessage("[System: timer my_timer fired]");
    expect(r).toEqual({
      kind: "timer_fired",
      label: "Timer my_timer fired",
      body: "",
    });
  });

  it("parses timer firing with note in body", () => {
    const r = parseSystemMessage("[System: timer my_timer fired]\nnote: 'check on the build'");
    expect(r).toEqual({
      kind: "timer_fired",
      label: "Timer my_timer fired",
      body: "note: 'check on the build'",
    });
  });

  it("parses terminal idle with id", () => {
    const r = parseSystemMessage("[System: terminal shell:session1 is idle]");
    expect(r).toEqual({
      kind: "terminal_idle",
      label: "Terminal shell:session1 idle",
      body: "",
    });
  });

  it("classifies sub-agent wake notices separately from generic system rows", () => {
    const r = parseSystemMessage(
      "[System: sub-agent claude_code/joke-programming finished (completed) — 1 result waiting in inbox. Call sys_read_inbox to collect.]",
    );

    expect(r).toEqual({
      kind: "subagent_wake",
      label: "Sub-agent result ready",
      body: "",
    });
  });

  it("falls back to generic for known prefix but unknown pattern", () => {
    const r = parseSystemMessage("[System: something brand new]");
    expect(r).toEqual({
      kind: "generic",
      label: "something brand new",
      body: "",
    });
  });

  it.each(["[Request interrupted by user]", "[Request interrupted by user for tool use]"])(
    "classifies Claude's interrupt marker %s as a muted indicator",
    (text) => {
      // Claude Code's own Escape record, mirrored from its transcript. We keep
      // it in history but render it as "System: Interrupted", not a user bubble.
      const r = parseSystemMessage(text);
      expect(r).toEqual({ kind: "interrupted", label: "Interrupted", body: "" });
    },
  );

  it("classifies the runner's synthesized codex interrupt marker", () => {
    // codex-native writes no interrupt record, so the runner synthesizes
    // `[System: interrupted]`; it must render as the same "Interrupted" badge
    // as Claude's marker (consistent UX across native harnesses).
    const r = parseSystemMessage(
      "[System: interrupted]\nThe assistant response may be incomplete.",
    );
    expect(r).toEqual({
      kind: "interrupted",
      label: "Interrupted",
      body: "The assistant response may be incomplete.",
    });
  });

  it("does not misclassify a real user message mentioning interruption", () => {
    // Prefix-anchored: only the exact bracketed marker re-classifies, so a
    // user genuinely talking about interrupts still renders as their message.
    expect(parseSystemMessage("can you handle [Request interrupted by user]?")).toBeNull();
    expect(parseSystemMessage("[Request interrupted by user?]")).toBeNull();
  });
});
