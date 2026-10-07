import { describe, expect, it } from "vitest";
import { CLIENT_ID } from "@/lib/clientId";
import type { SharedQueuedMessage } from "@/lib/events";
import type { QueuedMessage } from "@/store/chatStore";
import { mergeQueuedMessages, ownFlushHead, shouldQueueSend } from "./messageQueue";

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

describe("shouldQueueSend with another window's queued follow-ups", () => {
  const shared = (clientId: string, requiresRetry = false): SharedQueuedMessage => ({
    queueId: "q_1",
    clientId,
    seq: 1,
    text: "theirs",
    attachments: [],
    requiresRetry,
  });

  it("queues an idle send behind a follow-up another window holds", () => {
    // The queue belongs to the session: a new message must not overtake a
    // follow-up queued from the desktop app while the browser sends.
    expect(shouldQueueSend("conv_a", "idle", "idle", [], false, false, [shared("c_other")])).toBe(
      true,
    );
    // Even with always-steer on, ordering wins (same guard as own entries).
    expect(
      shouldQueueSend("conv_a", "streaming", "running", [], true, false, [shared("c_other")]),
    ).toBe(true);
  });

  it("queues an idle send until this connection has seen the session's queue", () => {
    // Between the stream (re)connecting and its `session.queue` snapshot, the
    // queue may hold a follow-up this window knows nothing about yet.
    expect(shouldQueueSend("conv_a", "idle", "idle", [], false, false, [], true)).toBe(true);
    expect(shouldQueueSend("conv_a", "idle", "idle", [], true, false, [], true)).toBe(true);
    // A /side command still bypasses the queue.
    expect(shouldQueueSend("conv_a", "idle", "idle", [], false, true, [], true)).toBe(false);
  });

  it("ignores this window's own echoed entries and failed remote ones", () => {
    expect(shouldQueueSend("conv_a", "idle", "idle", [], false, false, [shared(CLIENT_ID)])).toBe(
      false,
    );
    expect(
      shouldQueueSend("conv_a", "idle", "idle", [], false, false, [shared("c_other", true)]),
    ).toBe(false);
  });
});

describe("mergeQueuedMessages / ownFlushHead", () => {
  const own = (queueId: string, text: string, conversationId = "conv_a"): QueuedMessage => ({
    queueId,
    text,
    conversationId,
  });
  const entry = (
    clientId: string,
    queueId: string,
    seq: number,
    text: string,
    extra: Partial<SharedQueuedMessage> = {},
  ): SharedQueuedMessage => ({
    queueId,
    clientId,
    seq,
    text,
    attachments: [],
    requiresRetry: false,
    ...extra,
  });

  it("interleaves remote entries by session-wide order around own entries", () => {
    const merged = mergeQueuedMessages(
      [own("q_1", "mine first"), own("q_2", "mine second"), own("q_9", "other conv", "conv_b")],
      [
        entry("c_desktop", "q_1", 1, "theirs first"),
        entry(CLIENT_ID, "q_1", 2, "mine first"),
        entry("c_desktop", "q_2", 3, "theirs second", {
          attachments: ["shot.png"],
          createdBy: "alice@example.com",
        }),
        entry(CLIENT_ID, "q_2", 4, "mine second"),
      ],
      "conv_a",
    );
    expect(merged.map((m) => [m.text, m.remote?.clientId ?? "own"])).toEqual([
      ["theirs first", "c_desktop"],
      ["mine first", "own"],
      ["theirs second", "c_desktop"],
      ["mine second", "own"],
    ]);
    // Remote rows carry what the strip needs and a namespaced id.
    expect(merged[2]).toMatchObject({
      queueId: "c_desktop:q_2",
      conversationId: "conv_a",
      remote: { clientId: "c_desktop", attachments: ["shot.png"], createdBy: "alice@example.com" },
    });
    // Own rows are the local objects themselves (actions keep working on them).
    expect(merged[1]).toEqual(own("q_1", "mine first"));
  });

  it("keeps own entries in local order and places unechoed ones last", () => {
    // A local reorder shows immediately even though the server still lists the
    // old order; an entry the server has not seen yet sorts after remote ones.
    const merged = mergeQueuedMessages(
      [own("q_2", "mine second"), own("q_1", "mine first"), own("q_3", "brand new")],
      [
        entry(CLIENT_ID, "q_1", 1, "mine first"),
        entry(CLIENT_ID, "q_2", 2, "mine second"),
        entry("c_desktop", "q_1", 3, "theirs"),
      ],
      "conv_a",
    );
    expect(merged.map((m) => m.text)).toEqual(["mine second", "mine first", "theirs", "brand new"]);
  });

  it("ownFlushHead waits behind a remote head but skips a failed remote one", () => {
    const mine = [own("q_1", "mine")];
    expect(ownFlushHead(mine, [entry("c_desktop", "q_1", 1, "theirs")], "conv_a")).toBeNull();
    expect(
      ownFlushHead(
        mine,
        [entry("c_desktop", "q_1", 1, "theirs", { requiresRetry: true })],
        "conv_a",
      ),
    ).toEqual(mine[0]);
    // Own head ahead of the remote entry (echoed earlier) flushes.
    expect(
      ownFlushHead(
        mine,
        [entry(CLIENT_ID, "q_1", 1, "mine"), entry("c_desktop", "q_1", 2, "theirs")],
        "conv_a",
      ),
    ).toEqual(mine[0]);
    expect(ownFlushHead([], [entry("c_desktop", "q_1", 1, "theirs")], "conv_a")).toBeNull();
  });
});
