import { describe, expect, it } from "vitest";

import {
  APPROVE_HELD,
  APPROVE_STATE_UNKNOWN,
  type ApproveFacts,
  approveUnavailableReason,
} from "./approveEligibility";

const IDLE = { status: { kind: "idle" as const }, body: "" };
const facts = (over: Partial<ApproveFacts> = {}): ApproveFacts => ({
  mode: "no-comment",
  open: true,
  author: "carol",
  actor: "octo",
  reviewsOffered: true,
  headSha: "a".repeat(40),
  entry: IDLE,
  ...over,
});

describe("when an approval may be sent", () => {
  it("allows an open PR by someone else at a known head, whoever it asks for a review", () => {
    expect(approveUnavailableReason(facts())).toBeNull();
    expect(approveUnavailableReason(facts({ entry: null }))).toBeNull();
    expect(approveUnavailableReason(facts({ mode: "with-body" }))).toBeNull();
  });

  it.each([
    { case: "state unknown", over: { open: null }, reason: APPROVE_STATE_UNKNOWN },
    { case: "closed", over: { open: false }, reason: "It’s no longer open." },
    { case: "no account", over: { actor: null }, reason: "Reviews can’t be sent from this Mac." },
    {
      case: "reviews not offered",
      over: { reviewsOffered: false },
      reason: "Reviews can’t be sent from this Mac.",
    },
    {
      case: "your own, in any case",
      over: { author: "OCTO" },
      reason: "You can’t approve your own pull request.",
    },
    {
      case: "a review in flight",
      over: { entry: { status: { kind: "submitting" as const }, body: "" } },
      reason: APPROVE_HELD,
    },
    { case: "head unknown", over: { headSha: null }, reason: "Its latest commit isn’t known yet." },
  ])("refuses one $case", ({ over, reason }) => {
    expect(approveUnavailableReason(facts(over))).toBe(reason);
  });

  it("checks in order: state, then account, then authorship, then holds, then the head", () => {
    expect(approveUnavailableReason(facts({ open: false, actor: null }))).toBe(
      "It’s no longer open.",
    );
    expect(approveUnavailableReason(facts({ actor: null, author: "octo" }))).toBe(
      "Reviews can’t be sent from this Mac.",
    );
    expect(
      approveUnavailableReason(
        facts({ author: "octo", entry: { status: { kind: "submitting" }, body: "" } }),
      ),
    ).toBe("You can’t approve your own pull request.");
    expect(
      approveUnavailableReason(
        facts({ headSha: null, entry: { status: { kind: "submitting" }, body: "" } }),
      ),
    ).toBe(APPROVE_HELD);
  });

  it("holds a no-comment approval for a draft, but sends the draft with one from the menu", () => {
    const drafted = { status: { kind: "idle" as const }, body: "Looks good" };
    expect(approveUnavailableReason(facts({ entry: drafted }))).toBe(APPROVE_HELD);
    expect(approveUnavailableReason(facts({ mode: "with-body", entry: drafted }))).toBeNull();
  });

  it("holds either mode while an earlier review's outcome is unknown", () => {
    const sent = { event: "APPROVE" as const, body: "", head: "a", knownReviewIds: [], sentAt: 0 };
    const uncertain = {
      status: {
        kind: "uncertain" as const,
        sent,
        message: "",
        checking: false,
        checked: "never" as const,
      },
      body: "",
    };
    expect(approveUnavailableReason(facts({ entry: uncertain }))).toBe(APPROVE_HELD);
    expect(approveUnavailableReason(facts({ mode: "with-body", entry: uncertain }))).toBe(
      APPROVE_HELD,
    );
  });
});
