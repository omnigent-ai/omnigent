import { describe, expect, it, vi } from "vitest";
import { readParquet } from "./parquet";

// The Parquet reader loads on first use; a deploy or network fault can stop it.
vi.mock("hyparquet", () => {
  throw new Error("Failed to fetch dynamically imported module");
});

describe("readParquet when its reader can't load", () => {
  it("says the reader failed, not the file", async () => {
    await expect(readParquet(new Uint8Array([1, 2, 3]))).rejects.toThrow(
      "The Parquet reader couldn't load. Check your connection, then reopen the file.",
    );
  });
});
