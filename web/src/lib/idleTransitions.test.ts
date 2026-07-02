import { describe, it, expect } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  buildElicitationMap,
  buildStatusMap,
  computeUnreadBadgeIds,
  detectIdleTransitions,
  detectNewElicitations,
  type ConversationStatus,
} from "./idleTransitions";

function conv(id: string, status?: Conversation["status"]): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    status,
  };
}

function statusMap(entries: Record<string, ConversationStatus>): Map<string, ConversationStatus> {
  return new Map(Object.entries(entries));
}

describe("buildStatusMap", () => {
  it("keys each conversation's status by id", () => {
    const map = buildStatusMap([conv("a", "running"), conv("b", "idle")]);
    expect(map.get("a")).toBe("running");
    expect(map.get("b")).toBe("idle");
  });

  it("omits conversations with undefined status", () => {
    const map = buildStatusMap([conv("a"), conv("b", "idle")]);
    expect(map.has("a")).toBe(false);
    expect(map.get("b")).toBe("idle");
  });
});

describe("detectIdleTransitions", () => {
  it("detects running -> idle", () => {
    const prev = statusMap({ a: "running" });
    const result = detectIdleTransitions(prev, [conv("a", "idle")]);
    expect(result.map((c) => c.id)).toEqual(["a"]);
  });

  it("detects running -> failed", () => {
    const prev = statusMap({ a: "running" });
    const result = detectIdleTransitions(prev, [conv("a", "failed")]);
    expect(result.map((c) => c.id)).toEqual(["a"]);
  });

  it("ignores conversations that were not previously running", () => {
    // Was already idle — a poll refresh must not re-notify.
    const prev = statusMap({ a: "idle" });
    expect(detectIdleTransitions(prev, [conv("a", "idle")])).toEqual([]);
  });

  it("ignores conversations with no prior snapshot (fresh load)", () => {
    expect(detectIdleTransitions(new Map(), [conv("a", "idle")])).toEqual([]);
  });

  it("ignores conversations still running", () => {
    const prev = statusMap({ a: "running" });
    expect(detectIdleTransitions(prev, [conv("a", "running")])).toEqual([]);
  });

  it("ignores transitions to undefined status", () => {
    const prev = statusMap({ a: "running" });
    expect(detectIdleTransitions(prev, [conv("a")])).toEqual([]);
  });

  it("returns only the newly-finished conversations from a mixed list", () => {
    const prev = statusMap({ a: "running", b: "running", c: "idle" });
    const result = detectIdleTransitions(prev, [
      conv("a", "idle"), // finished
      conv("b", "running"), // still working
      conv("c", "idle"), // unchanged
      conv("d", "idle"), // brand new, no prior status
    ]);
    expect(result.map((c) => c.id)).toEqual(["a"]);
  });
});

describe("detectNewElicitations", () => {
  function convE(id: string, count: number): Conversation {
    return {
      id,
      object: "conversation",
      title: id,
      created_at: 0,
      updated_at: 0,
      labels: {},
      permission_level: null,
      pending_elicitations_count: count,
    };
  }

  it("detects a 0 -> 1 increase (agent newly asks for input)", () => {
    const prev = new Map([["a", 0]]);
    // Previous count 0, now 1 -> a genuine new prompt, must fire.
    expect(detectNewElicitations(prev, [convE("a", 1)]).map((c) => c.id)).toEqual(["a"]);
  });

  it("detects an increase across more than one (1 -> 3)", () => {
    const prev = new Map([["a", 1]]);
    // A second and third prompt arrived between polls; still a single fire
    // for the session (the hook de-dupes per id, not per count).
    expect(detectNewElicitations(prev, [convE("a", 3)]).map((c) => c.id)).toEqual(["a"]);
  });

  it("ignores a session with no prior snapshot (fresh load)", () => {
    // No previous entry -> a page load with already-pending prompts must not
    // fire, mirroring the idle fresh-load behavior.
    expect(detectNewElicitations(new Map(), [convE("a", 2)])).toEqual([]);
  });

  it("ignores a steady elicitation count", () => {
    const prev = new Map([["a", 2]]);
    expect(detectNewElicitations(prev, [convE("a", 2)])).toEqual([]);
  });

  it("ignores a decrease (the user answered a prompt)", () => {
    const prev = new Map([["a", 2]]);
    // Count dropped 2 -> 1: the user resolved one; not a new ask, must not fire.
    expect(detectNewElicitations(prev, [convE("a", 1)])).toEqual([]);
  });

  it("treats missing count as 0", () => {
    const prev = new Map([["a", 0]]);
    const conv = { ...convE("a", 0), pending_elicitations_count: undefined };
    expect(detectNewElicitations(prev, [conv])).toEqual([]);
  });
});

describe("buildElicitationMap", () => {
  function convE(id: string, count?: number): Conversation {
    return {
      id,
      object: "conversation",
      title: id,
      created_at: 0,
      updated_at: 0,
      labels: {},
      permission_level: null,
      pending_elicitations_count: count,
    };
  }

  it("keys each conversation's elicitation count by id", () => {
    const map = buildElicitationMap([convE("a", 2), convE("b", 0)]);
    expect(map.get("a")).toBe(2);
    expect(map.get("b")).toBe(0);
  });

  it("defaults a missing count to 0 (so a later 0 -> n increase can fire)", () => {
    // A session present with undefined count must seed as 0, not be absent —
    // otherwise detectNewElicitations would treat its first real count as a
    // fresh load and never fire.
    const map = buildElicitationMap([convE("a")]);
    expect(map.get("a")).toBe(0);
  });
});

describe("computeUnreadBadgeIds", () => {
  /** Conversation with badge-relevant fields under test. */
  function convB(
    id: string,
    opts: { status?: Conversation["status"]; pending?: number; updatedAt?: number } = {},
  ): Conversation {
    return {
      ...conv(id, opts.status ?? "idle"),
      updated_at: opts.updatedAt ?? 100,
      pending_elicitations_count: opts.pending ?? 0,
    };
  }

  /** Predicate marking exactly the given ids unseen (sidebar dot stand-in). */
  function unseenIds(...ids: string[]): (id: string) => boolean {
    const set = new Set(ids);
    return (id: string) => set.has(id);
  }

  it("counts a session the unseen predicate flags", () => {
    const next = computeUnreadBadgeIds([convB("a")], undefined, true, unseenIds("a"));
    expect([...next]).toEqual(["a"]);
  });

  it("counts a session awaiting input even when not unseen", () => {
    // Pending elicitation alone (sidebar "awaiting" badge) puts the session
    // on the dock badge — the user owes it a response.
    const next = computeUnreadBadgeIds([convB("a", { pending: 1 })], undefined, true, unseenIds());
    expect([...next]).toEqual(["a"]);
  });

  it("excludes a seen session with no pending elicitations", () => {
    // Neither unseen nor awaiting -> contributes nothing. A failure here
    // means the badge would count every listed session.
    const next = computeUnreadBadgeIds([convB("a")], undefined, true, unseenIds());
    expect(next.size).toBe(0);
  });

  it("suppresses the actively-viewed session (focused + active)", () => {
    // Focused AND viewing 'a' -> the user is looking at it, so not unread,
    // even though the predicate flags it and it has a pending elicitation.
    const next = computeUnreadBadgeIds([convB("a", { pending: 2 })], "a", true, unseenIds("a"));
    expect(next.size).toBe(0);
  });

  it("counts the open session when the window is blurred", () => {
    // Active conversation is 'a' but the window is blurred -> the user isn't
    // looking, so 'a' still counts. Suppression requires focus AND active.
    const next = computeUnreadBadgeIds([convB("a")], "a", false, unseenIds("a"));
    expect([...next]).toEqual(["a"]);
  });

  it("counts a non-active unseen session while focused elsewhere", () => {
    // Window focused but viewing 'b' -> 'a' is unread.
    const next = computeUnreadBadgeIds([convB("a"), convB("b")], "b", true, unseenIds("a"));
    expect([...next]).toEqual(["a"]);
  });

  it("aggregates unseen and awaiting sessions in one set", () => {
    // 'a' unseen, 'b' awaiting, 'c' both (counted once), 'd' neither,
    // active 'e' suppressed. Size 3 proves de-dupe and suppression together.
    const next = computeUnreadBadgeIds(
      [
        convB("a"),
        convB("b", { pending: 1 }),
        convB("c", { pending: 1 }),
        convB("d"),
        convB("e", { pending: 1 }),
      ],
      "e",
      true,
      unseenIds("a", "c", "e"),
    );
    expect([...next].sort()).toEqual(["a", "b", "c"]);
  });

  it("passes each session's id, updated_at, and status to the predicate", () => {
    // The hook wires isConversationUnseen here; wrong arguments would make
    // the localStorage lookup miss and the badge silently read 0.
    const calls: Array<{ id: string; updatedAt: number; status: string | undefined }> = [];
    computeUnreadBadgeIds(
      [convB("a", { updatedAt: 42, status: "failed" })],
      undefined,
      true,
      (id, updatedAt, status) => {
        calls.push({ id, updatedAt, status });
        return false;
      },
    );
    expect(calls).toEqual([{ id: "a", updatedAt: 42, status: "failed" }]);
  });

  it("returns an empty set for an empty list", () => {
    expect(computeUnreadBadgeIds([], undefined, true, unseenIds("a")).size).toBe(0);
  });
});
