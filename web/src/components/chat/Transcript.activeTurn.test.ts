// The active-turn mapping that drives the TurnRail highlight. The transcript
// feeds it the bubble index at the viewport midpoint (from the virtualizer's
// getVirtualItemForOffset, a lookup over ALL items — mounted or not), and this
// resolves the owning user turn. Pure, so it's tested without the virtualizer,
// which is what makes it robust to windowing: it never touches the DOM.

import { describe, expect, it } from "vitest";
import type { Bubble } from "@/lib/renderItems";
import { activeTurnIdAtBubbleIndex } from "./Transcript";

function user(itemId: string): Extract<Bubble, { kind: "user" }> {
  return { kind: "user", itemId, content: [{ type: "input_text", text: `msg ${itemId}` }] };
}

function systemUser(itemId: string): Extract<Bubble, { kind: "user" }> {
  return { kind: "user", itemId, content: [{ type: "input_text", text: "[System: interrupted]" }] };
}

function assistant(stableId: string): Extract<Bubble, { kind: "assistant" }> {
  return {
    kind: "assistant",
    responseId: `resp_${stableId}`,
    stableId,
    lifecycle: "completed",
    error: null,
    items: [{ kind: "text", itemId: stableId, text: "reply", final: true }],
  };
}

describe("activeTurnIdAtBubbleIndex", () => {
  // [u0, a, a, u1, a, a] — a long reply spans several bubbles per turn.
  const bubbles: Bubble[] = [
    user("u0"),
    assistant("a0a"),
    assistant("a0b"),
    user("u1"),
    assistant("a1a"),
    assistant("a1b"),
  ];

  it("resolves the user turn owning the midpoint index", () => {
    // Midpoint lands on turn 0's reply (index 2) → turn u0 owns it, even though
    // its user bubble (index 0) is several rows above and would be windowed out.
    expect(activeTurnIdAtBubbleIndex(bubbles, 2)).toBe("u0");
    // Midpoint on turn 1's reply (index 5) → u1.
    expect(activeTurnIdAtBubbleIndex(bubbles, 5)).toBe("u1");
  });

  it("returns the turn's own user bubble when the midpoint is on it", () => {
    expect(activeTurnIdAtBubbleIndex(bubbles, 3)).toBe("u1");
  });

  it("clamps an out-of-range index to the last turn / into range", () => {
    // Past the end (windowed-out below) clamps to the last bubble → last turn.
    expect(activeTurnIdAtBubbleIndex(bubbles, 999)).toBe("u1");
    // Negative clamps to 0 → first turn.
    expect(activeTurnIdAtBubbleIndex(bubbles, -5)).toBe("u0");
  });

  it("falls back to the first user turn when the index precedes every one", () => {
    // Leading assistant bubbles (no user turn yet) then a user turn.
    const leadingReply: Bubble[] = [assistant("x"), assistant("y"), user("u0")];
    expect(activeTurnIdAtBubbleIndex(leadingReply, 0)).toBe("u0");
  });

  it("skips system-marker user bubbles", () => {
    // [u0, sys, a] — the [System: …] marker is not a navigable turn.
    const withSystem: Bubble[] = [user("u0"), systemUser("sys"), assistant("a")];
    expect(activeTurnIdAtBubbleIndex(withSystem, 1)).toBe("u0");
    expect(activeTurnIdAtBubbleIndex(withSystem, 2)).toBe("u0");
  });

  it("returns null for an empty transcript", () => {
    expect(activeTurnIdAtBubbleIndex([], 0)).toBeNull();
  });

  it("returns null when no user turn exists (tool/assistant-only)", () => {
    expect(activeTurnIdAtBubbleIndex([assistant("a"), assistant("b")], 1)).toBeNull();
  });
});
