import type { Active, ClientRect, DroppableContainer } from "@dnd-kit/core";
import { describe, expect, it } from "vitest";
import { closestInSameGroup, sideChatTabDensity } from "./SideChatTab";

describe("sideChatTabDensity", () => {
  it.each([
    [0, "regular"],
    [3, "regular"],
    [4, "compact"],
    [6, "compact"],
    [7, "dense"],
    [20, "dense"],
  ] as const)("packs %i side-chat tabs as %s", (count, density) => {
    expect(sideChatTabDensity(count)).toBe(density);
  });
});

describe("closestInSameGroup", () => {
  const rect = (left: number): ClientRect => ({
    left,
    top: 0,
    width: 40,
    height: 30,
    right: left + 40,
    bottom: 30,
  });
  const sortable = (containerId: string) => ({
    current: { sortable: { containerId, index: 0, items: [] } },
  });
  const tab = (id: string, group: string) =>
    ({ id, data: sortable(group) }) as unknown as DroppableContainer;

  it("only offers drop targets in the dragged tab's group", () => {
    const containers = [tab("pinned", "pinned-group"), tab("far", "rest"), tab("near", "rest")];
    const collisions = closestInSameGroup({
      active: { id: "dragged", data: sortable("rest") } as unknown as Active,
      // The dragged tab sits right on top of the pinned tab.
      collisionRect: rect(0),
      droppableRects: new Map([
        ["pinned", rect(0)],
        ["near", rect(50)],
        ["far", rect(200)],
      ]),
      droppableContainers: containers,
      pointerCoordinates: null,
    });
    expect(collisions.map((collision) => collision.id)).toEqual(["near", "far"]);
  });
});
