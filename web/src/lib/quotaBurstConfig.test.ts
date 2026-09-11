import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  DEFAULT_QUOTA_BURST_CONFIG,
  QUOTA_BURST_CONFIG_STORAGE_KEY,
  getQuotaBurstControllerConfig,
  readQuotaBurstConfig,
  setQuotaBurstConfigWriter,
  updateQuotaBurstController,
  writeQuotaBurstConfig,
} from "./quotaBurstConfig";

describe("quota burst config", () => {
  beforeEach(() => {
    localStorage.clear();
    setQuotaBurstConfigWriter(null);
  });

  it("defaults to adaptive with an unlimited ceiling", () => {
    expect(readQuotaBurstConfig()).toEqual(DEFAULT_QUOTA_BURST_CONFIG);
  });

  it("persists the controller wire shape and notifies the adapter", async () => {
    const writer = vi.fn();
    setQuotaBurstConfigWriter(writer);
    await writeQuotaBurstConfig({ maxBurstFactor: 2.5, adaptiveBurstEnabled: false });
    expect(JSON.parse(localStorage.getItem(QUOTA_BURST_CONFIG_STORAGE_KEY)!)).toEqual({
      max_burst_factor: 2.5,
      adaptive_burst_enabled: false,
    });
    expect(writer).toHaveBeenCalledWith({ maxBurstFactor: 2.5, adaptiveBurstEnabled: false });
  });

  it("round-trips Unlimited as null", async () => {
    await writeQuotaBurstConfig({ maxBurstFactor: null, adaptiveBurstEnabled: true });
    expect(readQuotaBurstConfig().maxBurstFactor).toBeNull();
  });

  it("sends Unlimited to the authenticated same-origin proxy", async () => {
    const fetchMock = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response("{}", { status: 200 }));
    await updateQuotaBurstController({ maxBurstFactor: null, adaptiveBurstEnabled: true });
    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0]!;
    expect(url).toBe("/v1/quota/config");
    expect(init).toMatchObject({
      method: "PATCH",
      cache: "no-store",
      body: JSON.stringify({ max_burst_factor: null, adaptive_enabled: true }),
    });
    expect(new Headers(init?.headers).get("Content-Type")).toBe("application/json");
    fetchMock.mockRestore();
  });

  it("preserves an authoritative factor above the slider selection range", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ max_burst_factor: 12, adaptive_enabled: true }), {
        status: 200,
      }),
    );
    await expect(getQuotaBurstControllerConfig()).resolves.toEqual({
      maxBurstFactor: 12,
      adaptiveBurstEnabled: true,
    });
    expect(readQuotaBurstConfig().maxBurstFactor).toBe(12);
    fetchMock.mockRestore();
  });

  it("serializes controller writes so the last intent lands last", async () => {
    const calls: number[] = [];
    const releases: (() => void)[] = [];
    setQuotaBurstConfigWriter(async (config) => {
      calls.push(config.maxBurstFactor!);
      await new Promise<void>((resolve) => {
        releases.push(resolve);
      });
    });
    const first = writeQuotaBurstConfig({ maxBurstFactor: 2, adaptiveBurstEnabled: true });
    const second = writeQuotaBurstConfig({ maxBurstFactor: 3, adaptiveBurstEnabled: true });
    await vi.waitFor(() => expect(calls).toEqual([2]));
    releases.shift()!();
    await vi.waitFor(() => expect(calls).toEqual([2, 3]));
    releases.shift()!();
    await Promise.all([first, second]);
    expect(readQuotaBurstConfig().maxBurstFactor).toBe(3);
  });
});
