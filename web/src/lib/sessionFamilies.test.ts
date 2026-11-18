import { describe, expect, it } from "vitest";
import type { Conversation } from "@/hooks/useConversations";
import { isIndentedSibling, isSessionPinned, orderSessionFamilies } from "./sessionFamilies";

const member = (id: string, created_at: number, representative_id = "a") =>
  ({ id, created_at, family: { id: "family", representative_id, pinned: false } }) as Conversation;

describe("session families", () => {
  it("keeps family position, promotes the canonical representative and never nests twice", () => {
    const [a, b, c] = [member("a", 1), member("b", 2), member("c", 3)];
    const standalone = { id: "x", created_at: 4 } as Conversation;
    expect(orderSessionFamilies([standalone, c, a, b]).map((row) => row.id)).toEqual([
      "x",
      "a",
      "c",
      "b",
    ]);
    expect([a, b, c].map(isIndentedSibling)).toEqual([false, true, true]);
    const promoted = [member("c", 3, "b"), member("b", 2, "b")];
    expect(orderSessionFamilies(promoted).map((row) => row.id)).toEqual(["b", "c"]);
    expect(isIndentedSibling(promoted[1])).toBe(false);
    expect(orderSessionFamilies([b])).toEqual([b]);
    // Removal reaches the list before the server's replacement metadata.
    expect(orderSessionFamilies([c, standalone, b]).map((row) => row.id)).toEqual(["b", "c", "x"]);
    expect(b.family?.representative_id).toBe("a");
    const collapsed = new Set(["family"]);
    expect(orderSessionFamilies([standalone, c, a, b], collapsed).map((row) => row.id)).toEqual([
      "x",
      "a",
    ]);
    expect(orderSessionFamilies([c, standalone, b], collapsed).map((row) => row.id)).toEqual([
      "b",
      "x",
    ]);
    const pinned = { ...c, labels: { "omnigent.pinned": "1" } };
    expect(orderSessionFamilies([b, pinned, a], collapsed).map((row) => row.id)).toEqual(["a"]);
  });

  it("uses family pin state until a personally pinned representative leaves the nest", () => {
    const representative = { ...member("a", 1), labels: { "omnigent.pinned": "50" } };
    const sibling = { ...member("b", 2), labels: { "omnigent.pinned": "1" } };
    const personalPins = new Set(["a"]);
    expect(isSessionPinned(representative)).toBe(false);
    expect(isSessionPinned(representative, personalPins)).toBe(false);
    expect(isSessionPinned(member("b", 2), personalPins)).toBe(false);
    expect(isSessionPinned(sibling, new Set(["b"]))).toBe(false);
    expect(isSessionPinned({ ...representative, family: null })).toBe(true);
    expect(isSessionPinned({ id: "a" } as Conversation, personalPins)).toBe(true);
    for (const row of [representative, sibling]) {
      expect(isSessionPinned({ ...row, family: { ...row.family!, pinned: true } })).toBe(true);
    }
  });
});
