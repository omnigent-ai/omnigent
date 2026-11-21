import { afterEach, describe, expect, it, vi } from "vitest";

import {
  changedReviewFiles,
  publishShownReview,
  resetShownReviewsForTests,
  reviewMembership,
  settledShownReview,
  shownReview,
} from "./reviewFileLinks";

afterEach(() => {
  resetShownReviewsForTests();
  vi.useRealTimers();
});

describe("reviewMembership", () => {
  it("includes a file the review lists, and not one it doesn't", () => {
    const review = { state: "listed", paths: ["src/a.py"] } as const;
    expect(reviewMembership(review, "src/a.py")).toBe("included");
    expect(reviewMembership(review, "src/b.py")).toBe("excluded");
  });

  it("doesn't know while the comparison is pending, and excludes every file when it can't compare", () => {
    expect(reviewMembership({ state: "pending" }, "src/a.py")).toBe("unknown");
    expect(reviewMembership({ state: "unavailable" }, "src/a.py")).toBe("excluded");
  });
});

describe("changedReviewFiles", () => {
  it("lists the changed files once they load", () => {
    const listed = { comparison: { base: "head" }, data: { data: [{ path: "src/a.py" }] } };
    expect(changedReviewFiles(listed)).toEqual({ state: "listed", paths: ["src/a.py"] });
  });

  it("is pending while the comparison or its list loads, not an empty list", () => {
    expect(changedReviewFiles({ comparison: { base: "pending" } })).toEqual({ state: "pending" });
    expect(changedReviewFiles({ comparison: { base: "head" } })).toEqual({ state: "pending" });
  });

  it("shows no files when it can't compare, or its list failed to load", () => {
    expect(changedReviewFiles({ comparison: { base: "unavailable" } })).toEqual({
      state: "unavailable",
    });
    expect(changedReviewFiles({ comparison: { base: "head" }, isError: true })).toEqual({
      state: "unavailable",
    });
  });
});

describe("publishShownReview", () => {
  it("shows a session's review until its owner withdraws it", () => {
    const withdraw = publishShownReview("s1", {}, { state: "listed", paths: ["src/a.py"] });
    expect(shownReview("s1")).toEqual({ state: "listed", paths: ["src/a.py"] });
    expect(shownReview("s2")).toBeNull();
    withdraw();
    expect(shownReview("s1")).toBeNull();
  });

  it("keeps a newer owner's review when an older owner withdraws", () => {
    const withdrawOlder = publishShownReview("s1", {}, { state: "listed", paths: ["old.py"] });
    publishShownReview("s1", {}, { state: "listed", paths: ["new.py"] });
    withdrawOlder();
    expect(shownReview("s1")).toEqual({ state: "listed", paths: ["new.py"] });
  });
});

describe("settledShownReview", () => {
  it("answers at once for a known comparison, or no review", async () => {
    await expect(settledShownReview("s1")).resolves.toBeNull();
    publishShownReview("s1", {}, { state: "unavailable" });
    await expect(settledShownReview("s1")).resolves.toEqual({ state: "unavailable" });
  });

  it("waits for a pending comparison to settle", async () => {
    const owner = {};
    publishShownReview("s1", owner, { state: "pending" });
    const settled = settledShownReview("s1");
    publishShownReview("s1", owner, { state: "listed", paths: ["src/a.py"] });
    await expect(settled).resolves.toEqual({ state: "listed", paths: ["src/a.py"] });
  });

  it("keeps waiting through a review replacing its publication in one commit", async () => {
    const owner = {};
    const withdraw = publishShownReview("s1", owner, { state: "pending" });
    const settled = settledShownReview("s1");
    // An effect's cleanup and its next run: withdrawn, then published again at once.
    withdraw();
    publishShownReview("s1", owner, { state: "listed", paths: ["src/a.py"] });
    await expect(settled).resolves.toEqual({ state: "listed", paths: ["src/a.py"] });
  });

  it("ends when the review leaves the screen, or after its timeout", async () => {
    const withdraw = publishShownReview("s1", {}, { state: "pending" });
    const left = settledShownReview("s1");
    withdraw();
    await expect(left).resolves.toBeNull();

    vi.useFakeTimers();
    publishShownReview("s1", {}, { state: "pending" });
    const timedOut = settledShownReview("s1", 1_000);
    vi.advanceTimersByTime(1_000);
    await expect(timedOut).resolves.toBeNull();
  });
});
