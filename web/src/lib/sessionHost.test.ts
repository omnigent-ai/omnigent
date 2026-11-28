import { describe, expect, it } from "vitest";

import {
  getSessionHostRouting,
  parentIdFromLabels,
  setSessionHost,
  setSessionParent,
} from "./sessionHost";

describe("getSessionHostRouting", () => {
  it("tracks observations along the routing ancestry without including unrelated sessions", () => {
    setSessionHost("routing-parent", "host-a");
    setSessionParent("routing-child", "routing-parent");
    const inherited = getSessionHostRouting("routing-child");
    expect(inherited).toMatchObject({ ownerId: "routing-parent", hostId: "host-a" });

    setSessionHost("unrelated-session", "host-b");
    expect(getSessionHostRouting("routing-child")).toEqual(inherited);

    setSessionHost("routing-parent", "host-a");
    const refreshed = getSessionHostRouting("routing-child");
    expect(refreshed.revision).toBeGreaterThan(inherited.revision);
    setSessionHost("routing-child", "host-a");
    const bound = getSessionHostRouting("routing-child");
    expect(bound).toMatchObject({ ownerId: "routing-child", hostId: "host-a" });
    expect(bound.revision).toBeGreaterThan(refreshed.revision);
  });
});

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
