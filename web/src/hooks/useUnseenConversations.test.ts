import { act, cleanup, renderHook } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";

// `authenticatedFetch` is mocked so we can assert the read-state PUT
// round-trips without a server (the read path is the conversation list, fed
// directly via seedReadState). Declared via vi.hoisted so the (hoisted)
// vi.mock factory can reference it.
const { authFetch } = vi.hoisted(() => ({ authFetch: vi.fn() }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authFetch }));

type Mod = typeof import("./useUnseenConversations");

/**
 * The module keeps its read-state mirror in module-level singletons
 * (lastSeenMap / explicitlyUnread / seeded / hydrated), so each test
 * re-imports a fresh copy to reset that state. PUTs resolve 204.
 */
async function loadFresh(): Promise<Mod> {
  vi.resetModules();
  authFetch.mockReset();
  authFetch.mockResolvedValue({ ok: true, status: 204, json: async () => ({}) });
  return import("./useUnseenConversations");
}

/** The body of the most recent PUT, or undefined if none. */
function lastPutBody(): { last_seen: number; unread: boolean } | undefined {
  for (let i = authFetch.mock.calls.length - 1; i >= 0; i--) {
    const [, init] = authFetch.mock.calls[i] as [string, { method?: string; body?: string }];
    if (init?.method === "PUT" && init.body) return JSON.parse(init.body);
  }
  return undefined;
}

function putCount(): number {
  return authFetch.mock.calls.filter(([, i]) => (i as { method?: string })?.method === "PUT")
    .length;
}

function setWindowFocused(focused: boolean): void {
  vi.spyOn(document, "hasFocus").mockReturnValue(focused);
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.restoreAllMocks();
});

describe("seedReadState", () => {
  it("seeds baselines and unread flags from the conversation list", async () => {
    const mod = await loadFresh();
    mod.seedReadState([
      { id: "conv-1", viewer_last_seen: 1_000 },
      { id: "conv-2", viewer_unread: true },
    ]);

    expect(mod.isConversationUnseen("conv-1", 2_000, "idle")).toBe(true); // 2000 > 1000
    expect(mod.isConversationUnseen("conv-1", 500, "idle")).toBe(false); // 500 < 1000
    expect(mod.isExplicitlyUnread("conv-2")).toBe(true);
  });

  it("seeds a session only once — a later list value can't clobber a local write", async () => {
    const mod = await loadFresh();
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 1_000 }]);
    // User marks it unread locally (optimistic).
    mod.markConversationUnread("conv-1", 5_000);
    expect(mod.isExplicitlyUnread("conv-1")).toBe(true);

    // A stale poll arrives still showing it as seen — must be ignored.
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 1_000, viewer_unread: false }]);
    expect(mod.isExplicitlyUnread("conv-1")).toBe(true);
  });

  it("keeps the mark-seen gate closed while the list is still loading (undefined)", async () => {
    // useSeedReadState(undefined) — the query hasn't loaded — must NOT flip
    // `hydrated`, or an automatic mark-seen on a deep-link/reload would write
    // a 'seen' baseline before the server's viewer_* arrives (clobbering a
    // cross-device unread). Only a loaded list (even empty []) releases it.
    const mod = await loadFresh();
    vi.useFakeTimers({ now: 5_000_000 });
    const { rerender } = renderHook(
      ({ c }: { c: readonly { id: string }[] | undefined }) => mod.useSeedReadState(c),
      { initialProps: { c: undefined as readonly { id: string }[] | undefined } },
    );

    // Loading: gate closed — mark-seen is a no-op (no baseline written).
    mod.markConversationSeen("conv-1");
    expect(mod.isConversationUnseen("conv-1", 6_000, "idle")).toBe(false);

    // Loaded (empty) list arrives: gate releases, mark-seen now writes.
    rerender({ c: [] });
    mod.markConversationSeen("conv-1");
    expect(mod.isConversationUnseen("conv-1", 6_000, "idle")).toBe(true); // 6000 > 5000 baseline
  });
});

describe("isConversationUnseen", () => {
  it("returns false with no baseline, when running, or when status is undefined", async () => {
    const mod = await loadFresh();
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 1_000 }]);
    expect(mod.isConversationUnseen("missing", 2_000, "idle")).toBe(false);
    expect(mod.isConversationUnseen("conv-1", 2_000, "running")).toBe(false);
    expect(mod.isConversationUnseen("conv-1", 2_000, undefined)).toBe(false);
  });

  it("returns true when finished and updated_at exceeds the baseline", async () => {
    const mod = await loadFresh();
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 1_000 }]);
    expect(mod.isConversationUnseen("conv-1", 2_000, "idle")).toBe(true);
    expect(mod.isConversationUnseen("conv-1", 2_000, "failed")).toBe(true);
    expect(mod.isConversationUnseen("conv-1", 1_000, "idle")).toBe(false); // equal, not greater
  });
});

describe("markConversationSeen", () => {
  it("does nothing before the first seed (reload-clobber guard)", async () => {
    const mod = await loadFresh();
    vi.useFakeTimers({ now: 5_000_000 });
    mod.markConversationSeen("conv-1");
    // No PUT, and no baseline recorded (can't clobber a server unread the
    // list is about to seed).
    expect(putCount()).toBe(0);
    expect(mod.isConversationUnseen("conv-1", 6_000, "idle")).toBe(false);
  });

  it("records the baseline and PUTs it after the first seed", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]); // flips hydrated, even for an empty list
    vi.useFakeTimers({ now: 5_000_000 });

    mod.markConversationSeen("conv-1");

    expect(mod.isConversationUnseen("conv-1", 4_000, "idle")).toBe(false); // 4000 < 5000 baseline
    expect(lastPutBody()).toEqual({ last_seen: 5_000, unread: false });
  });

  it("is a no-op for an explicitly-unread conversation", async () => {
    const mod = await loadFresh();
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 4_999, viewer_unread: true }]);
    authFetch.mockClear();

    mod.markConversationSeen("conv-1", 6_000);

    expect(authFetch).not.toHaveBeenCalled(); // guarded: no write, no PUT
    expect(mod.isConversationUnseen("conv-1", 5_000, "idle")).toBe(true);
  });
});

describe("markConversationUnread", () => {
  it("pins the baseline just below updated_at and flags + PUTs unread", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]);
    authFetch.mockClear();

    mod.markConversationUnread("conv-1", 5_000);

    expect(mod.isConversationUnseen("conv-1", 5_000, "idle")).toBe(true);
    expect(mod.isExplicitlyUnread("conv-1")).toBe(true);
    expect(lastPutBody()).toEqual({ last_seen: 4_999, unread: true });
  });

  it("still defers to status — a running session shows no dot", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]);
    mod.markConversationUnread("conv-1", 5_000);
    expect(mod.isConversationUnseen("conv-1", 5_000, "running")).toBe(false);
  });

  it("survives an automatic mark-seen (the active-thread clobber guard)", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]);
    mod.markConversationUnread("conv-1", 5_000);
    mod.markConversationSeen("conv-1", 6_000); // suppressed by the override
    expect(mod.isConversationUnseen("conv-1", 5_000, "idle")).toBe(true);
  });
});

describe("clearUnreadOverride", () => {
  it("removes the override, PUTs the cleared state, and re-enables mark-seen", async () => {
    const mod = await loadFresh();
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 4_999, viewer_unread: true }]);
    authFetch.mockClear();

    mod.clearUnreadOverride("conv-1");

    expect(mod.isExplicitlyUnread("conv-1")).toBe(false);
    expect(lastPutBody()).toEqual({ last_seen: 4_999, unread: false });
    mod.markConversationSeen("conv-1", 5_000); // now takes hold
    expect(mod.isConversationUnseen("conv-1", 5_000, "idle")).toBe(false);
  });
});

describe("isExplicitlyUnread", () => {
  it("tracks the explicit-unread override and clears on reopen", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]);
    expect(mod.isExplicitlyUnread("conv-1")).toBe(false);
    mod.markConversationUnread("conv-1", 5_000);
    expect(mod.isExplicitlyUnread("conv-1")).toBe(true);
    mod.clearUnreadOverride("conv-1");
    expect(mod.isExplicitlyUnread("conv-1")).toBe(false);
  });
});

describe("useUnseenTick", () => {
  it("re-renders subscribers when the mirror is written", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]);
    const { result } = renderHook(() => mod.useUnseenTick());
    const before = result.current;
    act(() => mod.markConversationUnread("conv-1", 5_000));
    expect(result.current).not.toBe(before);
  });
});

describe("useMarkConversationSeen", () => {
  it("marks the active thread seen on mount when focused (after seed)", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]);
    setWindowFocused(true);
    vi.useFakeTimers({ now: 5_000_000 });

    renderHook(() => mod.useMarkConversationSeen("conv-1", 4_000));

    expect(mod.isConversationUnseen("conv-1", 4_000, "idle")).toBe(false);
    expect(lastPutBody()).toEqual({ last_seen: 5_000, unread: false });
  });

  it("does NOT mark seen while the window is blurred", async () => {
    const mod = await loadFresh();
    mod.seedReadState([]);
    setWindowFocused(false);
    vi.useFakeTimers({ now: 5_000_000 });

    renderHook(() => mod.useMarkConversationSeen("conv-1", 4_000));

    expect(mod.isConversationUnseen("conv-1", 6_000, "idle")).toBe(false); // no baseline written
  });

  it("preserves a seeded explicit-unread on reload (first mount does not clear)", async () => {
    // Reload landing back on /c/conv-1: the list seeds the server's unread,
    // and the first mount must neither clear the override nor mark seen.
    const mod = await loadFresh();
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 4_999, viewer_unread: true }]);
    setWindowFocused(true);

    renderHook(() => mod.useMarkConversationSeen("conv-1", 5_000));

    expect(mod.isExplicitlyUnread("conv-1")).toBe(true);
    expect(mod.isConversationUnseen("conv-1", 5_000, "idle")).toBe(true);
  });

  it("clears the override on a genuine in-app reopen (id changes while mounted)", async () => {
    const mod = await loadFresh();
    mod.seedReadState([{ id: "conv-1", viewer_last_seen: 4_999, viewer_unread: true }]);
    setWindowFocused(true);
    vi.useFakeTimers({ now: 9_000_000 });

    const { rerender } = renderHook(({ id }) => mod.useMarkConversationSeen(id, 5_000), {
      initialProps: { id: "conv-1" as string },
    });
    expect(mod.isConversationUnseen("conv-1", 5_000, "idle")).toBe(true); // still unread after mount

    rerender({ id: "conv-2" }); // navigate away
    rerender({ id: "conv-1" }); // reopen → override cleared, marked seen

    expect(mod.isExplicitlyUnread("conv-1")).toBe(false);
    expect(mod.isConversationUnseen("conv-1", 5_000, "idle")).toBe(false);
  });
});
