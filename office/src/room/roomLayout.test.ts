import { describe, expect, it } from "vitest";
import { MAX_DESKS, buildRoomLayout, deskAtTile, deskInReach, findPath, isWalkable, standingTile } from "./roomLayout.ts";

describe("room layout", () => {
  it("seats each worker at a desk up to the room's capacity", () => {
    expect(buildRoomLayout(3).desks.map((desk) => desk.workerIndex)).toEqual([0, 1, 2]);
    expect(buildRoomLayout(MAX_DESKS + 5).desks).toHaveLength(MAX_DESKS);
  });

  it("treats walls, desks, and seats as solid but leaves the door open", () => {
    const layout = buildRoomLayout(1);
    const [desk] = layout.desks;
    expect(isWalkable(layout, { column: 0, row: 5 })).toBe(false);
    expect(isWalkable(layout, { column: 5, row: 1 })).toBe(false);
    expect(isWalkable(layout, desk)).toBe(false);
    expect(isWalkable(layout, { column: desk.column, row: desk.row - 1 })).toBe(false);
    expect(isWalkable(layout, layout.door)).toBe(true);
    expect(isWalkable(layout, { column: layout.door.column + 1, row: layout.door.row })).toBe(false);
  });

  it("walks around desks to reach a worker and finds them by click", () => {
    const layout = buildRoomLayout(8);
    const target = layout.desks[1];
    const path = findPath(layout, layout.spawn, standingTile(target));
    expect(path.at(-1)).toEqual(standingTile(target));
    expect(path.every((tile) => isWalkable(layout, tile))).toBe(true);
    expect(deskInReach(layout, standingTile(target))).toBe(target);
    expect(deskAtTile(layout, { column: target.column + 1, row: target.row - 1 })).toBe(target);
  });

  it("returns no path to a solid tile", () => {
    const layout = buildRoomLayout(1);
    expect(findPath(layout, layout.spawn, layout.desks[0])).toEqual([]);
  });
});
