import { describe, expect, it } from "vitest";
import type { QueuedMessage } from "@/store/chatStore";
import { shouldQueueSend } from "./messageQueue";

describe("shouldQueueSend", () => {
  const q = (conversationId: string): QueuedMessage => ({
    queueId: `q_${conversationId}`,
    text: "queued",
    conversationId,
  });

  it("sends directly (no queue) for a brand-new chat with no conversation", () => {
    expect(shouldQueueSend(null, "streaming", "running", [])).toBe(false);
  });

  it("queues while the session is busy (streaming or running)", () => {
    expect(shouldQueueSend("conv_a", "streaming", "idle", [])).toBe(true);
    expect(shouldQueueSend("conv_a", "idle", "running", [])).toBe(true);
  });

  it("sends directly when idle and nothing is queued for this conversation", () => {
    expect(shouldQueueSend("conv_a", "idle", "idle", [])).toBe(false);
  });

  it("sends directly on `waiting` (turn ended, only background work remains)", () => {
    // A background shell / still-running sub-agent keeps the session in
    // `waiting`, but the server's turn gate is already free — a new message
    // must start a fresh turn rather than stalling in the client queue.
    expect(shouldQueueSend("conv_a", "idle", "waiting", [])).toBe(false);
  });

  it("queues when idle but this conversation already has a queued message", () => {
    // The ordering fix: an idle flicker must not let a later send overtake the
    // still-queued earlier one.
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_a")])).toBe(true);
  });

  it("ignores queued messages belonging to a different conversation", () => {
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_b")])).toBe(false);
  });

  it("sends directly while busy when alwaysSteer is on", () => {
    // The whole point of the preference: a mid-turn follow-up is POSTed now
    // (steered) instead of parking in the queue strip.
    expect(shouldQueueSend("conv_a", "streaming", "idle", [], true)).toBe(false);
    expect(shouldQueueSend("conv_a", "idle", "running", [], true)).toBe(false);
  });

  it("still queues under alwaysSteer when this conversation has a queued message", () => {
    // The ordering guard outranks always-steer: draining must stay in order, so
    // a direct send can't overtake a still-queued earlier one.
    expect(shouldQueueSend("conv_a", "streaming", "running", [q("conv_a")], true)).toBe(true);
  });

  it("sends directly for a /side command even while busy or with a queued message", () => {
    // A codex /side forks its own side chat and is non-interrupting — it must
    // POST now while the parent turn runs, bypassing both the busy gate and the
    // main-thread ordering guard.
    expect(shouldQueueSend("conv_a", "streaming", "running", [], false, true)).toBe(false);
    expect(shouldQueueSend("conv_a", "idle", "idle", [q("conv_a")], false, true)).toBe(false);
  });
});
