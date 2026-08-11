// Unit tests for `capabilities.ts` — the `/v1/info` probe's defensive parse.
//
// `capabilities.ts` caches the probe result at module scope, so each test calls
// `vi.resetModules()` and re-imports to start from a clean slate.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

function mockJsonResponse(body: unknown, init?: { ok?: boolean }): Response {
  return {
    ok: init?.ok ?? true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.resetModules();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

/** Probe once against *body* and hand back the parsed `ServerInfo`. */
async function probe(body: unknown) {
  fetchMock.mockResolvedValueOnce(mockJsonResponse(body));
  const { resolveServerInfo } = await import("./capabilities");
  return resolveServerInfo();
}

describe("resolveServerInfo smart_routing_sources", () => {
  it("reads an explicit field verbatim", async () => {
    const info = await probe({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: true },
    });
    expect(info.smart_routing_sources).toEqual({ external: false, oss: true });
  });

  it("reads a partial field's missing key as false", async () => {
    const info = await probe({
      smart_routing_enabled: true,
      smart_routing_sources: { external: true },
    });
    expect(info.smart_routing_sources).toEqual({ external: true, oss: false });
  });

  // A server that predates the field says nothing about sources. It can route,
  // so it's assumed able to serve either one — degrading to neither would hide
  // Smart Routing on every deployment that can't yet answer.
  it.each([
    ["the field is absent", {}],
    ["the field is null", { smart_routing_sources: null }],
    ["the field isn't an object", { smart_routing_sources: "yes" }],
  ] as const)("degrades from smart_routing_enabled when %s", async (_case, extra) => {
    const on = await probe({ smart_routing_enabled: true, ...extra });
    expect(on.smart_routing_sources).toEqual({ external: true, oss: true });

    vi.resetModules();
    const off = await probe({ smart_routing_enabled: false, ...extra });
    expect(off.smart_routing_sources).toEqual({ external: false, oss: false });
  });

  // The failed-probe sentinel fails closed, matching `smart_routing_enabled`.
  it("reports neither source on a failed probe", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    const { resolveServerInfo } = await import("./capabilities");
    const info = await resolveServerInfo();
    expect(info.smart_routing_enabled).toBe(false);
    expect(info.smart_routing_sources).toEqual({ external: false, oss: false });
  });
});
