// Unit tests for `capabilities.ts` — the `/v1/info` probe's defensive parse
// and the sandbox-provider option helpers.
//
// `capabilities.ts` caches the probe result at module scope, so each test calls
// `vi.resetModules()` and re-imports to start from a clean slate.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
// resolveServerInfo is imported dynamically inside each probe (below) so every
// test starts from a fresh module cache; only the pure helpers are static.
import {
  pickerCuration,
  sandboxOptionLabel,
  sandboxProviderOptions,
  UNRESTRICTED_PICKER,
} from "./capabilities";
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
  it.each([null, [], "invalid", { arclet: null }, { arclet: [] }, { arclet: "invalid" }])(
    "ignores malformed provider capabilities (%j)",
    async (value) => {
      expect(
        (await probe({ sandbox_provider_capabilities: value })).sandbox_provider_capabilities,
      ).toEqual({});
    },
  );

  it.each([null, "invalid", { name: "repo", url: "https://github.com/example/repo" }])(
    "ignores a non-array repository catalog (%j)",
    async (repo_catalog) => {
      const result = await probe({
        sandbox_provider_capabilities: { arclet: { multi_repo: true, repo_catalog } },
      });
      expect(result.sandbox_provider_capabilities?.arclet).toEqual({
        multi_repo: true,
        repo_catalog: [],
      });
    },
  );

  it("filters malformed catalog entries while preserving provider flags", async () => {
    const valid = { name: "runtime", url: "https://github.com/example/runtime" };
    const result = await probe({
      sandbox_provider_capabilities: {
        arclet: {
          inference_models: true,
          default_models: true,
          repo_catalog: [
            null,
            "invalid",
            {},
            { name: "repo" },
            { name: 1, url: valid.url },
            { name: "repo", url: 42 },
            { name: "", url: valid.url },
            { name: "repo", url: "" },
            valid,
          ],
        },
      },
    });
    expect(result.sandbox_provider_capabilities?.arclet).toEqual({
      inference_models: true,
      default_models: true,
      repo_catalog: [valid],
    });
  });

  it.each([undefined, false, "true", true])(
    "requires an explicit durable-input capability (%s)",
    async (value) => {
      expect((await probe({ durable_initial_input: value })).durable_initial_input).toBe(
        value === true,
      );
    },
  );

  it.each([undefined, null, "", "manage", 2, true])(
    "defaults an absent or invalid public ceiling %s to Read",
    async (value) => {
      expect((await probe({ public_sharing_max_level: value })).public_sharing_max_level).toBe(
        "read",
      );
    },
  );

  it("recognizes an explicit public Edit ceiling", async () => {
    expect((await probe({ public_sharing_max_level: "edit" })).public_sharing_max_level).toBe(
      "edit",
    );
  });

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
      features: {
        usage_page: true,
        harness_install: false,
        automations_v2: true,
        malformed: "yes",
      },
    });
    expect(parsed.features).toEqual({
      usage_page: true,
      harness_install: false,
      automations_v2: true,
    });
    const { isFeatureEnabled } = await import("./capabilities");
    expect(isFeatureEnabled(parsed, "automations_v2")).toBe(true);
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

describe("resolveServerInfo session_search_scope", () => {
  it("reads the advertised capability", async () => {
    expect((await probe({ session_search_scope: true })).session_search_scope).toBe(true);
  });

  it("is off for an older server that omits it", async () => {
    expect((await probe({})).session_search_scope).toBe(false);
  });
});

describe.each([
  "scheduled_task_projects",
  "scheduled_task_harness_settings",
  "scheduled_task_subhour",
  "scheduled_task_sandbox_workspaces",
  "scheduled_task_inbox",
  "retry_session_wake_only",
] as const)("resolveServerInfo %s", (capability) => {
  it.each([
    [true, true],
    [false, false],
    [undefined, false],
    ["true", false],
    ["yes", false],
  ])("reads %j as %j", async (advertised, expected) => {
    const payload = advertised === undefined ? {} : { [capability]: advertised };
    expect((await probe(payload))[capability]).toBe(expected);
  });
});

it.each([
  [undefined, 10],
  [10, 10],
  [30, 30],
  [240, 240],
  [9, 10],
  [241, 10],
  [10.5, 10],
  ["30", 10],
  [true, 10],
])("parses trigger polling minutes %j as %j", async (advertised, expected) => {
  expect(
    (await probe({ automation_trigger_poll_minutes: advertised })).automation_trigger_poll_minutes,
  ).toBe(expected);
});

describe("resolveServerInfo scheduled_task_session_modes", () => {
  it("reads the advertised capability", async () => {
    expect((await probe({ scheduled_task_session_modes: true })).scheduled_task_session_modes).toBe(
      true,
    );
  });

  it("is off for an older server that omits it", async () => {
    expect((await probe({})).scheduled_task_session_modes).toBe(false);
  });
});

describe("resolveServerInfo archive_worktree_cleanup", () => {
  it("reads the advertised capability", async () => {
    expect((await probe({ archive_worktree_cleanup: true })).archive_worktree_cleanup).toBe(true);
  });

  it("is off for an older server that omits it", async () => {
    expect((await probe({})).archive_worktree_cleanup).toBe(false);
  });
});

describe("resolveServerInfo request_managed_sandbox_host_id", () => {
  it.each([
    [true, true],
    [undefined, false],
    ["true", false],
  ])("parses %s strictly as %s", async (advertised, expected) => {
    const parsed = await probe({ request_managed_sandbox_host_id: advertised });
    expect(parsed.request_managed_sandbox_host_id).toBe(expected);
  });
});

describe("resolveServerInfo session_fork_workspaces", () => {
  it.each([
    [true, true],
    [false, false],
    [undefined, false],
    ["true", false],
  ])("parses %s strictly as %s", async (advertised, expected) => {
    const parsed = await probe({ session_fork_workspaces: advertised });
    expect(parsed.session_fork_workspaces).toBe(expected);
  });
});

describe("resolveServerInfo session_claude_sdk_clear", () => {
  it.each([
    [true, true],
    [false, false],
    [undefined, false],
    ["true", false],
  ])("parses %s strictly as %s", async (advertised, expected) => {
    const parsed = await probe({ session_claude_sdk_clear: advertised });
    expect(parsed.session_claude_sdk_clear).toBe(expected);
  });
});

describe("resolveServerInfo session_event_types", () => {
  it.each([
    [undefined, []],
    ["clear", []],
    [
      ["message", 1, "clear"],
      ["message", "clear"],
    ],
  ])("parses %o as %o", async (advertised, expected) => {
    expect((await probe({ session_event_types: advertised })).session_event_types).toEqual(
      expected,
    );
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

describe("custom agents settings capability", () => {
  it.each([undefined, false, "true", true])(
    "parses agent_config_edit strictly: %s",
    async (edit) => {
      expect((await probe({ agent_config_edit: edit })).agent_config_edit).toBe(edit === true);
    },
  );
  it.each([undefined, false, "true", true])("parses agent_detail strictly: %s", async (detail) => {
    const parsed = await probe({ agent_detail: detail });
    expect(parsed.agent_detail).toBe(detail === true);
  });

  it.each([undefined, false, "true", true])(
    "parses session_pull_requests strictly: %s",
    async (stored) => {
      const parsed = await probe({ session_pull_requests: stored });
      expect(parsed.session_pull_requests).toBe(stored === true);
    },
  );

  it.each([undefined, false, "true", true])(
    "requires a boolean install capability: %s",
    async (install) => {
      const parsed = await probe({
        features: { custom_agents_settings_ui: true },
        agent_install: install,
      });
      const { customAgentsSettingsEnabled } = await import("./capabilities");
      expect(customAgentsSettingsEnabled(parsed)).toBe(install === true);
    },
  );
});

describe("picker curation", () => {
  it("reads the names and the default off the probe", async () => {
    const parsed = await probe({
      picker: { restrict_to_agent_names: ["polly", "debby"], default_agent_name: "debby" },
    });

    expect([...parsed.picker!.restrictToAgentNames]).toEqual(["polly", "debby"]);
    expect(parsed.picker!.defaultAgentName).toBe("debby");
  });

  it("reads an older server, which reports nothing, as unrestricted", async () => {
    const parsed = await probe({});

    expect(parsed.picker).toEqual(UNRESTRICTED_PICKER);
  });

  it("drops a default the restriction does not offer", async () => {
    // It could never be selected; pinning it would strand the picker.
    const parsed = await probe({
      picker: { restrict_to_agent_names: ["polly"], default_agent_name: "debby" },
    });

    expect(parsed.picker!.defaultAgentName).toBeNull();
  });

  it.each([
    { restrict_to_agent_names: "polly", default_agent_name: 7 },
    // A list with one bad member is dropped whole, as the server drops it.
    { restrict_to_agent_names: ["polly", 42], default_agent_name: 7 },
  ])("ignores malformed values rather than emptying the picker: %j", async (picker) => {
    const parsed = await probe({ picker });

    expect(parsed.picker!.restrictToAgentNames.size).toBe(0);
    expect(parsed.picker!.defaultAgentName).toBeNull();
  });

  it("treats a loading probe and a missing block as unrestricted", () => {
    expect(pickerCuration("loading").restrictToAgentNames.size).toBe(0);
    expect(pickerCuration(info({})).defaultAgentName).toBeNull();
  });
});

describe("resolveServerInfo scheduled_task_worktrees", () => {
  it.each([
    [true, true],
    [undefined, false],
    ["true", false],
  ])("requires explicit support (%s)", async (advertised, expected) => {
    expect((await probe({ scheduled_task_worktrees: advertised })).scheduled_task_worktrees).toBe(
      expected,
    );
  });
});
