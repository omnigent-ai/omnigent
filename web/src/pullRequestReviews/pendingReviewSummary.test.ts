import { afterEach, describe, expect, it } from "vitest";

import {
  carryRegularSummary,
  editPendingSummary,
  keepMyPendingSummary,
  mergedSummary,
  openPendingSummary,
  pendingSummaryDiscarded,
  pendingSummaryKey,
  pendingSummarySubmitted,
  readPendingSummary,
  reconcilePendingSummary,
  resetPendingSummariesForTests,
  takeGitHubsPendingSummary,
} from "./pendingReviewSummary";
import {
  pendingSubmitKey,
  readReviewSubmit,
  resetReviewSubmitForTests,
  reviewSubmitKey,
  updateReviewSubmit,
} from "./reviewSubmit";

const PR = "https://github.com/acme/app/pull/42";
const key = pendingSummaryKey("user-1", PR, { login: "alice", namedReader: null }, "PRR_1");

afterEach(() => {
  resetPendingSummariesForTests();
  resetReviewSubmitForTests();
});

describe("a pending review's summary", () => {
  it("is seeded from GitHub's once, when Finish first opens on the review", () => {
    expect(openPendingSummary(key, "Started on GitHub.")).toEqual({
      text: "Started on GitHub.",
      seededFrom: "Started on GitHub.",
      edited: false,
    });
    editPendingSummary(key, "Mine.");
    // Closing and opening again keeps what was typed.
    expect(openPendingSummary(key, "Started on GitHub.").text).toBe("Mine.");
  });

  it("is kept apart by workspace user, scope, login and review", () => {
    openPendingSummary(key, "Alice's.");
    const named = pendingSummaryKey(
      "user-1",
      PR,
      { login: "alice", namedReader: "alice" },
      "PRR_1",
    );
    const otherUser = pendingSummaryKey(
      "user-2",
      PR,
      { login: "alice", namedReader: null },
      "PRR_1",
    );
    const otherReview = pendingSummaryKey(
      "user-1",
      PR,
      { login: "alice", namedReader: null },
      "PRR_2",
    );
    for (const other of [named, otherUser, otherReview]) {
      expect(other).not.toBe(key);
      expect(readPendingSummary(other)).toBeNull();
    }
  });

  it("follows GitHub's quietly until edited, and never overwrites an edit", () => {
    openPendingSummary(key, "First.");
    expect(reconcilePendingSummary(key, "Second.")).toEqual({
      summary: { text: "Second.", seededFrom: "Second.", edited: false },
      conflict: false,
    });
    editPendingSummary(key, "Mine.");
    // A refetch with the same summary as seeded changes nothing.
    expect(reconcilePendingSummary(key, "Second.")).toMatchObject({
      conflict: false,
      summary: { text: "Mine." },
    });
    expect(reconcilePendingSummary(key, "Third.")).toMatchObject({
      conflict: true,
      summary: { text: "Mine." },
    });
  });

  it("offers Use GitHub's and Keep mine when GitHub's changed after an edit", () => {
    openPendingSummary(key, "First.");
    editPendingSummary(key, "Mine.");
    expect(reconcilePendingSummary(key, "Theirs.").conflict).toBe(true);
    keepMyPendingSummary(key, "Theirs.");
    expect(reconcilePendingSummary(key, "Theirs.")).toMatchObject({
      conflict: false,
      summary: { text: "Mine." },
    });
    takeGitHubsPendingSummary(key, "Theirs.");
    expect(readPendingSummary(key)).toEqual({
      text: "Theirs.",
      seededFrom: "Theirs.",
      edited: false,
    });
  });

  it("isn't a conflict when the edit already matches GitHub's", () => {
    openPendingSummary(key, "First.");
    editPendingSummary(key, "Same.");
    expect(reconcilePendingSummary(key, "Same.\r\n").conflict).toBe(false);
  });

  it("clears after a confirmed submit only if it still holds what was sent", () => {
    openPendingSummary(key, "");
    editPendingSummary(key, "Sent.");
    pendingSummarySubmitted(key, "Sent.");
    expect(readPendingSummary(key)).toBeNull();
    editPendingSummary(key, "Sent.");
    // Typed after pressing Submit: the newer text survives.
    editPendingSummary(key, "Sent. And more.");
    pendingSummarySubmitted(key, "Sent.");
    expect(readPendingSummary(key)?.text).toBe("Sent. And more.");
  });

  it("goes with a confirmed discard", () => {
    openPendingSummary(key, "Started.");
    pendingSummaryDiscarded(key);
    expect(readPendingSummary(key)).toBeNull();
  });
});

describe("a regular review's summary moving into the first pending review", () => {
  const regular = reviewSubmitKey("user-1", PR, "alice");
  const pending = pendingSubmitKey("user-1", PR, { login: "alice" }, "PRR_1");
  const carry = (githubBody = "") =>
    carryRegularSummary({ regularKey: regular, pendingKey: pending, summaryKey: key, githubBody });
  const typeRegular = (body: string, event: "COMMENT" | "REQUEST_CHANGES" = "COMMENT") =>
    updateReviewSubmit(regular, (entry) => ({ ...entry, body, event }));

  it.each([
    ["", "Typed.", "Typed."],
    ["From GitHub.", "Typed.", "From GitHub.\n\nTyped."],
    ["From GitHub.\n\nTyped.", "Typed.", "From GitHub.\n\nTyped."],
    ["Short", "Short and longer", "Short and longer"],
    ["Mine.", "  ", "Mine."],
  ])("merges %j with %j as %j", (existing, addition, merged) => {
    expect(mergedSummary(existing, addition)).toBe(merged);
  });

  it("takes the typed text into an empty summary, with its verdict, and clears the regular one", () => {
    typeRegular("Typed.", "REQUEST_CHANGES");
    carry();
    expect(readPendingSummary(key)).toEqual({ text: "Typed.", seededFrom: "", edited: true });
    expect(readReviewSubmit(pending).event).toBe("REQUEST_CHANGES");
    expect(readReviewSubmit(regular).body).toBe("");
  });

  it("keeps GitHub's summary and the person's edit, adding the typed text after them", () => {
    typeRegular("Typed.");
    carry("From GitHub.");
    expect(readPendingSummary(key)).toEqual({
      text: "From GitHub.\n\nTyped.",
      seededFrom: "From GitHub.",
      edited: true,
    });
    // GitHub's own summary changing later is still offered against the merged text.
    expect(reconcilePendingSummary(key, "Changed on GitHub.").conflict).toBe(true);

    resetPendingSummariesForTests();
    openPendingSummary(key, "From GitHub.");
    editPendingSummary(key, "Edited here.");
    typeRegular("Typed.");
    carry("From GitHub.");
    expect(readPendingSummary(key)?.text).toBe("Edited here.\n\nTyped.");
  });

  it("moves once: a later read or a replacement review gets nothing more", () => {
    typeRegular("Typed.");
    carry("From GitHub.");
    carry("From GitHub.");
    expect(readPendingSummary(key)?.text).toBe("From GitHub.\n\nTyped.");
    const replacement = pendingSummaryKey(
      "user-1",
      PR,
      { login: "alice", namedReader: null },
      "PRR_2",
    );
    carryRegularSummary({
      regularKey: regular,
      pendingKey: pendingSubmitKey("user-1", PR, { login: "alice" }, "PRR_2"),
      summaryKey: replacement,
      githubBody: "",
    });
    expect(readPendingSummary(replacement)).toBeNull();
  });

  it("leaves a review on its way or held unconfirmed, on either side, as it is", () => {
    typeRegular("Sent.");
    updateReviewSubmit(regular, (entry) => ({ ...entry, status: { kind: "submitting" } }));
    carry();
    expect(readPendingSummary(key)).toBeNull();
    expect(readReviewSubmit(regular).body).toBe("Sent.");

    updateReviewSubmit(regular, (entry) => ({ ...entry, status: { kind: "idle" } }));
    updateReviewSubmit(pending, (entry) => ({ ...entry, status: { kind: "submitting" } }));
    carry();
    expect(readPendingSummary(key)).toBeNull();
    expect(readReviewSubmit(regular).body).toBe("Sent.");
  });

  it("keeps a verdict already picked for the pending review", () => {
    updateReviewSubmit(pending, (entry) => ({ ...entry, event: "APPROVE" }));
    typeRegular("Typed.", "REQUEST_CHANGES");
    carry();
    expect(readReviewSubmit(pending).event).toBe("APPROVE");
  });
});
