import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import {
  BOARD_ACCEPTED_LABEL_KEY,
  boardAcceptedAt,
  boardColumn,
  canAcceptOnBoard,
  groupBoardColumns,
  isBoardAccepted,
} from "./sessionBoard";

function row(overrides: Partial<Conversation> = {}): Conversation {
  return {
    id: "conv_a",
    object: "conversation",
    title: "A",
    created_at: 1_700_000_000,
    updated_at: 1_700_000_100,
    labels: {},
    permission_level: null,
    status: "idle",
    ...overrides,
  };
}

const accepted = (at: number) => ({ [BOARD_ACCEPTED_LABEL_KEY]: String(at) });

describe("boardAcceptedAt", () => {
  it("reads the accepted timestamp and ignores empty or malformed values", () => {
    expect(boardAcceptedAt(row({ labels: accepted(1_700_000_101) }))).toBe(1_700_000_101);
    expect(boardAcceptedAt(row({ labels: { [BOARD_ACCEPTED_LABEL_KEY]: "" } }))).toBeNull();
    expect(boardAcceptedAt(row({ labels: { [BOARD_ACCEPTED_LABEL_KEY]: "soon" } }))).toBeNull();
    expect(boardAcceptedAt(row())).toBeNull();
  });
});

describe("isBoardAccepted", () => {
  it("holds until the session changes after acceptance", () => {
    expect(isBoardAccepted(row({ labels: accepted(1_700_000_101) }))).toBe(true);
    expect(isBoardAccepted(row({ labels: accepted(1_700_000_099) }))).toBe(false);
  });

  it("reopens on activity within the second the server stamped the acceptance", () => {
    // The server stamps a second after the reviewed activity; a write landing
    // in that same second gives updated_at equal to the stamp.
    expect(
      isBoardAccepted(row({ updated_at: 1_700_000_101, labels: accepted(1_700_000_101) })),
    ).toBe(false);
  });
});

describe("boardColumn", () => {
  it("puts pending approvals and terminal prompts in Needs you, even when accepted", () => {
    const labels = accepted(1_700_000_101);
    expect(boardColumn(row({ status: "running", pending_elicitations_count: 1, labels }))).toBe(
      "needs_you",
    );
    expect(boardColumn(row({ status: "running", blocked_on: "permission prompt" }))).toBe(
      "needs_you",
    );
  });

  it("puts running turns and background work in Running", () => {
    expect(boardColumn(row({ status: "running" }))).toBe("running");
    expect(boardColumn(row({ status: "idle", background_work_running: true }))).toBe("running");
  });

  it("keeps idle work in Needs review until it is accepted", () => {
    expect(boardColumn(row())).toBe("needs_review");
    expect(boardColumn(row({ labels: accepted(1_700_000_101) }))).toBe("done");
    // New activity after acceptance reopens the review.
    expect(boardColumn(row({ labels: accepted(1_700_000_050) }))).toBe("needs_review");
  });

  it("asks for attention on a failed turn until the user accepts it", () => {
    expect(boardColumn(row({ status: "failed" }))).toBe("needs_you");
    expect(boardColumn(row({ status: "failed", labels: accepted(1_700_000_101) }))).toBe("done");
  });
});

describe("canAcceptOnBoard", () => {
  it("offers Accept only for idle work that is not already accepted", () => {
    expect(canAcceptOnBoard(row())).toBe(true);
    expect(canAcceptOnBoard(row({ status: "failed" }))).toBe(true);
    expect(canAcceptOnBoard(row({ status: "running" }))).toBe(false);
    expect(canAcceptOnBoard(row({ pending_elicitations_count: 2 }))).toBe(false);
    expect(canAcceptOnBoard(row({ labels: accepted(1_700_000_101) }))).toBe(false);
  });
});

describe("groupBoardColumns", () => {
  it("orders each column by latest activity and drops archived and provisional rows", () => {
    const columns = groupBoardColumns([
      row({ id: "old_review", updated_at: 10 }),
      row({ id: "new_review", updated_at: 30 }),
      row({ id: "running", status: "running", updated_at: 20 }),
      row({ id: "archived", archived: true, updated_at: 40 }),
      row({ id: "temp:new", provisional: true, updated_at: 50 }),
    ]);
    expect(columns.needs_review.map((c) => c.id)).toEqual(["new_review", "old_review"]);
    expect(columns.running.map((c) => c.id)).toEqual(["running"]);
    expect(columns.needs_you).toEqual([]);
    expect(columns.done).toEqual([]);
  });
});
