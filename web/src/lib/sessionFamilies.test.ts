import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { isIndentedSibling, orderSessionFamilies } from "./sessionFamilies";

const member = (id: string, created_at: number, representative_id = "a") =>
  ({ id, created_at, family: { id: "family", representative_id, pinned: false } }) as Conversation;

describe("session families", () => {
  it("keeps family position, promotes the canonical representative and never nests twice", () => {
    const [a, b, c] = [member("a", 1), member("b", 2), member("c", 3)];
    const standalone = { id: "x", created_at: 4 } as Conversation;
    expect(orderSessionFamilies([standalone, c, a, b]).map((row) => row.id)).toEqual([
      "x",
      "a",
      "b",
      "c",
    ]);
    expect([a, b, c].map(isIndentedSibling)).toEqual([false, true, true]);
    const promoted = [member("c", 3, "b"), member("b", 2, "b")];
    expect(orderSessionFamilies(promoted).map((row) => row.id)).toEqual(["b", "c"]);
    expect(isIndentedSibling(promoted[1])).toBe(false);
  });

  it("retains an individually pinned shortcut when its representative is outside this section", () => {
    const b = member("b", 2);
    expect(orderSessionFamilies([b])).toEqual([b]);
  });
});
