import { expect, it, vi } from "vitest";
import { loadOnce } from "./loadOnce";

it("loads once, and again only after a failure", async () => {
  const load = vi
    .fn<() => Promise<string>>()
    .mockRejectedValueOnce(new Error("chunk failed"))
    .mockResolvedValue("module");
  const get = loadOnce(load);
  await expect(get()).rejects.toThrow("chunk failed");
  expect(await get()).toBe("module");
  expect(await get()).toBe("module");
  expect(load).toHaveBeenCalledTimes(2);
  // Callers waiting together share one load.
  const shared = loadOnce(() => Promise.resolve(1));
  expect(shared()).toBe(shared());
});
