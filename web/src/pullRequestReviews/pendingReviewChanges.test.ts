// Changes to a pending review as this Mac's account: what each answer comes to, and how a change
// GitHub didn't confirm is held for the workspace user who made it until a read settles it.

import type * as IdentityModule from "@/lib/identity";
import { afterEach, describe, expect, it, vi } from "vitest";

const { workspaceUser, identityListeners } = vi.hoisted(() => ({
  workspaceUser: { id: "user-1" },
  identityListeners: new Set<() => void>(),
}));
vi.mock("@/lib/identity", async (importOriginal) => ({
  ...(await importOriginal<typeof IdentityModule>()),
  getCurrentUserId: () => workspaceUser.id,
  subscribeIdentity: (listener: () => void) => {
    identityListeners.add(listener);
    return () => identityListeners.delete(listener);
  },
}));

/** The workspace's identity check settles as `id`, as `/v1/me` would tell every listener. */
function signInAs(id: string) {
  workspaceUser.id = id;
  for (const listener of [...identityListeners]) listener();
}

import {
  macWriteRoute,
  PENDING_REVIEW_READ,
  PENDING_REVIEW_STATE_READ,
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

import { QueryClient } from "@tanstack/react-query";

import { checkReviewAgain } from "./reviewCheck";
import { readReviewSubmit, resetReviewSubmitForTests, reviewSubmitKey } from "./reviewSubmit";
import { sendReview } from "./sendReview";
import type { DraftPlace } from "./pendingReview";
import {
  checkPendingHold,
  dismissPendingHold,
  pendingHoldsOf,
  type PendingReviewChange,
  resetPendingReviewChangesForTests,
  sendPendingReviewChange,
} from "./pendingReviewChanges";

const PR = "https://github.com/acme/app/pull/42";
const ON_LINES: PendingReviewChange = {
  kind: "draft_add",
  body: "Rename this.",
  place: {
    kind: "lines",
    path: "src/app.ts",
    side: "RIGHT",
    line: 12,
    startLine: null,
    headSha: HEAD,
    baseSha: BASE,
  },
  pendingReviewId: null,
  expectedHead: HEAD,
};
const NEW_THREAD = {
  object: "session.github.pr_review_draft",
  pr_url: PR,
  review_id: REVIEW_ID,
  thread: draftThread("PRRT_new", [{ id: "PRRC_new", body: "Rename this." }]),
};
const UNKNOWN = { code: "outcome_unknown", message: "GitHub may have saved this." };
const LINES_BASELINE = {
  review_id: REVIEW_ID,
  path: "src/app.ts",
  line: 12,
  start_line: null,
  complete: true,
  known_comment_ids: [],
};

/** A shell offering pending reviews, answering writes with `answer` and reads with `read`. */
async function pendingShell(
  answer: (request: Record<string, unknown>) => unknown,
  read: { current: unknown } = { current: noReviewAnswer() },
  asBob = false,
) {
  const shell = installNamedWriteShell(answer, {
    extraWrites: [...PENDING_REVIEW_WRITE_OPERATIONS, PENDING_REVIEW_READ],
    reads: { [PENDING_REVIEW_READ]: () => read.current },
  });
  await shell.ready(PR, { asBob });
  setPendingReviewsFlagForTests(true);
  return shell;
}

afterEach(() => {
  workspaceUser.id = "user-1";
  identityListeners.clear();
  resetPendingReviewChangesForTests();
  resetReviewSubmitForTests();
  resetDesktopPullRequestWritesForTests();
  resetDesktopReadPathForTests();
  resetMacNamedReadsForTests();
  resetMacReadScopesForTests();
  removeNamedWriteShell();
});

describe("sending a pending review change", () => {
  it("starts a review with the draft on lines of the diff, pinned to the head shown", async () => {
    const shell = await pendingShell(() => ({
      ok: true,
      data: NEW_THREAD,
      actor: { login: "alice" },
    }));
    const outcome = await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES);
    expect(outcome).toMatchObject({
      kind: "done",
      writer: "alice",
      answer: { review_id: REVIEW_ID },
    });
    expect(shell.codeHostWrite.mock.calls[0]![0]).toMatchObject({
      operation: "pull_request_review_draft_add",
      params: {
        repository: "acme/app",
        number: 42,
        body: "Rename this.",
        anchor: {
          path: "src/app.ts",
          side: "RIGHT",
          line: 12,
          start_line: null,
          head_sha: HEAD,
          base_sha: BASE,
        },
        pending_review_id: null,
        expected_head: HEAD,
      },
    });
  });

  it("sends as the page's own login by name, never as the host's account", async () => {
    const shell = await pendingShell(
      () => ({ ok: true, data: NEW_THREAD, actor: { login: "bob" } }),
      undefined,
      true,
    );
    const outcome = await sendPendingReviewChange(PR, macWriteRoute(PR, BOB)!, {
      kind: "draft_delete",
      commentId: "PRRC_1",
      reviewId: REVIEW_ID,
    });
    expect(shell.codeHostWriteAs.mock.calls[0]![0]).toMatchObject({
      macLogin: "bob",
      operation: "pull_request_review_draft_delete",
      params: { comment_id: "PRRC_1", pending_review_id: REVIEW_ID },
    });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
    // The delete's answer is checked as one: a thread isn't one.
    expect(outcome).toMatchObject({ kind: "held" });
  });

  it("names the review a partial start made, so the draft is added to it next", async () => {
    await pendingShell(() => ({
      ok: false,
      error: {
        code: "partial",
        cause: "head_changed",
        message: "Your review was started, but this comment wasn’t added.",
      },
      steps: {
        completed: [{ step: "start_review", review_id: REVIEW_ID }],
        failed: { step: "comment" },
        not_attempted: [],
      },
    }));
    expect(await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES)).toEqual({
      kind: "partial",
      reviewId: REVIEW_ID,
      cause: "head_changed",
      message: "Your review was started, but this comment wasn’t added.",
    });
    expect(pendingHoldsOf("user-1", PR)).toEqual([]);
  });

  it("reports a refusal, with nothing held", async () => {
    await pendingShell(() => ({
      ok: false,
      error: { code: "review_changed", message: "Your review changed." },
    }));
    expect(await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES)).toEqual({
      kind: "refused",
      code: "review_changed",
      message: "Your review changed.",
    });
    expect(pendingHoldsOf("user-1", PR)).toEqual([]);
  });

  it("sends nothing once pending reviews stopped being offered on the route", async () => {
    const shell = await pendingShell(() => ({
      ok: true,
      data: NEW_THREAD,
      actor: { login: "alice" },
    }));
    const shown = macWriteRoute(PR, null)!;
    setPendingReviewsFlagForTests(false);
    expect(await sendPendingReviewChange(PR, shown, ON_LINES)).toEqual({
      kind: "refused",
      code: "pending_reviews_unavailable",
      message: "Pending reviews aren’t available here right now. Nothing was changed.",
    });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
  });
});

describe("a change GitHub didn't confirm", () => {
  it("is held, then settled by a read showing exactly one new draft where it went", async () => {
    const read = { current: noReviewAnswer() as unknown };
    await pendingShell(() => ({ ok: false, error: UNKNOWN, baseline: LINES_BASELINE }), read);
    const outcome = await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES);
    expect(outcome).toMatchObject({ kind: "held", hold: { kind: "draft_add", checked: "never" } });
    const [hold] = pendingHoldsOf("user-1", PR);
    // A read that finds no review yet proves nothing.
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "unsettled" });
    expect(pendingHoldsOf("user-1", PR)).toHaveLength(1);
    read.current = pendingAnswer({
      threads: [draftThread("PRRT_new", [{ id: "PRRC_new", body: "Rename this." }])],
    });
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "landed", commentId: "PRRC_new" });
    expect(pendingHoldsOf("user-1", PR)).toEqual([]);
  });

  it("stays held on an older shell's answer, which names no baseline", async () => {
    const read = {
      current: pendingAnswer({
        threads: [draftThread("PRRT_new", [{ id: "PRRC_new", body: "Rename this." }])],
      }) as unknown,
    };
    await pendingShell(() => ({ ok: false, error: UNKNOWN }), read);
    await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES);
    const [hold] = pendingHoldsOf("user-1", PR);
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "unsettled" });
    expect(pendingHoldsOf("user-1", PR)[0]).toMatchObject({ checked: "unsettled" });
    dismissPendingHold(hold!.id);
    expect(pendingHoldsOf("user-1", PR)).toEqual([]);
  });

  it("says a start GitHub didn't confirm made the review, so the draft is added to it", async () => {
    const read = { current: pendingAnswer() as unknown };
    await pendingShell(
      () => ({
        ok: false,
        error: UNKNOWN,
        start: { commit_oid: HEAD, prior_pending_review_id: null },
      }),
      read,
    );
    await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES);
    const [hold] = pendingHoldsOf("user-1", PR);
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "started", reviewId: REVIEW_ID });
  });

  it("stays held when its read fails, and once the account it was sent as is gone", async () => {
    const read = { current: { object: "session.github.pr_pending_review" } as unknown };
    const shell = await pendingShell(
      () => ({ ok: false, error: UNKNOWN, baseline: LINES_BASELINE }),
      read,
    );
    await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES);
    const [hold] = pendingHoldsOf("user-1", PR);
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "unreadable" });
    shell.switchActiveAccount("carol");
    await shell.readAsHost(PR);
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "unreadable" });
    expect(pendingHoldsOf("user-1", PR)).toHaveLength(1);
  });

  it("is held for the workspace user who sent it, and no one else", async () => {
    let answer!: (value: unknown) => void;
    await pendingShell(
      () =>
        new Promise((resolve) => {
          answer = resolve;
        }),
    );
    const sent = sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES);
    await vi.waitFor(() => expect(answer).toBeDefined());
    signInAs("user-2");
    answer({ ok: true, data: NEW_THREAD, actor: { login: "alice" } });
    expect(await sent).toEqual({ kind: "suppressed" });
    expect(pendingHoldsOf("user-2", PR)).toEqual([]);
    expect(pendingHoldsOf("user-1", PR)).toMatchObject([{ kind: "draft_add", owner: "user-1" }]);
    // Its owner's check, not another user's.
    expect(await checkPendingHold(pendingHoldsOf("user-1", PR)[0]!.id)).toBeNull();
  });

  /** An unconfirmed discard, then a check that reads the review by its id as `state`. */
  async function discardChecked(state: string | null, omitReads: readonly string[] = []) {
    const shell = installNamedWriteShell(() => ({ ok: false, error: UNKNOWN }), {
      extraWrites: [
        ...PENDING_REVIEW_WRITE_OPERATIONS,
        PENDING_REVIEW_READ,
        PENDING_REVIEW_STATE_READ,
      ],
      omitReads,
      reads: {
        [PENDING_REVIEW_READ]: () => noReviewAnswer(),
        // Thirty later reviews and none of them this one: proves nothing about it.
        pull_request_review_check: () => ({
          object: "session.github.pr_review_check",
          head_sha: HEAD,
          author: "carol",
          pending: false,
          reviews: Array.from({ length: 30 }, (_, n) => ({
            id: `PRR_later_${n}`,
            state: "COMMENTED",
            body: "",
            created_at: null,
            commit_oid: HEAD,
          })),
        }),
        [PENDING_REVIEW_STATE_READ]: () =>
          state === null
            ? {}
            : { object: "session.github.pr_review_state", review_id: REVIEW_ID, state },
      },
    });
    await shell.ready(PR, { asBob: false });
    setPendingReviewsFlagForTests(true);
    await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, {
      kind: "discard",
      reviewId: REVIEW_ID,
    });
    const [hold] = pendingHoldsOf("user-1", PR);
    return { shell, outcome: await checkPendingHold(hold!.id) };
  }

  it("a discard settles only from the review read by its id", async () => {
    expect((await discardChecked("absent")).outcome).toEqual({ kind: "discarded" });
    expect(pendingHoldsOf("user-1", PR)).toEqual([]);
  });

  it("a discard of a review submitted long ago, out of the recent reviews, is submitted elsewhere", async () => {
    const { shell, outcome } = await discardChecked("COMMENTED");
    expect(outcome).toEqual({ kind: "submitted_elsewhere" });
    const sent = shell.codeHostQuery.mock.calls.find(
      ([request]) => request.operation === PENDING_REVIEW_STATE_READ,
    );
    expect(sent![0].params).toEqual({ repository: "acme/app", number: 42, review_id: REVIEW_ID });
  });

  it.each([
    ["reads pending", "PENDING", [], { kind: "not_discarded" }],
    ["can't be read", "unreadable", [], { kind: "unreadable" }],
    ["answers with another shape", null, [], { kind: "unreadable" }],
    // An older shell without the read: held, never guessed from the recent reviews.
    [
      "is on a shell without the read",
      "absent",
      [PENDING_REVIEW_STATE_READ],
      { kind: "unreadable" },
    ],
  ] as const)("a discard stays held while the review %s", async (_label, state, omit, expected) => {
    expect((await discardChecked(state, omit)).outcome).toEqual(expected);
    expect(pendingHoldsOf("user-1", PR)).toHaveLength(expected.kind === "not_discarded" ? 0 : 1);
  });

  it("doesn't take another tab's draft on the other side of the line for one sent to the left", async () => {
    const left: PendingReviewChange = {
      ...ON_LINES,
      pendingReviewId: REVIEW_ID,
      place: { ...(ON_LINES.place as Extract<DraftPlace, { kind: "lines" }>), side: "LEFT" },
    };
    const read = {
      current: pendingAnswer({
        threads: [draftThread("PRRT_right", [{ id: "PRRC_right", body: "Rename this." }])],
      }) as unknown,
    };
    await pendingShell(() => ({ ok: false, error: UNKNOWN, baseline: LINES_BASELINE }), read);
    await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, left);
    const [hold] = pendingHoldsOf("user-1", PR);
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "unsettled" });
    expect(pendingHoldsOf("user-1", PR)).toHaveLength(1);
    read.current = pendingAnswer({
      threads: [
        draftThread("PRRT_right", [{ id: "PRRC_right", body: "Rename this." }]),
        { ...draftThread("PRRT_left", [{ id: "PRRC_left", body: "Rename this." }]), side: "LEFT" },
      ],
    });
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "landed", commentId: "PRRC_left" });
  });

  it("can be checked again by its owner after another workspace user signed in mid-check", async () => {
    const read = {
      current: pendingAnswer({
        threads: [draftThread("PRRT_new", [{ id: "PRRC_new", body: "Rename this." }])],
      }) as unknown,
    };
    const shell = await pendingShell(
      () => ({ ok: false, error: UNKNOWN, baseline: LINES_BASELINE }),
      read,
    );
    await sendPendingReviewChange(PR, macWriteRoute(PR, null)!, ON_LINES);
    const [hold] = pendingHoldsOf("user-1", PR);
    const paused = shell.pauseNextRead(PENDING_REVIEW_READ);
    const first = checkPendingHold(hold!.id);
    await paused.started;
    signInAs("user-2");
    paused.release();
    expect(await first).toBeNull();
    signInAs("user-1");
    // Nothing of the check was applied, and it isn't stuck checking.
    expect(pendingHoldsOf("user-1", PR)).toMatchObject([{ checking: false, checked: "never" }]);
    // Back as the owner, the page reads as the account again, and the check goes through.
    await shell.readAsHost(PR);
    expect(await checkPendingHold(hold!.id)).toEqual({ kind: "landed", commentId: "PRRC_new" });
  });
});

describe("an answer that arrives after the route moved", () => {
  const DONE = { ok: true, data: NEW_THREAD, actor: { login: "alice" } };

  /** Send `change` along `shown`, run `move` while its answer is held, then answer `reply`. */
  async function lateAnswer(
    shown: NonNullable<ReturnType<typeof macWriteRoute>>,
    move: () => Promise<void>,
    reply: unknown,
    change: PendingReviewChange = ON_LINES,
  ) {
    const sent = sendPendingReviewChange(PR, shown, change);
    await vi.waitFor(() => expect(answers.length).toBe(1));
    await move();
    answers[0]!(reply);
    return sent;
  }
  let answers: ((value: unknown) => void)[] = [];
  const holding = () =>
    new Promise((resolve) => {
      answers.push(resolve);
    });

  afterEach(() => {
    answers = [];
  });

  it("says only how it ended once the host account switched to another login", async () => {
    const shell = await pendingShell(holding);
    const shown = macWriteRoute(PR, null)!;
    const outcome = await lateAnswer(
      shown,
      async () => {
        shell.switchActiveAccount("carol");
        await shell.readAsHost(PR);
      },
      DONE,
    );
    expect(outcome).toEqual({
      kind: "moved",
      sentAs: { login: "alice", generation: 1, reader: null },
      settled: "done",
    });
  });

  it("says only how it ended once the same login signed in again", async () => {
    const shell = await pendingShell(holding, undefined, true);
    const shown = macWriteRoute(PR, BOB)!;
    const outcome = await lateAnswer(
      shown,
      async () => {
        shell.bobSignsInAgain(5);
        await shell.readAsBob(PR);
      },
      { ok: true, data: NEW_THREAD, actor: { login: "bob" } },
    );
    expect(outcome).toMatchObject({ kind: "moved", sentAs: { login: "bob" }, settled: "done" });
  });

  it("keeps a partial start, a refusal and an unconfirmed change to the scope it was sent in", async () => {
    const shell = await pendingShell(holding, undefined, true);
    const shown = macWriteRoute(PR, BOB)!;
    // The page stops writing as Bob by name: his receipts aren't the host account's.
    const leaveNamed = () => shell.setWriteAs(false);
    const partial = await lateAnswer(shown, leaveNamed, {
      ok: false,
      error: { code: "partial", cause: "head_changed", message: "Started." },
      steps: {
        completed: [{ step: "start_review", review_id: REVIEW_ID }],
        failed: { step: "comment" },
        not_attempted: [],
      },
    });
    expect(partial).toMatchObject({ kind: "moved", sentAs: { reader: BOB }, settled: "partial" });
    await shell.setWriteAs(true);
    await shell.readAsBob(PR);
    answers = [];
    const refused = await lateAnswer(macWriteRoute(PR, BOB)!, leaveNamed, {
      ok: false,
      error: { code: "review_changed", message: "Changed." },
    });
    expect(refused).toMatchObject({ kind: "moved", settled: "refused" });
    await shell.setWriteAs(true);
    await shell.readAsBob(PR);
    answers = [];
    const held = await lateAnswer(macWriteRoute(PR, BOB)!, leaveNamed, {
      ok: false,
      error: UNKNOWN,
      baseline: LINES_BASELINE,
    });
    expect(held).toMatchObject({ kind: "moved", settled: "held" });
    // Still held for the login and scope it was sent as.
    expect(pendingHoldsOf("user-1", PR)).toMatchObject([
      { kind: "draft_add", sentAs: { login: "bob", reader: BOB } },
    ]);
  });

  it("is done when nothing moved", async () => {
    await pendingShell(holding);
    const outcome = await lateAnswer(macWriteRoute(PR, null)!, async () => {}, DONE);
    expect(outcome).toMatchObject({ kind: "done", writer: "alice" });
  });
});

describe("finishing a pending review", () => {
  const OLDER_COMMIT = "d".repeat(40);
  const finish = (pending: { reviewId: string; reviewCommit: string } | undefined) => {
    const key = reviewSubmitKey("user-1", PR, "alice");
    const sent = sendReview({
      key,
      route: macWriteRoute(PR, null)!,
      prUrl: PR,
      event: "COMMENT",
      body: "Done.",
      headSha: HEAD,
      pending,
      origin: null,
      queryClient: new QueryClient(),
      conversationId: "session-1",
    });
    return { key, sent };
  };

  it("submits the review it names on the commit it was started on, behind the head", async () => {
    const shell = await pendingShell(() => ({ ok: false, error: UNKNOWN }));
    expect(finish({ reviewId: REVIEW_ID, reviewCommit: OLDER_COMMIT }).sent).toBe(true);
    await vi.waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWrite.mock.calls[0]![0]).toMatchObject({
      operation: "pull_request_review_submit",
      params: {
        event: "COMMENT",
        body: "Done.",
        expected_head: HEAD,
        pending_review_id: REVIEW_ID,
        review_commit: OLDER_COMMIT,
      },
    });
  });

  it("sends nothing once pending reviews stopped being offered, and says why", async () => {
    const shell = await pendingShell(() => ({ ok: false, error: UNKNOWN }));
    setPendingReviewsFlagForTests(false);
    const { key, sent } = finish({ reviewId: REVIEW_ID, reviewCommit: HEAD });
    expect(sent).toBe(false);
    expect(readReviewSubmit(key).status).toMatchObject({
      kind: "refused",
      code: "pending_reviews_unavailable",
    });
    expect(shell.codeHostWrite).not.toHaveBeenCalled();
  });

  it("leaves a review made at once as it was with the pending reviews flag off", async () => {
    const shell = await pendingShell(() => ({ ok: false, error: UNKNOWN }));
    setPendingReviewsFlagForTests(false);
    expect(finish(undefined).sent).toBe(true);
    await vi.waitFor(() => expect(shell.codeHostWrite).toHaveBeenCalledTimes(1));
    expect(shell.codeHostWrite.mock.calls[0]![0].params).not.toHaveProperty("pending_review_id");
  });

  it("settles an unconfirmed finish only on the review submitted on its own commit", async () => {
    const reviews: unknown[] = [];
    const shell = installNamedWriteShell(() => ({ ok: false, error: UNKNOWN }), {
      extraWrites: [...PENDING_REVIEW_WRITE_OPERATIONS, PENDING_REVIEW_READ],
      reads: {
        pull_request_review_check: () => ({
          object: "session.github.pr_review_check",
          head_sha: HEAD,
          author: "carol",
          pending: false,
          reviews,
        }),
      },
    });
    await shell.ready(PR, { asBob: false });
    setPendingReviewsFlagForTests(true);
    const { key } = finish({ reviewId: REVIEW_ID, reviewCommit: OLDER_COMMIT });
    await vi.waitFor(() => expect(readReviewSubmit(key).status.kind).toBe("uncertain"));
    const check = () =>
      checkReviewAgain({
        key,
        prUrl: PR,
        queryClient: new QueryClient(),
        conversationId: "session-1",
      });
    // Submitted, but as GitHub would show it on the head instead: not this one.
    reviews.push({
      id: REVIEW_ID,
      state: "COMMENTED",
      body: "Done.",
      created_at: null,
      commit_oid: HEAD,
    });
    expect(await check()).toMatchObject({ kind: "not_found" });
    reviews[0] = {
      id: REVIEW_ID,
      state: "COMMENTED",
      body: "Done.",
      created_at: null,
      commit_oid: OLDER_COMMIT,
    };
    expect(await check()).toEqual({ kind: "landed" });
  });
});
