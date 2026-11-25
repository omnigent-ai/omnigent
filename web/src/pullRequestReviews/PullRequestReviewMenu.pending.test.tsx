// The Review menu while the account has a pending review, with the real stores over a stand-in
// shell: Finish review · N, its verdict and summary rules, Discard after confirming, and a
// submit or discard GitHub didn't confirm coming first, pinned to its review and scope.

import type * as SonnerModule from "sonner";
import { notifyManager, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { toast } from "sonner";
import { afterEach, describe, expect, it, vi } from "vitest";

vi.mock("sonner", async (importOriginal) => ({
  ...(await importOriginal<typeof SonnerModule>()),
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    loading: vi.fn(() => "progress"),
    warning: vi.fn(),
    dismiss: vi.fn(),
  }),
}));
vi.mock("@/lib/identity", async (original) => ({
  ...(await original<Record<string, unknown>>()),
  getCurrentUserId: () => "user-1",
}));

import { TooltipProvider } from "@/components/ui/tooltip";
import { PullRequestActionOriginContext } from "@/lib/pullRequestActionOrigin";
import { gitProviderCopy } from "@/lib/gitProviders";
import {
  PENDING_REVIEW_READ,
  PENDING_REVIEW_STATE_READ,
  PENDING_REVIEW_WRITE_OPERATIONS,
  resetDesktopPullRequestWritesForTests,
  setPendingReviewsFlagForTests,
} from "@/lib/desktopPullRequestWrites";
import { resetDesktopReadPathForTests } from "@/lib/desktopReadPath";
import { resetMacNamedReadsForTests } from "@/lib/macNamedReads";
import { resetMacReadScopesForTests } from "@/lib/macReadScope";
import type { PullRequestDetailsState } from "@/pullRequestPage/detailsLayout";
import { usePaletteCommandsStore } from "@/store/paletteCommands";
import { installNamedWriteShell, removeNamedWriteShell } from "@/test/macNamedWriteShell";
import {
  HEAD,
  noReviewAnswer,
  OLDER,
  pendingAnswer,
  REVIEW_ID,
  unplacedDraft,
} from "@/test/pendingReviewAnswers";

import { resetPendingRecoveriesForTests } from "./pendingComposer";
import { pendingHoldsOf, resetPendingReviewChangesForTests } from "./pendingReviewChanges";
import {
  editPendingSummary,
  pendingSummaryKey,
  readPendingSummary,
  resetPendingSummariesForTests,
} from "./pendingReviewSummary";
import { PullRequestReviewMenu } from "./PullRequestReviewMenu";
import {
  pendingSubmitKey,
  readReviewSubmit,
  resetReviewSubmitForTests,
  reviewSubmitKey,
  reviewSubmitsOf,
  updateReviewSubmit,
} from "./reviewSubmit";
import { approvalGuardEntry } from "./sendReview";
import { invalidatePendingReview } from "./usePendingReview";

const PR = "https://github.com/acme/app/pull/42";
const UNKNOWN = { ok: false, error: { code: "outcome_unknown", message: "Unconfirmed." } };
const SUMMARY_KEY = pendingSummaryKey(
  "user-1",
  PR,
  { login: "alice", namedReader: null },
  REVIEW_ID,
);
const twoDrafts = (fields: Parameters<typeof pendingAnswer>[0] = {}) =>
  pendingAnswer({
    unplaced: [unplacedDraft("PRRC_1"), unplacedDraft("PRRC_2")],
    body: "From GitHub.",
    ...fields,
  });
const submitted = (fields: Record<string, unknown> = {}) => ({
  ok: true,
  actor: { login: "alice" },
  data: {
    object: "session.github.pr_review",
    pr_url: PR,
    review: {
      id: REVIEW_ID,
      state: "COMMENTED",
      body: "From GitHub.",
      created_at: null,
      url: null,
      commit_oid: HEAD,
      ...fields,
    },
  },
});

let client: QueryClient;
const ORIGIN = { begin: vi.fn(() => ({ succeeded: vi.fn(), confirmedByRead: vi.fn() })) };

/** A shell offering pending reviews; writes answered by `answers` in turn, reads by `reads`. */
async function ready(
  answers: unknown[],
  reads: Record<string, () => unknown>,
  omitReads: readonly string[] = [],
) {
  client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const queue = [...answers];
  const shell = installNamedWriteShell(() => queue.shift() ?? UNKNOWN, {
    extraWrites: [
      ...PENDING_REVIEW_WRITE_OPERATIONS,
      PENDING_REVIEW_READ,
      PENDING_REVIEW_STATE_READ,
    ],
    reads,
    omitReads,
  });
  await shell.ready(PR, { asBob: false });
  setPendingReviewsFlagForTests(true);
  return shell;
}

function show() {
  const state = {
    conversationId: "conv_1",
    copy: gitProviderCopy("github"),
    repository: "acme/app",
    pr: {
      number: 42,
      url: PR,
      state: "OPEN",
      isDraft: false,
      title: "Retry uploads",
      author: "author-octo",
    },
    lifecycle: { headSha: HEAD },
    active: true,
  } as unknown as PullRequestDetailsState;
  return render(
    <QueryClientProvider client={client}>
      <TooltipProvider>
        {/* As the Pull Requests page shows it, so a list can check what the menu sent. */}
        <PullRequestActionOriginContext.Provider value={ORIGIN}>
          <PullRequestReviewMenu state={state} />
        </PullRequestActionOriginContext.Provider>
      </TooltipProvider>
    </QueryClientProvider>,
  );
}

const writes = (shell: Awaited<ReturnType<typeof ready>>) =>
  shell.codeHostWrite.mock.calls.map(([request]) => request);
const paletteRows = () =>
  Object.values(usePaletteCommandsStore.getState().groups).flatMap((group) => group.commands);
const row = (label: string) => paletteRows().find((command) => command.label === label);
async function openFinish(label = /^Finish review|^Discarding review/) {
  fireEvent.click(await screen.findByRole("button", { name: label }));
  return screen.findByRole("dialog", { name: "Finish your review" });
}
const verdict = (name: string) =>
  fireEvent.click(screen.getByRole("radio", { name: new RegExp(`^${name}`) }));
const submit = () => screen.getByRole("button", { name: "Submit Review" });

afterEach(() => {
  cleanup();
  client?.clear();
  usePaletteCommandsStore.setState({ groups: {} });
  resetPendingSummariesForTests();
  resetPendingRecoveriesForTests();
  resetPendingReviewChangesForTests();
  resetReviewSubmitForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("finishing a pending review", () => {
  it("reads Finish review · N, seeds GitHub's summary, and submits it on the review's own commit", async () => {
    const shell = await ready([submitted()], {
      [PENDING_REVIEW_READ]: () => twoDrafts({ commit: OLDER }),
    });
    show();
    const menu = await openFinish(/^Finish review · 2/);
    expect(menu).toHaveTextContent("Submitting posts your 2 comments.");
    expect(menu).toHaveTextContent(/Review started on dddddddd?/);
    await waitFor(() =>
      expect(within(menu).getByRole("textbox", { name: "Review summary" })).toHaveTextContent(
        "From GitHub.",
      ),
    );
    fireEvent.click(submit());
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(writes(shell)[0]).toMatchObject({
      operation: "pull_request_review_submit",
      params: {
        event: "COMMENT",
        body: "From GitHub.",
        expected_head: HEAD,
        pending_review_id: REVIEW_ID,
        review_commit: OLDER,
      },
    });
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).toBeNull());
  });

  it("asks the page to show an Approve that finishes a pending review land", async () => {
    ORIGIN.begin.mockClear();
    const shell = await ready([submitted({ state: "APPROVED" })], {
      [PENDING_REVIEW_READ]: () => pendingAnswer({ body: "" }),
    });
    show();
    await openFinish();
    verdict("Approve");
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(writes(shell)[0]).toMatchObject({
      params: { event: "APPROVE", pending_review_id: REVIEW_ID },
    });
    expect(ORIGIN.begin).toHaveBeenCalledExactlyOnceWith(PR, expect.anything(), {
      headSha: HEAD,
      landing: "approved",
    });
  });

  it("asks for no landing when a pending review finishes as a comment", async () => {
    ORIGIN.begin.mockClear();
    const shell = await ready([submitted()], {
      [PENDING_REVIEW_READ]: () => twoDrafts(),
    });
    show();
    await openFinish();
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(ORIGIN.begin).toHaveBeenCalledExactlyOnceWith(PR, expect.anything(), {
      headSha: HEAD,
    });
  });

  it("adds the account's regular unsent summary after GitHub's, once", async () => {
    await ready([], { [PENDING_REVIEW_READ]: () => twoDrafts() });
    const regular = reviewSubmitKey("user-1", PR, "alice");
    updateReviewSubmit(regular, (entry) => ({ ...entry, body: "My regular review." }));
    show();
    const menu = await openFinish();
    const box = within(menu).getByRole("textbox", { name: "Review summary" });
    await waitFor(() => expect(box).toHaveValue("From GitHub.\n\nMy regular review."));
    expect(readReviewSubmit(regular).body).toBe("");
    // Editing the pending summary, and reading the review again, add nothing more.
    act(() => editPendingSummary(SUMMARY_KEY, "Pending summary."));
    act(() => invalidatePendingReview(client, PR));
    await waitFor(() => expect(box).toHaveValue("Pending summary."));
    expect(readReviewSubmit(regular).body).toBe("");
  });

  it.each([
    ["Request Changes", 2, "", false],
    ["Request Changes", 2, "Fix it.", true],
    ["Comment", 2, "", true],
    ["Comment", 0, "", false],
    ["Approve", 0, "", true],
  ] as const)(
    "lets %s with %i comments and summary %j go: %s",
    async (name, count, body, allowed) => {
      const answer = count === 0 ? pendingAnswer({ body }) : twoDrafts({ body });
      await ready([], { [PENDING_REVIEW_READ]: () => answer });
      show();
      await openFinish();
      if (name !== "Comment") verdict(name);
      if (allowed) await waitFor(() => expect(submit()).toBeEnabled());
      else expect(submit()).toBeDisabled();
    },
  );
});

describe("a summary typed before the review's first inline comment", () => {
  const typed = "Typed before the inline comment.";
  const summaryBox = () => screen.getByRole("textbox", { name: "Review summary" });

  /** The menu open on a regular review, then the first inline comment starts a pending one. */
  async function typeThenComment(answer: ReturnType<typeof pendingAnswer>) {
    let commented = false;
    const shell = await ready([submitted({ body: typed })], {
      [PENDING_REVIEW_READ]: () => (commented ? answer : noReviewAnswer()),
    });
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    await screen.findByRole("dialog", { name: "Submit review" });
    fireEvent.change(summaryBox(), { target: { value: typed } });
    commented = true;
    act(() => invalidatePendingReview(client, PR));
    await screen.findByRole("dialog", { name: "Finish your review" });
    return shell;
  }

  it("keeps the typed summary, and submits it with the pending review", async () => {
    const shell = await typeThenComment(pendingAnswer({ body: "" }));
    await waitFor(() => expect(summaryBox()).toHaveValue(typed));
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(writes(shell)[0]).toMatchObject({
      params: { body: typed, pending_review_id: REVIEW_ID },
    });
    expect(readReviewSubmit(reviewSubmitKey("user-1", PR, "alice")).body).toBe("");
  });
});

describe("discarding a pending review", () => {
  it("asks first, with the count, then discards it and its summary", async () => {
    const shell = await ready(
      [
        {
          ok: true,
          actor: { login: "alice" },
          data: { object: "session.github.pr_review_discard", review_id: REVIEW_ID },
        },
      ],
      { [PENDING_REVIEW_READ]: () => twoDrafts() },
    );
    show();
    await openFinish();
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).not.toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Discard Review…" }));
    const confirm = await screen.findByRole("dialog", { name: "Discard your pending review?" });
    expect(confirm).toHaveTextContent("This deletes its 2 comments.");
    expect(within(confirm).getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.click(within(confirm).getByRole("button", { name: "Discard Review" }));
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    expect(writes(shell)[0]).toMatchObject({
      operation: "pull_request_review_discard",
      params: { pending_review_id: REVIEW_ID },
    });
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).toBeNull());
  });

  it("refuses rather than retargets once the review it asked about is replaced", async () => {
    const read = { current: twoDrafts() as unknown };
    const shell = await ready([], { [PENDING_REVIEW_READ]: () => read.current });
    show();
    await openFinish();
    fireEvent.click(screen.getByRole("button", { name: "Discard Review…" }));
    await screen.findByRole("dialog", { name: "Discard your pending review?" });
    read.current = twoDrafts({ reviewId: "PRR_next" });
    await act(async () => {
      await client.refetchQueries({ queryKey: ["github-pending-review"] });
    });
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Discard your pending review?" })).toBeNull(),
    );
    expect(writes(shell)).toEqual([]);
  });

  it("keeps an unconfirmed discard first over a read of none, and settles it as submitted elsewhere", async () => {
    const read = { current: twoDrafts() as unknown };
    const state = { current: "COMMENTED" };
    await ready([UNKNOWN], {
      [PENDING_REVIEW_READ]: () => read.current,
      [PENDING_REVIEW_STATE_READ]: () => ({
        object: "session.github.pr_review_state",
        review_id: REVIEW_ID,
        state: state.current,
      }),
    });
    show();
    await openFinish();
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).not.toBeNull());
    fireEvent.click(screen.getByRole("button", { name: "Discard Review…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog", { name: "Discard your pending review?" })).getByRole(
        "button",
        { name: "Discard Review" },
      ),
    );
    await waitFor(() => expect(pendingHoldsOf("user-1", PR)).toHaveLength(1));
    read.current = noReviewAnswer();
    await act(async () => {
      await client.refetchQueries({ queryKey: ["github-pending-review"] });
    });
    expect(await screen.findByRole("button", { name: "Review unconfirmed" })).toBeInTheDocument();
    expect(row("Finish Review…")?.availability).toMatchObject({ available: false });
    // One-click approval waits for it too, without seeing anything of the review.
    expect(approvalGuardEntry("user-1", PR, "alice").status.kind).toBe("submitting");
    // The confirmation closed the menu; it opens again on the change to check.
    const menu = await openFinish(/^Review unconfirmed/);
    fireEvent.click(within(menu).getByRole("button", { name: "Check Again" }));
    await waitFor(() =>
      expect(toast).toHaveBeenCalledWith("This review was submitted elsewhere.", expect.anything()),
    );
    expect(pendingHoldsOf("user-1", PR)).toEqual([]);
    // Submitted, not discarded: its summary isn't dropped as discarded.
    expect(readPendingSummary(SUMMARY_KEY)).not.toBeNull();
  });

  it("keeps an unconfirmed discard held on a shell without the review read", async () => {
    await ready([UNKNOWN], { [PENDING_REVIEW_READ]: () => twoDrafts() }, [
      PENDING_REVIEW_STATE_READ,
    ]);
    show();
    await openFinish();
    fireEvent.click(screen.getByRole("button", { name: "Discard Review…" }));
    fireEvent.click(
      within(await screen.findByRole("dialog", { name: "Discard your pending review?" })).getByRole(
        "button",
        { name: "Discard Review" },
      ),
    );
    await waitFor(() => expect(pendingHoldsOf("user-1", PR)).toHaveLength(1));
    const menu = await openFinish(/^Review unconfirmed/);
    fireEvent.click(within(menu).getByRole("button", { name: "Check Again" }));
    await waitFor(() => expect(menu).toHaveTextContent("The app couldn’t check GitHub."));
    expect(pendingHoldsOf("user-1", PR)).toHaveLength(1);
  });
});

describe("a pending submit GitHub didn't confirm", () => {
  it("stays first over a read of none, blocks Finish and approvals, and settles on its own review", async () => {
    const read = { current: twoDrafts() as unknown };
    const reviews: unknown[] = [];
    await ready([UNKNOWN], {
      [PENDING_REVIEW_READ]: () => read.current,
      pull_request_review_check: () => ({
        object: "session.github.pr_review_check",
        head_sha: HEAD,
        author: "author-octo",
        pending: false,
        reviews,
      }),
    });
    show();
    await openFinish();
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    read.current = noReviewAnswer();
    await act(async () => {
      await client.refetchQueries({ queryKey: ["github-pending-review"] });
    });
    expect(await screen.findByRole("button", { name: "Review unconfirmed" })).toBeInTheDocument();
    expect(row("Discard Pending Review…")?.availability).toMatchObject({ available: false });
    const guard = approvalGuardEntry("user-1", PR, "alice");
    expect(guard.status.kind).toBe("uncertain");
    // Nothing of what was written reaches a list or guard.
    expect(guard.body).toBe("");
    reviews.push({
      id: REVIEW_ID,
      state: "COMMENTED",
      body: "From GitHub.",
      created_at: null,
      commit_oid: HEAD,
    });
    const menu = screen.getByRole("dialog", { name: "Finish your review" });
    fireEvent.click(within(menu).getByRole("button", { name: "Check Again" }));
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).toBeNull());
  });
});

describe("⌘K", () => {
  it("offers Finish Review… and Discard Pending Review…, the latter opening its confirmation", async () => {
    await ready([], { [PENDING_REVIEW_READ]: () => twoDrafts() });
    show();
    await screen.findByRole("button", { name: /^Finish review · 2/ });
    expect(row("Finish Review…")).toBeDefined();
    act(() => row("Discard Pending Review…")!.run());
    expect(
      await screen.findByRole("dialog", { name: "Discard your pending review?" }),
    ).toBeInTheDocument();
  });
});

describe("where a pending review isn't offered", () => {
  it("is today's Review menu with the pending reviews flag off", async () => {
    await ready([], { [PENDING_REVIEW_READ]: () => twoDrafts() });
    setPendingReviewsFlagForTests(false);
    show();
    fireEvent.click(await screen.findByRole("button", { name: "Review" }));
    expect(await screen.findByRole("dialog", { name: "Submit review" })).toBeInTheDocument();
    expect(row("Discard Pending Review…")).toBeUndefined();
  });
});

describe("a pending submit's scope", () => {
  it("keeps one in flight pinned to its review while a read names another", async () => {
    const read = { current: twoDrafts() as unknown };
    let answer!: (value: unknown) => void;
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { [PENDING_REVIEW_READ]: () => read.current },
    );
    show();
    await openFinish();
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    read.current = twoDrafts({ reviewId: "PRR_next" });
    await act(async () => {
      await client.refetchQueries({ queryKey: ["github-pending-review"] });
    });
    // Still this review's submit, and nothing else can be started meanwhile.
    expect(screen.getByRole("button", { name: "Discard Review…" })).toBeDisabled();
    expect(row("Discard Pending Review…")?.availability).toMatchObject({ available: false });
    answer(submitted());
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
  });

  it("shows lists and guards a pending submit's state, never what it holds", () => {
    const key = pendingSubmitKey("user-1", PR, { login: "alice", reader: null }, REVIEW_ID);
    updateReviewSubmit(key, (entry) => ({
      ...entry,
      body: "Private summary.",
      status: {
        kind: "uncertain",
        sent: {
          event: "COMMENT",
          body: "Private summary.",
          head: HEAD,
          knownReviewIds: [],
          sentAt: 0,
          pending: { reviewId: REVIEW_ID, reviewCommit: HEAD },
        },
        message: "GitHub may have it.",
        checking: false,
        checked: "never",
      },
    }));
    const [listed] = reviewSubmitsOf("user-1", "alice");
    expect(listed!.entry.body).toBe("");
    expect(listed!.entry.status).toMatchObject({
      kind: "uncertain",
      sent: { body: "" },
      message: "GitHub didn’t confirm your pending review was submitted.",
    });
    expect(approvalGuardEntry("user-1", PR, "alice").body).toBe("");
    // Another login's guard isn't held by it.
    expect(approvalGuardEntry("user-1", PR, "carol").status.kind).toBe("idle");
  });

  it("keeps each scope's summary apart, and a late answer settles only its own", async () => {
    let answer!: (value: unknown) => void;
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { [PENDING_REVIEW_READ]: () => twoDrafts() },
    );
    show();
    await openFinish();
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    // The Mac switches to Carol before GitHub answers Alice's submit.
    shell.switchActiveAccount("carol");
    await act(async () => shell.readAsHost(PR));
    const carolSummary = pendingSummaryKey(
      "user-1",
      PR,
      { login: "carol", namedReader: null },
      REVIEW_ID,
    );
    act(() => editPendingSummary(carolSummary, "Carol's own."));
    await act(async () => answer(submitted()));
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).toBeNull());
    expect(readPendingSummary(carolSummary)?.text).toBe("Carol's own.");
  });
});

describe("⌘K's pending rows", () => {
  it("run for the review shown now, not the one the row was made for", async () => {
    const read = { current: twoDrafts() as unknown };
    await ready([], { [PENDING_REVIEW_READ]: () => read.current });
    show();
    await screen.findByRole("button", { name: /^Finish review · 2/ });
    const discard = row("Discard Pending Review…")!;
    // Same labels and availability, another review: the row's run must follow it.
    read.current = pendingAnswer({
      reviewId: "PRR_next",
      unplaced: [unplacedDraft("PRRC_1"), unplacedDraft("PRRC_2"), unplacedDraft("PRRC_3")],
    });
    await act(async () => {
      await client.refetchQueries({ queryKey: ["github-pending-review"] });
    });
    await screen.findByRole("button", { name: /^Finish review · 3/ });
    act(() => discard.run());
    const confirm = await screen.findByRole("dialog", { name: "Discard your pending review?" });
    expect(confirm).toHaveTextContent("This deletes its 3 comments.");
  });
});

// Recovery and fencing, from the first review of this menu.
describe("recovery and fencing", () => {
  notifyManager.setScheduler((callback) => callback());

  async function startDiscard() {
    fireEvent.click(screen.getByRole("button", { name: "Discard Review…" }));
    const dialog = await screen.findByRole("dialog", { name: "Discard your pending review?" });
    fireEvent.click(within(dialog).getByRole("button", { name: "Discard Review" }));
  }

  it("late submit success keeps notifications out of another account's view", async () => {
    let answer!: (value: unknown) => void;
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { [PENDING_REVIEW_READ]: () => twoDrafts() },
    );
    show();
    await openFinish();
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    shell.switchActiveAccount("carol");
    await act(async () => shell.readAsHost(PR));
    vi.mocked(toast).mockClear();
    await act(async () => answer(submitted()));
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).toBeNull());
    expect(toast).not.toHaveBeenCalled();
  });

  it("a Check Again that lands after the menu unmounts keeps notifications out of the new view", async () => {
    const shell = await ready([UNKNOWN], {
      [PENDING_REVIEW_READ]: () => twoDrafts(),
      pull_request_review_check: () => ({
        object: "session.github.pr_review_check",
        head_sha: HEAD,
        author: "author-octo",
        pending: false,
        reviews: [
          {
            id: REVIEW_ID,
            state: "COMMENTED",
            body: "From GitHub.",
            created_at: null,
            commit_oid: HEAD,
          },
        ],
      }),
    });
    const mounted = show();
    await openFinish();
    await waitFor(() => expect(submit()).toBeEnabled());
    fireEvent.click(submit());
    await waitFor(() =>
      expect(
        readReviewSubmit(
          pendingSubmitKey("user-1", PR, { login: "alice", reader: null }, REVIEW_ID),
        ).status.kind,
      ).toBe("uncertain"),
    );
    const hold = shell.pauseNextRead("pull_request_review_check");
    fireEvent.click(screen.getByRole("button", { name: "Check Again" }));
    await hold.started;
    mounted.unmount();
    vi.mocked(toast).mockClear();
    await act(async () => hold.release());
    await waitFor(() => expect(readPendingSummary(SUMMARY_KEY)).toBeNull());
    expect(toast).not.toHaveBeenCalled();
  });

  it("a still unanswered discard stays pinned when the read reports none", async () => {
    let answer!: (value: unknown) => void;
    const read = { current: twoDrafts() as unknown };
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { [PENDING_REVIEW_READ]: () => read.current },
    );
    show();
    await openFinish();
    await startDiscard();
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    read.current = noReviewAnswer();
    await act(async () => {
      await client.refetchQueries({ queryKey: ["github-pending-review"] });
    });
    expect(screen.queryByRole("button", { name: "Review" })).toBeNull();
    expect(screen.getByRole("button", { name: /^Discarding review/ })).toBeInTheDocument();
    await act(async () => answer({ ok: false, error: { code: "rejected", message: "Refused." } }));
  });

  it("an unanswered discard remains busy after remount", async () => {
    let answer!: (value: unknown) => void;
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { [PENDING_REVIEW_READ]: () => twoDrafts() },
    );
    const mounted = show();
    await openFinish();
    await startDiscard();
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    mounted.unmount();
    show();
    await openFinish();
    const disabled = submit().hasAttribute("disabled");
    await act(async () => answer({ ok: false, error: { code: "rejected", message: "Refused." } }));
    expect(disabled).toBe(true);
  });

  it("a discard confirmation stays closed after switching accounts away and back", async () => {
    const read = { current: twoDrafts() as unknown };
    const shell = await ready([], { [PENDING_REVIEW_READ]: () => read.current });
    show();
    await openFinish();
    fireEvent.click(screen.getByRole("button", { name: "Discard Review…" }));
    await screen.findByRole("dialog", { name: "Discard your pending review?" });
    read.current = noReviewAnswer();
    shell.switchActiveAccount("carol");
    await act(async () => shell.readAsHost(PR));
    await waitFor(() =>
      expect(screen.queryByRole("dialog", { name: "Discard your pending review?" })).toBeNull(),
    );
    read.current = twoDrafts();
    shell.switchActiveAccount("alice");
    await act(async () => shell.readAsHost(PR));
    await waitFor(() => expect(screen.getByText("Finish review · 2")).toBeInTheDocument());
    expect(screen.queryByRole("dialog", { name: "Discard your pending review?" })).toBeNull();
  });

  it("discarding the old review does not close a replacement review's popover", async () => {
    const read = { current: twoDrafts() as unknown };
    let answer!: (value: unknown) => void;
    const shell = await ready(
      [
        new Promise((resolve) => {
          answer = resolve;
        }),
      ],
      { [PENDING_REVIEW_READ]: () => read.current },
    );
    show();
    await openFinish();
    await startDiscard();
    await waitFor(() => expect(writes(shell)).toHaveLength(1));
    read.current = twoDrafts({ reviewId: "PRR_next", body: "Replacement summary." });
    await act(async () => {
      await client.refetchQueries({ queryKey: ["github-pending-review"] });
    });
    // The menu stays on the review the discard was for until GitHub answers.
    expect(await openFinish()).not.toHaveTextContent("Replacement summary.");
    await act(async () =>
      answer({
        ok: true,
        actor: { login: "alice" },
        data: { object: "session.github.pr_review_discard", review_id: REVIEW_ID },
      }),
    );
    expect(screen.queryByRole("dialog", { name: "Finish your review" })).not.toBeNull();
  });
});
