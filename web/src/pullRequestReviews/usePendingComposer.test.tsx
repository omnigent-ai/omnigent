// A review conversation's box with pending reviews on, through the real stores over a stand-in
// shell: what it offers, what each send does with the draft, and how a held add and a half-done
// start keep to their own place, review and Mac account.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, render, renderHook, screen, waitFor } from "@testing-library/react";
import { type ReactNode, useMemo, useState } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("@/lib/identity", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  getCurrentUserId: () => "user-1",
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import { MacReadAsContext } from "@/hooks/macReadAs";
import { ThreadEntry } from "@/pullRequestComments/PullRequestCommentsEntries";
import type { PullRequestCommentDraft } from "@/newPullRequestPage/hooks/usePullRequests";
import type { MacNamedReader } from "@/lib/macNamedReads";
import type { ReviewThread } from "@/lib/pullRequestReviewThreads";
import {
  PENDING_REVIEW_READ,
  PENDING_REVIEW_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setPendingReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import { BOB, installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";
import {
  BASE,
  draftThread,
  HEAD,
  noReviewAnswer,
  pendingAnswer,
  REVIEW_ID,
} from "@/test/pendingReviewAnswers";

import { pendingRecoveriesOf, resetPendingRecoveriesForTests } from "./pendingComposer";
import type { DraftPlace } from "./pendingReview";
import { pendingHoldsOf, resetPendingReviewChangesForTests } from "./pendingReviewChanges";
import { ADDED_NEWER_KEPT, MOVED_BEFORE_ANSWER, usePendingComposer } from "./usePendingComposer";

const PR = "https://github.com/acme/app/pull/42";
const KEY = ["github-comment-draft", "user-1", "session-1", PR, "PRRT_1"] as const;
const REPLY: DraftPlace = { kind: "reply", threadId: "PRRT_1" };
const BODY = "Rename this.";
const UNKNOWN = { ok: false, error: { code: "outcome_unknown", message: "Unconfirmed." } };
const PARTIAL = {
  ok: false,
  error: { code: "partial", cause: "head_changed", message: "Started, not added." },
  steps: {
    completed: [{ step: "start_review", review_id: REVIEW_ID }],
    failed: { step: "comment" },
    not_attempted: [],
  },
};
const REPLY_BASELINE = {
  review_id: REVIEW_ID,
  thread_id: "PRRT_1",
  root_comment_id: "PRRC_root",
  complete: true,
  known_comment_ids: [],
};
const ADDED = {
  ok: true,
  actor: { login: "alice" },
  data: {
    object: "session.github.pr_review_draft",
    pr_url: PR,
    review_id: REVIEW_ID,
    thread_id: "PRRT_1",
    comment: {
      id: "PRRC_new",
      author: "alice",
      body: BODY,
      created_at: null,
      url: null,
      pending: true,
    },
  },
};

let client: QueryClient;
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={client}>{children}</QueryClientProvider>
);

/** A shell offering pending reviews, its next write answered by `answers` in turn. */
async function ready(answers: unknown[], read: { current: unknown }) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const queue = [...answers];
  const shell = installNamedWriteShell(() => queue.shift() ?? UNKNOWN, {
    extraWrites: [...PENDING_REVIEW_WRITE_OPERATIONS, PENDING_REVIEW_READ],
    reads: { [PENDING_REVIEW_READ]: () => read.current },
  });
  await shell.ready(PR, { asBob: false });
  setPendingReviewsFlagForTests(true);
  client.setQueryData<PullRequestCommentDraft>(KEY, { body: BODY, pending: false, error: null });
  return shell;
}

function box(post = vi.fn(), leads?: "comment" | "review") {
  const hook = renderHook(
    () =>
      usePendingComposer({
        conversationId: "session-1",
        prUrl: PR,
        place: REPLY,
        draftKey: KEY,
        head: HEAD,
        post,
        commentLabel: leads === "review" ? "Comment" : "Reply",
        leads,
      }),
    { wrapper },
  );
  return { hook, post };
}

const draft = () => client.getQueryData<PullRequestCommentDraft>(KEY)!;
const writes = (shell: Awaited<ReturnType<typeof ready>>) =>
  shell.codeHostWrite.mock.calls.map(([request]) => request.params);
const readAgain = async (hook: ReturnType<typeof box>["hook"]) => {
  await act(async () => {
    await client.refetchQueries({ queryKey: ["github-pending-review"] });
  });
  await waitFor(() => expect(hook.result.current).not.toBeNull());
};

afterEach(() => {
  client?.clear();
  resetPendingRecoveriesForTests();
  resetPendingReviewChangesForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("with no review pending", () => {
  it("leads with Start a review where asked, as a new line comment does, Comment beside it", async () => {
    const read = { current: noReviewAnswer() as unknown };
    const shell = await ready([ADDED], read);
    const { hook, post } = box(vi.fn(), "review");
    // While it checks, the filled action already says what it will be.
    expect(hook.result.current?.primary.label).toBe("Start a review");
    await waitFor(() => expect(hook.result.current?.mode).toBe("comment_or_start"));
    expect(hook.result.current!.primary.label).toBe("Start a review");
    expect(hook.result.current!.secondary!.label).toBe("Comment");
    act(() => hook.result.current!.secondary!.run());
    expect(post).toHaveBeenCalledOnce();
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(draft().body).toBe(""));
    expect(writes(shell)).toHaveLength(1);
    expect(writes(shell)[0]).toMatchObject({ pending_review_id: null });
  });

  it("replies at once, or starts a review with the reply, against the head shown", async () => {
    const read = { current: noReviewAnswer() as unknown };
    const shell = await ready([ADDED], read);
    const { hook, post } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("comment_or_start"));
    expect(hook.result.current!.primary.label).toBe("Reply");
    act(() => hook.result.current!.primary.run());
    expect(post).toHaveBeenCalledOnce();
    act(() => hook.result.current!.secondary!.run());
    await waitFor(() => expect(draft().body).toBe(""));
    expect(writes(shell)).toEqual([
      {
        repository: "acme/app",
        number: 42,
        body: BODY,
        thread_id: "PRRT_1",
        pending_review_id: null,
        expected_head: HEAD,
      },
    ]);
  });

  it("is today's box where pending reviews aren't offered", async () => {
    await ready([], { current: noReviewAnswer() });
    setPendingReviewsFlagForTests(false);
    expect(box().hook.result.current).toBeNull();
  });
});

describe("with a review pending", () => {
  it("only adds to it, and clears the draft it sent once GitHub says so", async () => {
    const read = { current: pendingAnswer() as unknown };
    const shell = await ready([ADDED], read);
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    expect(hook.result.current!.secondary).toBeNull();
    act(() => hook.result.current!.primary.run());
    expect(writes(shell)[0]).toMatchObject({ pending_review_id: REVIEW_ID });
    await waitFor(() => expect(draft().body).toBe(""));
  });
});

describe("a start that made the review but not the comment", () => {
  it("adds only to that review while the read is a stale none or fails", async () => {
    const read = { current: noReviewAnswer() as unknown };
    const shell = await ready([PARTIAL, ADDED], read);
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("comment_or_start"));
    // Every read since the start fails, so the box still has the none from before it.
    read.current = { object: "session.github.pr_pending_review" };
    act(() => hook.result.current!.secondary!.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("recovering"));
    await readAgain(hook);
    expect(hook.result.current).toMatchObject({
      mode: "recovering",
      primary: { label: "Add to review", disabled: false },
      secondary: null,
    });
    expect(draft().body).toBe(BODY);
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(draft().body).toBe(""));
    expect(writes(shell)[1]).toMatchObject({ pending_review_id: REVIEW_ID });
  });

  it("keeps the pin once the box is closed and opened again", async () => {
    const read = { current: noReviewAnswer() as unknown };
    await ready([PARTIAL], read);
    const first = box();
    await waitFor(() => expect(first.hook.result.current?.mode).toBe("comment_or_start"));
    read.current = pendingAnswer();
    act(() => first.hook.result.current!.secondary!.run());
    await waitFor(() => expect(first.hook.result.current?.mode).toBe("recovering"));
    first.hook.unmount();
    const again = box();
    await waitFor(() => expect(again.hook.result.current?.mode).toBe("recovering"));
  });

  it("says when that review is no longer pending, and leaves the text for the person", async () => {
    const read = { current: noReviewAnswer() as unknown };
    await ready([PARTIAL], read);
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("comment_or_start"));
    // GitHub has the review the start made.
    read.current = pendingAnswer();
    act(() => hook.result.current!.secondary!.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("recovering"));
    // Submitted elsewhere, and another review started: a read sent now names that one.
    read.current = pendingAnswer({ reviewId: "PRR_other" });
    await readAgain(hook);
    await waitFor(() => expect(hook.result.current?.mode).toBe("recovery_gone"));
    expect(hook.result.current!.primary.disabled).toBe(true);
    expect(draft().body).toBe(BODY);
    const use = hook.result.current!.notice!.actions.find(
      (action) => action.label === "Use for a new comment",
    )!;
    act(() => use.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
  });
});

describe("an add GitHub didn't confirm", () => {
  it("holds the box, and clears its draft only once a check finds it, as sent", async () => {
    const read = { current: pendingAnswer() as unknown };
    await ready([{ ...UNKNOWN, baseline: REPLY_BASELINE }], read);
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("held"));
    expect(hook.result.current!.primary.disabled).toBe(true);
    read.current = pendingAnswer({
      threads: [draftThread("PRRT_1", [{ id: "PRRC_new", body: BODY, reply_to_id: "PRRC_root" }])],
    });
    const check = hook.result.current!.notice!.actions.find(
      (action) => action.label === "Check again",
    )!;
    act(() => check.run());
    await waitFor(() => expect(draft().body).toBe(""));
    expect(pendingHoldsOf("user-1", PR)).toEqual([]);
  });

  it("leaves a draft cancelled and written again with the same text", async () => {
    const read = { current: pendingAnswer() as unknown };
    await ready([{ ...UNKNOWN, baseline: REPLY_BASELINE }], read);
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("held"));
    // Written again: the same text, but not the draft that was sent.
    client.setQueryData<PullRequestCommentDraft>(KEY, { body: BODY, pending: false, error: null });
    read.current = pendingAnswer({
      threads: [draftThread("PRRT_1", [{ id: "PRRC_new", body: BODY, reply_to_id: "PRRC_root" }])],
    });
    const check = hook.result.current!.notice!.actions.find(
      (action) => action.label === "Check again",
    )!;
    act(() => check.run());
    await waitFor(() => expect(draft().error).toBe(ADDED_NEWER_KEPT));
    expect(draft().body).toBe(BODY);
  });

  it("after a start stays held, not a known-unsent start, even once the review goes", async () => {
    const read = { current: noReviewAnswer() as unknown };
    await ready(
      [
        {
          ...UNKNOWN,
          steps: {
            completed: [{ step: "start_review", review_id: REVIEW_ID }],
            uncertain: { step: "comment" },
            not_attempted: [],
          },
          baseline: REPLY_BASELINE,
        },
      ],
      read,
    );
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("comment_or_start"));
    act(() => hook.result.current!.secondary!.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("held"));
    read.current = noReviewAnswer();
    await readAgain(hook);
    expect(hook.result.current?.mode).toBe("held");
  });

  it("is hidden while another account writes here, and back on its return", async () => {
    const read = { current: pendingAnswer() as unknown };
    const shell = await ready([{ ...UNKNOWN, baseline: REPLY_BASELINE }], read);
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("held"));
    shell.switchActiveAccount("carol");
    await act(async () => shell.readAsHost(PR));
    await waitFor(() => expect(hook.result.current?.mode).not.toBe("held"));
    shell.switchActiveAccount("alice");
    await act(async () => shell.readAsHost(PR));
    await waitFor(() => expect(hook.result.current?.mode).toBe("held"));
  });
});

describe("an answer after the account changed", () => {
  it("keeps the text and applies nothing of it", async () => {
    let answer!: (value: unknown) => void;
    const read = { current: pendingAnswer() as unknown };
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      read,
    );
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    shell.switchActiveAccount("carol");
    await act(async () => shell.readAsHost(PR));
    answer(ADDED);
    await waitFor(() => expect(draft().error).toBe(MOVED_BEFORE_ANSWER));
    expect(draft()).toMatchObject({ body: BODY, pending: false });
  });
});

describe("an answer for a view the page has left", () => {
  it("never clears the draft once the page reads as another login, though the first is still signed in", async () => {
    let answer!: (value: unknown) => void;
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { current: pendingAnswer() },
    );
    await shell.readAsBob(PR);
    let choose!: (reader: MacNamedReader | null) => void;
    function Scoped({ children }: { children: ReactNode }) {
      const [reader, setReader] = useState<MacNamedReader | null>(BOB);
      const value = useMemo(() => ({ reader }), [reader]);
      choose = setReader;
      return (
        <QueryClientProvider client={client}>
          <MacReadAsContext.Provider value={value}>{children}</MacReadAsContext.Provider>
        </QueryClientProvider>
      );
    }
    const hook = renderHook(
      () =>
        usePendingComposer({
          conversationId: "session-1",
          prUrl: PR,
          place: REPLY,
          draftKey: KEY,
          head: HEAD,
          post: vi.fn(),
          commentLabel: "Reply",
        }),
      { wrapper: Scoped },
    );
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    // The page goes back to Automatic, reading as the host's account; Bob is still signed in.
    act(() => choose(null));
    // And returns to Bob before the answer: a view left and come back to is still not the one.
    act(() => choose(BOB));
    answer({ ...ADDED, actor: { login: "bob" } });
    await waitFor(() => expect(draft().pending).toBe(false));
    expect(draft()).toMatchObject({ body: BODY, error: MOVED_BEFORE_ANSWER });
  });
});

describe("a retry of a half-done start", () => {
  it("retires the start's pin once Check again finds the comment", async () => {
    const read = { current: noReviewAnswer() as unknown };
    await ready([PARTIAL, { ...UNKNOWN, baseline: REPLY_BASELINE }], read);
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("comment_or_start"));
    read.current = pendingAnswer();
    act(() => hook.result.current!.secondary!.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("recovering"));
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("held"));
    read.current = pendingAnswer({
      threads: [draftThread("PRRT_1", [{ id: "PRRC_new", body: BODY, reply_to_id: "PRRC_root" }])],
    });
    act(() =>
      hook.result.current!.notice!.actions.find((each) => each.label === "Check again")!.run(),
    );
    await waitFor(() => expect(draft().body).toBe(""));
    expect(pendingRecoveriesOf("user-1", PR)).toEqual([]);
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
  });
});

describe("an older check finishing during a newer send", () => {
  it("leaves the newer send's draft exactly as it is", async () => {
    const lines12: DraftPlace = {
      kind: "lines",
      path: "src/app.ts",
      side: "RIGHT",
      line: 12,
      startLine: null,
      headSha: HEAD,
      baseSha: BASE,
    };
    const lines20: DraftPlace = { ...lines12, line: 20 };
    const key = ["github-new-review-thread", "user-1", "session-1", PR, "src/app.ts"] as const;
    const anchor = (line: number) => ({
      path: "src/app.ts",
      side: "RIGHT" as const,
      line,
      start_line: null,
      head_sha: HEAD,
      base_sha: BASE,
    });
    const read = { current: pendingAnswer() as unknown };
    const shell = await ready(
      [
        {
          ...UNKNOWN,
          baseline: {
            review_id: REVIEW_ID,
            path: "src/app.ts",
            line: 12,
            start_line: null,
            complete: true,
            known_comment_ids: [],
          },
        },
        new Promise(() => {}),
      ],
      read,
    );
    client.setQueryData(key, { body: BODY, pending: false, error: null, anchor: anchor(12) });
    const hook = renderHook(
      ({ place }: { place: DraftPlace }) =>
        usePendingComposer({
          conversationId: "session-1",
          prUrl: PR,
          place,
          draftKey: key,
          head: HEAD,
          post: vi.fn(),
          commentLabel: "Post comment",
        }),
      { wrapper, initialProps: { place: lines12 } },
    );
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(hook.result.current?.mode).toBe("held"));
    read.current = pendingAnswer({
      threads: [draftThread("PRRT_old", [{ id: "PRRC_old", body: BODY }])],
    });
    const paused = shell.pauseNextRead(PENDING_REVIEW_READ);
    act(() =>
      hook.result.current!.notice!.actions.find((each) => each.label === "Check again")!.run(),
    );
    await paused.started;
    // A new comment on other lines of the same file, sent while the old check runs.
    client.setQueryData(key, {
      body: "Another comment.",
      pending: false,
      error: null,
      anchor: anchor(20),
    });
    hook.rerender({ place: lines20 });
    act(() => hook.result.current!.primary.run());
    const sending = client.getQueryData<PullRequestCommentDraft>(key)!;
    expect(sending.pending).toBe(true);
    paused.release();
    await waitFor(() => expect(pendingHoldsOf("user-1", PR)).toEqual([]));
    expect(client.getQueryData<PullRequestCommentDraft>(key)).toEqual(sending);
  });
});

describe("the Comments tab's reply box", () => {
  it("adds to the pending review, as the Changes tab's does, and never replies at once", async () => {
    const shell = await ready([ADDED], { current: pendingAnswer() });
    const thread = draftThread("PRRT_1", [
      { id: "PRRC_root", body: "Published root" },
    ]) as unknown as ReviewThread;
    render(
      <TooltipProvider>
        <ThreadEntry
          thread={thread}
          context={{
            conversationId: "session-1",
            prUrl: PR,
            account: "alice",
            canReply: true,
            canComment: true,
            headSha: HEAD,
            baseSha: BASE,
            loadComments: vi.fn(),
            loadingComments: null,
          }}
          host="github.com"
          pullRequestAuthor="author"
          pullRequestUrl={PR}
          expanded
          onExpandedChange={vi.fn()}
          revealed={new Set()}
          onReveal={vi.fn()}
        />
      </TooltipProvider>,
      { wrapper },
    );
    const add = await screen.findByRole("button", { name: "Add to review" });
    expect(screen.queryByRole("button", { name: "Reply" })).not.toBeInTheDocument();
    act(() => add.click());
    await waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledOnce());
    expect(shell.codeHostWrite.mock.calls[0]![0]).toMatchObject({
      operation: "pull_request_review_draft_add",
      params: { thread_id: "PRRT_1", pending_review_id: REVIEW_ID },
    });
  });
});

describe("the box's lifecycle around an answer", () => {
  /** A box at the reply's place, in a page that reads as `reader`, shown or not. */
  function View({ reader, show }: { reader: MacNamedReader | null; show: boolean }) {
    const value = useMemo(() => ({ reader }), [reader]);
    return (
      <QueryClientProvider client={client}>
        <MacReadAsContext.Provider value={value}>{show && <Box />}</MacReadAsContext.Provider>
      </QueryClientProvider>
    );
  }
  let shown: ReturnType<typeof usePendingComposer> = null;
  function Box() {
    shown = usePendingComposer({
      conversationId: "session-1",
      prUrl: PR,
      place: REPLY,
      draftKey: KEY,
      head: HEAD,
      post: vi.fn(),
      commentLabel: "Reply",
    });
    return null;
  }

  it("retires the view when its box unmounts, so the same view mounted again never takes the old answer", async () => {
    let answer!: (value: unknown) => void;
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { current: pendingAnswer() },
    );
    await shell.readAsBob(PR);
    const view = render(<View reader={BOB} show />);
    await waitFor(() => expect(shown?.mode).toBe("add_to_review"));
    act(() => shown!.primary.run());
    view.rerender(<View reader={null} show={false} />);
    view.rerender(<View reader={BOB} show />);
    await act(async () => answer({ ...ADDED, actor: { login: "bob" } }));
    await waitFor(() => expect(draft().pending).toBe(false));
    expect(draft()).toMatchObject({ body: BODY, error: MOVED_BEFORE_ANSWER });
  });

  it("retires a half-done start a retry completed while the page was away, leaving the draft", async () => {
    const read = { current: noReviewAnswer() as unknown };
    const shell = await ready([PARTIAL, { ...UNKNOWN, baseline: REPLY_BASELINE }], read);
    await shell.readAsBob(PR);
    const view = render(<View reader={BOB} show />);
    await waitFor(() => expect(shown?.mode).toBe("comment_or_start"));
    read.current = pendingAnswer();
    act(() => shown!.secondary!.run());
    await waitFor(() => expect(shown?.mode).toBe("recovering"));
    act(() => shown!.primary.run());
    await waitFor(() => expect(shown?.mode).toBe("held"));
    read.current = pendingAnswer({
      threads: [draftThread("PRRT_1", [{ id: "PRRC_new", body: BODY, reply_to_id: "PRRC_root" }])],
    });
    const paused = shell.pauseNextRead(PENDING_REVIEW_READ);
    act(() => shown!.notice!.actions.find((each) => each.label === "Check again")!.run());
    await paused.started;
    view.rerender(<View reader={null} show />);
    paused.release();
    await waitFor(() => expect(pendingHoldsOf("user-1", PR)).toEqual([]));
    view.rerender(<View reader={BOB} show />);
    expect(pendingRecoveriesOf("user-1", PR)).toEqual([]);
    expect(draft().body).toBe(BODY);
    await waitFor(() => expect(shown?.mode).toBe("add_to_review"));
  });

  it("retires a half-done start a retry added while the page was away", async () => {
    let answer!: (value: unknown) => void;
    const read = { current: noReviewAnswer() as unknown };
    const shell = await ready(
      [
        PARTIAL,
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      read,
    );
    await shell.readAsBob(PR);
    const view = render(<View reader={BOB} show />);
    await waitFor(() => expect(shown?.mode).toBe("comment_or_start"));
    read.current = pendingAnswer();
    act(() => shown!.secondary!.run());
    await waitFor(() => expect(shown?.mode).toBe("recovering"));
    act(() => shown!.primary.run());
    view.rerender(<View reader={null} show />);
    await act(async () => answer({ ...ADDED, actor: { login: "bob" } }));
    await waitFor(() => expect(pendingRecoveriesOf("user-1", PR)).toEqual([]));
    expect(draft()).toMatchObject({ body: BODY, pending: false, error: MOVED_BEFORE_ANSWER });
  });

  it("ends its own wait when an editor changed the draft before the answer, keeping that text", async () => {
    let answer!: (value: unknown) => void;
    await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { current: pendingAnswer() },
    );
    const { hook } = box();
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    // As the composer's setBody does: a new body, and the sent snapshot's id dropped.
    client.setQueryData<PullRequestCommentDraft>(KEY, {
      ...draft(),
      body: "A later edit.",
      sentId: undefined,
    });
    await act(async () => answer(ADDED));
    await waitFor(() => expect(draft().pending).toBe(false));
    expect(draft()).toMatchObject({ body: "A later edit.", error: ADDED_NEWER_KEPT });
  });
});

describe("a new view's own send", () => {
  const KEY_FILE = ["github-new-review-thread", "user-1", "session-1", PR, "src/app.ts"] as const;
  const at = (line: number, startLine: number | null = null): DraftPlace => ({
    kind: "lines",
    path: "src/app.ts",
    side: "RIGHT",
    line,
    startLine,
    headSha: HEAD,
    baseSha: BASE,
  });
  const anchorAt = (place: Extract<DraftPlace, { kind: "lines" }>) => ({
    path: place.path,
    side: place.side,
    line: place.line,
    start_line: place.startLine,
    head_sha: place.headSha,
    base_sha: place.baseSha,
  });
  const ADDED_THREAD = (place: Extract<DraftPlace, { kind: "lines" }>) => ({
    ok: true,
    actor: { login: "alice" },
    data: {
      object: "session.github.pr_review_draft",
      pr_url: PR,
      review_id: REVIEW_ID,
      thread: draftThread("PRRT_new", [{ id: "PRRC_new", body: BODY }], {
        line: place.line,
        start_line: place.startLine,
      }),
    },
  });

  it.each([
    ["other lines", at(20)],
    ["a range", at(24, 20)],
  ])("clears the draft once its add lands after selecting %s first", async (_label, chosen) => {
    const lines = chosen as Extract<DraftPlace, { kind: "lines" }>;
    await ready([ADDED_THREAD(lines)], { current: pendingAnswer() });
    client.setQueryData<PullRequestCommentDraft>(KEY_FILE, {
      body: BODY,
      pending: false,
      error: null,
      anchor: anchorAt(at(12) as Extract<DraftPlace, { kind: "lines" }>),
    });
    const hook = renderHook(
      ({ place }: { place: DraftPlace }) =>
        usePendingComposer({
          conversationId: "session-1",
          prUrl: PR,
          place,
          draftKey: KEY_FILE,
          head: HEAD,
          post: vi.fn(),
          commentLabel: "Post comment",
        }),
      { wrapper, initialProps: { place: at(12) } },
    );
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    // The person selects other lines before sending, as selectLines does.
    client.setQueryData<PullRequestCommentDraft>(KEY_FILE, {
      ...client.getQueryData<PullRequestCommentDraft>(KEY_FILE)!,
      anchor: anchorAt(lines),
    });
    hook.rerender({ place: lines });
    act(() => hook.result.current!.primary.run());
    await waitFor(() =>
      expect(client.getQueryData<PullRequestCommentDraft>(KEY_FILE)!.body).toBe(""),
    );
    expect(client.getQueryData<PullRequestCommentDraft>(KEY_FILE)!.error).toBeNull();
  });

  it("clears the draft for a send made after the page switched scope and came back", async () => {
    const shell = await ready([{ ...ADDED, actor: { login: "bob" } }], {
      current: pendingAnswer(),
    });
    await shell.readAsBob(PR);
    let choose!: (reader: MacNamedReader | null) => void;
    function Scoped({ children }: { children: ReactNode }) {
      const [reader, setReader] = useState<MacNamedReader | null>(BOB);
      const value = useMemo(() => ({ reader }), [reader]);
      choose = setReader;
      return (
        <QueryClientProvider client={client}>
          <MacReadAsContext.Provider value={value}>{children}</MacReadAsContext.Provider>
        </QueryClientProvider>
      );
    }
    const hook = renderHook(
      () =>
        usePendingComposer({
          conversationId: "session-1",
          prUrl: PR,
          place: REPLY,
          draftKey: KEY,
          head: HEAD,
          post: vi.fn(),
          commentLabel: "Reply",
        }),
      { wrapper: Scoped },
    );
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => choose(null));
    act(() => choose(BOB));
    // Warm cache: the box is back on Bob's pending review at once, and this send is its own.
    await waitFor(() => expect(hook.result.current?.mode).toBe("add_to_review"));
    act(() => hook.result.current!.primary.run());
    await waitFor(() => expect(draft().body).toBe(""));
    expect(draft().error).toBeNull();
  });
});
