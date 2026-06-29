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

import { renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { Conversation } from "./useConversations";
import type { Session } from "@/lib/types";

vi.mock("./useConversations", () => ({ useConversations: vi.fn() }));
vi.mock("./useSession", () => ({ useSession: vi.fn() }));

import { useConversations } from "./useConversations";
import { useSession } from "./useSession";
import { useCanEdit } from "./usePermissions";

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
