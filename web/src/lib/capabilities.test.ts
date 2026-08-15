// Unit tests for `capabilities.ts` — the `/v1/info` probe's defensive parse
// and the sandbox-provider option helpers.
//
// `capabilities.ts` caches the probe result at module scope, so each test calls
// `vi.resetModules()` and re-imports to start from a clean slate.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// resolveServerInfo is imported dynamically inside each probe (below) so every
// test starts from a fresh module cache; only the pure helpers are static.
import { sandboxOptionLabel, sandboxProviderOptions } from "./capabilities";
import type { ServerInfo } from "./capabilities";

/** A ServerInfo with only the sandbox fields a test cares about set. */
function info(overrides: Partial<ServerInfo>): ServerInfo {
  return {
    accounts_enabled: false,
    single_user: true,
    login_url: null,
    needs_setup: false,
    databricks_features: false,
    managed_sandboxes_enabled: true,
    sandbox_provider: null,
    sandbox_providers: [],
    sharing_mode: "on",
    public_sharing_enabled: true,
    server_version: null,
    smart_routing_enabled: false,
    smart_routing_sources: { external: false, oss: false },
    features: {},
    harness_install_enabled: false,
    installable_harnesses: [],
    dictation_available: false,
    ...overrides,
  };
}

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
  vi.restoreAllMocks();
});

/** Probe once against *body* and hand back the parsed `ServerInfo`. */
async function probe(body: unknown) {
  fetchMock.mockResolvedValueOnce(mockJsonResponse(body));
  const { resolveServerInfo } = await import("./capabilities");
  return resolveServerInfo();
}

describe("sandboxProviderOptions", () => {
  it("yields one entry per configured provider, in order", () => {
    // The order the operator configured is the order the user sees.
    expect(
      sandboxProviderOptions(
        info({ sandbox_provider: "modal", sandbox_providers: ["modal", "e2b", "daytona"] }),
      ),
    ).toEqual(["modal", "e2b", "daytona"]);
  });

  it("falls back to the single provider when the list is empty", () => {
    // An older server reports only the scalar, and must still offer a row.
    expect(
      sandboxProviderOptions(info({ sandbox_provider: "modal", sandbox_providers: [] })),
    ).toEqual(["modal"]);
  });

  it("yields one unnamed row when the server names no provider", () => {
    // An embedding deployment may enable sandboxes without naming one.
    const options = sandboxProviderOptions(info({}));
    expect(options).toEqual([null]);
    expect(sandboxOptionLabel(options[0])).toBe("New Sandbox");
  });
});

describe("resolveServerInfo sandbox_providers", () => {
  it("keeps the provider list from the probe", async () => {
    // Regression: the probe rebuilds ServerInfo field by field, so a
    // forgotten field is dropped before any component sees it.
    const resolved = await probe({
      managed_sandboxes_enabled: true,
      sandbox_provider: "modal",
      sandbox_providers: ["modal", "e2b"],
    });
    expect(resolved.sandbox_providers).toEqual(["modal", "e2b"]);
  });

  it("defaults the provider list to empty when the server omits it", async () => {
    // Must land as [] so sandboxProviderOptions can read .length.
    const resolved = await probe({
      managed_sandboxes_enabled: true,
      sandbox_provider: "modal",
    });
    expect(resolved.sandbox_providers).toEqual([]);
    expect(sandboxProviderOptions(resolved)).toEqual(["modal"]);
  });

  it("drops non-string entries from the provider list", async () => {
    // /v1/info is untrusted input, as with installable_harnesses.
    const resolved = await probe({
      managed_sandboxes_enabled: true,
      sandbox_providers: ["modal", 7, null, "e2b"],
    });
    expect(resolved.sandbox_providers).toEqual(["modal", "e2b"]);
  });
});

describe("resolveServerInfo release features", () => {
  it("keeps boolean feature values and drops malformed entries", async () => {
    const parsed = await probe({
      features: { usage_page: true, harness_install: false, malformed: "yes" },
    });
    expect(parsed.features).toEqual({ usage_page: true, harness_install: false });
  });

  it("defaults missing features off", async () => {
    const { isFeatureEnabled } = await import("./capabilities");
    const parsed = await probe({});
    expect(isFeatureEnabled(parsed, "usage_page")).toBe(false);
    expect(isFeatureEnabled(parsed, "harness_install")).toBe(false);
  });

  it("falls back to the legacy harness field from an older server", async () => {
    const { isFeatureEnabled } = await import("./capabilities");
    const parsed = await probe({ harness_install_enabled: true });
    expect(isFeatureEnabled(parsed, "harness_install")).toBe(true);
  });

  it("fails all release features closed when the probe fails", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    const { isFeatureEnabled, resolveServerInfo } = await import("./capabilities");
    const parsed = await resolveServerInfo();
    expect(isFeatureEnabled(parsed, "usage_page")).toBe(false);
    expect(isFeatureEnabled(parsed, "harness_install")).toBe(false);
  });
});

describe("resolveServerInfo smart_routing_sources", () => {
  it("reads an explicit field verbatim", async () => {
    const parsed = await probe({
      smart_routing_enabled: true,
      smart_routing_sources: { external: false, oss: true },
    });
    expect(parsed.smart_routing_sources).toEqual({ external: false, oss: true });
  });

  it("reads a partial field's missing key as false", async () => {
    const parsed = await probe({
      smart_routing_enabled: true,
      smart_routing_sources: { external: true },
    });
    expect(parsed.smart_routing_sources).toEqual({ external: true, oss: false });
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
    const parsed = await resolveServerInfo();
    expect(parsed.smart_routing_enabled).toBe(false);
    expect(parsed.smart_routing_sources).toEqual({ external: false, oss: false });
  });
});
