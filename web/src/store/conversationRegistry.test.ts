import { beforeEach, describe, expect, it, vi } from "vitest";

import { ConversationRegistry, maxLiveConversations } from "./conversationRegistry";
import type { PendingUserMessage } from "./chatStore";

/** An unsettled optimistic bubble — the shape that pins an entry. */
function unsentBubble(tempId = "pend_1"): PendingUserMessage {
  return { tempId, content: [{ type: "input_text", text: "hi" }] };
}

/** A bubble whose POST has returned; the server owns it now. */
function postedBubble(tempId = "pend_1"): PendingUserMessage {
  return { ...unsentBubble(tempId), posted: true };
}

describe("maxLiveConversations", () => {
  it("allows 30 over HTTP/2 and only 3 over HTTP/1.1", () => {
    // Not a product number: HTTP/2 multiplexes over one connection, while
    // HTTP/1.1 caps ~6 per origin and an SSE stream holds one for its whole
    // life — 30 there deadlocks every other fetch.
    expect(maxLiveConversations("https:")).toBe(30);
    expect(maxLiveConversations("http:")).toBe(3);
  });
});

describe("ConversationRegistry", () => {
  let registry: ConversationRegistry;
  /** Small enough that eviction is reachable without opening 30 entries. */
  const CAPACITY = 3;

  beforeEach(() => {
    registry = new ConversationRegistry(() => CAPACITY);
  });

  it("creates an entry on first acquire and returns the same one after", () => {
    const first = registry.acquire("conv_a");
    expect(first.id).toBe("conv_a");
    expect(registry.acquire("conv_a")).toBe(first);
    expect(registry.ids()).toEqual(["conv_a"]);
  });

  it("starts an entry from the initial conversation state", () => {
    const entry = registry.acquire("conv_a");
    expect(entry.getState().blocks).toEqual([]);
    expect(entry.getState().sessionStatus).toBe("idle");
    expect(entry.disposed).toBe(false);
  });

  it("keeps each entry's state independent", () => {
    const a = registry.acquire("conv_a");
    const b = registry.acquire("conv_b");
    a.setState({ sessionStatus: "running" });
    // The whole point: a background conversation's status cannot bleed.
    expect(a.getState().sessionStatus).toBe("running");
    expect(b.getState().sessionStatus).toBe("idle");
  });

  it("notifies subscribers with the id of the entry that changed", () => {
    const seen: string[] = [];
    registry.subscribe((id) => seen.push(id));
    const a = registry.acquire("conv_a");
    const b = registry.acquire("conv_b");
    a.setState({ sessionStatus: "running" });
    b.setState({ status: "streaming" });
    expect(seen).toEqual(["conv_a", "conv_b"]);
  });

  it("does not notify when a patch changes nothing", () => {
    const entry = registry.acquire("conv_a");
    const listener = vi.fn();
    registry.subscribe(listener);
    entry.setState({ sessionStatus: "idle" }); // already idle
    expect(listener).not.toHaveBeenCalled();
  });

  it("ignores app-global keys written through an entry", () => {
    const entry = registry.acquire("conv_a");
    const spy = vi.spyOn(console, "error").mockImplementation(() => {});
    try {
      // `selectedModel` is a sticky app-level pref, not conversation state.
      entry.setState({ selectedModel: "opus" } as never);
      expect(
        (entry.getState() as unknown as Record<string, unknown>).selectedModel,
      ).toBeUndefined();
    } finally {
      spy.mockRestore();
    }
  });

  it("evicts the least-recently-viewed entry once over budget", () => {
    // Cap is 3 on HTTP/1.1. conv_a is oldest, so it goes.
    const a = registry.acquire("conv_a");
    registry.acquire("conv_b");
    registry.acquire("conv_c");
    registry.acquire("conv_d");
    expect(registry.ids()).toEqual(["conv_b", "conv_c", "conv_d"]);
    expect(a.disposed).toBe(true);
    expect(registry.has("conv_a")).toBe(false);
  });

  it("treats acquire as a recency touch, so a revisited entry is not the victim", () => {
    registry.acquire("conv_a");
    registry.acquire("conv_b");
    registry.acquire("conv_c");
    registry.acquire("conv_a"); // revisit → conv_b is now oldest
    registry.acquire("conv_d");
    expect(registry.ids()).toEqual(["conv_c", "conv_a", "conv_d"]);
  });

  it("never evicts the conversation on screen, even when it is oldest", () => {
    registry.acquire("conv_a");
    registry.setActive("conv_a");
    registry.acquire("conv_b");
    registry.acquire("conv_c");
    registry.acquire("conv_d");
    expect(registry.has("conv_a")).toBe(true);
  });

  it("never evicts an entry holding a send the server has not acknowledged", () => {
    // The one case where eviction is NOT equivalent to a cold load: until the
    // POST returns, the message exists nowhere but this tab.
    const a = registry.acquire("conv_a");
    a.setState({ pendingUserMessages: [unsentBubble()] });
    registry.acquire("conv_b");
    registry.acquire("conv_c");
    registry.acquire("conv_d");
    expect(registry.has("conv_a")).toBe(true);
    expect(a.disposed).toBe(false);
    // conv_b was the next-oldest unprotected entry.
    expect(registry.has("conv_b")).toBe(false);
  });

  it("does evict an entry whose sends have all settled", () => {
    // Once the POST returns, the server can account for the message — the
    // navigate-back snapshot re-seeds it, so dropping the entry is safe.
    const a = registry.acquire("conv_a");
    a.setState({ pendingUserMessages: [postedBubble()] });
    registry.acquire("conv_b");
    registry.acquire("conv_c");
    registry.acquire("conv_d");
    expect(registry.has("conv_a")).toBe(false);
  });

  it("exceeds the cap rather than destroying unsent work", () => {
    // Pathological: every entry is pinned. Going over budget costs a
    // connection slot; evicting would lose the user's messages.
    for (const id of ["conv_a", "conv_b", "conv_c"]) {
      registry.acquire(id).setState({ pendingUserMessages: [unsentBubble(`pend_${id}`)] });
    }
    registry.acquire("conv_d");
    expect(registry.ids()).toHaveLength(4);
  });

  it("never hands back an entry it just evicted", () => {
    // Eviction runs after insertion, so the entry being acquired is itself a
    // candidate. With every older entry pinned, the sweep would otherwise reach
    // the new one and dispose it — returning a dead entry whose writes are
    // silently dropped, which is worse than exceeding the cap.
    for (const id of ["conv_a", "conv_b", "conv_c"]) {
      registry.acquire(id).setState({ pendingUserMessages: [unsentBubble(`pend_${id}`)] });
    }
    const fresh = registry.acquire("conv_d");
    expect(fresh.disposed).toBe(false);
    expect(registry.has("conv_d")).toBe(true);
  });

  it("aborts the stream when an entry is disposed", () => {
    const entry = registry.acquire("conv_a");
    const controller = new AbortController();
    entry.setState({ abortController: controller });
    registry.release("conv_a");
    expect(controller.signal.aborted).toBe(true);
    expect(entry.disposed).toBe(true);
  });

  it("drops writes to a disposed entry but still serves reads", () => {
    // In-flight async work must be able to unwind without resurrecting state
    // for an evicted conversation.
    const entry = registry.acquire("conv_a");
    entry.setState({ sessionStatus: "running" });
    registry.release("conv_a");
    entry.setState({ sessionStatus: "failed" });
    expect(entry.getState().sessionStatus).toBe("running");
  });

  it("is idempotent on repeated release and dispose", () => {
    const entry = registry.acquire("conv_a");
    registry.release("conv_a");
    expect(() => {
      registry.release("conv_a");
      entry.dispose();
    }).not.toThrow();
  });

  it("clears every entry", () => {
    const a = registry.acquire("conv_a");
    const b = registry.acquire("conv_b");
    registry.setActive("conv_a");
    registry.clear();
    expect(registry.ids()).toEqual([]);
    expect(a.disposed).toBe(true);
    expect(b.disposed).toBe(true);
    expect(registry.getActive()).toBeNull();
  });

  it("reports the active entry, and null once it is gone", () => {
    registry.acquire("conv_a");
    registry.setActive("conv_a");
    expect(registry.getActive()?.id).toBe("conv_a");
    registry.release("conv_a");
    expect(registry.getActive()).toBeNull();
  });
});
