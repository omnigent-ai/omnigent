import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  MAIN_CANVAS_ID,
  type CanvasPosition,
  type CanvasPositions,
} from "./canvasLayout";

export const LAYOUT_VERSION = 1;
export const POSITION_BUCKET_COUNT = 16;
export const POSITION_BUCKET_MAX_ENTRIES = 250;
export const LAYOUT_META_KEY = "canvas.layout.meta.v1";
export const LAYOUT_VIEWPORT_KEY = "canvas.layout.viewport.v1";
const MAX_ABS_COORDINATE = 1_000_000;

export interface CanvasViewport {
  x: number;
  y: number;
  zoom: number;
  /** Container size used to restore the same center after resizing. */
  width?: number;
  height?: number;
}

export interface CanvasLayout {
  positions: CanvasPositions;
  viewport: CanvasViewport | null;
}

type StorageApi = ExtensionContext["storage"]["user"];
type PositionTuple = [id: string, x: number, y: number];

export function positionBucket(sessionId: string): number {
  let hash = 0x811c9dc5;
  for (const character of sessionId) {
    hash ^= character.charCodeAt(0);
    hash = Math.imul(hash, 0x01000193);
  }
  return (hash >>> 0) % POSITION_BUCKET_COUNT;
}

export function positionBucketKey(bucket: number): string {
  return `canvas.layout.positions.v1.${bucket}`;
}

// The Main canvas keeps the original key so layouts saved before canvases
// existed still restore.
export function viewportKey(canvasId: string): string {
  return canvasId === MAIN_CANVAS_ID
    ? LAYOUT_VIEWPORT_KEY
    : `${LAYOUT_VIEWPORT_KEY}.${canvasId}`;
}

function boundedCoordinate(value: number): number {
  return Math.max(
    -MAX_ABS_COORDINATE,
    Math.min(MAX_ABS_COORDINATE, Math.round(value)),
  );
}

function validViewport(value: unknown): value is CanvasViewport {
  if (!value || typeof value !== "object") return false;
  const viewport = value as Partial<CanvasViewport>;
  const finite = (n: unknown): n is number =>
    typeof n === "number" && Number.isFinite(n);
  const sizeOk = (n: unknown) => n === undefined || (finite(n) && n >= 0);
  return (
    finite(viewport.x) &&
    finite(viewport.y) &&
    finite(viewport.zoom) &&
    viewport.zoom > 0 &&
    sizeOk(viewport.width) &&
    sizeOk(viewport.height)
  );
}

function parseBucket(value: unknown): PositionTuple[] {
  if (!Array.isArray(value)) return [];
  return value
    .slice(-POSITION_BUCKET_MAX_ENTRIES)
    .filter(
      (entry): entry is PositionTuple =>
        Array.isArray(entry) &&
        entry.length === 3 &&
        typeof entry[0] === "string" &&
        typeof entry[1] === "number" &&
        Number.isFinite(entry[1]) &&
        typeof entry[2] === "number" &&
        Number.isFinite(entry[2]),
    );
}

async function writeVersion(storage: StorageApi): Promise<void> {
  await storage.set(LAYOUT_META_KEY, { version: LAYOUT_VERSION });
}

export async function readCanvasLayout(
  storage: StorageApi,
): Promise<CanvasLayout> {
  const meta = await storage.get<{ version?: number }>(LAYOUT_META_KEY);
  if (!meta || meta.version !== LAYOUT_VERSION) {
    return { positions: {}, viewport: null };
  }
  const [viewport, ...buckets] = await Promise.all([
    storage.get<unknown>(viewportKey(MAIN_CANVAS_ID)),
    ...Array.from({ length: POSITION_BUCKET_COUNT }, (_, bucket) =>
      storage.get<unknown>(positionBucketKey(bucket)),
    ),
  ]);
  const positions: CanvasPositions = {};
  for (const bucket of buckets) {
    for (const [id, x, y] of parseBucket(bucket)) {
      positions[id] = { x: boundedCoordinate(x), y: boundedCoordinate(y) };
    }
  }
  return {
    positions,
    viewport: validViewport(viewport) ? viewport : null,
  };
}

export async function readCanvasViewport(
  storage: StorageApi,
  canvasId: string,
): Promise<CanvasViewport | null> {
  const meta = await storage.get<{ version?: number }>(LAYOUT_META_KEY);
  if (!meta || meta.version !== LAYOUT_VERSION) return null;
  const viewport = await storage.get<unknown>(viewportKey(canvasId));
  return validViewport(viewport) ? viewport : null;
}

export async function writeCanvasViewport(
  storage: StorageApi,
  viewport: CanvasViewport,
  canvasId: string = MAIN_CANVAS_ID,
): Promise<void> {
  await writeVersion(storage);
  await storage.set(viewportKey(canvasId), {
    x: Math.round(viewport.x),
    y: Math.round(viewport.y),
    zoom: Math.round(viewport.zoom * 1000) / 1000,
    ...(viewport.width !== undefined && viewport.height !== undefined
      ? {
          width: Math.round(viewport.width),
          height: Math.round(viewport.height),
        }
      : {}),
  });
}

export function upsertPosition(
  positions: CanvasPositions,
  sessionId: string,
  position: CanvasPosition,
): CanvasPositions {
  const next = Object.fromEntries(
    Object.entries(positions).filter(([id]) => id !== sessionId),
  ) as CanvasPositions;
  next[sessionId] = {
    x: boundedCoordinate(position.x),
    y: boundedCoordinate(position.y),
  };
  const bucket = positionBucket(sessionId);
  const bucketIds = Object.keys(next).filter(
    (id) => positionBucket(id) === bucket,
  );
  for (const evictedId of bucketIds.slice(0, -POSITION_BUCKET_MAX_ENTRIES)) {
    delete next[evictedId];
  }
  return next;
}

export async function writePositionBucket(
  storage: StorageApi,
  positions: CanvasPositions,
  bucket: number,
): Promise<void> {
  const entries: PositionTuple[] = Object.entries(positions)
    .filter(([id]) => positionBucket(id) === bucket)
    .slice(-POSITION_BUCKET_MAX_ENTRIES)
    .map(([id, position]) => [
      id,
      boundedCoordinate(position.x),
      boundedCoordinate(position.y),
    ]);
  await writeVersion(storage);
  await storage.set(positionBucketKey(bucket), entries);
}

export async function writeAllPositionBuckets(
  storage: StorageApi,
  positions: CanvasPositions,
): Promise<void> {
  await writeVersion(storage);
  await Promise.all(
    Array.from({ length: POSITION_BUCKET_COUNT }, (_, bucket) => {
      const entries: PositionTuple[] = Object.entries(positions)
        .filter(([id]) => positionBucket(id) === bucket)
        .slice(-POSITION_BUCKET_MAX_ENTRIES)
        .map(([id, position]) => [
          id,
          boundedCoordinate(position.x),
          boundedCoordinate(position.y),
        ]);
      return storage.set(positionBucketKey(bucket), entries);
    }),
  );
}

// Forgets the saved spots of the given canvas's cards and its viewport only;
// other canvases keep their layouts. Returns the remaining persisted positions.
export async function resetCanvasLayout(
  storage: StorageApi,
  canvasId: string,
  positions: CanvasPositions,
  sessionIds: Iterable<string>,
): Promise<CanvasPositions> {
  const removed = new Set(sessionIds);
  const remaining = Object.fromEntries(
    Object.entries(positions).filter(([id]) => !removed.has(id)),
  ) as CanvasPositions;
  const buckets = new Set([...removed].map(positionBucket));
  await Promise.all([
    storage.delete(viewportKey(canvasId)),
    ...[...buckets].map((bucket) =>
      writePositionBucket(storage, remaining, bucket),
    ),
  ]);
  return remaining;
}
