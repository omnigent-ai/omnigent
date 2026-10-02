// Unit tests for `capabilities.ts` — the `/v1/info` probe's defensive parse,
// the sandbox-provider option helpers, and the git-provider list.
//
// `capabilities.ts` caches the probe result at module scope, so each test calls
// `vi.resetModules()` and re-imports to start from a clean slate.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// resolveServerInfo is imported dynamically inside each probe (below) so every
// test starts from a fresh module cache; only the pure helpers are static.
import { gitProviders, sandboxOptionLabel, sandboxProviderOptions } from "./capabilities";
import type { GitProviderInfo, ServerInfo } from "./capabilities";

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
    enabled_connections: [],
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
    expect(isFeatureEnabled(parsed, "canvas")).toBe(false);
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

describe("resolveServerInfo branding", () => {
  it("preserves an explicit empty heading and disabled attribution", async () => {
    const brandingInfo = await probe({
      branding: {
        app_name: "Acme Agent",
        heading: "",
        logos: { main: "/logo/main", loading: "/logo/loading", favicon: "/logo/favicon" },
        powered_by: false,
      },
    });

    expect(brandingInfo.branding).toEqual({
      app_name: "Acme Agent",
      heading: "",
      logos: { main: "/logo/main", loading: "/logo/loading", favicon: "/logo/favicon" },
      powered_by: false,
    });
  });

  it("normalizes an empty or malformed branding payload to null", async () => {
    const empty = await probe({ branding: { logos: { main: 123 }, powered_by: true } });
    expect(empty.branding).toBeNull();

    vi.resetModules();
    const malformed = await probe({ branding: "Acme" });
    expect(malformed.branding).toBeNull();
  });
});

/** A `git_providers` entry as `/v1/info` serves it; `capabilities` override the defaults. */
function gitProvider(
  id: string,
  displayName: string,
  capabilities: Partial<GitProviderInfo["capabilities"]> = {},
): GitProviderInfo {
  return {
    id,
    display_name: displayName,
    capabilities: {
      pull_requests: true,
      connection: false,
      repo_browser: false,
      credential_broker: false,
      ...capabilities,
    },
  };
}

const GITHUB_PROVIDER = gitProvider("github", "GitHub", {
  connection: true,
  repo_browser: true,
  credential_broker: true,
});
const AZURE_DEVOPS_PROVIDER = gitProvider("azure_devops", "Azure DevOps");

describe("resolveServerInfo git_providers", () => {
  it("keeps the entries the server lists, in order", async () => {
    // The probe rebuilds ServerInfo field by field, so a forgotten field is
    // dropped before any component sees it.
    const parsed = await probe({
      git_providers: [GITHUB_PROVIDER, AZURE_DEVOPS_PROVIDER],
      enabled_connections: ["github"],
    });
    expect(parsed.git_providers).toEqual([GITHUB_PROVIDER, AZURE_DEVOPS_PROVIDER]);
    expect(parsed.enabled_connections).toEqual(["github"]);
  });

  it("keeps an empty list as the server's word that it has none", async () => {
    const parsed = await probe({ git_providers: [] });
    expect(parsed.git_providers).toEqual([]);
  });

  // A server that predates the field says nothing, so gitProviders() must be
  // able to tell "absent" from "empty".
  it.each([
    ["the field is absent", {}],
    ["the field is null", { git_providers: null }],
    ["the field is a string", { git_providers: "github" }],
    ["the field is an object", { git_providers: { id: "github" } }],
  ] as const)("leaves the list unset when %s", async (_case, extra) => {
    const parsed = await probe({ enabled_connections: ["github"], ...extra });
    expect(parsed.git_providers).toBeUndefined();
  });

  it("drops entries that are malformed", async () => {
    const parsed = await probe({
      git_providers: [
        null,
        "github",
        7,
        [],
        { display_name: "No id", capabilities: {} },
        { id: "", display_name: "Empty id", capabilities: {} },
        { id: 3, display_name: "Numeric id", capabilities: {} },
        { id: "no_name", capabilities: {} },
        { id: "named_badly", display_name: 5, capabilities: {} },
        { id: "no_caps", display_name: "No capabilities" },
        { id: "caps_null", display_name: "Null capabilities", capabilities: null },
        { id: "caps_string", display_name: "String capabilities", capabilities: "all" },
        { id: "caps_list", display_name: "List capabilities", capabilities: [true] },
        AZURE_DEVOPS_PROVIDER,
      ],
    });
    expect(parsed.git_providers).toEqual([AZURE_DEVOPS_PROVIDER]);
  });

  it("counts a capability only when it is exactly true", async () => {
    const parsed = await probe({
      git_providers: [
        {
          id: "github",
          display_name: "GitHub",
          capabilities: { pull_requests: true, connection: "yes", repo_browser: 1 },
        },
      ],
    });
    expect(parsed.git_providers).toEqual([
      gitProvider("github", "GitHub", {
        pull_requests: true,
        connection: false,
        repo_browser: false,
        credential_broker: false,
      }),
    ]);
  });

  it("keeps the first entry when an id repeats", async () => {
    const parsed = await probe({
      git_providers: [GITHUB_PROVIDER, gitProvider("github", "Imposter")],
    });
    expect(parsed.git_providers).toEqual([GITHUB_PROVIDER]);
  });

  it("leaves the list unset on a failed probe, so nothing is offered", async () => {
    fetchMock.mockRejectedValueOnce(new Error("offline"));
    const { resolveServerInfo } = await import("./capabilities");
    const parsed = await resolveServerInfo();
    expect(parsed.git_providers).toBeUndefined();
    expect(gitProviders(parsed)).toEqual([]);
  });
});

describe("gitProviders", () => {
  it("returns the list the server sent", () => {
    const listed = [GITHUB_PROVIDER, AZURE_DEVOPS_PROVIDER];
    expect(gitProviders(info({ git_providers: listed }))).toEqual(listed);
  });

  it("takes an empty list at the server's word, whatever enabled_connections says", () => {
    expect(gitProviders(info({ git_providers: [], enabled_connections: ["github"] }))).toEqual([]);
  });

  it("does not add GitHub to a list that omits it", () => {
    expect(
      gitProviders(
        info({ git_providers: [AZURE_DEVOPS_PROVIDER], enabled_connections: ["github"] }),
      ),
    ).toEqual([AZURE_DEVOPS_PROVIDER]);
  });

  it("synthesizes GitHub for an older server whose connection is enabled", () => {
    // An older server lists GitHub in enabled_connections exactly when its
    // connection is configured, and that connection can list repos.
    expect(gitProviders(info({ enabled_connections: ["github"] }))).toEqual([
      {
        id: "github",
        display_name: "GitHub",
        capabilities: {
          pull_requests: true,
          connection: true,
          repo_browser: true,
          credential_broker: true,
        },
      },
    ]);
    expect(gitProviders(info({ enabled_connections: ["databricks", "github"] }))).toHaveLength(1);
  });

  it.each([
    ["no connection is enabled", []],
    ["only another connection is enabled", ["databricks"]],
  ])("offers none to an older server when %s", (_case, enabledConnections) => {
    expect(gitProviders(info({ enabled_connections: enabledConnections }))).toEqual([]);
  });

  it("offers none when a hand-built info omits enabled_connections", () => {
    const bare = info({});
    delete (bare as Partial<ServerInfo>).enabled_connections;
    expect(gitProviders(bare)).toEqual([]);
  });

  it("synthesizes GitHub from a probe of an older server", async () => {
    const parsed = await probe({ enabled_connections: ["github"] });
    expect(parsed.git_providers).toBeUndefined();
    expect(gitProviders(parsed).map((provider) => provider.id)).toEqual(["github"]);
  });
});
