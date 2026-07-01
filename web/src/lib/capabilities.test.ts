import { afterEach, describe, expect, it, vi } from "vitest";

/**
 * Probe ``/v1/info`` with a mocked ``hostFetch`` and a fresh module instance so
 * the module-level resolve-once cache doesn't bleed between cases.
 */
async function probeWith(payload: unknown, ok = true) {
  vi.resetModules();
  vi.doMock("./host", () => ({
    hostFetch: vi.fn(async () => ({
      ok,
      json: async () => payload,
    })),
  }));
  const mod = await import("./capabilities");
  return mod.resolveServerInfo();
}

afterEach(() => {
  vi.resetModules();
  vi.doUnmock("./host");
});

describe("resolveServerInfo — canvas_enabled (#2)", () => {
  it("parses canvas_enabled: true", async () => {
    const info = await probeWith({ canvas_enabled: true });
    expect(info.canvas_enabled).toBe(true);
  });

  it("parses canvas_enabled: false", async () => {
    const info = await probeWith({ canvas_enabled: false });
    expect(info.canvas_enabled).toBe(false);
  });

  it("fails closed when the field is absent", async () => {
    const info = await probeWith({ accounts_enabled: false });
    expect(info.canvas_enabled).toBe(false);
  });

  it("fails closed on a failed probe (the _OFF sentinel)", async () => {
    const info = await probeWith(null, false);
    expect(info.canvas_enabled).toBe(false);
  });
});
