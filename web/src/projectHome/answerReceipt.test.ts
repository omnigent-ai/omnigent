import { describe, expect, it } from "vitest";
import type { TicketAnswerReceipt } from "@/lib/projectTicketsApi";
import { answerReceiptLine, canNudge, NUDGE_AFTER_MS } from "./answerReceipt";

const NOW = 1_800_000_000_000;

function receipt(overrides: Partial<TicketAnswerReceipt> = {}): TicketAnswerReceipt {
  return {
    position: 5,
    text: "Decision: A",
    answered_at: NOW / 1000 - 60,
    asker_session_id: "worker",
    delivery: "sent",
    replied_at: null,
    replied_by_session_id: null,
    ...overrides,
  };
}

describe("answerReceiptLine", () => {
  it("says only what the stored delivery state shows", () => {
    expect(answerReceiptLine(receipt({ delivery: "waiting" }), "Designer", NOW)).toBe(
      "Not delivered yet",
    );
    expect(answerReceiptLine(receipt(), "Designer", NOW)).toBe(
      "Sent to Designer · not picked up yet",
    );
    expect(answerReceiptLine(receipt({ asker_status: "running" }), "Designer", NOW)).toBe(
      "Sent to Designer · working now",
    );
    expect(answerReceiptLine(receipt({ delivery: "escalated" }), "Designer", NOW)).toBe(
      "No reply from Designer yet · the Lead was told",
    );
    expect(
      answerReceiptLine(
        receipt({ delivery: "replied", replied_at: NOW / 1000 - 120 }),
        "Designer",
        NOW,
      ),
    ).toBe("Designer picked it up · replied 2m ago");
    expect(
      answerReceiptLine(receipt({ delivery: "replied", replied_at: NOW / 1000 }), "the agent", NOW),
    ).toBe("The agent picked it up · replied just now");
  });
});

describe("canNudge", () => {
  it("offers Nudge only after the timeout, with no reply and a session to reach", () => {
    const late = receipt({ answered_at: (NOW - NUDGE_AFTER_MS) / 1000 });
    expect(canNudge(receipt(), NOW)).toBe(false);
    expect(canNudge(late, NOW)).toBe(true);
    expect(canNudge({ ...late, delivery: "replied" }, NOW)).toBe(false);
    expect(canNudge({ ...late, asker_session_id: null }, NOW)).toBe(false);
  });
});
