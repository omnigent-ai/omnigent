import { QueryClient } from "@tanstack/react-query";
import { afterEach, describe, expect, it, vi } from "vitest";

import type { PullRequestCommentDraft } from "@/newPullRequestPage/hooks/usePullRequests";
import { BASE, HEAD, noReviewAnswer, pendingAnswer, REVIEW_ID } from "@/test/pendingReviewAnswers";

import {
  clearSentPendingDraft,
  type HeldAdd,
  pendingComposerMode,
  pendingRecoveriesOf,
  recordPendingRecovery,
  resetPendingRecoveriesForTests,
  resolvePendingRecoveries,
} from "./pendingComposer";
import {
  type DraftPlace,
  parsePendingReviewAnswer,
  type PendingReviewRead,
  pendingReviewState,
  type SentDraft,
} from "./pendingReview";

const PR = "https://github.com/acme/app/pull/42";
const HOST_SCOPE = { conversationId: "session-1", key: "route" };
const ALICE = { login: "alice", host: HOST_SCOPE };
const LINES: DraftPlace = {
  kind: "lines",
  path: "src/app.ts",
  side: "RIGHT",
  line: 12,
  startLine: null,
  headSha: HEAD,
  baseSha: BASE,
};
const read = (answer: unknown, issuedAt?: number): PendingReviewRead => ({
  ...(parsePendingReviewAnswer(answer) as PendingReviewRead),
  issued_at: issuedAt,
});
const stateOf = (answer: unknown) =>
  pendingReviewState({
    offered: true,
    data: read(answer),
    dataUpdatedAt: 1,
    failed: false,
    fetching: false,
  });

afterEach(() => {
  resetPendingRecoveriesForTests();
  vi.useRealTimers();
});

describe("what a box offers", () => {
  const hold = { kind: "draft_add", id: "hold-1" } as HeldAdd;
  const recover = () =>
    recordPendingRecovery({
      owner: "user-1",
      prUrl: PR,
      sentAs: ALICE,
      reviewId: REVIEW_ID,
      place: LINES,
      sent: null,
    });

  it("follows the read without a hold or recovery", () => {
    const none = stateOf(noReviewAnswer());
    expect(pendingComposerMode({ state: none, hold: null, recovery: null })).toEqual({
      kind: "comment_or_start",
    });
    expect(
      pendingComposerMode({ state: stateOf(pendingAnswer()), hold: null, recovery: null }),
    ).toEqual({ kind: "add_to_review", reviewId: REVIEW_ID, outdated: null });
    expect(pendingComposerMode({ state: { kind: "error" }, hold: null, recovery: null })).toEqual({
      kind: "unreadable",
    });
    expect(
      pendingComposerMode({ state: { kind: "unavailable" }, hold: null, recovery: null }),
    ).toEqual({ kind: "today" });
  });

  it("puts a held add first, whatever the read or a recovery says", () => {
    const recovery = recover();
    for (const state of [
      stateOf(noReviewAnswer()),
      stateOf(pendingAnswer()),
      { kind: "error" } as const,
    ]) {
      expect(pendingComposerMode({ state, hold, recovery })).toEqual({ kind: "held", hold });
    }
  });

  it("pins a recovery to its review over a stale none, a failure or another review", () => {
    const recovery = recover();
    for (const state of [
      stateOf(noReviewAnswer()),
      stateOf(pendingAnswer({ reviewId: "PRR_other" })),
      { kind: "error" } as const,
    ]) {
      expect(pendingComposerMode({ state, hold: null, recovery })).toEqual({
        kind: "recovering",
        recovery,
      });
    }
  });
});

describe("a recovery's review going away", () => {
  const record = () =>
    recordPendingRecovery({
      owner: "user-1",
      prUrl: PR,
      sentAs: ALICE,
      reviewId: REVIEW_ID,
      place: LINES,
      sent: null,
    });
  const gone = () => pendingRecoveriesOf("user-1", PR)[0]!.gone;

  it("is known only from a read sent after the recovery, not one that answers after it", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    const sentBefore = 900;
    const recovery = record();
    // A read sent before the start, answering late with no review, says nothing.
    resolvePendingRecoveries("user-1", PR, ALICE, read(noReviewAnswer(), sentBefore));
    expect(gone()).toBe(false);
    resolvePendingRecoveries("user-1", PR, ALICE, read(noReviewAnswer(), recovery.createdAt));
    expect(gone()).toBe(false);
    resolvePendingRecoveries("user-1", PR, ALICE, read(noReviewAnswer()));
    expect(gone()).toBe(false);
    resolvePendingRecoveries("user-1", PR, ALICE, read(pendingAnswer(), 2_000));
    expect(gone()).toBe(false);
    resolvePendingRecoveries("user-1", PR, ALICE, read(noReviewAnswer(), 2_000));
    expect(gone()).toBe(true);
  });

  it("is gone when a later read names another review", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    record();
    resolvePendingRecoveries(
      "user-1",
      PR,
      ALICE,
      read(pendingAnswer({ reviewId: "PRR_next" }), 2_000),
    );
    expect(gone()).toBe(true);
  });

  it("is never settled by another account's read", () => {
    vi.useFakeTimers();
    vi.setSystemTime(1_000);
    record();
    resolvePendingRecoveries(
      "user-1",
      PR,
      { login: "carol", host: HOST_SCOPE },
      read(noReviewAnswer(), 2_000),
    );
    resolvePendingRecoveries("user-2", PR, ALICE, read(noReviewAnswer(), 2_000));
    expect(gone()).toBe(false);
  });
});

describe("clearing the draft an add was sent from", () => {
  const KEY = ["pr-page-github-comment-draft", "user-1", "session-1", PR, "src/app.ts"] as const;
  const sent: SentDraft = { draftKey: KEY, sentId: "sent-1", body: "Rename this.", place: LINES };
  const anchor = {
    path: "src/app.ts",
    side: "RIGHT" as const,
    line: 12,
    start_line: null,
    head_sha: HEAD,
    base_sha: BASE,
  };
  const draft = (fields: Partial<PullRequestCommentDraft> = {}): PullRequestCommentDraft => ({
    body: "Rename this.",
    pending: false,
    error: null,
    anchor,
    sentId: "sent-1",
    generation: 3,
    ...fields,
  });
  const attempt = (current: PullRequestCommentDraft) => {
    const client = new QueryClient();
    client.setQueryData(KEY, current);
    const result = clearSentPendingDraft(client, sent, "Added; yours is kept.");
    return { result, after: client.getQueryData<PullRequestCommentDraft>(KEY) };
  };

  it("clears that exact draft, as sent, and starts the next one", () => {
    expect(attempt(draft())).toEqual({
      result: "cleared",
      after: { body: "", pending: false, error: null, generation: 4 },
    });
  });

  it.each([
    ["was edited since", { sentId: undefined, body: "Rename this, please." }],
    ["was cancelled and written again with the same text", { sentId: undefined }],
    ["is on other lines", { anchor: { ...anchor, line: 13 } }],
    ["is on another head", { anchor: { ...anchor, head_sha: "e".repeat(40) } }],
    ["is on another base", { anchor: { ...anchor, base_sha: "e".repeat(40) } }],
  ])("keeps a draft that %s, saying the comment was added", (_label, fields) => {
    const current = draft(fields);
    expect(attempt(current)).toEqual({
      result: "kept",
      after: { ...current, sentId: undefined, error: "Added; yours is kept." },
    });
  });

  it.each([
    ["a later send's, still in flight", { sentId: "sent-2", pending: true }],
    ["a later send's, answered", { sentId: "sent-2" }],
    ["uploading", { uploading: 1 }],
    ["waiting on a send that isn't this one", { pending: true, sentId: undefined }],
  ])("touches nothing of a draft that's %s", (_label, fields) => {
    const current = draft(fields);
    expect(attempt(current)).toEqual({ result: "other", after: current });
  });
});

describe("recoveries of different accounts at one place", () => {
  it("keeps each account's pin, replacing only its own", () => {
    const at = (login: string, reviewId: string) =>
      recordPendingRecovery({
        owner: "user-1",
        prUrl: PR,
        sentAs: { login, host: HOST_SCOPE },
        reviewId,
        place: LINES,
        sent: null,
      });
    at("alice", "PRR_alice");
    at("carol", "PRR_carol");
    at("alice", "PRR_alice_again");
    expect(
      pendingRecoveriesOf("user-1", PR)
        .map((each) => each.reviewId)
        .sort(),
    ).toEqual(["PRR_alice_again", "PRR_carol"]);
  });
});
