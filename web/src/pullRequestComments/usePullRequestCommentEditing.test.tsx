// Editing and deleting a comment through a card's hooks, with the real read-path and
// write-path stores over a stand-in shell: when this Mac offers them, what each sends, what
// its answer may touch, and how a change GitHub didn't confirm is held and checked.

import type * as IdentityModule from "@/lib/identity";
import type * as SonnerModule from "sonner";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import {
  act,
  fireEvent,
  render,
  renderHook,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const { currentUser, identityListeners } = vi.hoisted(() => ({
  currentUser: { id: "user-1" as string | null },
  identityListeners: new Set<() => void>(),
}));
vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), { success: vi.fn(), error: vi.fn() }),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => currentUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
  authenticatedFetch: vi.fn(async () => new Response("{}", { status: 500 })),
}));

import type {
  PullRequestComment,
  PullRequestInfo,
} from "@/newPullRequestPage/hooks/usePullRequests";
import {
  COMMENT_EDIT_WRITE_OPERATIONS,
  GITHUB_WRITE_SET_V1,
  macWriteRoute,
  PENDING_REVIEW_READ,
  PENDING_REVIEW_WRITE_OPERATIONS,
  REVIEW_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setCommentEditsFlagForTests,
  setMacWritesReleaseFlagForTests,
  setPendingReviewsFlagForTests,
  setReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import {
  acceptMacAnswer,
  desktopReadTarget,
  GITHUB_MAC_READ_OPERATIONS,
  githubMacReadsPreference,
  macReadTicket,
  readScope,
  resetDesktopReadPathForTests,
} from "@/lib/desktopReadPath";
import type { ReviewThreadsPage } from "@/lib/pullRequestReviewThreads";
import type { ReviewThreadContext } from "@/newPullRequestPage/shell/PullRequestReviewThread";
import { resetCommentChangeRetirementForTests } from "./commentChangeRetirement";
import {
  EMPTY_ENTRY,
  commentEditKey,
  readCommentEdit,
  resetCommentEditsForTests,
  updateCommentEdit,
} from "./commentEditStore";
import { PullRequestCommentRecoveries } from "./PullRequestCommentRecoveries";
import { ConversationEntry } from "./PullRequestCommentsEntries";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { useConversationCommentCard, useReviewCommentCard } from "./PullRequestCommentActions";
import { PullRequestCommentEditSurface } from "./PullRequestCommentEditSurface";
import { TooltipProvider } from "@/components/ui/tooltip";

const PR = "https://github.com/acme/app/pull/42";
const SESSION = "session-1";
const COMMENT: PullRequestComment = {
  id: "IC_1",
  author: "mac-octo",
  body: "Before",
  created_at: "2026-11-05T10:00:00Z",
  url: `${PR}#issuecomment-1`,
  can_update: true,
  can_delete: true,
};
const CONTEXT: ReviewThreadContext = {
  conversationId: SESSION,
  prUrl: PR,
  account: "mac-octo",
  canReply: true,
  canComment: true,
  loadComments: vi.fn(),
  loadingComments: null,
};

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (value: T) => void;
}
function deferred<T>(): Deferred<T> {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>((settle) => {
    resolve = settle;
  });
  return { promise, resolve };
}

const edited = (body: string) => ({
  ok: true,
  data: { object: "session.github.pr_comment_edit", comment_id: "IC_1", body },
  actor: { login: "mac-octo" },
});
const deleted = (commentId = "IC_1") => ({
  ok: true,
  data: { object: "session.github.pr_comment_delete", comment_id: commentId },
  actor: { login: "mac-octo" },
});
const refused = (code: string, message: string, extra: Record<string, unknown> = {}) => ({
  ok: false,
  error: { code, message },
  ...extra,
});
const UNKNOWN = refused("outcome_unknown", "GitHub may have made this change.");
const commentState = (state: Record<string, unknown>, login = "mac-octo") => ({
  ok: true,
  data: { object: "session.github.pr_comment_state", comment_id: "IC_1", ...state },
  identity: { host: "github.com", login, generation: 1 },
});

interface Shell {
  writes: { operation: string; params: Record<string, unknown> }[];
  reads: { operation: string; params: Record<string, unknown> }[];
}

/**
 * A shell reading and writing GitHub as `mac-octo`, listing comment edits, with the flag on.
 * `write` answers each write; `read` answers the comment state read.
 */
async function installShell({
  write,
  read = async () => commentState({ state: "unreadable" }),
  writes = COMMENT_EDIT_WRITE_OPERATIONS,
}: {
  write: (operation: string) => Promise<unknown>;
  read?: () => Promise<unknown>;
  writes?: readonly string[];
}): Promise<Shell> {
  const shell: Shell = { writes: [], reads: [] };
  (window as unknown as Record<string, unknown>).omnigentDesktop = {
    kind: "electron",
    codeHostQuery: vi.fn(
      async (request: { operation: string; params: Record<string, unknown> }) => {
        if (request.operation !== "pull_request_comment_state") return { ok: true, data: [] };
        shell.reads.push({ operation: request.operation, params: request.params });
        return read();
      },
    ),
    codeHostWrite: vi.fn(
      async (request: { operation: string; params: Record<string, unknown> }) => {
        shell.writes.push({ operation: request.operation, params: request.params });
        return write(request.operation);
      },
    ),
    codeHostWriteCancel: vi.fn(),
    codeHostAccounts: {
      get: async () => ({
        ok: true,
        data: { intent: { mode: "automatic" }, effective: null, accounts: [] },
      }),
      set: async () => ({ ok: false }),
      onChanged: () => () => {},
    },
    getDesktopFeatures: async () => ({
      codeHosts: [
        {
          id: "github",
          operations: [...GITHUB_MAC_READ_OPERATIONS, "pull_request_comment_state"],
          accounts: true,
          writes: { version: 1, operations: [...GITHUB_WRITE_SET_V1, ...writes] },
        },
      ],
    }),
  };
  githubMacReadsPreference.write("on");
  await desktopReadTarget(PR);
  expect(
    acceptMacAnswer(macReadTicket("github", "github.com"), {
      identity: { host: "github.com", login: "mac-octo", generation: 1 },
    }),
  ).toBe("accepted");
  setMacWritesReleaseFlagForTests(true);
  setCommentEditsFlagForTests(true);
  return shell;
}

/** The PR's info as this Mac read it, under its Mac scope, and as the host read it. */
function setup() {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const info = (comments: PullRequestComment[]) =>
    ({
      available: true,
      selected_pr_url: PR,
      pr: { url: PR, comments },
    }) as unknown as PullRequestInfo;
  const macKey = ["github-info", SESSION, PR, ...readScope(PR)];
  const hostKey = ["github-info", SESSION, PR];
  queryClient.setQueryData(macKey, info([COMMENT]));
  queryClient.setQueryData(hostKey, info([COMMENT]));
  const bodies = (key: unknown[]) =>
    queryClient.getQueryData<PullRequestInfo>(key)?.pr?.comments?.map((c) => c.body);
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
  );
  return { queryClient, wrapper, macKey, hostKey, bodies };
}

function renderCard(wrapper: ReturnType<typeof setup>["wrapper"], comment = COMMENT) {
  return renderHook(() => useConversationCommentCard(comment, CONTEXT, PR), { wrapper });
}

type Card = ReturnType<typeof useConversationCommentCard>;
const rowLabels = (card: Card) => card.actions.rows.map((row) => row.definition.menuLabel);

/** Open the editor, type `text`, and save, as the person would. */
function editAndSave(card: { current: Card }, text: string) {
  act(() => card.current.editing.actions!.start());
  act(() => card.current.editing.setText(text));
  act(() => card.current.editing.actions!.save());
}

function signInAs(id: string | null) {
  act(() => {
    currentUser.id = id;
    for (const listener of [...identityListeners]) listener();
  });
}

beforeEach(() => {
  currentUser.id = "user-1";
  identityListeners.clear();
  resetDesktopPullRequestWritesForTests();
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
});

afterEach(() => {
  usePaletteCommandsStore.setState({ groups: {} });
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetCommentEditsForTests();
  resetCommentChangeRetirementForTests();
  delete (window as unknown as Record<string, unknown>).omnigentDesktop;
  githubMacReadsPreference.write("automatic");
});

describe("when this Mac offers Edit and Delete", () => {
  it("offers both on the viewer's own comment, Edit first and Delete… last", async () => {
    await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    expect(rowLabels(result.current)).toEqual([
      "Edit",
      "Quote Reply",
      "Copy Link",
      "Open in Browser",
      "Delete…",
    ]);
  });

  it.each([
    ["a comment the viewer may not change", { ...COMMENT, can_update: false, can_delete: false }],
    ["a comment read without its id, as the host reads it", { ...COMMENT, id: undefined }],
    ["a read that didn't say", { ...COMMENT, can_update: undefined, can_delete: undefined }],
  ])("offers neither on %s", async (_label, comment) => {
    await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper, comment);
    expect(rowLabels(result.current)).toEqual(["Quote Reply", "Copy Link", "Open in Browser"]);
  });

  it("offers neither while the flag is off, and both again once it's on", async () => {
    await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    act(() => setCommentEditsFlagForTests(false));
    const { result } = renderCard(wrapper);
    expect(rowLabels(result.current)).not.toContain("Edit");
    act(() => setCommentEditsFlagForTests(true));
    expect(rowLabels(result.current)).toContain("Edit");
  });

  it("offers only what an older shell lists", async () => {
    await installShell({ write: async () => deleted(), writes: ["pull_request_comment_delete"] });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    expect(rowLabels(result.current)).toEqual([
      "Quote Reply",
      "Copy Link",
      "Open in Browser",
      "Delete…",
    ]);
  });
});

describe("editing", () => {
  it("sends the new text with the text it started from, and shows GitHub's answer", async () => {
    const shell = await installShell({ write: async () => edited("After, as saved") });
    const { wrapper, bodies, macKey, hostKey } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "After");
    expect(shell.writes).toEqual([
      {
        operation: "pull_request_comment_edit",
        params: {
          repository: "acme/app",
          number: 42,
          comment_id: "IC_1",
          kind: "issue",
          body: "After",
          expected: "Before",
        },
      },
    ]);
    await waitFor(() => expect(result.current.editing.entry.editing).toBeNull());
    expect(bodies(macKey)).toEqual(["After, as saved"]);
    // The host's own read of the PR is never written with this Mac's answer.
    expect(bodies(hostKey)).toEqual(["Before"]);
  });

  it("asks before Cancel throws away changed text, and closes at once when nothing changed", async () => {
    await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    act(() => result.current.editing.actions!.start());
    act(() => result.current.editing.actions!.cancel());
    expect(result.current.editing.entry.editing).toBeNull();
    act(() => result.current.editing.actions!.start());
    act(() => result.current.editing.setText("Changed"));
    act(() => result.current.editing.actions!.cancel());
    expect(result.current.editing.discardAsked).toBe(true);
    expect(result.current.editing.entry.editing?.text).toBe("Changed");
    act(() => result.current.editing.confirmDiscard());
    expect(result.current.editing.entry.editing).toBeNull();
  });

  it("keeps the edit when GitHub's text changed, and Keep mine saves over it", async () => {
    const answers = [
      refused("comment_changed", "This comment changed on GitHub while you were editing.", {
        current: { field: "body", value: "Theirs" },
      }),
      edited("Mine"),
    ];
    const shell = await installShell({ write: async () => answers.shift() });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "Mine");
    await waitFor(() =>
      expect(result.current.editing.entry.conflict).toEqual({ current: "Theirs" }),
    );
    expect(result.current.editing.entry.editing?.text).toBe("Mine");
    act(() => result.current.editing.actions!.keepMine());
    act(() => result.current.editing.actions!.save());
    expect(shell.writes[1].params).toMatchObject({ body: "Mine", expected: "Theirs" });
  });

  it("keeps the text to copy when the comment was deleted on GitHub meanwhile", async () => {
    await installShell({
      write: async () => refused("comment_deleted", "This comment was deleted on GitHub."),
    });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "After");
    await waitFor(() => expect(result.current.editing.entry.tombstone).toEqual({ text: "After" }));
    expect(rowLabels(result.current)).not.toContain("Edit");
    const palette = result.current.actions.rows.map((row) => row.definition.paletteLabel);
    expect(palette).toContain("Copy Unsaved Comment Text");
    act(() => result.current.editing.actions!.discardKept());
    expect(result.current.editing.entry.tombstone).toBeNull();
  });

  it("sends nothing, and says where to change it, once this Mac stops offering it", async () => {
    const shell = await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    act(() => result.current.editing.actions!.start());
    act(() => result.current.editing.setText("After"));
    act(() => setCommentEditsFlagForTests(false));
    act(() => result.current.editing.actions?.save());
    expect(shell.writes).toEqual([]);
    expect(result.current.editing.entry.editing?.text).toBe("After");
  });
});

describe("when this Mac switches accounts with the editor or the dialog open", () => {
  /** This Mac now reads, and would write, as another of its accounts. */
  function switchMacAccount() {
    act(() => {
      // The first answer as another account moves the scope on; the next is accepted.
      for (let read = 0; read < 2; read += 1) {
        acceptMacAnswer(macReadTicket("github", "github.com"), {
          identity: { host: "github.com", login: "mac-other", generation: 2 },
        });
      }
    });
    expect(macWriteRoute(PR, null)?.actor.login).toBe("mac-other");
  }

  it("sends no edit, keeps the text, and says the account changed", async () => {
    const shell = await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    act(() => result.current.editing.actions!.start());
    act(() => result.current.editing.setText("After"));
    switchMacAccount();
    act(() => result.current.editing.actions?.save());
    expect(shell.writes).toEqual([]);
    expect(result.current.editing.entry.editing?.text).toBe("After");
    expect(result.current.editing.entry.message).toBe(
      "The GitHub account on this Mac changed. Check who you’re acting as, then try again.",
    );
  });

  it("sends no delete confirmed after the switch", async () => {
    const shell = await installShell({ write: async () => deleted() });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    act(() => result.current.editing.actions!.askDelete());
    switchMacAccount();
    act(() => result.current.editing.confirmDelete());
    expect(shell.writes).toEqual([]);
  });
});

describe("deleting", () => {
  it("asks first, then deletes only from this Mac's own read of the PR", async () => {
    const shell = await installShell({ write: async () => deleted() });
    const { wrapper, bodies, macKey, hostKey } = setup();
    const { result } = renderCard(wrapper);
    act(() => result.current.editing.actions!.askDelete());
    expect(result.current.editing.deleteAsked).toBe(true);
    expect(shell.writes).toEqual([]);
    act(() => result.current.editing.confirmDelete());
    expect(shell.writes).toEqual([
      {
        operation: "pull_request_comment_delete",
        params: { repository: "acme/app", number: 42, comment_id: "IC_1", kind: "issue" },
      },
    ]);
    await waitFor(() => expect(bodies(macKey)).toEqual([]));
    expect(bodies(hostKey)).toEqual(["Before"]);
  });

  it("deletes a review comment from its thread, and the thread with its last comment", async () => {
    const shell = await installShell({ write: async () => deleted("PRRC_1") });
    const { queryClient, wrapper } = setup();
    const threadsKey = ["github-review-threads", SESSION, PR, ...readScope(PR)];
    const page = {
      data: [
        {
          id: "thread-1",
          path: "a.ts",
          can_reply: true,
          comments: {
            data: [
              {
                id: "PRRC_1",
                author: "mac-octo",
                body: "Rename?",
                created_at: null,
                url: null,
                can_delete: true,
              },
            ],
            has_more: false,
            next_cursor: null,
          },
        },
      ],
      has_more: false,
      next_cursor: null,
      head_sha: "h",
      base_sha: "b",
    } as unknown as ReviewThreadsPage;
    queryClient.setQueryData(threadsKey, page);
    const thread = page.data[0];
    const { result } = renderHook(
      () => useReviewCommentCard(thread.comments.data[0], thread, CONTEXT, PR),
      { wrapper },
    );
    act(() => result.current.editing.confirmDelete());
    expect(shell.writes[0].params).toMatchObject({ comment_id: "PRRC_1", kind: "review" });
    await waitFor(() =>
      expect(queryClient.getQueryData<ReviewThreadsPage>(threadsKey)?.data).toEqual([]),
    );
  });
});

describe("a change GitHub didn't confirm", () => {
  it("holds every change to the comment, in every card showing it, until it's checked", async () => {
    const shell = await installShell({ write: async () => UNKNOWN });
    const { wrapper } = setup();
    const first = renderCard(wrapper);
    const second = renderCard(wrapper);
    editAndSave(first.result, "After");
    await waitFor(() => expect(first.result.current.editing.entry.unknown).not.toBeNull());
    expect(second.result.current.editing.entry.unknown).toMatchObject({ sent: "After" });
    for (const card of [first.result, second.result]) {
      act(() => card.current.editing.actions!.save());
      act(() => card.current.editing.confirmDelete());
      const edit = card.current.actions.rows.find((row) => row.definition.id === "delete");
      expect(edit?.disabledReason).toBe("Check this comment before changing it again");
    }
    expect(shell.writes).toHaveLength(1);
    // A card mounted later sees the same hold.
    first.unmount();
    const later = renderCard(wrapper);
    expect(later.result.current.editing.entry.unknown).not.toBeNull();
  });

  it("isn't settled by the PR being read again, only by a read of the comment", async () => {
    await installShell({ write: async () => UNKNOWN });
    const { wrapper, queryClient, macKey } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "After");
    await waitFor(() => expect(result.current.editing.entry.unknown).not.toBeNull());
    // A refresh that no longer lists the comment proves nothing about it.
    act(() => {
      queryClient.setQueryData(macKey, {
        available: true,
        selected_pr_url: PR,
        pr: { url: PR, comments: [] },
      });
    });
    expect(result.current.editing.entry.unknown).not.toBeNull();
  });

  it.each([
    ["the text sent: it was saved", { state: "present", body: "After" }, "cleared"],
    [
      "the old text: it wasn't saved, and the edit stays",
      { state: "present", body: "Before" },
      "editing",
    ],
    [
      "someone else's text: Keep mine or Load latest",
      { state: "present", body: "Theirs" },
      "conflict",
    ],
    ["no comment: the edit's text is kept", { state: "absent" }, "tombstone"],
  ])("settles on Check Again finding %s", async (_label, state, expected) => {
    const shell = await installShell({
      write: async () => UNKNOWN,
      read: async () => commentState(state),
    });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "After");
    await waitFor(() => expect(result.current.editing.actions?.canCheck).toBe(true));
    act(() => result.current.editing.actions!.checkAgain());
    await waitFor(() => expect(result.current.editing.entry.unknown).toBeNull());
    expect(shell.reads[0].params).toEqual({
      repository: "acme/app",
      number: 42,
      comment_id: "IC_1",
      kind: "issue",
    });
    // Check Again never sends the change again.
    expect(shell.writes).toHaveLength(1);
    const entry = result.current.editing.entry;
    if (expected === "cleared") expect(entry.editing).toBeNull();
    if (expected === "editing") expect(entry.editing?.text).toBe("After");
    if (expected === "conflict") expect(entry.conflict).toEqual({ current: "Theirs" });
    if (expected === "tombstone") expect(entry.tombstone).toEqual({ text: "After" });
  });

  it("stays held when Check Again can't read the comment, or reads it as someone else", async () => {
    const answers = [
      commentState({ state: "unreadable" }),
      commentState({ state: "absent" }, "other"),
    ];
    await installShell({ write: async () => UNKNOWN, read: async () => answers.shift() });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "After");
    await waitFor(() => expect(result.current.editing.actions?.canCheck).toBe(true));
    for (let attempt = 0; attempt < 2; attempt += 1) {
      act(() => result.current.editing.actions!.checkAgain());
      // eslint-disable-next-line no-await-in-loop -- each check settles before the next.
      await waitFor(() => expect(result.current.editing.entry.checkFailed).toBe(true));
      expect(result.current.editing.entry.unknown).not.toBeNull();
    }
  });

  it("listens only to the latest Check Again", async () => {
    const first = deferred<unknown>();
    const second = deferred<unknown>();
    const answers = [first.promise, second.promise];
    await installShell({ write: async () => UNKNOWN, read: () => answers.shift()! });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "After");
    await waitFor(() => expect(result.current.editing.actions?.canCheck).toBe(true));
    act(() => result.current.editing.actions!.checkAgain());
    act(() => result.current.editing.actions!.checkAgain());
    await act(async () => second.resolve(commentState({ state: "present", body: "Before" })));
    await waitFor(() => expect(result.current.editing.entry.unknown).toBeNull());
    // The first check's late answer, that the edit was saved, changes nothing now.
    await act(async () => first.resolve(commentState({ state: "present", body: "After" })));
    expect(result.current.editing.entry.editing?.text).toBe("After");
    expect(result.current.editing.entry.message).toBe("Your edit wasn't saved.");
  });

  it("holds a change the workspace user left before GitHub answered, and shows it only to them", async () => {
    const answer = deferred<unknown>();
    const shell = await installShell({ write: () => answer.promise });
    const { wrapper } = setup();
    const { result } = renderCard(wrapper);
    editAndSave(result, "After");
    expect(result.current.editing.entry.pending).not.toBeNull();
    signInAs("user-2");
    // Another workspace user sees nothing of it, and can't be held by it.
    expect(result.current.editing.entry.unknown).toBeNull();
    expect(result.current.editing.entry.pending).toBeNull();
    signInAs("user-1");
    // Back: held to check, never stuck saving.
    expect(result.current.editing.entry.pending).toBeNull();
    expect(result.current.editing.entry.unknown).toMatchObject({ sent: "After" });
    await act(async () => answer.resolve(edited("After")));
    expect(shell.writes).toHaveLength(1);
  });
});

describe("a change sent before this Mac's account, the route or the flag changed", () => {
  function moveToAnotherAccount() {
    act(() => {
      for (let read = 0; read < 2; read += 1) {
        acceptMacAnswer(macReadTicket("github", "github.com"), {
          identity: { host: "github.com", login: "mac-other", generation: 2 },
        });
      }
    });
    expect(macWriteRoute(PR, null)?.actor.login).toBe("mac-other");
  }

  it.each([
    ["the comment edits flag turns off", () => act(() => setCommentEditsFlagForTests(false))],
    ["this Mac reads as another account", moveToAnotherAccount],
  ])(
    "retires a pending edit into the held state when %s, never left saving",
    async (_label, change) => {
      const answer = deferred<unknown>();
      await installShell({ write: () => answer.promise });
      const { wrapper, bodies, macKey } = setup();
      const { result } = renderCard(wrapper);
      editAndSave(result, "After");
      expect(result.current.editing.entry.pending).not.toBeNull();
      change();
      await waitFor(() => expect(result.current.editing.entry.pending).toBeNull());
      expect(result.current.editing.entry.unknown).toMatchObject({ change: "edit", sent: "After" });
      expect(result.current.editing.entry.editing?.text).toBe("After");
      // GitHub's late answer is ignored: the held state waits for Check Again.
      await act(async () => answer.resolve(edited("After")));
      expect(result.current.editing.entry.unknown).not.toBeNull();
      expect(bodies(macKey)).toEqual(["Before"]);
    },
  );

  it("retires a pending delete the same way, and leaves the comment shown", async () => {
    const answer = deferred<unknown>();
    await installShell({ write: () => answer.promise });
    const { wrapper, bodies, macKey } = setup();
    const { result } = renderCard(wrapper);
    act(() => result.current.editing.actions!.askDelete());
    act(() => result.current.editing.confirmDelete());
    act(() => setCommentEditsFlagForTests(false));
    await waitFor(() =>
      expect(result.current.editing.entry.unknown).toMatchObject({ change: "delete" }),
    );
    await act(async () => answer.resolve(deleted()));
    expect(result.current.editing.entry.unknown).not.toBeNull();
    expect(bodies(macKey)).toEqual(["Before"]);
  });

  it("retires it even with no card showing the comment", async () => {
    const answer = deferred<unknown>();
    await installShell({ write: () => answer.promise });
    const { wrapper } = setup();
    const first = renderCard(wrapper);
    editAndSave(first.result, "After");
    first.unmount();
    act(() => setCommentEditsFlagForTests(false));
    act(() => setCommentEditsFlagForTests(true));
    const later = renderCard(wrapper);
    expect(later.result.current.editing.entry.pending).toBeNull();
    expect(later.result.current.editing.entry.unknown).toMatchObject({ sent: "After" });
  });
});

describe("an answer older than the reads cached since", () => {
  const later = () => Date.now() + 60_000;

  it("leaves a newer read of the PR as it is, for an edit and for a delete", async () => {
    for (const [operation, answerWith] of [
      ["edit", () => edited("After")],
      ["delete", () => deleted()],
    ] as const) {
      const answer = deferred<unknown>();
      // eslint-disable-next-line no-await-in-loop -- each operation gets a fresh shell.
      await installShell({ write: () => answer.promise });
      const { wrapper, queryClient, bodies, macKey } = setup();
      const { result, unmount } = renderCard(wrapper);
      if (operation === "edit") editAndSave(result, "After");
      else {
        act(() => result.current.editing.actions!.askDelete());
        act(() => result.current.editing.confirmDelete());
      }
      act(() => {
        queryClient.setQueryData(
          macKey,
          {
            available: true,
            selected_pr_url: PR,
            pr: { url: PR, comments: [{ ...COMMENT, body: "Newer read" }] },
          },
          { updatedAt: later() },
        );
      });
      // eslint-disable-next-line no-await-in-loop -- the answer settles before the check.
      await act(async () => answer.resolve(answerWith()));
      expect(bodies(macKey)).toEqual(["Newer read"]);
      unmount();
      resetCommentEditsForTests();
      resetCommentChangeRetirementForTests();
    }
  });

  it("leaves a newer page of review threads as it is", async () => {
    const answer = deferred<unknown>();
    await installShell({ write: () => answer.promise });
    const { queryClient, wrapper } = setup();
    const threadsKey = ["github-review-threads", SESSION, PR, ...readScope(PR)];
    const page = (body: string) =>
      ({
        data: [
          {
            id: "thread-1",
            path: "a.ts",
            can_reply: true,
            comments: {
              data: [
                {
                  id: "PRRC_1",
                  author: "mac-octo",
                  body,
                  created_at: null,
                  url: null,
                  can_update: true,
                },
              ],
              has_more: true,
              next_cursor: "c2",
            },
          },
        ],
        has_more: false,
        next_cursor: null,
        head_sha: "h",
        base_sha: "b",
      }) as unknown as ReviewThreadsPage;
    queryClient.setQueryData(threadsKey, page("Before"));
    const thread = page("Before").data[0];
    const { result } = renderHook(
      () => useReviewCommentCard(thread.comments.data[0], thread, CONTEXT, PR),
      { wrapper },
    );
    act(() => result.current.editing.actions!.start());
    act(() => result.current.editing.setText("After"));
    act(() => result.current.editing.actions!.save());
    act(() => {
      queryClient.setQueryData(threadsKey, page("Newer read"), { updatedAt: later() });
    });
    await act(async () =>
      answer.resolve({
        ok: true,
        data: { object: "session.github.pr_comment_edit", comment_id: "PRRC_1", body: "After" },
        actor: { login: "mac-octo" },
      }),
    );
    const cached = queryClient.getQueryData<ReviewThreadsPage>(threadsKey);
    expect(cached?.data[0].comments.data[0].body).toBe("Newer read");
    // The page's place in its replies is kept.
    expect(cached?.data[0].comments.next_cursor).toBe("c2");
  });
});

describe("saving while GitHub's text changed and no choice was made", () => {
  function Card() {
    const card = useConversationCommentCard(COMMENT, CONTEXT, PR);
    return (
      <PullRequestCommentEditSurface editing={card.editing}>
        <p>{COMMENT.body}</p>
      </PullRequestCommentEditSurface>
    );
  }

  async function conflicted() {
    const shell = await installShell({
      write: async () =>
        refused("comment_changed", "This comment changed on GitHub while you were editing.", {
          current: { field: "body", value: "Theirs" },
        }),
    });
    const { wrapper } = setup();
    const card = renderCard(wrapper);
    const view = render(
      <TooltipProvider>
        <Card />
      </TooltipProvider>,
      { wrapper },
    );
    editAndSave(card.result, "Mine");
    await waitFor(() => expect(card.result.current.editing.entry.conflict).not.toBeNull());
    expect(shell.writes).toHaveLength(1);
    return { shell, card, view };
  }

  /** ⌘↵ in the composer; jsdom isn't a Mac, so the chord is Control+Enter. */
  function commandEnter(target: Element) {
    act(() => {
      target.dispatchEvent(
        new KeyboardEvent("keydown", {
          key: "Enter",
          ctrlKey: true,
          bubbles: true,
          cancelable: true,
        }),
      );
    });
  }

  it("sends nothing from Save's callback, as ⌘K's Save Comment Edit calls it", async () => {
    const { shell, card } = await conflicted();
    act(() => card.result.current.editing.actions!.save());
    expect(shell.writes).toHaveLength(1);
  });

  it("sends nothing on ⌘↵ in Write or in Preview", async () => {
    const { shell } = await conflicted();
    const box = screen.getByRole("textbox", { name: "Edit comment" });
    commandEnter(box);
    expect(shell.writes).toHaveLength(1);
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Preview" }));
    commandEnter(screen.getByRole("tabpanel", { name: "Preview" }));
    expect(shell.writes).toHaveLength(1);
    // Keep Mine makes it saveable again.
    fireEvent.click(screen.getByRole("button", { name: "Keep Mine" }));
    fireEvent.mouseDown(screen.getByRole("tab", { name: "Write" }));
    commandEnter(screen.getByRole("textbox", { name: "Edit comment" }));
    await waitFor(() => expect(shell.writes).toHaveLength(2));
  });
});

describe("changes to comments the lists no longer show", () => {
  function showRecoveries(wrapper: ReturnType<typeof setup>["wrapper"], listed: string[]) {
    return render(
      <TooltipProvider>
        <PullRequestCommentRecoveries
          conversationId={SESSION}
          prUrl={PR}
          listedIds={new Set(listed)}
          active
        />
      </TooltipProvider>,
      { wrapper },
    );
  }
  const recoveryRows = () =>
    Object.values(usePaletteCommandsStore.getState().groups)
      .flatMap((group) => group.commands)
      .filter((command) => command.id.startsWith("pull-request-comment-recovery:"))
      .map((command) => command.label);

  it("keeps an unconfirmed edit in reach once its card is gone, and still held", async () => {
    await installShell({
      write: async () => UNKNOWN,
      read: async () => commentState({ state: "unreadable" }),
    });
    const { wrapper } = setup();
    const card = renderCard(wrapper);
    editAndSave(card.result, "After");
    await waitFor(() => expect(card.result.current.editing.entry.unknown).not.toBeNull());
    // While the comment is listed, its own card shows it; nothing more here.
    const listed = showRecoveries(wrapper, ["IC_1"]);
    expect(screen.queryByRole("group")).toBeNull();
    listed.unmount();
    // The refreshed list leaves the comment out, and its card unmounts.
    card.unmount();
    showRecoveries(wrapper, []);
    const group = screen.getByRole("group", {
      name: "GitHub didn’t confirm a change to a comment that isn’t listed here.",
    });
    expect(within(group).getByText("After")).toBeVisible();
    expect(recoveryRows()).toEqual([
      "Check Comment Again",
      "Copy Unsaved Comment Text",
      "Discard Unsaved Comment Text…",
    ]);
    // Discarding the text, confirmed, keeps the hold for Check Again.
    fireEvent.click(within(group).getByRole("button", { name: "Discard" }));
    fireEvent.click(
      within(screen.getByRole("dialog", { name: "Discard this text?" })).getByRole("button", {
        name: "Discard",
      }),
    );
    expect(screen.queryByText("After")).toBeNull();
    expect(screen.getByRole("button", { name: "Check Again" })).toBeVisible();
    expect(readCommentEdit(commentEditKey("user-1", PR, "IC_1")).unknown).not.toBeNull();
  });

  it("keeps the text of an edit whose comment was deleted, and an unsaved edit", async () => {
    await installShell({
      write: async () => refused("comment_deleted", "This comment was deleted on GitHub."),
    });
    const { wrapper } = setup();
    const card = renderCard(wrapper);
    editAndSave(card.result, "After");
    await waitFor(() => expect(card.result.current.editing.entry.tombstone).not.toBeNull());
    card.unmount();
    showRecoveries(wrapper, []);
    expect(
      screen.getByRole("group", {
        name: "A comment you were editing was deleted on GitHub. Your edit wasn’t saved.",
      }),
    ).toBeVisible();
    expect(screen.getByText("After")).toBeVisible();
  });

  it("lists nothing for an editor opened without changes", async () => {
    await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const card = renderCard(wrapper);
    act(() => card.result.current.editing.actions!.start());
    card.unmount();
    showRecoveries(wrapper, []);
    expect(screen.queryByRole("group")).toBeNull();
  });
});

describe("a refresh of the PR's comments that leaves an edited comment out", () => {
  it("moves its kept text from the card to the list at the top of Comments", async () => {
    await installShell({
      write: async () => UNKNOWN,
      read: async () => commentState({ state: "absent" }),
    });
    const { wrapper: Base, queryClient, macKey } = setup();
    const wrapper = ({ children }: { children: ReactNode }) => (
      <Base>
        <TooltipProvider>{children}</TooltipProvider>
      </Base>
    );
    // The Comments tab as the panel builds it: the listed comments, and the list of the rest.
    function Comments() {
      const { data } = useQuery<PullRequestInfo>({ queryKey: macKey, enabled: false });
      const comments = data?.pr?.comments ?? [];
      return (
        <>
          <PullRequestCommentRecoveries
            conversationId={SESSION}
            prUrl={PR}
            listedIds={new Set(comments.flatMap((comment) => (comment.id ? [comment.id] : [])))}
            active
          />
          {comments.map((comment) => (
            <ConversationEntry
              key={comment.id}
              comment={comment}
              context={CONTEXT}
              host="github.com"
              pullRequestAuthor={null}
              pullRequestUrl={PR}
            />
          ))}
        </>
      );
    }
    render(<Comments />, { wrapper });
    const { result } = renderCard(wrapper);
    editAndSave(result, "Unsaved edit");
    await waitFor(() => expect(result.current.editing.entry.unknown).not.toBeNull());
    act(() => result.current.editing.actions!.checkAgain());
    await waitFor(() =>
      expect(
        screen.getByText("This comment was deleted on GitHub. Your edit wasn’t saved."),
      ).toBeVisible(),
    );
    await act(async () => {
      queryClient.setQueryData(macKey, {
        available: true,
        selected_pr_url: PR,
        pr: { url: PR, comments: [] },
      });
    });
    const kept = await screen.findByRole("group", {
      name: "A comment you were editing was deleted on GitHub. Your edit wasn’t saved.",
    });
    expect(screen.queryByRole("article")).toBeNull();
    expect(within(kept).getByText("Unsaved edit")).toBeVisible();
    expect(within(kept).getByRole("button", { name: "Copy Text" })).toBeVisible();
  });
});

describe("Discard in the list of changes no longer shown", () => {
  function seed() {
    const mine = commentEditKey("user-1", PR, "IC_1");
    updateCommentEdit(mine, () => ({ ...EMPTY_ENTRY, tombstone: { text: "Owner A's text" } }));
    updateCommentEdit(commentEditKey("user-2", PR, "IC_2"), () => ({
      ...EMPTY_ENTRY,
      tombstone: { text: "Owner B's text" },
    }));
    return mine;
  }
  function show(prUrl = PR) {
    return (
      <TooltipProvider>
        <PullRequestCommentRecoveries
          conversationId={SESSION}
          prUrl={prUrl}
          listedIds={new Set()}
          active
        />
      </TooltipProvider>
    );
  }

  it("closes when the workspace user changes, and never discards the last user's text", async () => {
    await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const mine = seed();
    render(show(), { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    expect(screen.getByRole("dialog")).toBeVisible();
    signInAs("user-2");
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(screen.getByText("Owner B's text")).toBeVisible();
    expect(readCommentEdit(mine).tombstone?.text).toBe("Owner A's text");
  });

  it("closes when the panel moves to another pull request", async () => {
    await installShell({ write: async () => edited("x") });
    const { wrapper } = setup();
    const mine = seed();
    const view = render(show(), { wrapper });
    fireEvent.click(screen.getByRole("button", { name: "Discard" }));
    view.rerender(show("https://github.com/acme/app/pull/43"));
    expect(screen.queryByRole("dialog")).toBeNull();
    view.rerender(show());
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(readCommentEdit(mine).tombstone?.text).toBe("Owner A's text");
  });
});

describe("an answer retired while its reads were being cancelled", () => {
  it("isn't applied, and the change stays held", async () => {
    const answer = deferred<unknown>();
    await installShell({ write: () => answer.promise });
    const { wrapper, queryClient, bodies, macKey } = setup();
    const cancellation = deferred<void>();
    const cancel = vi
      .spyOn(queryClient, "cancelQueries")
      .mockImplementation(() => cancellation.promise);
    const card = renderCard(wrapper);
    editAndSave(card.result, "After");
    await act(async () => answer.resolve(edited("After")));
    await waitFor(() => expect(cancel).toHaveBeenCalled());
    act(() => setCommentEditsFlagForTests(false));
    expect(card.result.current.editing.entry.unknown).not.toBeNull();
    await act(async () => cancellation.resolve());
    expect(bodies(macKey)).toEqual(["Before"]);
    expect(card.result.current.editing.entry.unknown).not.toBeNull();
  });
});

describe("a draft in the pending review", () => {
  const DRAFT = {
    id: "PRRC_draft",
    author: "mac-octo",
    body: "Before",
    created_at: null,
    url: null,
    can_update: true,
    can_delete: true,
    pending: true,
    pending_review_id: "PRR_pending",
  };
  const THREAD = {
    id: "PRRT_1",
    path: "a.ts",
    can_reply: false,
    comments: { data: [DRAFT], has_more: false, next_cursor: null },
  } as unknown as ReviewThreadsPage["data"][number];
  const draftEdited = (body: string) => ({
    ok: true,
    data: { object: "session.github.pr_comment_edit", comment_id: "PRRC_draft", body },
    actor: { login: "mac-octo" },
  });

  /** A shell that offers pending reviews, with comment edits off unless `commentEdits`. */
  async function pendingShell(
    write: (operation: string) => Promise<unknown>,
    commentEdits = false,
  ) {
    const shell = await installShell({
      write,
      writes: [...REVIEW_WRITE_OPERATIONS, ...PENDING_REVIEW_WRITE_OPERATIONS, PENDING_REVIEW_READ],
    });
    setCommentEditsFlagForTests(commentEdits);
    setReviewsFlagForTests(true);
    setPendingReviewsFlagForTests(true);
    return shell;
  }
  const renderDraft = (wrapper: ReturnType<typeof setup>["wrapper"]) =>
    renderHook(() => useReviewCommentCard(DRAFT, THREAD, CONTEXT, PR), { wrapper });

  it("offers Edit and Delete where pending reviews are offered, whatever the comment edits flag", async () => {
    await pendingShell(async () => draftEdited("x"));
    const { wrapper } = setup();
    const { result } = renderDraft(wrapper);
    expect(result.current.editing.actions).toMatchObject({ canEdit: true, canDelete: true });
    act(() => setPendingReviewsFlagForTests(false));
    expect(result.current.editing.actions).toBeNull();
  });

  it("edits it through the pending review, never as a published comment", async () => {
    const shell = await pendingShell(async () => draftEdited("After"), true);
    const { wrapper } = setup();
    const { result } = renderDraft(wrapper);
    act(() => result.current.editing.actions!.start());
    act(() => result.current.editing.setText("After"));
    act(() => result.current.editing.actions!.save());
    expect(shell.writes).toEqual([
      {
        operation: "pull_request_review_draft_edit",
        params: {
          repository: "acme/app",
          number: 42,
          comment_id: "PRRC_draft",
          pending_review_id: "PRR_pending",
          body: "After",
          expected: "Before",
        },
      },
    ]);
    await waitFor(() => expect(result.current.editing.entry.editing).toBeNull());
  });

  it("deletes it through the pending review", async () => {
    const shell = await pendingShell(async () => deleted("PRRC_draft"));
    const { wrapper } = setup();
    const { result } = renderDraft(wrapper);
    act(() => result.current.editing.confirmDelete());
    expect(shell.writes).toEqual([
      {
        operation: "pull_request_review_draft_delete",
        params: {
          repository: "acme/app",
          number: 42,
          comment_id: "PRRC_draft",
          pending_review_id: "PRR_pending",
        },
      },
    ]);
  });

  it("holds an edit GitHub didn't confirm, and checks it by reading the draft", async () => {
    const shell = await pendingShell(async () => UNKNOWN);
    const { wrapper } = setup();
    const { result } = renderDraft(wrapper);
    act(() => result.current.editing.actions!.start());
    act(() => result.current.editing.setText("After"));
    act(() => result.current.editing.actions!.save());
    await waitFor(() => expect(result.current.editing.actions?.unconfirmed).toBe(true));
    act(() => result.current.editing.actions!.checkAgain());
    await waitFor(() => expect(shell.reads).toHaveLength(1));
    expect(shell.reads[0]!.params).toMatchObject({ comment_id: "PRRC_draft", kind: "review" });
  });
});
