import { describe, expect, it } from "vitest";
import { type ActivityItem, repliesTo } from "./sessionActivity.ts";

const message = (id: string, role: string, text: string, extra: Partial<ActivityItem> = {}): ActivityItem => ({
  id,
  type: "message",
  role,
  created_at: Number(id.replace(/\D/g, "")),
  content: [{ type: "text", text }],
  ...extra,
});

describe("repliesTo", () => {
  it("keeps the agent's answers to a PO message until the next user turn", () => {
    const items: ActivityItem[] = [
      message("m1", "user", "current status"),
      message("m2", "assistant", "Checking the board."),
      { id: "m3", type: "function_call", name: "office_board_get" },
      message("m4", "assistant", "QA: 43 of 46 pass."),
      message("m5", "user", "[From QA via the office] done", { office_relayed: true }),
      message("m6", "assistant", "Noted QA."),
    ];
    expect(repliesTo(new Set(["current status"]), items).map((entry) => [entry.id, entry.text, entry.at])).toEqual([
      ["m2", "Checking the board.", 2],
      ["m4", "QA: 43 of 46 pass.", 4],
    ]);
  });

  it("skips answers to messages the PO did not send from the room", () => {
    const items = [message("m1", "user", "sent from the CLI"), message("m2", "assistant", "Answer")];
    expect(repliesTo(new Set(["current status"]), items)).toEqual([]);
  });
});
