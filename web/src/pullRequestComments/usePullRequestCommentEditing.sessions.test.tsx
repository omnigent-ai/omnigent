// A pending review's draft is private to the session whose host read it: with the same PR
// open in two sessions, each with a valid host route, one never shows or discards the other's
// edit text, before it's sent or after its answer is lost.

import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));
const host = vi.hoisted(() => ({ write: vi.fn() }));
// Every session's host serves the draft methods, each along its own route.
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: () => true,
  hostChangeRoute: (conversationId: string) => ({ conversationId, key: `${conversationId}:route` }),
  hostChangeRouteCurrent: (route: { conversationId: string; key: string }) =>
    route.key === `${route.conversationId}:route`,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  writePullRequestRpc: host.write,
}));

import { PullRequestMaybeSentError } from "@/lib/workspaceApi";

import { resetCommentChangeRetirementForTests } from "./commentChangeRetirement";
import { EMPTY_ENTRY, resetCommentEditsForTests } from "./commentEditStore";
import { useUnlistedCommentEdits } from "./PullRequestCommentRecoveries";
import {
  type CommentEditSubject,
  usePullRequestCommentEditing,
} from "./usePullRequestCommentEditing";

const PR = "https://github.com/acme/app/pull/42";
const DRAFT: Omit<CommentEditSubject, "conversationId"> = {
  prUrl: PR,
  commentId: "PRRC_1",
  commentNumber: 101,
  threadId: "PRRT_1",
  body: "Before",
  canUpdate: true,
  canDelete: true,
  viewerDidAuthor: true,
  pendingReviewId: "PRR_1",
};
const NONE_LISTED = new Set<string>();

function renderSessions() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return renderHook(
    () => ({
      a: usePullRequestCommentEditing({ ...DRAFT, conversationId: "session-a" }),
      b: usePullRequestCommentEditing({ ...DRAFT, conversationId: "session-b" }),
      bRecoveries: useUnlistedCommentEdits("session-b", PR, NONE_LISTED),
    }),
    { wrapper },
  );
}

beforeEach(() => {
  host.write.mockReset();
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
});
afterEach(() => {
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
});

describe("a draft's edit, with the PR open in two sessions", () => {
  it("shows only in the session editing it, before anything is sent", () => {
    const { result } = renderSessions();
    act(() => result.current.a.actions!.start());
    act(() => result.current.a.setText("Session A's draft"));

    expect(result.current.a.entry.editing?.text).toBe("Session A's draft");
    expect(result.current.b.entry).toBe(EMPTY_ENTRY);
    expect(result.current.b.actions?.canEdit ?? false).toBe(false);
    expect(host.write).not.toHaveBeenCalled();
  });

  it("stays with the session that sent it after its answer is lost, which the other can't discard", async () => {
    host.write.mockRejectedValue(new PullRequestMaybeSentError(new TypeError("dropped")));
    const { result } = renderSessions();
    act(() => result.current.a.actions!.start());
    act(() => result.current.a.setText("Session A's draft"));
    act(() => result.current.a.actions!.save());
    await waitFor(() => expect(result.current.a.entry.unknown).not.toBeNull());

    expect(result.current.b.entry).toBe(EMPTY_ENTRY);
    expect(result.current.bRecoveries).toEqual([]);
    act(() => result.current.b.confirmDiscard());
    expect(result.current.a.entry.unknown?.sent).toBe("Session A's draft");
    expect(host.write).toHaveBeenCalledTimes(1);
  });
});
