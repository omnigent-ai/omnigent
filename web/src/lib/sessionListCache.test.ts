import { describe, it, expect } from "vitest";
import { QueryClient } from "@tanstack/react-query";
import type { Conversation, ConversationsPage } from "@/hooks/useConversations";
import { sortConversations } from "@/shell/sidebarNav";
import { mergeScopeRows } from "./sidebarData";
import {
  type ConversationsInfiniteData,
  type SessionListWireItem,
  collectConversationIds,
  filtersFromConversationQueryKey,
  firstPageOnly,
  insertNewRowsIntoPages,
  mergeItemsIntoPages,
  nullsToUndefined,
  overlayArchivedIntoCaches,
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
    pages: pages.map((rows): ConversationsPage => ({
      data: rows,
      first_id: rows[0]?.id ?? null,
      last_id: rows[rows.length - 1]?.id ?? null,
      has_more: false,
    })),
    pageParams: pages.map(() => undefined),
  };
}

const DEFAULT_FILTERS = { searchQuery: "", includeArchived: false };

describe("mergeItemsIntoPages", () => {
  it("overlays changed fields onto the matching row", () => {
    const before = data([conv("a", { status: "idle", title: "old" }), conv("b")]);
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", status: "running", title: "new" }],
    ]);

    const { data: after, found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS);

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

    const { data: after, found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS);

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

    const { data: after } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS);

    // Snippet survives; only the field the frame carried is applied.
    expect(after!.pages[0].data[0].search_snippet).toBe("…setup.py test…");
    expect(after!.pages[0].data[0].status).toBe("running");
  });

  it("returns the same data reference when nothing actually changed", () => {
    const before = data([conv("a", { status: "running" })]);
    // Wire item restates the current values — an idempotent snapshot replay.
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", status: "running" }]]);

    const { data: after, found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS);

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
    expect(mergeItemsIntoPages(before, sameLabels, DEFAULT_FILTERS).data).toBe(before);
    // Different label value → row rewritten with the new labels.
    const { data: after } = mergeItemsIntoPages(before, changedLabels, DEFAULT_FILTERS);
    expect(after).not.toBe(before);
    expect(after!.pages[0].data[0].labels).toEqual({ x: "2" });
  });

  it("does not report ids absent from any page (structural additions)", () => {
    const before = data([conv("a")]);
    const items = new Map<string, SessionListWireItem>([
      ["a", { id: "a", title: "t" }],
      ["zzz", { id: "zzz", title: "new session" }],
    ]);

    const { found } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS);

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
    } = mergeItemsIntoPages(before, items, { includeArchived: false, searchQuery: "" });

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

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, DEFAULT_FILTERS);

    expect(after!.pages[0].data.map((row) => [row.id, row.runner_online])).toEqual([
      ["a", false],
      ["b", true],
    ]);
    expect(needsRefetch).toBe(false);
  });

  it("asks for a refetch when a searched row's title changes", () => {
    const before = data([conv("a", { title: "alpha" })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", title: "beta" }]]);

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, {
      searchQuery: "alp",
      includeArchived: false,
    });

    // The local cache cannot know whether the server-side search still
    // matches via title or item content, so it patches the row then
    // reconciles with the filtered list endpoint.
    expect(after!.pages[0].data[0].title).toBe("beta");
    expect(needsRefetch).toBe(true);
  });

  it("re-sorts repeated updated_at bumps locally without asking for a refetch", () => {
    const before = data(
      [conv("a", { updated_at: 3 }), conv("b", { updated_at: 2 })],
      [conv("c", { updated_at: 1 })],
    );
    let current: ConversationsInfiniteData | undefined = before;
    for (const [id, updatedAt] of [
      ["c", 4],
      ["b", 5],
      ["c", 6],
    ] as const) {
      const result = mergeItemsIntoPages(
        current,
        new Map([[id, { id, updated_at: updatedAt }]]),
        DEFAULT_FILTERS,
      );
      // A bump only raises a row, so the loaded pages stay a prefix of the
      // server's order and no reconcile is needed.
      expect(result.needsRefetch).toBe(false);
      current = result.data;
    }
    // Rows stay on their pages (sorted within them); the sidebar orders the
    // whole list by updated_at at render time.
    expect(current!.pages.map((page) => page.data.map((row) => row.id))).toEqual([
      ["b", "a"],
      ["c"],
    ]);
    expect(
      sortConversations(
        current!.pages.flatMap((page) => page.data),
        null,
      ).map((row) => row.id),
    ).toEqual(["c", "b", "a"]);
  });

  it("moves a legacy cursor off a bumped anchor so the window boundary stays valid", () => {
    const before = data([
      conv("a", { updated_at: 30 }),
      conv("b", { updated_at: 20 }),
      conv("c", { updated_at: 10 }),
    ]);
    before.pages[0].has_more = true;

    const { data: after } = mergeItemsIntoPages(
      before,
      new Map([["c", { id: "c", updated_at: 40 }]]),
      DEFAULT_FILTERS,
    );

    const page = after!.pages[0];
    expect(page.data.map((row) => row.id)).toEqual(["c", "a", "b"]);
    // The next page continues after "b"; "after=c" would replay a and b.
    expect(page.last_id).toBe("b");
    // The All view's watermark is the true oldest row, not the bumped one.
    const merged = mergeScopeRows(page.data, [], true, false, page.last_id);
    expect(merged.rows.map((row) => row.id)).toEqual(["c", "a", "b"]);
  });

  it("leaves a creation-ordered page and its cursor alone when updated_at moves", () => {
    const before = data([
      conv("a", { created_at: 30, updated_at: 30 }),
      conv("b", { created_at: 20, updated_at: 20 }),
      conv("c", { created_at: 10, updated_at: 10 }),
    ]);
    before.pages[0].has_more = true;

    const { data: after } = mergeItemsIntoPages(
      before,
      new Map([["c", { id: "c", updated_at: 40 }]]),
      { ...DEFAULT_FILTERS, sortBy: "created_at" },
    );

    expect(after!.pages[0].data.map((row) => row.id)).toEqual(["a", "b", "c"]);
    expect(after!.pages[0].last_id).toBe("c");
  });

  it("keeps an opaque cursor when its page reorders", () => {
    const before = data([conv("a", { updated_at: 30 }), conv("b", { updated_at: 20 })]);
    before.pages[0] = { ...before.pages[0], has_more: true, last_id: "opaque-token" };

    const { data: after } = mergeItemsIntoPages(
      before,
      new Map([["b", { id: "b", updated_at: 40 }]]),
      DEFAULT_FILTERS,
    );

    expect(after!.pages[0].last_id).toBe("opaque-token");
  });

  it("asks for a refetch when a first-class project move changes project_id", () => {
    const before = data([conv("a", { project_id: "p1" })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", project_id: "p2" }]]);

    expect(mergeItemsIntoPages(before, items, DEFAULT_FILTERS).needsRefetch).toBe(true);
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

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, alpha);

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

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, unfiltered);

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

    const { data: after, needsRefetch } = mergeItemsIntoPages(before, items, alpha);

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

    const { data: after } = mergeItemsIntoPages(before, items, allProjects);

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

  it("accepts creation-order caches for live updates and optimistic mutations", () => {
    expect(
      filtersFromConversationQueryKey(["conversations", "", false, null, "mine", "created_at"]),
    ).toEqual({
      searchQuery: "",
      includeArchived: false,
      project: undefined,
      visibility: "mine",
      sortBy: "created_at",
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

  it("repairs a legacy row-ID cursor when its anchor is removed", () => {
    const before = data([conv("a"), conv("b"), conv("c")]);

    const { data: after } = removeIdsFromPages(before, new Set(["a", "c"]));

    // last_id is the `after=` anchor fetchNextPage sends; left at the
    // deleted id, the server's keyset lookup misses and the next page
    // comes back empty. first_id must track the same way.
    expect(after!.pages[0].first_id).toBe("b");
    expect(after!.pages[0].last_id).toBe("b");
  });

  it("nulls a deleted legacy row-ID cursor on an emptied page", () => {
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

describe("opaque cursors after cached filtering", () => {
  it.each([true, false])("preserves continuation metadata with has_more=%s", (hasMore) => {
    const before = data([conv("a"), conv("b")]);
    const cursor = "eyJvZmZzZXQiOjYwfQ==/+opaque";
    before.pages[0].last_id = cursor;
    before.pages[0].has_more = hasMore;
    const partial = removeIdsFromPages(before, new Set(["b"])).data!;
    expect(partial.pages[0]).toMatchObject({ last_id: cursor, has_more: hasMore });
    expect(partial.pages[0].data.map((r) => r.id)).toEqual(["a"]);
    const empty = removeIdsFromPages(partial, new Set(["a"])).data!;
    expect(empty.pages[0]).toMatchObject({ data: [], last_id: cursor, has_more: hasMore });
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

describe("overlayArchivedIntoCaches", () => {
  it("flips the flag in an include-archived list and drops the row from a folder", () => {
    const qc = new QueryClient();
    // Sidebar/archived-view cache keeps archived rows (client filters them).
    qc.setQueryData(["conversations", "", true], data([conv("a"), conv("b")]));
    // Project folder is non-archived — an archived row no longer belongs.
    qc.setQueryData(["project-sessions", "proj"], data([conv("a")]));

    overlayArchivedIntoCaches(qc, "a", true);

    const list = qc.getQueryData<ConversationsInfiniteData>(["conversations", "", true])!;
    expect(list.pages[0].data.find((c) => c.id === "a")!.archived).toBe(true);

    const folder = qc.getQueryData<ConversationsInfiniteData>(["project-sessions", "proj"])!;
    expect(folder.pages[0].data.map((c) => c.id)).toEqual([]);
  });
});

describe("insertNewRowsIntoPages", () => {
  const candidate = (id: string, extra: Partial<Conversation> = {}) =>
    new Map<string, SessionListWireItem>([[id, { id, updated_at: 100, ...extra }]]);

  it("prepends a brand-new row to the top of page 0 and reports it inserted", () => {
    const before = data([conv("a"), conv("b")]);
    const { data: after, inserted } = insertNewRowsIntoPages(
      before,
      candidate("new"),
      DEFAULT_FILTERS,
    );
    expect(after!.pages[0].data.map((c) => c.id)).toEqual(["new", "a", "b"]);
    expect(after!.pages[0].first_id).toBe("new");
    expect(inserted.map((c) => c.id)).toEqual(["new"]);
  });

  it("skips a row already present (idempotent)", () => {
    const before = data([conv("new"), conv("a")]);
    const { data: after, inserted } = insertNewRowsIntoPages(
      before,
      candidate("new"),
      DEFAULT_FILTERS,
    );
    expect(after).toBe(before);
    expect(inserted).toHaveLength(0);
  });

  it("skips search-filtered lists (membership unknown)", () => {
    const before = data([conv("a")]);
    const { inserted } = insertNewRowsIntoPages(before, candidate("new"), {
      searchQuery: "hi",
      includeArchived: false,
    });
    expect(inserted).toHaveLength(0);
  });

  it("skips an archived row in a non-archived list", () => {
    const before = data([conv("a")]);
    const { inserted } = insertNewRowsIntoPages(before, candidate("new", { archived: true }), {
      searchQuery: "",
      includeArchived: false,
    });
    expect(inserted).toHaveLength(0);
  });

  it("never inserts a sub-agent/child session (parent_session_id set)", () => {
    const before = data([conv("a")]);
    const { inserted } = insertNewRowsIntoPages(
      before,
      candidate("child", { parent_session_id: "parent" }),
      DEFAULT_FILTERS,
    );
    expect(inserted).toHaveLength(0);
  });

  it("skips ids the caller excludes (e.g. a session being deleted)", () => {
    const before = data([conv("a")]);
    const { inserted } = insertNewRowsIntoPages(
      before,
      candidate("gone"),
      DEFAULT_FILTERS,
      (id) => id === "gone",
    );
    expect(inserted).toHaveLength(0);
  });

  it("leaves a row older than the loaded window for its unloaded page", () => {
    const before = data([conv("a", { updated_at: 30 }), conv("b", { updated_at: 20 })]);
    before.pages[0].has_more = true;

    const older = insertNewRowsIntoPages(
      before,
      candidate("old", { updated_at: 10 }),
      DEFAULT_FILTERS,
    );
    expect(older.inserted).toHaveLength(0);
    expect(older.data).toBe(before);

    const newer = insertNewRowsIntoPages(
      before,
      candidate("new", { updated_at: 25 }),
      DEFAULT_FILTERS,
    );
    expect(newer.inserted.map((c) => c.id)).toEqual(["new"]);
  });

  it("places rows by creation time in a creation-ordered list", () => {
    const createdOrder = { ...DEFAULT_FILTERS, sortBy: "created_at" as const };
    const before = data([
      conv("a", { created_at: 30, updated_at: 30 }),
      conv("b", { created_at: 20, updated_at: 20 }),
    ]);
    before.pages[0].has_more = true;
    const rows = new Map<string, SessionListWireItem>([
      // Recently active, but created before the loaded window.
      ["busy-old", { id: "busy-old", created_at: 10, updated_at: 99 }],
      ["new", { id: "new", created_at: 40, updated_at: 40 }],
    ]);

    const { inserted } = insertNewRowsIntoPages(before, rows, createdOrder);

    expect(inserted.map((c) => c.id)).toEqual(["new"]);
  });

  it("breaks updated_at ties at the window boundary by id, like the server", () => {
    const before = data([conv("m", { updated_at: 30 }), conv("m2", { updated_at: 20 })]);
    before.pages[0].has_more = true;

    // Same second as the oldest row: "z…" sorts before "m2", "a…" after it.
    const rows = new Map<string, SessionListWireItem>([
      ["zz", { id: "zz", updated_at: 20 }],
      ["aa", { id: "aa", updated_at: 20 }],
    ]);
    const { inserted } = insertNewRowsIntoPages(before, rows, DEFAULT_FILTERS);

    expect(inserted.map((c) => c.id)).toEqual(["zz"]);
  });

  it("places an older row when the list is fully loaded", () => {
    const before = data([conv("a", { updated_at: 30 })]);
    const { inserted } = insertNewRowsIntoPages(
      before,
      candidate("old", { updated_at: 10 }),
      DEFAULT_FILTERS,
    );
    expect(inserted.map((c) => c.id)).toEqual(["old"]);
  });

  it("decides project-folder membership from project_id or the legacy label", () => {
    const folder = {
      searchQuery: "",
      includeArchived: false,
      visibility: "mine" as const,
      folder: { id: "p1", name: "Alpha" },
    };
    const before = data([conv("a", { project_id: "p1", updated_at: 5 })]);
    const rows = new Map<string, SessionListWireItem>([
      ["byId", { id: "byId", project_id: "p1", updated_at: 9 }],
      ["byLabel", { id: "byLabel", labels: { omni_project: "Alpha" }, updated_at: 8 }],
      ["elsewhere", { id: "elsewhere", project_id: "p2", updated_at: 7 }],
      ["unfiled", { id: "unfiled", updated_at: 6 }],
      ["foreign", { id: "foreign", project_id: "p1", owner: "someone-else", updated_at: 9 }],
    ]);

    const { inserted } = insertNewRowsIntoPages(before, rows, folder, undefined, "viewer");

    expect(inserted.map((c) => c.id)).toEqual(["byId", "byLabel"]);
  });
});

describe("mergeItemsIntoPages project-folder membership", () => {
  it("evicts a row moved out of the folder", () => {
    const folder = {
      searchQuery: "",
      includeArchived: false,
      visibility: "mine" as const,
      folder: { id: "p1", name: "Alpha" },
    };
    const before = data([conv("a", { project_id: "p1" }), conv("b", { project_id: "p1" })]);
    const items = new Map<string, SessionListWireItem>([["a", { id: "a", project_id: "p2" }]]);

    const { data: after } = mergeItemsIntoPages(before, items, folder);

    expect(after!.pages[0].data.map((c) => c.id)).toEqual(["b"]);
  });
});

describe("firstPageOnly", () => {
  it("drops every page after the first so a refetch costs one request", () => {
    const before = data([conv("a")], [conv("b")], [conv("c")]);
    before.pageParams = [undefined, "a", "b"];

    const after = firstPageOnly(before);

    expect(after!.pages.map((page) => page.data.map((c) => c.id))).toEqual([["a"]]);
    expect(after!.pageParams).toEqual([undefined]);
    const single = data([conv("a")]);
    expect(firstPageOnly(single)).toBe(single);
  });
});
