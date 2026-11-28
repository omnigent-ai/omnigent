import { afterEach, describe, expect, it, vi } from "vitest";

import type { ReviewCheck } from "@/lib/pullRequestWriteAnswers";

import {
  EMPTY_REVIEW,
  landedReview,
  landedReviewCandidates,
  OWN_APPROVAL_REASON,
  OWN_PULL_REQUEST_REASON,
  pendingSubmitKey,
  pendingSubmitsOf,
  readReviewSubmit,
  REVIEW_MENU_REQUEST_WAIT_MS,
  requestReviewMenu,
  resetReviewSubmitForTests,
  reviewSubmitKey,
  type SentReview,
  summaryRequired,
  takeReviewMenuRequest,
  updateReviewSubmit,
  verdictUnavailableReason,
} from "./reviewSubmit";
import { submitUnavailableReason } from "./useReviewSubmit";

const HEAD = "a".repeat(40);
const sent = (fields: Partial<SentReview> = {}): SentReview => ({
  event: "APPROVE",
  body: "Ship it",
  head: HEAD,
  knownReviewIds: ["PRR_old"],
  // The click, a minute before GitHub stamped the review it made.
  sentAt: Date.parse("2026-11-08T09:59:00Z"),
  ...fields,
});
const checkWith = (reviews: ReviewCheck["reviews"]): ReviewCheck => ({
  head_sha: HEAD,
  author: "author-octo",
  pending: false,
  reviews,
});
const review = (fields: Partial<ReviewCheck["reviews"][number]> = {}) => ({
  id: "PRR_new",
  state: "APPROVED",
  body: "Ship it",
  created_at: "2026-11-08T10:00:00Z",
  commit_oid: HEAD,
  ...fields,
});

afterEach(() => resetReviewSubmitForTests());

describe("who may pick which verdict", () => {
  it("keeps the author from approving or requesting changes, in any case", () => {
    expect(verdictUnavailableReason("APPROVE", "Octo", "octo")).toBe(OWN_APPROVAL_REASON);
    expect(verdictUnavailableReason("REQUEST_CHANGES", "octo", "octo")).toBe(
      OWN_PULL_REQUEST_REASON,
    );
    expect(verdictUnavailableReason("COMMENT", "octo", "octo")).toBeNull();
  });

  it("lets everyone else pick any verdict, and a deleted author's PR too", () => {
    expect(verdictUnavailableReason("APPROVE", "reviewer", "octo")).toBeNull();
    expect(verdictUnavailableReason("APPROVE", "reviewer", null)).toBeNull();
  });

  it("needs a summary to comment or request changes, not to approve", () => {
    expect(summaryRequired("COMMENT")).toBe(true);
    expect(summaryRequired("REQUEST_CHANGES")).toBe(true);
    expect(summaryRequired("APPROVE")).toBe(false);
  });
});

describe("whether Submit can run", () => {
  const ready = { viewer: "reviewer", author: "octo", headSha: HEAD, pending: false };
  const entry = (fields: Partial<typeof EMPTY_REVIEW> = {}) => ({
    ...EMPTY_REVIEW,
    body: "Looks good",
    ...fields,
  });

  it("runs with a summary on a known head", () => {
    expect(submitUnavailableReason(entry(), ready)).toBeNull();
    expect(submitUnavailableReason(entry({ event: "APPROVE", body: "" }), ready)).toBeNull();
  });

  it("waits for a summary, a known head, and a review in progress elsewhere to finish", () => {
    expect(submitUnavailableReason(entry({ body: "  " }), ready)).toBe("Write a summary first.");
    expect(submitUnavailableReason(entry(), { ...ready, headSha: null })).toMatch(/Refresh/);
    expect(submitUnavailableReason(entry(), { ...ready, pending: true })).toMatch(
      /in progress on GitHub/,
    );
  });

  it("never runs while a submit is out or unconfirmed", () => {
    expect(
      submitUnavailableReason(entry({ status: { kind: "submitting" } }), ready),
    ).not.toBeNull();
    expect(
      submitUnavailableReason(
        entry({
          status: {
            kind: "uncertain",
            sent: sent(),
            message: "",
            checking: false,
            checked: "never",
          },
        }),
        ready,
      ),
    ).toMatch(/didn’t confirm/);
  });

  it("refuses the author's approval", () => {
    expect(submitUnavailableReason(entry({ event: "APPROVE" }), { ...ready, viewer: "OCTO" })).toBe(
      OWN_APPROVAL_REASON,
    );
  });
});

describe("settling a submit GitHub didn't confirm", () => {
  it("finds the new review with the same verdict, commit and summary", () => {
    expect(landedReview(sent(), checkWith([review()]))?.id).toBe("PRR_new");
    // GitHub may store line endings as CRLF and trim the ends.
    expect(
      landedReview(sent({ body: "Ship\nit " }), checkWith([review({ body: "Ship\r\nit" })]))?.id,
    ).toBe("PRR_new");
  });

  it("never counts a review that was there before the submit", () => {
    expect(landedReview(sent(), checkWith([review({ id: "PRR_old" })]))).toBeNull();
  });

  it("never counts the viewer's unrelated reviews", () => {
    const unrelated = [
      review({ state: "COMMENTED" }),
      review({ commit_oid: "b".repeat(40) }),
      review({ commit_oid: null }),
      review({ body: "Something else" }),
    ];
    for (const other of unrelated) expect(landedReview(sent(), checkWith([other]))).toBeNull();
  });

  it("never counts a matching review made before the click, whose id main didn't see", () => {
    // Older than the 30 reviews main snapshots, back in view after another was deleted.
    expect(
      landedReview(
        sent(),
        checkWith([review({ id: "PRR_ancient", created_at: "2020-01-01T00:00:00Z" })]),
      ),
    ).toBeNull();
    expect(
      landedReview(sent(), checkWith([review({ created_at: "2026-11-08T09:50:00Z" })])),
    ).toBeNull();
  });

  it("allows for GitHub's clock running a little behind this Mac's", () => {
    expect(
      landedReview(sent(), checkWith([review({ created_at: "2026-11-08T09:58:00Z" })]))?.id,
    ).toBe("PRR_new");
  });

  it("never counts a review without a time it was made, or one it can't read", () => {
    for (const created_at of [null, "", "yesterday", "2026-13-45T99:00:00Z"]) {
      expect(landedReview(sent(), checkWith([review({ created_at })]))).toBeNull();
    }
  });

  it("settles a send a reload cut off only when exactly one review could be it", () => {
    const cutOff = sent({ knownReviewIds: null });
    expect(landedReview(cutOff, checkWith([review()]))?.id).toBe("PRR_new");
    const twin = review({ id: "PRR_twin" });
    expect(landedReview(cutOff, checkWith([review(), twin]))).toBeNull();
    expect(landedReviewCandidates(cutOff, checkWith([review(), twin])).map((r) => r.id)).toEqual([
      "PRR_new",
      "PRR_twin",
    ]);
    expect(landedReview(cutOff, checkWith([]))).toBeNull();
    // Still only reviews made after the click count, allowing for GitHub's clock.
    expect(
      landedReview(cutOff, checkWith([review({ created_at: "2026-11-08T09:50:00Z" })])),
    ).toBeNull();
    // Another verdict, commit or summary isn't it.
    expect(landedReview(cutOff, checkWith([review({ state: "COMMENTED" })]))).toBeNull();
    expect(landedReview(cutOff, checkWith([review({ commit_oid: "other" })]))).toBeNull();
    expect(landedReview(cutOff, checkWith([review({ body: "Something else" })]))).toBeNull();
  });

  it("doesn't take a review that's gone, or none at all, as submitted", () => {
    expect(landedReview(sent(), checkWith([]))).toBeNull();
  });
});

describe("one review per workspace user, PR and account", () => {
  it("keys a PR's review by user and Mac account, any case of URL or login", () => {
    const pr = "https://github.com/acme/app/pull/42";
    expect(reviewSubmitKey("user-1", pr, "Octo")).toBe(
      reviewSubmitKey("user-1", pr.toUpperCase(), "octo"),
    );
    const keys = new Set([
      reviewSubmitKey("user-1", pr, "octo"),
      reviewSubmitKey("user-2", pr, "octo"),
      reviewSubmitKey("user-1", pr, "other"),
      reviewSubmitKey("user-1", "https://github.com/acme/app/pull/43", "octo"),
    ]);
    expect(keys.size).toBe(4);
  });

  it("keeps a summary under its own key only", () => {
    const pr = "https://github.com/acme/app/pull/42";
    const mine = reviewSubmitKey("user-1", pr, "octo");
    const other = reviewSubmitKey("user-1", pr, "other");
    updateReviewSubmit(mine, (entry) => ({ ...entry, body: "Mine" }));
    expect(readReviewSubmit(mine).body).toBe("Mine");
    expect(readReviewSubmit(other)).toBe(EMPTY_REVIEW);
  });
});

describe("a review this window kept before page logins went away", () => {
  it("still loads, without the page login it was read as", () => {
    resetReviewSubmitForTests();
    const pr = "https://github.com/acme/app/pull/42";
    const key = pendingSubmitKey("user-1", pr, { login: "octo" }, "PRR_1");
    resetReviewSubmitForTests();
    const reader = { host: "github.com", login: "octo" };
    const value = {
      owner: "user-1",
      prUrl: pr,
      login: "octo",
      pending: { reader, reviewId: "PRR_1" },
      event: "APPROVE",
      body: "Ship it",
      held: { sent: sent(), message: "Check whether it was submitted." },
      sentAs: { actor: { login: "octo", generation: 2 }, reader },
    };
    window.sessionStorage.setItem(
      `omnigent:review-drafts:v1:review-submits:${encodeURIComponent(key)}`,
      JSON.stringify({ v: 1, key, value }),
    );
    expect(readReviewSubmit(key)).toMatchObject({
      body: "Ship it",
      status: { kind: "uncertain" },
      sentAs: { actor: { login: "octo", generation: 2 } },
    });
    expect(readReviewSubmit(key).sentAs).not.toHaveProperty("reader");
    expect(pendingSubmitsOf("user-1", pr).map((each) => each.scope)).toEqual([
      { reviewId: "PRR_1" },
    ]);
  });
});

describe("asking for the Review menu", () => {
  const URL = "https://github.com/acme/app/pull/42";

  it("hands the verdict to the first menu for the pull request, and only that one", () => {
    requestReviewMenu(URL, "APPROVE");
    expect(takeReviewMenuRequest("https://github.com/acme/app/pull/7")).toBeUndefined();
    expect(takeReviewMenuRequest(URL)?.event).toBe("APPROVE");
    expect(takeReviewMenuRequest(URL)).toBeUndefined();
  });

  it("takes a request handed back by a menu that went away, but not a replaced one", () => {
    requestReviewMenu(URL, "APPROVE");
    takeReviewMenuRequest(URL)!.release();
    const again = takeReviewMenuRequest(URL);
    // The verdict was applied when first taken; the replacement keeps the draft's.
    expect(again).toBeDefined();
    expect(again?.event).toBeNull();
    requestReviewMenu(URL, "REQUEST_CHANGES");
    // Handing back the older request leaves the newer one as it is.
    again!.release();
    expect(takeReviewMenuRequest(URL)?.event).toBe("REQUEST_CHANGES");
    expect(takeReviewMenuRequest(URL)).toBeUndefined();
  });

  it("waits afresh for the replacement of a menu that hands its request back, naming no verdict", () => {
    const start = Date.now();
    requestReviewMenu(URL, "APPROVE");
    const now = vi.spyOn(Date, "now").mockReturnValue(start + 2 * REVIEW_MENU_REQUEST_WAIT_MS);
    takeReviewMenuRequest(URL, start)!.release();
    const again = takeReviewMenuRequest(URL, start + 2 * REVIEW_MENU_REQUEST_WAIT_MS + 1_000);
    expect(again).toBeDefined();
    expect(again?.event).toBeNull();
    now.mockRestore();
  });

  it("stops waiting for the menu after a while", () => {
    const now = Date.now();
    requestReviewMenu(URL, null);
    expect(takeReviewMenuRequest(URL, now + REVIEW_MENU_REQUEST_WAIT_MS + 1_000)).toBeUndefined();
    requestReviewMenu(URL, null);
    expect(takeReviewMenuRequest(URL, now + 1_000)?.event).toBeNull();
  });
});
