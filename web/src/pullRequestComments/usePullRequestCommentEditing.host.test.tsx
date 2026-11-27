// Editing and deleting a published comment through the session's host: where the host
// lists the method for this caller, the card offers it and sends it through the host by the
// comment's number, once. Delete is the owner's alone; an editor's host lists only Edit.

import { createHash } from "node:crypto";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider, QueryObserver } from "@tanstack/react-query";
import { act, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));
const host = vi.hoisted(() => ({
  served: new Set<string>(),
  key: "server:session-1",
  write: vi.fn(),
}));
vi.mock("@/pullRequestPage/pullRequestPageRoute", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  usePullRequestMethodServed: (_sessionId: string, method: string) => host.served.has(method),
  hostChangeRoute: (conversationId: string, method: string) =>
    host.served.has(method) ? { conversationId, key: host.key } : null,
  hostChangeRouteCurrent: (route: { key: string }, method: string) =>
    host.served.has(method) && route.key === host.key,
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  writePullRequestRpc: host.write,
}));

import type {
  PullRequestComment,
  PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import { PullRequestApiError } from "@/lib/pullRequestRpc";
import type { WorkspaceClient } from "@/lib/workspaceApi";
import { PullRequestMaybeSentError } from "@/lib/workspaceApi";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";

import { resetCommentChangeRetirementForTests } from "./commentChangeRetirement";
import { EMPTY_ENTRY, resetCommentEditsForTests } from "./commentEditStore";
import { useConversationCommentCard } from "./PullRequestCommentActions";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";
const OWNER = ["editComment", "deleteComment"];
// What next's levels give an editor: EditComment is EDIT, DeleteComment OWNER.
const EDITOR = ["editComment"];
const COMMENT: PullRequestComment = {
  id: "IC_1",
  database_id: 101,
  author: "octo",
  body: "Before",
  created_at: "2026-11-05T10:00:00Z",
  url: `${PR}#issuecomment-101`,
  can_update: true,
  can_delete: true,
  viewer_did_author: true,
};
const CONTEXT: ReviewThreadContext = {
  conversationId: SESSION,
  prUrl: PR,
  account: "octo",
  canReply: true,
  canComment: true,
  loadComments: vi.fn(),
  loadingComments: null,
};
const HOST_KEY = ["pr-page-github-info", SESSION, PR];
const sha256 = (text: string) => createHash("sha256").update(text, "utf8").digest("hex");
const edited = (body: string) => ({
  object: "session.github.comment_edit",
  pr_url: PR,
  comment_id: 101,
  body,
});
const deleted = { object: "session.github.comment_deleted", pr_url: PR, comment_id: 101 };

/** The requests each host write sent, by method. */
async function sent(): Promise<[string, unknown][]> {
  const requests: [string, unknown][] = [];
  const workspace = new Proxy(
    {},
    {
      get: (_target, method: string) => async (request: unknown) => {
        requests.push([method, request]);
        return { payload: {} };
      },
    },
  ) as unknown as WorkspaceClient;
  await Promise.all(host.write.mock.calls.map(([, rpc]) => rpc.call(workspace)));
  return requests;
}

function renderCard(served: readonly string[], comment = COMMENT) {
  host.served = new Set(served);
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const info = (comments: PullRequestComment[]) =>
    ({
      available: true,
      selected_pr_url: PR,
      pr: { url: PR, comments },
    }) as unknown as PullRequestInfo;
  queryClient.setQueryData(HOST_KEY, info([comment]));
  // The page's own read of the PR, which a check reads again through the host.
  let next = [comment];
  const pageRead = new QueryObserver(queryClient, {
    queryKey: HOST_KEY,
    queryFn: async () => info(next),
    staleTime: Infinity,
  });
  pageRead.subscribe(() => {});
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  const view = renderHook(() => useConversationCommentCard(comment, CONTEXT, PR), { wrapper });
  return {
    ...view,
    /** The PR as the session reads it next, through the host. */
    readsNext: (comments: PullRequestComment[]) => {
      next = comments;
    },
  };
}

type Card = ReturnType<typeof useConversationCommentCard>;
const rowLabels = (card: Card) => card.actions.rows.map((row) => row.definition.menuLabel);

function editAndSave(card: { current: Card }, text: string) {
  act(() => card.current.editing.actions!.start());
  act(() => card.current.editing.setText(text));
  act(() => card.current.editing.actions!.save());
}

beforeEach(() => {
  host.write.mockReset();
  host.key = "server:session-1";
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
});
afterEach(() => {
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
});

describe("as the owner, whose host lists Edit and Delete", () => {
  it("offers both on the viewer's own comment", () => {
    const { result } = renderCard(OWNER);
    expect(rowLabels(result.current)).toEqual([
      "Edit",
      "Quote Reply",
      "Copy Link",
      "Open in Browser",
      "Delete…",
    ]);
  });

  it("offers Edit on someone else's comment the host's account may edit", () => {
    const { result } = renderCard(OWNER, { ...COMMENT, viewer_did_author: false });
    expect(rowLabels(result.current)).toContain("Edit");
  });

  it("sends the new text with the digest of the text it started from, once", async () => {
    host.write.mockResolvedValue(edited("After"));
    const { result } = renderCard(OWNER);
    editAndSave(result, "After");
    await waitFor(() => expect(result.current.editing.entry).toEqual(EMPTY_ENTRY));
    expect(host.write).toHaveBeenCalledTimes(1);
    expect(host.write.mock.calls[0]?.[0]).toBe(SESSION);
    await expect(sent()).resolves.toEqual([
      [
        "editComment",
        {
          prUrl: PR,
          kind: "issue",
          commentId: 101n,
          body: "After",
          expectedBodySha256: sha256("Before"),
        },
      ],
    ]);
  });

  it("asks first, then deletes once by the comment's number", async () => {
    host.write.mockResolvedValue(deleted);
    const { result } = renderCard(OWNER);
    act(() => result.current.editing.actions!.askDelete());
    expect(result.current.editing.deleteAsked).toBe(true);
    expect(host.write).not.toHaveBeenCalled();
    act(() => result.current.editing.confirmDelete());
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    await expect(sent()).resolves.toEqual([
      ["deleteComment", { prUrl: PR, kind: "issue", commentId: 101n }],
    ]);
  });

  it("keeps the text and says why when the host refuses, as for text changed on GitHub", async () => {
    host.write.mockRejectedValue(
      new PullRequestApiError("The comment changed. Refresh to see its latest text.", 409),
    );
    const { result } = renderCard(OWNER);
    editAndSave(result, "After");
    await waitFor(() =>
      expect(result.current.editing.entry.message).toBe(
        "The comment changed. Refresh to see its latest text.",
      ),
    );
    expect(result.current.editing.entry.editing?.text).toBe("After");
    expect(host.write).toHaveBeenCalledTimes(1);
  });

  it("holds an edit that may have reached GitHub until Check Again reads the PR again", async () => {
    host.write.mockRejectedValue(new PullRequestMaybeSentError(new TypeError("dropped")));
    const { result, readsNext } = renderCard(OWNER);
    editAndSave(result, "After");
    await waitFor(() => expect(result.current.editing.entry.unknown).not.toBeNull());
    expect(result.current.editing.actions?.canCheck).toBe(true);
    // Nothing more goes for the comment while it's held.
    act(() => result.current.editing.actions!.askDelete());
    expect(result.current.editing.deleteAsked).toBe(false);
    readsNext([{ ...COMMENT, body: "After" }]);
    act(() => result.current.editing.actions!.checkAgain());
    await waitFor(() => expect(result.current.editing.entry).toEqual(EMPTY_ENTRY));
    expect(host.write).toHaveBeenCalledTimes(1);
  });

  it("settles a held delete once the PR's read no longer lists the comment", async () => {
    host.write.mockRejectedValue(new PullRequestMaybeSentError(new TypeError("dropped")));
    const { result, readsNext } = renderCard(OWNER);
    act(() => result.current.editing.actions!.askDelete());
    act(() => result.current.editing.confirmDelete());
    await waitFor(() => expect(result.current.editing.entry.unknown).not.toBeNull());
    readsNext([]);
    act(() => result.current.editing.actions!.checkAgain());
    await waitFor(() => expect(result.current.editing.entry).toEqual(EMPTY_ENTRY));
  });

  it("keeps a held delete when the read lists only the latest comments", async () => {
    host.write.mockRejectedValue(new PullRequestMaybeSentError(new TypeError("dropped")));
    const { result, readsNext } = renderCard(OWNER);
    act(() => result.current.editing.actions!.askDelete());
    act(() => result.current.editing.confirmDelete());
    await waitFor(() => expect(result.current.editing.entry.unknown).not.toBeNull());
    readsNext(Array.from({ length: 100 }, (_, index) => ({ ...COMMENT, id: `IC_other_${index}` })));
    act(() => result.current.editing.actions!.checkAgain());
    await waitFor(() => expect(result.current.editing.entry.checkFailed).toBe(true));
    expect(result.current.editing.entry.unknown).not.toBeNull();
  });

  it("holds the change when the page's route changed before the host answered", async () => {
    let answer: (value: unknown) => void = () => {};
    host.write.mockImplementation(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const { result } = renderCard(OWNER);
    editAndSave(result, "After");
    await waitFor(() => expect(host.write).toHaveBeenCalledTimes(1));
    host.key = "server:session-1:again";
    await act(async () => answer(edited("After")));
    await waitFor(() => expect(result.current.editing.entry.unknown).not.toBeNull());
  });
});

describe("as an editor, whose host lists only Edit", () => {
  it("offers Edit and no Delete", () => {
    const { result } = renderCard(EDITOR);
    expect(rowLabels(result.current)).toEqual([
      "Edit",
      "Quote Reply",
      "Copy Link",
      "Open in Browser",
    ]);
  });

  it("offers no Edit on someone else's comment, which GitHub lets an admin account edit", () => {
    const { result } = renderCard(EDITOR, { ...COMMENT, viewer_did_author: false });
    expect(rowLabels(result.current)).toEqual(["Quote Reply", "Copy Link", "Open in Browser"]);
  });

  it("sends nothing from a delete the host wouldn't make for them", () => {
    const { result } = renderCard(EDITOR);
    act(() => result.current.editing.actions!.askDelete());
    act(() => result.current.editing.confirmDelete());
    expect(host.write).not.toHaveBeenCalled();
  });
});

describe("without the host's methods", () => {
  it.each([
    ["a host serving neither", [], COMMENT],
    ["a comment the host's read gave no number", OWNER, { ...COMMENT, database_id: null }],
    [
      "a comment the viewer may not change",
      OWNER,
      { ...COMMENT, can_update: false, can_delete: false },
    ],
  ] as const)("offers neither for %s", (_label, served, comment) => {
    const { result } = renderCard(served, comment);
    expect(rowLabels(result.current)).toEqual(["Quote Reply", "Copy Link", "Open in Browser"]);
  });
});
