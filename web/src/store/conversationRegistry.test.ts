import { beforeEach, describe, expect, it, vi } from "vitest";

import {
  ConversationRegistry,
  getConnectionProtocol,
  maxLiveConversations,
} from "./conversationRegistry";
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
  it("allows 30 when multiplexed and only 3 when serial", () => {
    // Not a product number: HTTP/2 multiplexes over one connection, while
    // HTTP/1.1 caps ~6 per origin and an SSE stream holds one for its whole
    // life — 30 there deadlocks every other fetch.
    expect(maxLiveConversations("multiplexed")).toBe(30);
    expect(maxLiveConversations("serial")).toBe(3);
  });
});

describe("getConnectionProtocol", () => {
  /** Pin the navigation entry's negotiated ALPN id. */
  function withNextHop<T>(nextHopProtocol: string | undefined, body: () => T): T {
    const spy = vi
      .spyOn(performance, "getEntriesByType")
      .mockReturnValue(
        nextHopProtocol === undefined ? [] : [{ nextHopProtocol } as unknown as PerformanceEntry],
      );
    try {
      return body();
    } finally {
      spy.mockRestore();
    }
  }

  it("reads the negotiated protocol rather than the URL scheme", () => {
    // The scheme does not imply the protocol: an HTTPS origin that negotiated
    // HTTP/1.1 has the same ~6-connection ceiling as the dev server, so
    // treating it as multiplexed would deadlock the pool.
    expect(withNextHop("h2", getConnectionProtocol)).toBe("multiplexed");
    expect(withNextHop("h3", getConnectionProtocol)).toBe("multiplexed");
    expect(withNextHop("http/1.1", getConnectionProtocol)).toBe("serial");
  });

  it("falls back to the scheme when no navigation entry is available", () => {
    // Old browsers, non-navigation contexts: `https:` is the better guess.
    expect(withNextHop(undefined, getConnectionProtocol)).toBe(
      location.protocol === "https:" ? "multiplexed" : "serial",
    );
  });

  it("treats an empty nextHopProtocol as unknown", () => {
    // Cross-origin and cached navigations report "" — not a serial transport.
    expect(withNextHop("", getConnectionProtocol)).toBe(
      location.protocol === "https:" ? "multiplexed" : "serial",
    );
  });
});

describe("ConversationRegistry", () => {
  let registry: ConversationRegistry;

  beforeEach(() => {
    registry = new ConversationRegistry();
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

  it("evictLruEvictable disposes the least-recently-viewed entry and returns its id", () => {
    // The slot layer calls this to reclaim one of this tab's own background
    // streams when the origin is saturated. conv_a is oldest → it goes.
    const a = registry.acquire("conv_a");
    registry.acquire("conv_b");
    registry.acquire("conv_c");
    expect(registry.evictLruEvictable()).toBe("conv_a");
    expect(a.disposed).toBe(true);
    expect(registry.ids()).toEqual(["conv_b", "conv_c"]);
  });

  it("returns null when there is nothing to evict", () => {
    expect(registry.evictLruEvictable()).toBeNull();
  });

  it("treats acquire as a recency touch, so a revisited entry is not the victim", () => {
    registry.acquire("conv_a");
    registry.acquire("conv_b");
    registry.acquire("conv_c");
    registry.acquire("conv_a"); // revisit → conv_b is now oldest
    expect(registry.evictLruEvictable()).toBe("conv_b");
    expect(registry.ids()).toEqual(["conv_c", "conv_a"]);
  });

  it("never evicts the conversation on screen, even when it is oldest", () => {
    const a = registry.acquire("conv_a");
    registry.setActive("conv_a");
    registry.acquire("conv_b");
    // conv_a is oldest but on screen → skipped; conv_b is reclaimed instead.
    expect(registry.evictLruEvictable()).toBe("conv_b");
    expect(registry.has("conv_a")).toBe(true);
    expect(a.disposed).toBe(false);
  });

  it("never evicts the conversation being bound (exemptId)", () => {
    // The entry whose stream the slot layer is opening must survive — evicting
    // it would hand back a dead entry.
    registry.acquire("conv_a");
    registry.acquire("conv_d");
    expect(registry.evictLruEvictable("conv_d")).toBe("conv_a");
    expect(registry.has("conv_d")).toBe(true);
  });

  it("never evicts an entry holding a send the server has not acknowledged", () => {
    // The one case where eviction is NOT equivalent to a cold load: until the
    // POST returns, the message exists nowhere but this tab. Skip it, take next.
    const a = registry.acquire("conv_a");
    a.setState({ pendingUserMessages: [unsentBubble()] });
    registry.acquire("conv_b");
    expect(registry.evictLruEvictable()).toBe("conv_b");
    expect(registry.has("conv_a")).toBe(true);
    expect(a.disposed).toBe(false);
  });

  it("never evicts an entry holding a failed-send draft", () => {
    // A send that failed rolls its optimistic bubble back but stashes the text
    // + files as `failedSendDraft` — the only surviving copy, since the server
    // never received it. If that failure settles after the conversation was
    // backgrounded, the rolled-back bubble leaves nothing else pinning the
    // entry, so eviction would drop the retry draft. Pin on the draft too.
    const a = registry.acquire("conv_a");
    a.setState({
      pendingUserMessages: [], // bubble already rolled back
      failedSendDraft: { conversationId: "conv_a", text: "retry me", files: [] },
    });
    registry.acquire("conv_b");
    expect(registry.evictLruEvictable()).toBe("conv_b");
    expect(registry.has("conv_a")).toBe(true);
    expect(a.disposed).toBe(false);
  });

  it("evicts once a failed-send draft is cleared (composer restored it)", () => {
    // The pin releases as soon as the draft is consumed on return, so the entry
    // stops holding a slot the moment its retry copy is safe in the composer.
    const a = registry.acquire("conv_a");
    a.setState({ failedSendDraft: { conversationId: "conv_a", text: "x", files: [] } });
    a.setState({ failedSendDraft: null });
    expect(registry.evictLruEvictable()).toBe("conv_a");
    expect(a.disposed).toBe(true);
  });

  it("does evict an entry whose sends have all settled", () => {
    // Once the POST returns, the server can account for the message — the
    // navigate-back snapshot re-seeds it, so reclaiming its slot is safe.
    const a = registry.acquire("conv_a");
    a.setState({ pendingUserMessages: [postedBubble()] });
    expect(registry.evictLruEvictable()).toBe("conv_a");
    expect(a.disposed).toBe(true);
  });

  it("returns null when every entry is protected rather than destroying unsent work", () => {
    // The slot layer reads this null as "origin saturated, nothing of mine to
    // reclaim" and opens the active conversation over budget with the
    // too-many-tabs banner.
    const a = registry.acquire("conv_a");
    a.setState({ pendingUserMessages: [unsentBubble("pend_a")] });
    registry.acquire("conv_b").setState({ pendingUserMessages: [unsentBubble("pend_b")] });
    registry.setActive("conv_b"); // conv_b on screen, conv_a pinned
    expect(registry.evictLruEvictable()).toBeNull();
    expect(registry.ids()).toEqual(["conv_a", "conv_b"]);
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
