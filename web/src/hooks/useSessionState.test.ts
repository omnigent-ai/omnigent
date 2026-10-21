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
    expect(getSessionState(conv({ status: "idle" }), "error")).toEqual({ kind: "error" });
  });

  it("keeps running and approval states ahead of a previous message error", () => {
    expect(getSessionState(conv({ status: "running" }), "error")).toEqual({ kind: "running" });
    expect(
      getSessionState(conv({ status: "idle", pending_elicitations_count: 1 }), "error"),
    ).toEqual({ kind: "awaiting", count: 1 });
  });

  it("returns error from the existing failed session status", () => {
    expect(getSessionState(conv({ status: "failed" }))).toEqual({ kind: "error" });
  });

  it("renders a runner disconnect neutrally and clears stale failed status after recovery", () => {
    expect(getSessionState(conv({ status: "failed" }), "disconnected")).toEqual({
      kind: "disconnected",
    });
    expect(getSessionState(conv({ status: "failed" }), "recovered_disconnect")).toBeNull();
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

describe("getSessionState — runner liveness (asleep)", () => {
  const runnerAsleep = { runnerOnline: false, hostOnline: true } as const;
  const hostDown = { runnerOnline: false, hostOnline: false } as const;

  it("returns asleep for a stopped runner on a live host", () => {
    expect(getSessionState(conv({ status: "idle" }), null, runnerAsleep)).toEqual({
      kind: "asleep",
    });
  });

  it("marks asleep read-only (no restart promise) for viewers who cannot send", () => {
    expect(
      getSessionState(conv({ status: "idle" }), null, { ...runnerAsleep, readOnly: true }),
    ).toEqual({ kind: "asleep", readOnly: true });
  });

  it("shows no badge when the host is down: resumability is not known on the row", () => {
    expect(getSessionState(conv({ status: "idle" }), null, hostDown)).toBeNull();
  });

  it("returns null when the runner state is not yet known or online", () => {
    expect(
      getSessionState(conv({ status: "idle" }), null, {
        runnerOnline: undefined,
        hostOnline: true,
      }),
    ).toBeNull();
    expect(
      getSessionState(conv({ status: "idle" }), null, { runnerOnline: true, hostOnline: true }),
    ).toBeNull();
  });

  it("returns null for a stopped runner on a non-host-bound session", () => {
    expect(
      getSessionState(conv({ status: "idle" }), null, { runnerOnline: false, hostOnline: null }),
    ).toBeNull();
  });

  it("never overrides awaiting or running", () => {
    expect(
      getSessionState(
        conv({ status: "running", pending_elicitations_count: 1 }),
        null,
        runnerAsleep,
      ),
    ).toEqual({ kind: "awaiting", count: 1 });
    expect(getSessionState(conv({ status: "running" }), null, runnerAsleep)).toEqual({
      kind: "running",
    });
  });

  it("orders disconnected > error > asleep", () => {
    expect(getSessionState(conv({ status: "idle" }), "disconnected", runnerAsleep)).toEqual({
      kind: "disconnected",
    });
    expect(getSessionState(conv({ status: "idle" }), "error", runnerAsleep)).toEqual({
      kind: "error",
    });
    expect(getSessionState(conv({ status: "failed" }), null, runnerAsleep)).toEqual({
      kind: "error",
    });
  });

  it("lets a recovered disconnect fall through to asleep", () => {
    expect(getSessionState(conv({ status: "idle" }), "recovered_disconnect", runnerAsleep)).toEqual(
      { kind: "asleep" },
    );
    expect(getSessionState(conv({ status: "idle" }), "recovered_disconnect")).toBeNull();
  });
});
