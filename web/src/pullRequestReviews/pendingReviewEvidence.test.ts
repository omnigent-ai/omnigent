import { describe, expect, it } from "vitest";

import type { ReviewCheck } from "@/lib/pullRequestWriteAnswers";
import type { PendingDraftBaseline } from "@/lib/nativeBridge";
import {
  BASE,
  draftThread,
  HEAD,
  noReviewAnswer,
  OLDER,
  pendingAnswer,
  REVIEW_ID,
  unplacedDraft,
} from "@/test/pendingReviewAnswers";

import { type DraftPlace, parsePendingReviewAnswer, type PendingReviewRead } from "./pendingReview";
import {
  discardVerdict,
  draftAddVerdict,
  draftDeleteVerdict,
  draftEditVerdict,
  reviewStartVerdict,
} from "./pendingReviewEvidence";
import { landedReview, type SentReview } from "./reviewSubmit";

const read = (answer: unknown) => parsePendingReviewAnswer(answer) as PendingReviewRead;
const HELD = { kind: "held" };

const lines = (fields: Partial<PendingDraftBaseline> = {}): PendingDraftBaseline => ({
  reviewId: REVIEW_ID,
  scope: { kind: "thread", path: "src/app.ts", line: 12, startLine: null },
  complete: true,
  knownCommentIds: [],
  ...fields,
});
const ON_LINES: DraftPlace = {
  kind: "lines",
  path: "src/app.ts",
  side: "RIGHT",
  line: 12,
  startLine: null,
  headSha: HEAD,
  baseSha: BASE,
};
const IN_THREAD: DraftPlace = { kind: "reply", threadId: "PRRT_1" };
const reply = (fields: Partial<PendingDraftBaseline> = {}): PendingDraftBaseline => ({
  reviewId: REVIEW_ID,
  scope: { kind: "reply", threadId: "PRRT_1", rootCommentId: "PRRC_root" },
  complete: true,
  knownCommentIds: [],
  ...fields,
});

describe("an unconfirmed draft on lines of the diff", () => {
  it("landed once exactly one new draft with its body starts a thread on those lines", () => {
    const after = read(
      pendingAnswer({
        threads: [draftThread("PRRT_new", [{ id: "PRRC_new", body: "Rename this." }])],
      }),
    );
    expect(
      draftAddVerdict({ place: ON_LINES, baseline: lines(), body: "Rename this.\r\n" }, after),
    ).toEqual({
      kind: "landed",
      commentId: "PRRC_new",
    });
  });

  it("stays held when the same text was already there before it was sent", () => {
    const after = read(
      pendingAnswer({
        threads: [draftThread("PRRT_old", [{ id: "PRRC_old", body: "Rename this." }])],
      }),
    );
    const baseline = lines({ knownCommentIds: ["PRRC_old"] });
    expect(draftAddVerdict({ place: ON_LINES, baseline, body: "Rename this." }, after)).toEqual(
      HELD,
    );
  });

  it("stays held with two new drafts that could each be it", () => {
    const after = read(
      pendingAnswer({
        threads: [
          draftThread("PRRT_a", [{ id: "PRRC_a", body: "Same." }]),
          draftThread("PRRT_b", [{ id: "PRRC_b", body: "Same." }]),
        ],
      }),
    );
    expect(draftAddVerdict({ place: ON_LINES, baseline: lines(), body: "Same." }, after)).toEqual(
      HELD,
    );
  });

  it("doesn't count a reply on those lines, other lines, or another body", () => {
    const after = read(
      pendingAnswer({
        threads: [
          draftThread("PRRT_a", [{ id: "PRRC_reply", body: "Same.", reply_to_id: "PRRC_x" }]),
          draftThread("PRRT_b", [{ id: "PRRC_other", body: "Same." }], { line: 13 }),
          draftThread("PRRT_c", [{ id: "PRRC_text", body: "Different." }]),
        ],
      }),
    );
    expect(draftAddVerdict({ place: ON_LINES, baseline: lines(), body: "Same." }, after)).toEqual(
      HELD,
    );
  });

  it("stays held on an older shell's read, which can't say a draft starts its thread", () => {
    const after = read(
      pendingAnswer({
        threads: [
          draftThread("PRRT_new", [{ id: "PRRC_new", body: "Same.", reply_to_id: undefined }]),
        ],
      }),
    );
    expect(draftAddVerdict({ place: ON_LINES, baseline: lines(), body: "Same." }, after)).toEqual(
      HELD,
    );
  });

  it.each([
    ["no baseline, as from an older shell", null],
    ["an incomplete baseline", lines({ complete: false })],
    ["another review's baseline", lines({ reviewId: "PRR_other" })],
  ])("stays held with %s", (_label, baseline) => {
    const after = read(
      pendingAnswer({ threads: [draftThread("PRRT_new", [{ id: "PRRC_new", body: "Same." }])] }),
    );
    expect(draftAddVerdict({ place: ON_LINES, baseline, body: "Same." }, after)).toEqual(HELD);
  });

  it("stays held on a read that couldn't list every draft, or found no review", () => {
    const incomplete = read(
      pendingAnswer({
        threads: [draftThread("PRRT_new", [{ id: "PRRC_new", body: "Same." }])],
        total: 5,
      }),
    );
    expect(
      draftAddVerdict({ place: ON_LINES, baseline: lines(), body: "Same." }, incomplete),
    ).toEqual(HELD);
    expect(
      draftAddVerdict(
        { place: ON_LINES, baseline: lines(), body: "Same." },
        read(noReviewAnswer()),
      ),
    ).toEqual(HELD);
  });
});

describe("an unconfirmed draft's side of the diff", () => {
  const withThread = (
    place: Parameters<typeof draftThread>[2] & { side?: string; start_side?: string | null },
  ) => {
    const thread = { ...draftThread("PRRT_new", [{ id: "PRRC_new", body: "Same." }], place) };
    return read(
      pendingAnswer({
        threads: [
          {
            ...thread,
            side: place.side ?? "RIGHT",
            start_side: place.start_side === undefined ? null : place.start_side,
          },
        ] as never,
      }),
    );
  };

  it("doesn't take a new conversation on the other side of the same line for it", () => {
    const left: DraftPlace = { ...ON_LINES, side: "LEFT" };
    const after = withThread({ side: "RIGHT" });
    expect(draftAddVerdict({ place: left, baseline: lines(), body: "Same." }, after)).toEqual(HELD);
    expect(
      draftAddVerdict(
        { place: left, baseline: lines(), body: "Same." },
        withThread({ side: "LEFT" }),
      ),
    ).toMatchObject({ kind: "landed", commentId: "PRRC_new" });
  });

  it("stays held for a new conversation no thread placed, whose side the read can't say", () => {
    const after = read(pendingAnswer({ unplaced: [unplacedDraft("PRRC_new", { body: "Same." })] }));
    expect(draftAddVerdict({ place: ON_LINES, baseline: lines(), body: "Same." }, after)).toEqual(
      HELD,
    );
  });

  it("matches a range on the side it starts on as well", () => {
    const range: DraftPlace = { ...ON_LINES, startLine: 10 };
    const baseline = lines({
      scope: { kind: "thread", path: "src/app.ts", line: 12, startLine: 10 },
    });
    const sent = { place: range, baseline, body: "Same." };
    expect(
      draftAddVerdict(sent, withThread({ start_line: 10, side: "RIGHT", start_side: "RIGHT" })),
    ).toMatchObject({ kind: "landed" });
    expect(
      draftAddVerdict(sent, withThread({ start_line: 10, side: "RIGHT", start_side: "LEFT" })),
    ).toEqual(HELD);
    expect(
      draftAddVerdict(sent, withThread({ start_line: 10, side: "RIGHT", start_side: null })),
    ).toEqual(HELD);
  });

  it("stays held when the shell's baseline names other lines than were sent", () => {
    const after = withThread({ side: "RIGHT" });
    const baseline = lines({
      scope: { kind: "thread", path: "src/app.ts", line: 13, startLine: null },
    });
    expect(draftAddVerdict({ place: ON_LINES, baseline, body: "Same." }, after)).toEqual(HELD);
  });
});

describe("an unconfirmed reply", () => {
  it("landed once exactly one new reply to the thread's root has its body, placed or not", () => {
    const placed = read(
      pendingAnswer({
        threads: [
          draftThread("PRRT_1", [{ id: "PRRC_r", body: "Yes.", reply_to_id: "PRRC_root" }]),
        ],
      }),
    );
    expect(
      draftAddVerdict({ place: IN_THREAD, baseline: reply(), body: "Yes." }, placed),
    ).toMatchObject({
      kind: "landed",
      commentId: "PRRC_r",
    });
    const unplaced = read(
      pendingAnswer({
        unplaced: [unplacedDraft("PRRC_r", { body: "Yes.", reply_to_id: "PRRC_root" })],
      }),
    );
    expect(
      draftAddVerdict({ place: IN_THREAD, baseline: reply(), body: "Yes." }, unplaced),
    ).toMatchObject({
      kind: "landed",
    });
  });

  it("stays held without the thread's root, which tells its replies apart", () => {
    const after = read(
      pendingAnswer({
        threads: [
          draftThread("PRRT_1", [{ id: "PRRC_r", body: "Yes.", reply_to_id: "PRRC_root" }]),
        ],
      }),
    );
    const baseline = reply({ scope: { kind: "reply", threadId: "PRRT_1", rootCommentId: null } });
    expect(draftAddVerdict({ place: IN_THREAD, baseline, body: "Yes." }, after)).toEqual(HELD);
  });
});

describe("an unconfirmed start", () => {
  it("started the review pending now on the commit it was pinned to, with none before", () => {
    const after = read(pendingAnswer());
    expect(reviewStartVerdict({ commitOid: HEAD, priorPendingReviewId: null }, after)).toEqual({
      kind: "started",
      reviewId: REVIEW_ID,
    });
  });

  it("stays held for a review on another commit, no review, or one that was there before", () => {
    const start = { commitOid: HEAD, priorPendingReviewId: null };
    expect(reviewStartVerdict(start, read(pendingAnswer({ commit: OLDER })))).toEqual(HELD);
    expect(reviewStartVerdict(start, read(noReviewAnswer()))).toEqual(HELD);
    expect(
      reviewStartVerdict(
        { commitOid: HEAD, priorPendingReviewId: "PRR_was" },
        read(pendingAnswer()),
      ),
    ).toEqual(HELD);
    expect(reviewStartVerdict(null, read(pendingAnswer()))).toEqual(HELD);
  });
});

describe("an unconfirmed edit and delete", () => {
  const withDraft = (body: string) =>
    read(pendingAnswer({ threads: [draftThread("PRRT_1", [{ id: "PRRC_1", body }])] }));

  it("an edit landed once the draft reads with the new body, in the same review", () => {
    const sent = { commentId: "PRRC_1", reviewId: REVIEW_ID, body: "New." };
    expect(draftEditVerdict(sent, withDraft("New."))).toMatchObject({ kind: "landed" });
    expect(draftEditVerdict(sent, withDraft("Old."))).toEqual(HELD);
    expect(draftEditVerdict({ ...sent, reviewId: "PRR_other" }, withDraft("New."))).toEqual(HELD);
  });

  it("a delete landed only once a complete read of the same review lacks the draft", () => {
    const sent = { commentId: "PRRC_1", reviewId: REVIEW_ID };
    expect(draftDeleteVerdict(sent, read(pendingAnswer()))).toMatchObject({ kind: "landed" });
    expect(draftDeleteVerdict(sent, withDraft("Still here."))).toEqual(HELD);
    // Fewer read than counted: the draft may be one of those not read.
    expect(draftDeleteVerdict(sent, read(pendingAnswer({ total: 3 })))).toEqual(HELD);
    // The review gone or replaced may have been submitted with the draft in it.
    expect(draftDeleteVerdict(sent, read(noReviewAnswer()))).toEqual(HELD);
    expect(draftDeleteVerdict(sent, read(pendingAnswer({ reviewId: "PRR_next" })))).toEqual(HELD);
  });
});

describe("an unconfirmed finish and discard", () => {
  const check = (reviews: ReviewCheck["reviews"]): ReviewCheck => ({
    head_sha: HEAD,
    author: "carol",
    pending: false,
    reviews,
  });
  const sent: SentReview = {
    event: "APPROVE",
    body: "Ship it.",
    head: HEAD,
    knownReviewIds: [],
    sentAt: Date.parse("2026-11-12T10:00:00Z"),
    pending: { reviewId: REVIEW_ID, reviewCommit: OLDER },
  };
  const submitted = (fields: Partial<ReviewCheck["reviews"][number]> = {}) => ({
    id: REVIEW_ID,
    state: "APPROVED",
    body: "Ship it.",
    created_at: "2026-11-12T09:00:00Z",
    commit_oid: OLDER,
    ...fields,
  });

  it("a finish landed once the review reads submitted on its own commit, with its verdict and summary", () => {
    expect(landedReview(sent, check([submitted()]))).toMatchObject({ id: REVIEW_ID });
  });

  it("a finish matched on the head, not the review's commit, isn't it", () => {
    expect(landedReview(sent, check([submitted({ commit_oid: HEAD })]))).toBeNull();
    expect(landedReview(sent, check([submitted({ state: "COMMENTED" })]))).toBeNull();
    expect(landedReview(sent, check([submitted({ body: "Other." })]))).toBeNull();
    // Gone from the pending list alone says nothing.
    expect(landedReview(sent, check([]))).toBeNull();
  });

  it("a discard is gone only when the review by its id reads absent", () => {
    expect(discardVerdict(REVIEW_ID, "absent")).toEqual({ kind: "discarded", reviewId: REVIEW_ID });
    expect(discardVerdict(REVIEW_ID, "PENDING")).toEqual({
      kind: "not_discarded",
      reviewId: REVIEW_ID,
    });
    for (const state of ["COMMENTED", "APPROVED", "CHANGES_REQUESTED", "DISMISSED"] as const) {
      expect(discardVerdict(REVIEW_ID, state)).toEqual({
        kind: "submitted_elsewhere",
        reviewId: REVIEW_ID,
      });
    }
    expect(discardVerdict(REVIEW_ID, "unreadable")).toEqual(HELD);
  });
});
