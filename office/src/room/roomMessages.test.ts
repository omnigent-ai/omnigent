import { describe, expect, it } from "vitest";
import type { RoomMessage } from "../../shared/roomMessages.ts";
import { mergeRoomMessages } from "./RoomView.tsx";

const message = (id: string, at: number): RoomMessage => ({ id, at, text: id, recipients: [] });

describe("mergeRoomMessages", () => {
  it("keeps a just-sent message when an older fetch returns without it", () => {
    const shown = [message("a", 1), message("sent", 3)];
    expect(mergeRoomMessages(shown, [message("a", 1)]).map((entry) => entry.id)).toEqual(["a", "sent"]);
  });

  it("adds new messages in time order without repeating any", () => {
    expect(mergeRoomMessages([message("b", 2)], [message("a", 1), message("b", 2), message("c", 3)]).map((entry) => entry.id)).toEqual(["a", "b", "c"]);
  });
});
