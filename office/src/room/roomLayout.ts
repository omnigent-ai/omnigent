// Tile layout, walking, and hit testing for a room interior.

export const TILE = 32;
export const ROOM_COLUMNS = 16;
export const ROOM_ROWS = 12;
export const DESK_WIDTH = 2;
export const DESKS_PER_ROW = 4;
const DESK_ROWS = [3, 6, 9];
const DESK_COLUMNS = [2, 5, 9, 12];
export const MAX_DESKS = DESK_ROWS.length * DESKS_PER_ROW;

export interface Tile {
  column: number;
  row: number;
}

export interface Desk extends Tile {
  /** Index of the worker seated here. */
  workerIndex: number;
}

export interface RoomLayout {
  columns: number;
  rows: number;
  desks: Desk[];
  door: Tile;
  spawn: Tile;
}

export function buildRoomLayout(workerCount: number): RoomLayout {
  const desks: Desk[] = [];
  for (let index = 0; index < Math.min(workerCount, MAX_DESKS); index++) {
    desks.push({
      column: DESK_COLUMNS[index % DESKS_PER_ROW],
      row: DESK_ROWS[Math.floor(index / DESKS_PER_ROW)],
      workerIndex: index,
    });
  }
  const door = { column: Math.floor(ROOM_COLUMNS / 2), row: ROOM_ROWS - 1 };
  return { columns: ROOM_COLUMNS, rows: ROOM_ROWS, desks, door, spawn: { column: door.column, row: door.row - 1 } };
}

/** Top wall occupies rows 0-1, side walls the outer columns, and the door is the only gap below. */
export function isWalkable(layout: RoomLayout, tile: Tile): boolean {
  const { column, row } = tile;
  if (column < 1 || column >= layout.columns - 1 || row < 2 || row >= layout.rows) return false;
  if (row === layout.rows - 1) return column === layout.door.column;
  // Desks and the seats behind them are solid.
  return deskAtTile(layout, tile) === null;
}

const key = (tile: Tile) => `${tile.column},${tile.row}`;

/** Breadth-first path excluding the start; empty when unreachable. */
export function findPath(layout: RoomLayout, from: Tile, to: Tile): Tile[] {
  if (!isWalkable(layout, to)) return [];
  const previous = new Map<string, Tile | null>([[key(from), null]]);
  const queue: Tile[] = [from];
  while (queue.length) {
    const current = queue.shift() as Tile;
    if (current.column === to.column && current.row === to.row) {
      const path: Tile[] = [];
      let step: Tile | null = current;
      while (step && key(step) !== key(from)) {
        path.unshift(step);
        step = previous.get(key(step)) ?? null;
      }
      return path;
    }
    for (const [dc, dr] of [[1, 0], [-1, 0], [0, 1], [0, -1]]) {
      const next = { column: current.column + dc, row: current.row + dr };
      if (previous.has(key(next)) || !isWalkable(layout, next)) continue;
      previous.set(key(next), current);
      queue.push(next);
    }
  }
  return [];
}

/** The worker whose desk or seat (the tile row above the desk) covers a tile. */
export function deskAtTile(layout: RoomLayout, tile: Tile): Desk | null {
  return (
    layout.desks.find(
      (desk) =>
        (tile.row === desk.row || tile.row === desk.row - 1) &&
        tile.column >= desk.column &&
        tile.column < desk.column + DESK_WIDTH,
    ) ?? null
  );
}

/** The desk the avatar can interact with: standing directly below or beside it. */
export function deskInReach(layout: RoomLayout, position: Tile): Desk | null {
  return (
    layout.desks.find((desk) => {
      const below = position.row === desk.row + 1 && position.column >= desk.column && position.column < desk.column + DESK_WIDTH;
      const beside =
        position.row === desk.row && (position.column === desk.column - 1 || position.column === desk.column + DESK_WIDTH);
      return below || beside;
    }) ?? null
  );
}

/** The walkable tile directly below a desk, where the avatar stands to talk. */
export function standingTile(desk: Desk): Tile {
  return { column: desk.column, row: desk.row + 1 };
}

export function pixelToTile(x: number, y: number): Tile {
  return { column: Math.floor(x / TILE), row: Math.floor(y / TILE) };
}
