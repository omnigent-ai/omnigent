import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";

import type { Host } from "@/hooks/useHosts";

const authenticatedFetchMock = vi.hoisted(() => vi.fn());
vi.mock("@/lib/identity", () => ({ authenticatedFetch: authenticatedFetchMock }));

import { installedHarnesses, useHarnessInventory } from "./useHarnessInventory";

const HOST: Host = {
  host_id: "host_1",
  name: "laptop",
  owner: "me",
  status: "online",
  configured_harnesses: {
    "claude-native": true,
    "codex-native": "needs-auth",
    "cursor-native": "binary-missing",
  },
  gateway_inference: { "claude-native": true },
};

type Routes = Record<string, unknown | number>;

/** Route `/v1/skills` by harness and the MCP route by path; a number is an HTTP error. */
function serve(routes: Routes) {
  authenticatedFetchMock.mockImplementation(async (url: string) => {
    const parsed = new URL(url, "http://test");
    const key =
      parsed.pathname === "/v1/skills"
        ? `skills:${parsed.searchParams.get("harness")}:${parsed.searchParams.get("path")}`
        : parsed.pathname;
    const body = routes[key];
    if (body === undefined) throw new Error(`unexpected request ${url}`);
    if (typeof body === "number") return new Response("{}", { status: body });
    return Response.json(body);
  });
}

function wrapper({ children }: { children: ReactNode }) {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return <QueryClientProvider client={client}>{children}</QueryClientProvider>;
}

beforeEach(() => {
  authenticatedFetchMock.mockReset();
});

describe("installedHarnesses", () => {
  it("skips harnesses whose binary is missing or unreported", () => {
    expect(installedHarnesses(HOST)).toEqual(["claude", "codex"]);
    expect(
      installedHarnesses({ ...HOST, configured_harnesses: { "codex-native": false } }),
    ).toEqual(["codex"]);
  });

  it("probes every harness when an older host doesn't report readiness", () => {
    expect(installedHarnesses({ ...HOST, configured_harnesses: null })).toEqual([
      "claude",
      "codex",
      "cursor",
    ]);
  });
});

describe("useHarnessInventory", () => {
  it("maps login state, skills, plugins, and MCPs per harness", async () => {
    serve({
      "/v1/hosts/host_1/plugins": 501,
      "skills:claude-native:~": {
        skills: [
          { name: "review", description: "" },
          { name: "toolkit:lint", description: "" },
          { name: "toolkit:ship", description: "" },
        ],
      },
      "skills:codex-native:~": { skills: [{ name: "fix", description: "" }] },
      "/v1/hosts/host_1/mcp-servers": {
        mcp_servers: [
          { name: "github", harness: "claude", transport: "stdio", scope: "user" },
          {
            name: "figma",
            harness: "claude",
            transport: "http",
            scope: "user",
            plugin: "figma",
            url_host: "mcp.figma.com",
          },
          { name: "docs", harness: "codex", transport: "http", scope: "user" },
          { name: "slack", harness: "cursor", transport: "stdio", scope: "user" },
        ],
      },
    });

    const { result } = renderHook(
      () => useHarnessInventory(HOST, { includePluginMetadata: true }),
      { wrapper },
    );
    expect(result.current.status).toBe("loading");
    await waitFor(() => expect(result.current.status).toBe("ready"));

    expect(result.current.unavailable).toEqual([]);
    expect(result.current.isEmpty).toBe(false);
    expect(result.current.context).toEqual({
      credentials: [{ harness: "claude", source: "Databricks AI Gateway" }],
      skills: [
        { id: "claude:review", name: "review", harness: "claude", description: "" },
        { id: "codex:fix", name: "fix", harness: "codex", description: "" },
      ],
      mcps: [
        { id: "claude:github", name: "github", harness: "claude", detail: undefined },
        {
          id: "claude:plugin:figma:figma",
          name: "figma",
          harness: "claude",
          detail: "figma plugin · mcp.figma.com",
          plugin: "figma",
        },
        { id: "codex:docs", name: "docs", harness: "codex", detail: undefined },
      ],
      plugins: [
        { id: "claude:toolkit", name: "toolkit", harness: "claude", skills: ["lint", "ship"] },
        { id: "claude:figma", name: "figma", harness: "claude", skills: [] },
      ],
    });
  });

  it("says Signed in when the login source is unknown", async () => {
    serve({
      "/v1/hosts/host_1/plugins": 501,
      "skills:claude-native:~": { skills: [] },
      "skills:codex-native:~": { skills: [] },
      "/v1/hosts/host_1/mcp-servers": { mcp_servers: [] },
    });
    const host = { ...HOST, gateway_inference: null };
    const { result } = renderHook(
      () => useHarnessInventory(host, { includePluginMetadata: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.context.credentials).toEqual([
      { harness: "claude", source: "Signed in" },
    ]);
    expect(result.current.isEmpty).toBe(true);
  });

  it.each([404, 409, 501, 502])(
    "keeps skills and identifies unsupported MCP inventory (%s)",
    async (status) => {
      serve({
        "/v1/hosts/host_1/plugins": 501,
        "skills:claude-native:~": { skills: [{ name: "review", description: "" }] },
        "skills:codex-native:~": 502,
        "/v1/hosts/host_1/mcp-servers": status,
      });
      const { result } = renderHook(
        () => useHarnessInventory(HOST, { includePluginMetadata: true }),
        { wrapper },
      );
      await waitFor(() => expect(result.current.status).toBe("ready"));
      expect(result.current.unavailable).toEqual(["mcps", "skills", "plugins"]);
      expect(result.current.mcpUnsupported).toBe(status === 501);
      expect(result.current.context.skills.map((skill) => skill.name)).toEqual(["review"]);
    },
  );

  it("reports an offline host without requesting anything", () => {
    const { result } = renderHook(() => useHarnessInventory({ ...HOST, status: "offline" }), {
      wrapper,
    });
    expect(result.current.status).toBe("offline");
    expect(result.current.isEmpty).toBe(true);
    expect(authenticatedFetchMock).not.toHaveBeenCalled();
  });
});

describe("plugin metadata", () => {
  it.each([404, 501])("preserves derived plugins on %s", async (status) => {
    serve({
      "skills:claude-native:~": { skills: [{ name: "kit:review", description: "" }] },
      "skills:codex-native:~": { skills: [] },
      "/v1/hosts/host_1/mcp-servers": { mcp_servers: [] },
      "/v1/hosts/host_1/plugins": status,
    });
    const { result } = renderHook(
      () => useHarnessInventory(HOST, { includePluginMetadata: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.context.plugins.map((p) => p.name)).toEqual(["kit"]);
    expect(result.current.unavailable).toEqual([]);
  });

  it("uses installed metadata including disabled and hook-only plugins, keeping Codex", async () => {
    const plugin = {
      id: "plugin-source-id",
      skill_entries: [{ id: "skill-source-id", name: "hidden" }],
      mcp_entries: [{ id: "mcp-source-id", name: "docs" }],
      harness: "claude",
      name: "hooks",
      marketplace: "market",
      version: "1.2",
      enabled: false,
      description: "Hook helpers",
      skills: [],
      mcp_servers: [],
      has_hooks: true,
      has_commands: false,
    };
    serve({
      "skills:claude-native:~": { skills: [{ name: "stale:review", description: "" }] },
      "skills:codex-native:~": { skills: [{ name: "codex-kit:review", description: "" }] },
      "/v1/hosts/host_1/mcp-servers": { mcp_servers: [] },
      "/v1/hosts/host_1/plugins": { plugins: [plugin] },
    });
    const { result } = renderHook(
      () => useHarnessInventory(HOST, { includePluginMetadata: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.context.plugins).toEqual([
      { id: "codex:codex-kit", harness: "codex", name: "codex-kit", skills: ["review"] },
      plugin,
    ]);
  });

  it("reports metadata failures without fabricating Claude metadata", async () => {
    serve({
      "skills:claude-native:~": { skills: [{ name: "kit:review", description: "" }] },
      "skills:codex-native:~": { skills: [] },
      "/v1/hosts/host_1/mcp-servers": { mcp_servers: [] },
      "/v1/hosts/host_1/plugins": 502,
    });
    const { result } = renderHook(
      () => useHarnessInventory(HOST, { includePluginMetadata: true }),
      { wrapper },
    );
    await waitFor(() => expect(result.current.status).toBe("ready"));
    expect(result.current.context.plugins).toEqual([]);
    expect(result.current.unavailable).toEqual(["plugins"]);
  });
});

it("keeps import review on active derived assets without fetching installed metadata", async () => {
  serve({
    "skills:claude-native:~": { skills: [{ name: "kit:review", description: "" }] },
    "skills:codex-native:~": { skills: [] },
    "/v1/hosts/host_1/mcp-servers": { mcp_servers: [] },
  });
  const { result } = renderHook(() => useHarnessInventory(HOST), { wrapper });
  await waitFor(() => expect(result.current.status).toBe("ready"));
  expect(result.current.context.plugins.map((p) => p.name)).toEqual(["kit"]);
  expect(authenticatedFetchMock.mock.calls.some(([url]) => String(url).endsWith("/plugins"))).toBe(
    false,
  );
});
