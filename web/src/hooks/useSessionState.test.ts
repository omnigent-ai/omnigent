import { describe, expect, it } from "vitest";
import { getSessionState } from "./useSessionState";
import type { Conversation } from "@/hooks/useConversations";

function conv(
  partial: Partial<Pick<Conversation, "status" | "pending_elicitations_count">>,
): Pick<Conversation, "status" | "pending_elicitations_count"> {
  return partial;
}

describe("getSessionState — priority composition", () => {
  it("returns awaiting with count when pending_elicitations_count > 0", () => {
    // Awaiting beats Running — the elicitation is the actionable signal.
    expect(getSessionState(conv({ status: "running", pending_elicitations_count: 3 }))).toEqual({
      kind: "awaiting",
      count: 3,
    });
  });

  it("surfaces count=1 distinctly so callers can choose label/tooltip wording", () => {
    expect(getSessionState(conv({ pending_elicitations_count: 1 }))).toEqual({
      kind: "awaiting",
      count: 1,
    });
  });

  it("returns running for conversation.status = running", () => {
    expect(getSessionState(conv({ status: "running" }))).toEqual({
      kind: "running",
    });
  });

  it("returns null when nothing is happening", () => {
    expect(getSessionState(conv({ status: "idle" }))).toBeNull();
  });

  it("flags a latest-message error even when the session has settled to idle", () => {
    expect(getSessionState(conv({ status: "idle" }), true)).toEqual({ kind: "error" });
  });

  it("keeps running and approval states ahead of a previous message error", () => {
    expect(getSessionState(conv({ status: "running" }), true)).toEqual({ kind: "running" });
    expect(getSessionState(conv({ status: "idle", pending_elicitations_count: 1 }), true)).toEqual({
      kind: "awaiting",
      count: 1,
    });
  });

  it("returns error from the existing failed session status", () => {
    expect(getSessionState(conv({ status: "failed" }))).toEqual({ kind: "error" });
  });

  it("follows status updates without retaining an old error", () => {
    const conversation = conv({ status: "failed" });
    expect(getSessionState(conversation)).toEqual({ kind: "error" });
    conversation.status = "running";
    expect(getSessionState(conversation)).toEqual({ kind: "running" });
    conversation.status = "idle";
    expect(getSessionState(conversation)).toBeNull();
  });

  it("treats failed + pending elicitation as awaiting (the actionable signal)", () => {
    expect(getSessionState(conv({ status: "failed", pending_elicitations_count: 2 }))).toEqual({
      kind: "awaiting",
      count: 2,
    });
  });

  it("no longer reads runner liveness — a running session is running regardless", () => {
    // Liveness moved to the open-session view (useSessionLiveness); the
    // sidebar badge state is purely activity-based now, so there is no
    // disconnected state to return.
    expect(getSessionState(conv({ status: "running" }))).toEqual({
      kind: "running",
    });
  });

  it("returns null when conversation is null (missing record)", () => {
    expect(getSessionState(null)).toBeNull();
  });

  it("returns null when conversation status is undefined", () => {
    // Older sessions whose server payload predates the `status` field.
    expect(getSessionState(conv({}))).toBeNull();
  });
});
