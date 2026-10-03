import { attachmentAccept, validateAttachments } from "./attachments";
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

it.each([[".mp4"], [], "*"])("preserves published attachment policy %j", async (allowed) => {
  const policy = {
    allowed_extensions: allowed,
    denied_extensions: [".exe"],
    max_bytes: 100,
    max_files: 2,
    max_total_bytes: 200,
    harnesses: ["claude-native"],
  };
  expect(
    (await probe({ filesystem_attachment_policy: policy })).filesystem_attachment_policy,
  ).toEqual(policy);
});

const validAttachmentPolicy = {
  allowed_extensions: [".mp4"],
  denied_extensions: [".exe"],
  max_bytes: 100,
  max_files: 2,
  max_total_bytes: 200,
  harnesses: ["claude-native"],
  inline_extensions: { ".txt": "text" },
};
describe("attachment policy parsing", () => {
  it.each([
    ["null", null],
    ["array", []],
    ["string", "*"],
    ["missing fields", {}],
    ["allowed missing", { ...validAttachmentPolicy, allowed_extensions: undefined }],
    ["allowed object", { ...validAttachmentPolicy, allowed_extensions: {} }],
    ["allowed mixed", { ...validAttachmentPolicy, allowed_extensions: [1] }],
    ["allowed empty suffix", { ...validAttachmentPolicy, allowed_extensions: [""] }],
    ["allowed list wildcard", { ...validAttachmentPolicy, allowed_extensions: ["*"] }],
    ["bytes zero", { ...validAttachmentPolicy, max_bytes: 0 }],
    ["denied missing", { ...validAttachmentPolicy, denied_extensions: undefined }],
    ["denied null", { ...validAttachmentPolicy, denied_extensions: null }],
    ["denied mixed", { ...validAttachmentPolicy, denied_extensions: [false] }],
    ["bytes string", { ...validAttachmentPolicy, max_bytes: "100" }],
    ["bytes infinite", { ...validAttachmentPolicy, max_bytes: Infinity }],
    ["files negative", { ...validAttachmentPolicy, max_files: -1 }],
    ["files fractional", { ...validAttachmentPolicy, max_files: 1.5 }],
    ["total missing", { ...validAttachmentPolicy, max_total_bytes: undefined }],
    ["harnesses string", { ...validAttachmentPolicy, harnesses: "claude-native" }],
    ["harnesses mixed", { ...validAttachmentPolicy, harnesses: [1] }],
    ["inline array", { ...validAttachmentPolicy, inline_extensions: [] }],
  ])("falls back safely for malformed %s", async (_label, malformed) => {
    const result = await probe({ filesystem_attachment_policy: malformed });
    expect(result.filesystem_attachment_policy).toBeUndefined();
    expect(attachmentAccept(result.filesystem_attachment_policy)).toBeUndefined();
    const unknown = new File(["video"], "clip.mp4", { type: "video/mp4" });
    expect(validateAttachments([unknown], result.filesystem_attachment_policy).accepted).toEqual([
      unknown,
    ]);
  });
  it("requires page reload after server restart to replace cached malformed policy fallback", async () => {
    const initial = await probe({
      filesystem_attachment_policy: { ...validAttachmentPolicy, denied_extensions: null },
    });
    expect(initial.filesystem_attachment_policy).toBeUndefined();
    fetchMock.mockResolvedValue(
      mockJsonResponse({ filesystem_attachment_policy: validAttachmentPolicy }),
    );
    const { resolveServerInfo } = await import("./capabilities");
    expect((await resolveServerInfo()).filesystem_attachment_policy).toBeUndefined();
    expect(fetchMock).toHaveBeenCalledTimes(1);
    vi.resetModules();
    const reloaded = await import("./capabilities");
    expect((await reloaded.resolveServerInfo()).filesystem_attachment_policy).toEqual(
      validAttachmentPolicy,
    );
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("additive attachment policy fields", () => {
  it.each([
    { ".future": "video" },
    { "invalid/suffix": "text" },
    { txt: "text" },
    { ".empty": null },
  ])("keeps core policy and valid inline entries alongside %j", async (unknown) => {
    const mixed = {
      ...validAttachmentPolicy,
      inline_extensions: { ".txt": "text", ".pdf": "pdf", ...unknown },
    };
    const result = await probe({ filesystem_attachment_policy: mixed });
    expect(result.filesystem_attachment_policy).toEqual({
      ...validAttachmentPolicy,
      inline_extensions: { ".txt": "text", ".pdf": "pdf" },
    });
    expect(attachmentAccept(result.filesystem_attachment_policy)).toContain(".mp4");
    expect(
      validateAttachments([new File(["content"], "note.txt")], result.filesystem_attachment_policy)
        .accepted,
    ).toHaveLength(1);
  });
  it("uses valid non-inline suffix knowledge and drops invalid entries", async () => {
    const result = await probe({
      filesystem_attachment_policy: {
        ...validAttachmentPolicy,
        allowed_extensions: "*",
        non_inline_extensions: [".mp4", "bad/path", 1],
      },
    });
    expect(result.filesystem_attachment_policy?.non_inline_extensions).toEqual([".mp4"]);
    const video = new File(["longer than quota"], "clip.mp4", { type: "text/plain" });
    expect(
      validateAttachments([video], { ...result.filesystem_attachment_policy!, max_bytes: 1 })
        .errors[0],
    ).toContain("too large");
  });
  it("falls back safely for a malformed non-inline suffix container", async () => {
    expect(
      (
        await probe({
          filesystem_attachment_policy: {
            ...validAttachmentPolicy,
            non_inline_extensions: { ".mp4": true },
          },
        })
      ).filesystem_attachment_policy,
    ).toBeUndefined();
  });
});
