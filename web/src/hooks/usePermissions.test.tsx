import { beforeEach, describe, expect, it, vi } from "vitest";
import { useConversations as useTestConversations } from "@/hooks/useConversations";

vi.mock("@/hooks/useSidebarData", () => ({ useLoadedConversations: () => useTestConversations() }));
// Tests for the `useCanEdit` edit-gate hook.
//
// `useCanEdit` is what every editable surface (CodeViewer,
// MarkdownRichTextViewer, the comments panel via ChatPage) reads to
// decide whether a collaborator may mutate. It wires the session
// snapshot and the sidebar row into `derivePermissionLevel` (whose
// resolution order is covered in permissionsApi.test.ts) and applies
// the edit boundary: level >= 2, with null treated as permissive.
//
// These tests mock the two source hooks so we exercise the boundary
// and the loading-is-permissive invariant directly, without standing
// up a QueryClient or re-testing the resolution order.

import { act, renderHook } from "@testing-library/react";
import type { Conversation, PinnedConversationsResult } from "./useConversations";
import type { Session } from "@/lib/types";
import type * as PermissionsApi from "@/lib/permissionsApi";
import { PUBLIC_USER_ID } from "@/lib/permissionsApi";
import { PINNED_CONVERSATIONS_KEY } from "@/lib/sessionListCache";

vi.mock("./useConversations", () => ({ useConversations: vi.fn() }));
vi.mock("./useSession", () => ({ useSession: vi.fn() }));
vi.mock("@/lib/permissionsApi", async (importOriginal) => ({
  ...(await importOriginal<typeof PermissionsApi>()),
  grantPermission: vi.fn(async () => ({})),
  revokePermission: vi.fn(async () => undefined),
}));

import type { ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useConversations } from "./useConversations";
import { useSession } from "./useSession";
import { useCanEdit, useGrantPermission, useRevokePermission } from "./usePermissions";

const useConversationsMock = vi.mocked(useConversations);
const useSessionMock = vi.mocked(useSession);

function makeSession(permissionLevel: number | null): Session {
  return {
    id: "conv_1",
    agentId: "ag_1",
    agentName: null,
    runnerId: null,
    status: "idle",
    createdAt: 0,
    title: null,
    labels: {},
    items: [],
    pendingElicitations: [],
    permissionLevel,
    parentSessionId: null,
    subAgentName: null,
    kind: "default",
  };
}

function makeConv(permissionLevel: number | null): Conversation {
  return {
    id: "conv_1",
    object: "conversation",
    title: null,
    created_at: 0,
    updated_at: 0,
    labels: {},
    permission_level: permissionLevel,
  };
}

/** Wire the source hooks: a session snapshot and an optional sidebar list. */
function setHooks(opts: {
  session: Session | null;
  sessionLoading?: boolean;
  /** Sidebar rows; pass `undefined` to model the list still loading. */
  conversations?: Conversation[] | undefined;
}) {
  const conversations = opts.conversations;
  useConversationsMock.mockReturnValue({
    data: conversations === undefined ? undefined : { pages: [{ data: conversations }] },
  } as unknown as ReturnType<typeof useConversations>);
  useSessionMock.mockReturnValue({
    session: opts.session,
    isLoading: opts.sessionLoading ?? false,
    error: null,
  });
}

beforeEach(() => {
  useConversationsMock.mockReset();
  useSessionMock.mockReset();
});

describe("useCanEdit — edit boundary from the session snapshot", () => {
  it.each([
    [1, false],
    [2, true],
    [3, true],
    [4, true],
  ])("level %i → canEdit %s", (level, expected) => {
    // The session snapshot is authoritative; the sidebar row is ignored
    // when it's present. Level 2 (edit) is the threshold.
    setHooks({ session: makeSession(level), conversations: [makeConv(1)] });
    const { result } = renderHook(() => useCanEdit("conv_1"));
    expect(result.current).toBe(expected);
  });

  it("treats a null snapshot level as permissive (single-user mode)", () => {
    // Single-user / unauthenticated servers report null, which must not
    // lock the user out of editing their own session.
    setHooks({ session: makeSession(null), conversations: [] });
    expect(renderHook(() => useCanEdit("conv_1")).result.current).toBe(true);
  });
});

describe("useCanEdit — sidebar fallback before the snapshot resolves", () => {
  it("uses the sidebar row's read-only level when no snapshot yet", () => {
    setHooks({ session: null, sessionLoading: true, conversations: [makeConv(1)] });
    expect(renderHook(() => useCanEdit("conv_1")).result.current).toBe(false);
  });

  it("uses the sidebar row's edit level when no snapshot yet", () => {
    setHooks({ session: null, sessionLoading: true, conversations: [makeConv(2)] });
    expect(renderHook(() => useCanEdit("conv_1")).result.current).toBe(true);
  });
});

describe("useCanEdit — safe defaults", () => {
  it("denies editing for an unknown conversation once the list has loaded", () => {
    // List resolved but neither it nor the snapshot knows this conv —
    // a deleted/unauthorized id. derivePermissionLevel returns read-only
    // (1), so the edit gate closes.
    setHooks({ session: null, sessionLoading: false, conversations: [] });
    expect(renderHook(() => useCanEdit("conv_1")).result.current).toBe(false);
  });

  it("stays permissive while the conversation list is still loading", () => {
    // Cold boot: don't flash read-only (and disable editing) before the
    // sidebar settles. derivePermissionLevel returns null → permissive.
    setHooks({ session: null, sessionLoading: false, conversations: undefined });
    expect(renderHook(() => useCanEdit("conv_1")).result.current).toBe(true);
  });
});

describe("share mutations — sidebar shared flag", () => {
  const listKey = ["conversations", "", false];
  const grantsKey = ["permissions", "conv_1"];
  const grant = (user_id: string, level = 1) => ({ user_id, conversation_id: "conv_1", level });
  const sessionRow = (row: Partial<Conversation> = {}): Conversation => ({
    ...makeConv(4),
    owner: "owner@example.com",
    ...row,
  });
  // The list and pinned caches hold the row in different shapes.
  const listCache = {
    key: listKey,
    data: (row: Partial<Conversation> = {}) => ({
      pages: [{ data: [sessionRow(row)] }],
      pageParams: [null],
    }),
    flag: (qc: QueryClient) =>
      qc.getQueryData<{ pages: { data: Conversation[] }[] }>(listKey)?.pages[0].data[0]
        .shared_with_others,
  };
  const pinnedCache = {
    key: [...PINNED_CONVERSATIONS_KEY],
    data: (row: Partial<Conversation> = {}) => ({
      conversations: [sessionRow({ labels: { "omnigent.pinned": "123" }, ...row })],
      filterHonored: true,
    }),
    flag: (qc: QueryClient) =>
      qc.getQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY)?.conversations[0]
        .shared_with_others,
  };

  function setup(grantees?: string[]) {
    const qc = new QueryClient();
    if (grantees) {
      qc.setQueryData(grantsKey, [
        grant("owner@example.com", 4),
        ...grantees.map((id) => grant(id)),
      ]);
    }
    const wrapper = ({ children }: { children: ReactNode }) => (
      <QueryClientProvider client={qc}>{children}</QueryClientProvider>
    );
    const hooks = renderHook(
      () => ({ grant: useGrantPermission("conv_1"), revoke: useRevokePermission("conv_1") }),
      { wrapper },
    ).result;
    return { qc, hooks };
  }

  /** Start a fetch for `key` whose first response stays pending until released. */
  function deferFirstFetch<T>(qc: QueryClient, key: unknown[], fresh: T) {
    let release!: (data: T) => void;
    let calls = 0;
    const done = qc
      .fetchQuery({
        queryKey: key,
        queryFn: () => {
          calls += 1;
          if (calls > 1) return Promise.resolve(fresh);
          return new Promise<T>((resolve) => {
            release = resolve;
          });
        },
      })
      .catch(() => undefined);
    return { release: (data: T) => release(data), done };
  }

  it.each([
    ["a user", "friend@example.com", true],
    ["the public link", PUBLIC_USER_ID, undefined],
  ])("sets the flag after granting %s", async (_label, userId, expected) => {
    const { qc, hooks } = setup();
    qc.setQueryData(listKey, listCache.data());
    await act(() => hooks.current.grant.mutateAsync({ userId, level: 1 }));
    expect(listCache.flag(qc)).toBe(expected);
  });

  it.each([
    ["the last user grantee", ["friend@example.com", PUBLIC_USER_ID], false],
    ["one of two user grantees", ["friend@example.com", "other@example.com"], true],
  ])("recomputes the flag after revoking %s", async (_label, grantees, expected) => {
    const { qc, hooks } = setup(grantees);
    qc.setQueryData(listKey, listCache.data());
    await act(() => hooks.current.revoke.mutateAsync("friend@example.com"));
    expect(listCache.flag(qc)).toBe(expected);
  });

  it("tracks back-to-back changes before the grant list refetches", async () => {
    const { qc, hooks } = setup(["a@example.com", "b@example.com"]);
    qc.setQueryData(listKey, listCache.data());
    await act(() => hooks.current.revoke.mutateAsync("a@example.com"));
    await act(() => hooks.current.revoke.mutateAsync("b@example.com"));
    expect(listCache.flag(qc)).toBe(false);

    await act(() => hooks.current.grant.mutateAsync({ userId: "c@example.com", level: 1 }));
    await act(() => hooks.current.revoke.mutateAsync(PUBLIC_USER_ID));
    expect(listCache.flag(qc)).toBe(true);
  });

  it("updates a pinned session outside the loaded list pages", async () => {
    const { qc, hooks } = setup([PUBLIC_USER_ID]);
    qc.setQueryData(PINNED_CONVERSATIONS_KEY, pinnedCache.data());
    await act(() => hooks.current.grant.mutateAsync({ userId: "friend@example.com", level: 1 }));
    expect(pinnedCache.flag(qc)).toBe(true);
    const pinned = qc.getQueryData<PinnedConversationsResult>(PINNED_CONVERSATIONS_KEY)!;
    expect(pinned.conversations[0].labels["omnigent.pinned"]).toBe("123");

    await act(() => hooks.current.revoke.mutateAsync("friend@example.com"));
    expect(pinnedCache.flag(qc)).toBe(false);
  });

  it.each([
    ["session list", listCache],
    ["pinned set", pinnedCache],
  ])("replaces a %s read that predates the change", async (_label, cache) => {
    const { qc, hooks } = setup();
    qc.setQueryData(cache.key, cache.data());
    const read = deferFirstFetch(qc, cache.key, cache.data({ shared_with_others: true }));
    await act(() => hooks.current.grant.mutateAsync({ userId: "friend@example.com", level: 1 }));
    read.release(cache.data({ shared_with_others: false }));
    await act(() => read.done);
    expect(cache.flag(qc)).toBe(true);
  });

  it("lets a first list load finish when sharing mid-load", async () => {
    const { qc, hooks } = setup();
    const read = deferFirstFetch(qc, listKey, listCache.data());
    await act(() => hooks.current.grant.mutateAsync({ userId: "friend@example.com", level: 1 }));
    read.release(listCache.data({ shared_with_others: true }));
    await act(() => read.done);
    expect(qc.getQueryState(listKey)).toMatchObject({ status: "success", fetchStatus: "idle" });
    expect(listCache.flag(qc)).toBe(true);
  });
});
