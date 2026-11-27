import { act, renderHook } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import type * as IdentityModule from "@/lib/identity";
import {
  acceptMacRead,
  acceptedMacReader,
  advanceMacReadScope,
  beginMacRead,
  macReadCurrent,
  macReadScopeKey,
  onMacReadScopeAdvanced,
  refuseMacReadSentAt,
  resetMacReadScopesForTests,
  showsMacReader,
  useMacReadScopeVersion,
} from "@/lib/macReadScope";

const currentUser = vi.hoisted(() => ({
  id: "user-1" as string | null,
  listeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    currentUser.listeners.add(listener);
    return () => currentUser.listeners.delete(listener);
  },
}));

/** Sign in as `id`, telling the scopes as the identity store would. */
function switchUser(id: string) {
  currentUser.id = id;
  for (const listener of [...currentUser.listeners]) listener();
}

const DEVICE_HOST = "2.host.example.com";
const scope = () => macReadScopeKey("named-host", DEVICE_HOST);

beforeEach(() => {
  currentUser.id = "user-1";
  resetMacReadScopesForTests();
});
afterEach(resetMacReadScopesForTests);

describe("macReadScopeKey", () => {
  it("keys reads by workspace user, provider and host, read when asked", () => {
    expect(macReadScopeKey("named-host", "2.Host.example.com")).toEqual([
      "mac-read",
      "user-1",
      "named-host",
      DEVICE_HOST,
    ]);
    currentUser.id = "user-2";
    expect(scope()[1]).toBe("user-2");
  });

  it("keeps each user's readers apart", () => {
    acceptMacRead(beginMacRead(scope()), "alice");
    currentUser.id = "user-2";
    expect(acceptedMacReader(scope())).toBeNull();
  });
});

describe("acceptMacRead", () => {
  it("refuses a read sent before a newer one that answered first as another reader", () => {
    const a = beginMacRead(scope());
    const b = beginMacRead(scope());
    expect(acceptMacRead(b, "bob")).toBe("accepted");
    expect(acceptMacRead(a, "alice")).toBe("rejected");
    expect(acceptedMacReader(scope())).toBe("bob");
  });

  it("takes a fresh return to an earlier reader as a transition", () => {
    expect(acceptMacRead(beginMacRead(scope()), "alice")).toBe("accepted");
    expect(acceptMacRead(beginMacRead(scope()), "bob")).toBe("transition");
    expect(acceptMacRead(beginMacRead(scope()), "alice")).toBe("transition");
    expect(acceptedMacReader(scope())).toBe("alice");
  });

  it("refuses a delayed first-reader answer from another batch after A, B, A", () => {
    expect(acceptMacRead(beginMacRead(scope()), "alice")).toBe("accepted"); // 1
    const heldTooltip = beginMacRead(scope()); // 2, as alice, held
    expect(acceptMacRead(beginMacRead(scope()), "bob")).toBe("transition"); // 3
    expect(acceptMacRead(beginMacRead(scope()), "alice")).toBe("transition"); // 4, from the list
    // Same login as now, but sent before the switch back: it can't prove it's fresh.
    expect(acceptMacRead(heldTooltip, "alice")).toBe("rejected");
  });

  it("refuses reads sent before the shell reported an account change, whoever answered", () => {
    expect(acceptMacRead(beginMacRead(scope()), "alice")).toBe("accepted");
    const before = beginMacRead(scope());
    advanceMacReadScope(scope());
    expect(acceptedMacReader(scope())).toBeNull();
    expect(acceptMacRead(before, "alice")).toBe("rejected");
    expect(acceptMacRead(before, "bob")).toBe("rejected");
    // The first answer after the change names the new reader.
    const after = beginMacRead(scope());
    const alsoAfter = beginMacRead(scope());
    expect(acceptMacRead(alsoAfter, "bob")).toBe("accepted");
    expect(acceptMacRead(after, "bob")).toBe("accepted");
  });

  it("accepts the current reader's answers out of order", () => {
    acceptMacRead(beginMacRead(scope()), "alice");
    const fifth = beginMacRead(scope());
    const sixth = beginMacRead(scope());
    expect(acceptMacRead(sixth, "alice")).toBe("accepted");
    expect(acceptMacRead(fifth, "ALICE")).toBe("accepted");
  });

  it("orders every batch in a scope together, and scopes apart", () => {
    const list = beginMacRead(scope());
    const tooltip = beginMacRead(scope());
    expect(tooltip.sequence).toBe(list.sequence + 1);
    expect(beginMacRead(macReadScopeKey("named-host", "3.host.example.com")).sequence).toBe(1);
  });

  it("shows only answers cached as the scope's reader", () => {
    expect(showsMacReader(scope(), "alice")).toBe(false);
    acceptMacRead(beginMacRead(scope()), "alice");
    expect(showsMacReader(scope(), "Alice")).toBe(true);
    expect(showsMacReader(scope(), "bob")).toBe(false);
    expect(showsMacReader(scope(), undefined)).toBe(false);
  });

  it("re-renders subscribers when a scope's reader changes", () => {
    const { result } = renderHook(() => useMacReadScopeVersion());
    const before = result.current;
    act(() => {
      acceptMacRead(beginMacRead(scope()), "alice");
    });
    expect(result.current).toBeGreaterThan(before);
  });
});

describe("what Mac reads without a named reader rely on", () => {
  it("keys a previous user's scope when asked for it", () => {
    expect(macReadScopeKey("github", "GitHub.com", "user-0")).toEqual([
      "mac-read",
      "user-0",
      "github",
      "github.com",
    ]);
  });

  it("keeps a read current until its scope advances or changes reader", () => {
    const sent = beginMacRead(scope());
    expect(macReadCurrent(sent)).toBe(true);
    acceptMacRead(beginMacRead(scope()), "alice");
    expect(macReadCurrent(sent)).toBe(true);
    // A newer read as another reader: everything sent before it is out of date.
    acceptMacRead(beginMacRead(scope()), "bob");
    expect(macReadCurrent(sent)).toBe(false);
    const after = beginMacRead(scope());
    advanceMacReadScope(scope());
    expect(macReadCurrent(after)).toBe(false);
    expect(macReadCurrent(beginMacRead(scope()))).toBe(true);
  });

  it("tells listeners which scope advanced, until they unsubscribe, also across a reset", () => {
    const advanced: unknown[] = [];
    const stop = onMacReadScopeAdvanced((key) => advanced.push(key));
    advanceMacReadScope(scope());
    resetMacReadScopesForTests();
    advanceMacReadScope(macReadScopeKey("github", "github.com"));
    stop();
    advanceMacReadScope(scope());
    expect(advanced).toEqual([scope(), macReadScopeKey("github", "github.com")]);
  });

  it("tells listeners a page left a scope as `left`, refusing its reads as an advance does", () => {
    const moves: unknown[] = [];
    const stop = onMacReadScopeAdvanced((key, move) => moves.push([key, move]));
    const sent = beginMacRead(scope());
    expect(acceptMacRead(sent, "alice")).toBe("accepted");
    const late = beginMacRead(scope());
    advanceMacReadScope(scope(), "left");
    advanceMacReadScope(scope());
    stop();
    expect(moves).toEqual([
      [scope(), "left"],
      [scope(), "advanced"],
    ]);
    expect(acceptMacRead(late, "alice")).toBe("rejected");
    expect(acceptedMacReader(scope())).toBeNull();
  });
});

describe("a workspace user switch", () => {
  it("refuses a first read still in flight, even after switching back", () => {
    const sent = beginMacRead(scope());
    switchUser("user-2");
    switchUser("user-1");
    expect(acceptMacRead(sent, "alice")).toBe("rejected");
    expect(acceptMacRead(beginMacRead(scope()), "alice")).toBe("accepted");
  });

  it("advances every scope of the previous user, including ones only list stats read", () => {
    const advanced: unknown[] = [];
    const stop = onMacReadScopeAdvanced((key, move) => advanced.push([key, move]));
    const github = macReadScopeKey("github", "github.com");
    acceptMacRead(beginMacRead(github), "octo");
    beginMacRead(scope());
    switchUser("user-2");
    stop();
    expect(advanced).toEqual([
      [github, "advanced"],
      [macReadScopeKey("named-host", DEVICE_HOST, "user-1"), "advanced"],
    ]);
    // The new user's scopes start fresh.
    expect(acceptedMacReader(macReadScopeKey("github", "github.com"))).toBeNull();
  });
});

describe("a change of reader", () => {
  it("tells listeners the scope's reader changed, without refusing the read that changed it", () => {
    const moves: unknown[] = [];
    const stop = onMacReadScopeAdvanced((key, move) => moves.push([key, move]));
    acceptMacRead(beginMacRead(scope()), "alice");
    expect(acceptMacRead(beginMacRead(scope()), "bob")).toBe("transition");
    stop();
    expect(moves).toEqual([[scope(), "changed-reader"]]);
    expect(acceptedMacReader(scope())).toBe("bob");
  });
});

describe("refuseMacReadSentAt", () => {
  it("refuses the scope for a refusal code, and reads its account again from no one", () => {
    acceptMacRead(beginMacRead(scope()), "alice");
    const moves: string[] = [];
    onMacReadScopeAdvanced((_key, move) => moves.push(move));
    expect(refuseMacReadSentAt(scope(), beginMacRead(scope()), "account_invalid")).toBe(true);
    expect(moves).toEqual(["refused"]);
    expect(acceptedMacReader(scope())).toBeNull();
  });

  it("refuses nothing for a failure that isn't a refusal", () => {
    acceptMacRead(beginMacRead(scope()), "alice");
    for (const code of ["network", "rate_limited", "cancelled", "query_failed"]) {
      expect(refuseMacReadSentAt(scope(), beginMacRead(scope()), code)).toBe(false);
    }
    expect(acceptedMacReader(scope())).toBe("alice");
  });

  it("refuses nothing for a read sent before the scope moved or before a newer kept answer", () => {
    acceptMacRead(beginMacRead(scope()), "alice");
    const beforeMove = beginMacRead(scope());
    advanceMacReadScope(scope());
    expect(refuseMacReadSentAt(scope(), beforeMove, "signed_out")).toBe(false);
    const beforeAnswer = beginMacRead(scope());
    acceptMacRead(beginMacRead(scope()), "alice");
    expect(refuseMacReadSentAt(scope(), beforeAnswer, "signed_out")).toBe(false);
    expect(acceptedMacReader(scope())).toBe("alice");
  });

  it("still refuses a read in flight when a restore since confirmed the reader", () => {
    acceptMacRead(beginMacRead(scope()), "alice");
    const inFlight = beginMacRead(scope());
    // A saved answer checked against main's account: it proves no newer read.
    expect(acceptMacRead(beginMacRead(scope()), "alice", { restored: true })).toBe("accepted");
    expect(refuseMacReadSentAt(scope(), inFlight, "account_invalid")).toBe(true);
  });

  it("lets a restore name the first reader, but never switch an established one", () => {
    const inFlight = beginMacRead(scope());
    expect(acceptMacRead(beginMacRead(scope()), "alice", { restored: true })).toBe("accepted");
    expect(acceptedMacReader(scope())).toBe("alice");
    expect(acceptMacRead(beginMacRead(scope()), "bob", { restored: true })).toBe("rejected");
    expect(acceptedMacReader(scope())).toBe("alice");
    expect(refuseMacReadSentAt(scope(), inFlight, "signed_out")).toBe(true);
  });

  it("refuses nothing of another workspace user's scope, or under another scope's ticket", () => {
    acceptMacRead(beginMacRead(scope()), "alice");
    const previousUsers = beginMacRead(scope());
    const previousScope = scope();
    switchUser("user-2");
    expect(refuseMacReadSentAt(previousScope, previousUsers, "account_invalid")).toBe(false);
    const other = macReadScopeKey("github", "github.com");
    expect(refuseMacReadSentAt(scope(), beginMacRead(other), "account_invalid")).toBe(false);
  });
});
