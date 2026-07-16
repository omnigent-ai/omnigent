import { describe, it, expect } from "vitest";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import {
  type ConversationsInfiniteData,
  type SessionListWireItem,
  collectConversationIds,
  filtersFromConversationQueryKey,
  mergeItemsIntoPages,
  nullsToUndefined,
  removeIdsFromPages,
} from "./sessionListCache";

function conv(id: string, overrides: Partial<Conversation> = {}): Conversation {
  return {
    id,
    object: "conversation",
    title: id,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: null,
    status: "idle",
    ...overrides,
  };
}

function data(...pages: Conversation[][]): ConversationsInfiniteData {
  return {
    pages: pages.map(
      (rows): ConversationsPage => ({
        data: rows,
        first_id: rows[0]?.id ?? null,
        last_id: rows[rows.length - 1]?.id ?? null,
        has_more: false,
      }),
    ),
    pageParams: pages.map(() => undefined),
  };
}

const DEFAULT_FILTERS = { searchQuery: "", includeArchived: false };

// Most cases aren't on a chat route, so no row is the pinned active one.
const NO_ACTIVE = undefined;

describe("mergeItemsIntoPages", () => {
  it("overlays changed fields onto the matching row", () => {
    const before = data([conv("a", { status: "idle", title: "old" }), conv("b")]);
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", status: "running", title: "new" }],
    ]);

    const { data: after, found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS, NO_ACTIVE);

    // The matched row reflects the wire values; the proof the delta
    // traversed into the cache (a broken merge would leave "old"/"idle").
    expect(after!.pages[0].data[0]).toMatchObject({
      id: "a",
      status: "running",
      title: "new",
    });
    // Untouched row keeps its identity (no needless re-render churn).
    expect(after!.pages[0].data[1]).toBe(before.pages[0].data[1]);
    // `found` reports the id so the caller doesn't treat it as a new session.
    expect(found).toEqual(new Set(["a"]));
  });

  it("clears a previously-set field when the wire carries the cleared key", () => {
    const before = data([conv("a", { runner_id: "rnr_old" })]);
    // Frames arrive already run through nullsToUndefined, so a cleared field
    // is present with an `undefined` value (not absent) — that's what lets the
    // merge detect and apply the clear. The key being present is the point: a
    // key-absent overlay would leave the stale "rnr_old".
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", runner_id: undefined }]]);

    const { data: after, found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS, NO_ACTIVE);

    // runner_id went non-null → cleared: the cached value must be replaced,
    // landing as undefined (the list's absent-field shape), not the stale value.
    expect(after).not.toBe(before);
    expect(after!.pages[0].data[0].runner_id).toBeUndefined();
    expect(found).toEqual(new Set(["a"]));
  });

  it("leaves a cached search_snippet intact when the wire omits the key", () => {
    // search_snippet is search-only: the WS stream excludes it from its dump,
    // so a changed/snapshot frame never carries the key. A key-absent overlay
    // must NOT touch the snippet the search response put in the cache — this is
    // what stops the palette's match preview from flickering away on a tick.
    const before = data([conv("a", { search_snippet: "…setup.py test…" })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", status: "running" }]]);

    const { data: after } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS, NO_ACTIVE);

    // Snippet survives; only the field the frame carried is applied.
    expect(after!.pages[0].data[0].search_snippet).toBe("…setup.py test…");
    expect(after!.pages[0].data[0].status).toBe("running");
  });

  it("returns the same data reference when nothing actually changed", () => {
    const before = data([conv("a", { status: "running" })]);
    // Wire item restates the current values — an idempotent snapshot replay.
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", status: "running" }]]);

    const { data: after, found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS, NO_ACTIVE);

    // Same reference → React Query notify (and re-render) is skipped.
    // If this returned a new object, idle snapshots would churn the UI.
    expect(after).toBe(before);
    expect(found).toEqual(new Set(["a"]));
  });

  it("detects label changes structurally despite fresh object identity", () => {
    const before = data([conv("a", { labels: { x: "1" } })]);
    // A freshly-parsed frame always has a new labels object reference;
    // only a value difference should count as a change.
    const sameLabels = new Map<string, SessionListWireItem>([
      ["a", { id: "a", labels: { x: "1" } }],
    ]);
    const changedLabels = new Map<string, SessionListWireItem>([
      ["a", { id: "a", labels: { x: "2" } }],
    ]);

    // Equal labels by value → no change.
    expect(mergeItemsIntoPages(before, sameLabels, DEFAULT_FILTERS, NO_ACTIVE).data).toBe(before);
    // Different label value → row rewritten with the new labels.
    const { data: after } = mergeItemsIntoPages(before, changedLabels, DEFAULT_FILTERS, NO_ACTIVE);
    expect(after).not.toBe(before);
    expect(after!.pages[0].data[0].labels).toEqual({ x: "2" });
  });

  it("does not report ids absent from any page (structural additions)", () => {
    const before = data([conv("a")]);
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", title: "t" }],
      ["zzz", { id: "zzz", title: "new session" }],
    ]);

    const { found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS, NO_ACTIVE);

    // "zzz" isn't in the cache, so it's NOT found — the caller uses this
    // to trigger a refetch rather than guessing its sort position.
    expect(found.has("a")).toBe(true);
    expect(found.has("zzz")).toBe(false);
  });

  it("removes rows that no longer belong in the unarchived query", () => {
    const before = data([conv("a", { archived: false }), conv("b", { archived: false })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", archived: true }]]);

    const {
      data: after,
      found,
      needsRefetch,
    } = mergeItemsIntoPages(before, items, { includeArchived: false, searchQuery: "" }, NO_ACTIVE);

    // A pushed archive delta must not leave the row visible in the
    // default sidebar query while the server refetch is in flight.
    expect(after!.pages[0].data.map((row) => row.id)).toEqual(["b"]);
    expect(found).toEqual(new Set(["a"]));
    expect(needsRefetch).toBe(true);
  });

  it("does not refetch on a runner_online-only push delta", () => {
    // runner_online is no longer a list membership / sort dimension — the
    // sidebar fetches one undifferentiated session list, so a liveness
    // change is patched into the visible row without forcing a server
    // reconciliation.
    const before = data([conv("a", { runner_online: true }), conv("b", { runner_online: true })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", runner_online: false }]]);

    const { data: after, needsRefetch } = mergeItemsIntoPages(
      before,
      items,
      DEFAULT_FILTERS,
      NO_ACTIVE,
    );

    expect(after!.pages[0].data.map((row) => [row.id, row.runner_online])).toEqual([
      ["a", false],
      ["b", true],
    ]);
    expect(needsRefetch).toBe(false);
  });

  it("asks for a refetch when a searched row's title changes", () => {
    const before = data([conv("a", { title: "alpha" })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", title: "beta" }]]);

    const { data: after, needsRefetch } = mergeItemsIntoPages(
      before,
      items,
      { searchQuery: "alp", includeArchived: false },
      NO_ACTIVE,
    );

    // The local cache cannot know whether the server-side search still
    // matches via title or item content, so it patches the row then
    // reconciles with the filtered list endpoint.
    expect(after!.pages[0].data[0].title).toBe("beta");
    expect(needsRefetch).toBe(true);
  });

  it("asks for a refetch when updated_at changes the server sort key", () => {
    const before = data([conv("a", { updated_at: 1 })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", updated_at: 2 }]]);

    const { data: after, needsRefetch } = mergeItemsIntoPages(
      before,
      items,
      DEFAULT_FILTERS,
      NO_ACTIVE,
    );

    // The pushed delta updates the visible timestamp immediately, then
    // the refetch restores the server's descending updated_at order.
    expect(after!.pages[0].data[0].updated_at).toBe(2);
    expect(needsRefetch).toBe(true);
  });

  it("skips the refetch when only the active row's updated_at changed", () => {
    const before = data([conv("a", { updated_at: 1 }), conv("b", { updated_at: 1 })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", updated_at: 2 }]]);

    // "a" is the active chat — pinned in place by ActiveChatOverride.
    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS, "a");

    // The timestamp is still patched into the cache (the row stays current)...
    expect(after!.pages[0].data[0].updated_at).toBe(2);
    // ...but no refetch fires: re-sorting wouldn't move the pinned active row,
    // so the per-tick list poll the old code triggered for an actively-used
    // session is eliminated. A regression that dropped the active-row carve-out
    // would flip this back to true.
    expect(needsRefetch).toBe(false);
  });

  it("still refetches when a non-active row's updated_at changes", () => {
    const before = data([conv("a", { updated_at: 1 }), conv("b", { updated_at: 1 })]);
    // "b" changed, but "a" is the active/pinned row — "b" still needs resorting.
    const items = new Map<string, SessionListWireItem>([["b", { id: "b", updated_at: 2 }]]);

    const { needsRefetch } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS, "a");

    // The active-row carve-out must not suppress resorts for other rows, or a
    // bumped sibling would sit in the wrong sidebar position until the next poll.
    expect(needsRefetch).toBe(true);
  });
});

describe("nullsToUndefined", () => {
  it("converts null values to undefined while keeping the keys", () => {
    const result = nullsToUndefined({ id: "a", runner_id: null, title: "kept" });

    // Key stays present so the merge's diff still sees the field as cleared;
    // if the key were dropped, an absent overlay couldn't clear the stale value.
    expect("runner_id" in result).toBe(true);
    expect(result.runner_id).toBeUndefined();
    // Non-null values pass through untouched.
    expect(result.title).toBe("kept");
  });

  it("converts a null permission_level to undefined (sidebar god-mode guard)", () => {
    // Sidebar treats permission_level === null as full access. A streamed null
    // must become undefined so a stream frame can never flip a row to owner.
    const result = nullsToUndefined({ id: "a", permission_level: null });

    expect(result.permission_level).toBeUndefined();
    expect(result.permission_level === null).toBe(false);
  });
});

describe("mergeItemsIntoPages project-filtered membership", () => {
  const alpha = filtersFromConversationQueryKey(["conversations", "", true, "Alpha"]);
  const unfiltered = filtersFromConversationQueryKey(["conversations", "", true]);

  it("evicts a row relabeled OUT of the selected project and flags a refetch", () => {
    const before = data([
      conv("a", { archived: true, labels: { omni_project: "Alpha" } }),
      conv("b", { archived: true, labels: { omni_project: "Alpha" } }),
    ]);
    // A push-delta moves `a` from Alpha to Beta (full row, new labels).
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", archived: true, labels: { omni_project: "Beta" } }],
    ]);

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, alpha, NO_ACTIVE);

    // `a` no longer belongs in the Alpha cache; `b` (still Alpha) stays.
    expect(after!.pages[0].data.map((c) => c.id)).toEqual(["b"]);
    expect(needsRefetch).toBe(true);
  });

  it("flags a refetch when a row is relabeled INTO a project so filtered variants reconcile", () => {
    // The row lives in the unfiltered archived variant; a session moved into
    // "Alpha" isn't in the Alpha-filtered cache yet, so only a server reconcile
    // can place it. The label change here must trigger the prefix-wide refetch.
    const before = data([conv("a", { archived: true, labels: {} })]);
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", archived: true, labels: { omni_project: "Alpha" } }],
    ]);

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, unfiltered, NO_ACTIVE);

    expect(after!.pages[0].data.map((c) => c.id)).toEqual(["a"]);
    expect(needsRefetch).toBe(true);
  });

  it("keeps a row whose project still matches when a non-label field changes", () => {
    const before = data([
      conv("a", { archived: true, status: "idle", labels: { omni_project: "Alpha" } }),
    ]);
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", archived: true, status: "running", labels: { omni_project: "Alpha" } }],
    ]);

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, alpha, NO_ACTIVE);

    // Membership holds, so the row is patched in place (not evicted), and a
    // status-only change needs no server reconcile.
    expect(after!.pages[0].data.map((c) => c.id)).toEqual(["a"]);
    expect(after!.pages[0].data[0].status).toBe("running");
    expect(needsRefetch).toBe(false);
  });

  it("treats an empty-string project as 'all projects' (no membership constraint)", () => {
    // The contract: a falsy project is "all projects", not a distinct "unfiled"
    // slice (this list never requests unfiled). So gaining a label does NOT
    // evict the row — matching the request, which omits `project=` for "".
    const allProjects = filtersFromConversationQueryKey(["conversations", "", true, ""]);
    const before = data([conv("a", { archived: true, labels: {} })]);
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", archived: true, labels: { omni_project: "Alpha" } }],
    ]);

    const { data: after } = mergeItemsIntoPages(before, items, allProjects, NO_ACTIVE);

    expect(after!.pages[0].data.map((c) => c.id)).toEqual(["a"]);
  });
});

describe("filtersFromConversationQueryKey", () => {
  it("parses current conversation query keys", () => {
    expect(filtersFromConversationQueryKey(["conversations", "needle", true])).toEqual({
      searchQuery: "needle",
      includeArchived: true,
    });
  });

  it("parses the project-filtered four-element key", () => {
    // The Archived picker appends `project`; the parser must accept it so the
    // rename overlay / push-delta merge don't throw when this variant is cached.
    expect(filtersFromConversationQueryKey(["conversations", "", true, "Design"])).toEqual({
      searchQuery: "",
      includeArchived: true,
      project: "Design",
    });
  });

  it("rejects non-canonical conversation query keys", () => {
    expect(() => filtersFromConversationQueryKey(["conversations", ""])).toThrow(
      "Invalid conversations query key",
    );
    // A non-string project element is malformed and must fail loudly.
    expect(() => filtersFromConversationQueryKey(["conversations", "", true, 5])).toThrow(
      "Invalid conversations query key",
    );
  });
});

describe("removeIdsFromPages", () => {
  it("drops matching rows and reports the removal", () => {
    const before = data([conv("a"), conv("b")], [conv("c")]);

    const { data: after, removed } = removeIdsFromPages(before, new Set(["b", "c"]));

    expect(removed).toBe(true);
    expect(after!.pages[0].data.map((r) => r.id)).toEqual(["a"]);
    expect(after!.pages[1].data).toEqual([]);
  });

  it("returns the same data reference when no id matched", () => {
    const before = data([conv("a")]);
    const { data: after, removed } = removeIdsFromPages(before, new Set(["missing"]));
    // No-op → identity preserved, no re-render.
    expect(after).toBe(before);
    expect(removed).toBe(false);
  });

  it("recomputes page cursors when boundary rows are removed", () => {
    const before = data([conv("a"), conv("b"), conv("c")]);

    const { data: after } = removeIdsFromPages(before, new Set(["a", "c"]));

    // last_id is the `after=` anchor fetchNextPage sends; left at the
    // deleted id, the server's keyset lookup misses and the next page
    // comes back empty. first_id must track the same way.
    expect(after!.pages[0].first_id).toBe("b");
    expect(after!.pages[0].last_id).toBe("b");
  });

  it("nulls the cursors of an emptied page", () => {
    const before = data([conv("a")]);

    const { data: after } = removeIdsFromPages(before, new Set(["a"]));

    // Null (not the deleted id): getNextPageParam then stops paginating
    // until the next reconcile refetch, instead of anchoring on a row
    // the server can no longer resolve.
    expect(after!.pages[0].data).toEqual([]);
    expect(after!.pages[0].first_id).toBeNull();
    expect(after!.pages[0].last_id).toBeNull();
  });
});

describe("collectConversationIds", () => {
  it("unions ids across query variants and dedupes", () => {
    const base = data([conv("a"), conv("b")]);
    const connected = data([conv("b"), conv("c")]);
    const ids = collectConversationIds([base, undefined, connected]);
    // Dedupe across the base + connected variants; `undefined` (unfetched
    // query) contributes nothing rather than throwing.
    expect(new Set(ids)).toEqual(new Set(["a", "b", "c"]));
    expect(ids.length).toBe(3);
  });
});
