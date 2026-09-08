import { describe, expect, it } from "vitest";
import type { ExtensionContext } from "@omnigent/extension-sdk";
import {
  LAYOUT_META_KEY,
  LAYOUT_VIEWPORT_KEY,
  POSITION_BUCKET_COUNT,
  POSITION_BUCKET_MAX_ENTRIES,
  positionBucket,
  positionBucketKey,
  readCanvasLayout,
  readCanvasViewport,
  resetCanvasLayout,
  viewportKey,
  upsertPosition,
  writeAllPositionBuckets,
  writeCanvasViewport,
  writePositionBucket,
} from "./canvasStorage";
import type { CanvasPositions } from "./canvasLayout";

type StorageApi = ExtensionContext["storage"]["user"];

function fakeStorage(): StorageApi & { values: Map<string, unknown> } {
  const values = new Map<string, unknown>();
  return {
    values,
    async get<T>(key: string) {
      return (values.get(key) as T | undefined) ?? null;
    },
    async set(key: string, value: unknown) {
      values.set(key, structuredClone(value));
    },
    async delete(key: string) {
      values.delete(key);
    },
  };
}

describe("canvas storage", () => {
  it("round-trips compact integer positions and viewport", async () => {
    const storage = fakeStorage();
    const positions = {
      one: { x: 1.2, y: 2.8 },
      two: { x: -30.7, y: 40.1 },
    };
    await writeAllPositionBuckets(storage, positions);
    await writeCanvasViewport(storage, { x: 10.7, y: -9.4, zoom: 1.23456 });

    await expect(readCanvasLayout(storage)).resolves.toEqual({
      positions: { one: { x: 1, y: 3 }, two: { x: -31, y: 40 } },
      viewport: { x: 11, y: -9, zoom: 1.235 },
    });
  });

  it("falls back for unknown layout versions", async () => {
    const storage = fakeStorage();
    await storage.set(LAYOUT_META_KEY, { version: 999 });
    await expect(readCanvasLayout(storage)).resolves.toEqual({
      positions: {},
      viewport: null,
    });
    await expect(readCanvasViewport(storage, "proj_a")).resolves.toBeNull();
  });

  it("keeps a viewport per canvas and resets only the active canvas", async () => {
    const storage = fakeStorage();
    const positions = { one: { x: 1, y: 2 }, two: { x: 3, y: 4 } };
    await writeAllPositionBuckets(storage, positions);
    await writeCanvasViewport(storage, { x: 1, y: 1, zoom: 1 });
    await writeCanvasViewport(
      storage,
      { x: 2, y: 2, zoom: 2, width: 1200.4, height: 700.6 },
      "proj_a",
    );
    expect(viewportKey("proj_a")).toBe(`${LAYOUT_VIEWPORT_KEY}.proj_a`);
    await expect(readCanvasViewport(storage, "proj_a")).resolves.toEqual({
      x: 2,
      y: 2,
      zoom: 2,
      width: 1200,
      height: 701,
    });

    await expect(
      resetCanvasLayout(storage, "proj_a", positions, ["two"]),
    ).resolves.toEqual({ one: { x: 1, y: 2 } });

    await expect(readCanvasViewport(storage, "proj_a")).resolves.toBeNull();
    await expect(readCanvasLayout(storage)).resolves.toEqual({
      positions: { one: { x: 1, y: 2 } },
      viewport: { x: 1, y: 1, zoom: 1 },
    });
  });

  it("keeps thousands of moved-card positions within storage budgets", async () => {
    const storage = fakeStorage();
    const positions: CanvasPositions = Object.fromEntries(
      Array.from({ length: 3_000 }, (_, index) => [
        `conv_${String(index).padStart(32, "0")}`,
        { x: index * 10.4, y: index * -4.7 },
      ]),
    );

    await writeAllPositionBuckets(storage, positions);

    let totalBytes = 0;
    for (let bucket = 0; bucket < POSITION_BUCKET_COUNT; bucket += 1) {
      const value = storage.values.get(positionBucketKey(bucket));
      expect(Array.isArray(value) ? value.length : 0).toBeLessThanOrEqual(
        POSITION_BUCKET_MAX_ENTRIES,
      );
      const bytes = new TextEncoder().encode(JSON.stringify(value)).byteLength;
      totalBytes += bytes;
      expect(bytes).toBeLessThan(32 * 1024);
    }
    expect(totalBytes).toBeLessThan(256 * 1024);
  });

  it("keeps positions readable if a later viewport write fails", async () => {
    const storage = fakeStorage();
    const originalSet = storage.set.bind(storage);
    storage.set = async (key, value) => {
      if (key === LAYOUT_VIEWPORT_KEY) throw new Error("quota exceeded");
      await originalSet(key, value);
    };

    const positions = { conv_1: { x: 12, y: 34 } };
    await writePositionBucket(storage, positions, positionBucket("conv_1"));
    await expect(
      writeCanvasViewport(storage, { x: 0, y: 0, zoom: 1 }),
    ).rejects.toThrow("quota exceeded");
    await expect(readCanvasLayout(storage)).resolves.toMatchObject({
      positions,
    });
  });

  it("evicts the oldest position when a bucket reaches its cap", () => {
    const targetBucket = positionBucket("newest");
    const ids: string[] = [];
    for (let index = 0; ids.length < POSITION_BUCKET_MAX_ENTRIES; index += 1) {
      const id = `session_${index}`;
      if (positionBucket(id) === targetBucket) ids.push(id);
    }
    const positions = Object.fromEntries(ids.map((id) => [id, { x: 0, y: 0 }]));
    const updated = upsertPosition(positions, "newest", { x: 1, y: 2 });

    expect(updated[ids[0]]).toBeUndefined();
    expect(updated.newest).toEqual({ x: 1, y: 2 });
    expect(Object.keys(updated)).toHaveLength(POSITION_BUCKET_MAX_ENTRIES);
  });

  it("uses a stable bucket for each session ID", () => {
    expect(positionBucket("conv_1")).toBe(positionBucket("conv_1"));
    expect(positionBucket("conv_1")).toBeGreaterThanOrEqual(0);
    expect(positionBucket("conv_1")).toBeLessThan(POSITION_BUCKET_COUNT);
  });
});
