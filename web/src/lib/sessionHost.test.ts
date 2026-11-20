import { describe, expect, it } from "vitest";

import { parentIdFromLabels } from "./sessionHost";

describe("parentIdFromLabels", () => {
  it("reads the explicit side-chat source id", () => {
    expect(
      parentIdFromLabels({
        "omnigent.side_chat": "1",
        "omnigent.side_chat.source_id": "parent_1",
      }),
    ).toBe("parent_1");
  });

  it("falls back to the fork source id for a side chat without an explicit source", () => {
    expect(
      parentIdFromLabels({ "omnigent.side_chat": "1", "omnigent.fork.source_id": "parent_2" }),
    ).toBe("parent_2");
  });

  it("ignores a fork source id when the session is not a side chat", () => {
    expect(parentIdFromLabels({ "omnigent.fork.source_id": "parent_3" })).toBeNull();
  });

  it("returns null for an independent session or missing labels", () => {
    expect(parentIdFromLabels({})).toBeNull();
    expect(parentIdFromLabels(undefined)).toBeNull();
  });
});
