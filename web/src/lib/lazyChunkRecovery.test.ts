import { describe, expect, it, vi } from "vitest";
import {
  CHUNK_RELOAD_AT_STORAGE_KEY,
  clearChunkReloadGuard,
  LazyChunkLoadError,
  reloadOnMissingChunk,
  type ChunkRecoveryEnv,
} from "./lazyChunkRecovery";

function makeStorage(): Storage {
  const map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    clear: () => map.clear(),
    getItem: (key: string) => map.get(key) ?? null,
    key: (index: number) => [...map.keys()][index] ?? null,
    removeItem: (key: string) => void map.delete(key),
    setItem: (key: string, value: string) => void map.set(key, value),
  };
}

function makeEnv(overrides: Partial<ChunkRecoveryEnv> = {}) {
  const storage = makeStorage();
  const reload = vi.fn();
  const env: ChunkRecoveryEnv = {
    now: () => 1_000_000,
    reload,
    storage: () => storage,
    ...overrides,
  };
  return { env, reload, storage };
}

/** Resolves "pending" if `promise` hasn't settled by the next macrotask. */
async function settleState(promise: Promise<unknown>): Promise<"pending" | "settled"> {
  const tick = new Promise<"pending">((resolve) => {
    setTimeout(() => resolve("pending"), 0);
  });
  return Promise.race([
    promise.then(
      () => "settled" as const,
      () => "settled" as const,
    ),
    tick,
  ]);
}

describe("reloadOnMissingChunk", () => {
  it("passes a successful load through untouched", async () => {
    const { env, reload } = makeEnv();
    const load = reloadOnMissingChunk(() => Promise.resolve("page"), env);
    await expect(load()).resolves.toBe("page");
    expect(reload).not.toHaveBeenCalled();
  });

  it("reloads the tab once when the chunk import fails", async () => {
    const { env, reload, storage } = makeEnv();
    const load = reloadOnMissingChunk(() => Promise.reject(new Error("chunk 404")), env);
    const result = load();
    // The import stays suspended (blank-free) while the reload takes over.
    await expect(settleState(result)).resolves.toBe("pending");
    expect(reload).toHaveBeenCalledTimes(1);
    expect(storage.getItem(CHUNK_RELOAD_AT_STORAGE_KEY)).toBe(String(env.now()));
  });

  it("surfaces a failure right after the recovery reload instead of looping", async () => {
    const { env, reload, storage } = makeEnv();
    // The previous document already spent the auto reload moments ago.
    storage.setItem(CHUNK_RELOAD_AT_STORAGE_KEY, String(env.now() - 5_000));
    const cause = new Error("chunk still missing");
    const load = reloadOnMissingChunk(() => Promise.reject(cause), env);
    await expect(load()).rejects.toBeInstanceOf(LazyChunkLoadError);
    await expect(load()).rejects.toMatchObject({ cause });
    expect(reload).not.toHaveBeenCalled();
  });

  it("re-arms after the loop window so a later deploy can recover again", async () => {
    const { env, reload, storage } = makeEnv();
    storage.setItem(CHUNK_RELOAD_AT_STORAGE_KEY, String(env.now() - 120_000));
    const load = reloadOnMissingChunk(() => Promise.reject(new Error("chunk 404")), env);
    await expect(settleState(load())).resolves.toBe("pending");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("never auto-reloads when the once-guard cannot be persisted", async () => {
    const { env, reload } = makeEnv({ storage: () => null });
    const load = reloadOnMissingChunk(() => Promise.reject(new Error("chunk 404")), env);
    await expect(load()).rejects.toBeInstanceOf(LazyChunkLoadError);
    expect(reload).not.toHaveBeenCalled();
  });

  it("holds sibling chunk failures while the reload is in flight", async () => {
    const { env, reload } = makeEnv();
    const first = reloadOnMissingChunk(() => Promise.reject(new Error("chunk A 404")), env);
    const second = reloadOnMissingChunk(() => Promise.reject(new Error("chunk B 404")), env);
    await expect(settleState(first())).resolves.toBe("pending");
    // Same document lifetime: the sibling must not flash the error boundary.
    await expect(settleState(second())).resolves.toBe("pending");
    expect(reload).toHaveBeenCalledTimes(1);
  });

  it("clearChunkReloadGuard re-arms the auto reload", async () => {
    const { env, reload, storage } = makeEnv();
    storage.setItem(CHUNK_RELOAD_AT_STORAGE_KEY, String(env.now() - 5_000));
    clearChunkReloadGuard(env);
    const load = reloadOnMissingChunk(() => Promise.reject(new Error("chunk 404")), env);
    await expect(settleState(load())).resolves.toBe("pending");
    expect(reload).toHaveBeenCalledTimes(1);
  });
});
