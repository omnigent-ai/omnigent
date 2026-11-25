import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { CapabilitiesProvider } from "@/lib/CapabilitiesContext";
import { FALLBACK_SERVER_INFO, type ServerInfo } from "@/lib/capabilities";
import { CLAUDE_NATIVE_MODELS } from "@/lib/claudeNativeModels";
import { authenticatedFetch } from "@/lib/identity";
import { sandboxSessionCatalogKey, writeModelCatalogCache } from "@/lib/modelCatalogCache";
import { newSandboxModelOptions, useNewSandboxModelCatalog } from "./useNewSandboxModelCatalog";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn(), getCurrentUserId: () => "alice" }));
const fetchMock = vi.mocked(authenticatedFetch);

type ProviderCapabilities = NonNullable<ServerInfo["sandbox_provider_capabilities"]>[string];

const gatewayRows = [{ id: "gateway/opus", displayName: "Opus", isDefault: false }];
const savedRows = [{ id: "opus", displayName: "Opus" }];
const PREVIEW_URL =
  "/v1/sandbox-providers/modal/harnesses/claude-native/model-options?agent_id=ag_1";

function preview(overrides: Record<string, unknown> = {}) {
  return {
    configured: false,
    status: "ready",
    models: gatewayRows,
    configuration_revision: null,
    provider_label: "AI Gateway",
    default_model: null,
    ...overrides,
  };
}

function render(capabilities: ProviderCapabilities, sandboxSelected = true) {
  const info: ServerInfo = {
    ...FALLBACK_SERVER_INFO,
    managed_sandboxes_enabled: true,
    sandbox_provider: "modal",
    sandbox_provider_capabilities: { modal: capabilities },
  };
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return renderHook(
    () => useNewSandboxModelCatalog("modal", "claude-native", "ag_1", "alice", sandboxSelected),
    {
      wrapper: ({ children }: { children: ReactNode }) => (
        <QueryClientProvider client={client}>
          <CapabilitiesProvider info={info}>{children}</CapabilitiesProvider>
        </QueryClientProvider>
      ),
    },
  );
}

function saveSessionCatalog() {
  writeModelCatalogCache(
    sandboxSessionCatalogKey("modal", "claude-native", "ag_1"),
    savedRows,
    false,
  );
}

afterEach(() => {
  cleanup();
  localStorage.clear();
  fetchMock.mockReset();
});

describe("useNewSandboxModelCatalog", () => {
  it.each([
    {
      name: "a default catalog's gateway rows",
      capabilities: { default_models: true },
      body: preview(),
      expected: gatewayRows,
    },
    {
      name: "a bound profile's configured rows",
      capabilities: { inference_models: true },
      body: preview({ configured: true }),
      expected: gatewayRows,
    },
    {
      name: "no catalog for a harness the gateway cannot serve",
      capabilities: { default_models: true },
      body: preview({ models: [], status: "unconfigured" }),
      expected: undefined,
    },
  ])("lists $name", async ({ capabilities, body, expected }) => {
    fetchMock.mockResolvedValue(new Response(JSON.stringify(body)));
    const { result } = render(capabilities);
    await waitFor(() => expect(result.current.preview.data).toEqual(body));
    expect(result.current.catalog).toEqual(expected);
    expect(fetchMock).toHaveBeenCalledExactlyOnceWith(PREVIEW_URL);
  });

  it.each([
    {
      name: "prefers it to a default catalog's gateway rows",
      capabilities: { default_models: true },
      body: preview(),
      expected: savedRows,
    },
    {
      name: "ignores it for a bound profile",
      capabilities: { inference_models: true },
      body: preview({ configured: true }),
      expected: gatewayRows,
    },
    {
      name: "ignores it when no model can serve the harness",
      capabilities: { default_models: true },
      body: preview({ models: [], status: "unsupported" }),
      expected: [],
    },
  ])("given the agent's saved session catalog, $name", async ({ capabilities, body, expected }) => {
    saveSessionCatalog();
    fetchMock.mockResolvedValue(new Response(JSON.stringify(body)));
    const { result } = render(capabilities);
    await waitFor(() => expect(result.current.preview.data).toEqual(body));
    expect(result.current.catalog).toEqual(expected);
  });

  it.each([
    { name: "for a provider without a catalog", capabilities: {}, sandboxSelected: true },
    {
      name: "until a sandbox is chosen",
      capabilities: { default_models: true },
      sandboxSelected: false,
    },
  ])("requests nothing $name", async ({ capabilities, sandboxSelected }) => {
    const { result } = render(capabilities, sandboxSelected);
    await act(
      () =>
        new Promise<void>((resolve) => {
          setTimeout(resolve, 0);
        }),
    );
    expect(result.current.catalog).toBeUndefined();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("reports a pending preview without rows", () => {
    fetchMock.mockReturnValue(new Promise<Response>(() => {}));
    const { result } = render({ default_models: true });
    expect(result.current).toMatchObject({ pending: true, catalog: [], unsupported: false });
  });

  it.each([
    {
      name: "a failed default listing",
      capabilities: { default_models: true },
      response: () => new Response(JSON.stringify(preview({ models: [], status: "unavailable" }))),
      expected: { defaultCatalogError: "Could not load AI Gateway models.", catalog: [] },
    },
    {
      name: "a rejected preview request",
      capabilities: { default_models: true },
      response: () => new Response(JSON.stringify({ detail: "Gateway down" }), { status: 502 }),
      expected: { defaultCatalogError: "Gateway down", catalog: [] },
    },
    {
      name: "a bound profile without usable models",
      capabilities: { inference_models: true },
      response: () =>
        new Response(
          JSON.stringify(preview({ configured: true, models: [], status: "empty", error: "None" })),
        ),
      expected: { boundCatalogError: "None", defaultCatalogError: null },
    },
    {
      name: "a harness no model can serve",
      capabilities: { default_models: true },
      response: () => new Response(JSON.stringify(preview({ models: [], status: "unsupported" }))),
      expected: { unsupported: true, defaultCatalogError: null, catalog: [] },
    },
  ])("reports $name", async ({ capabilities, response, expected }) => {
    fetchMock.mockImplementation(async () => response());
    const { result } = render(capabilities);
    await waitFor(() => expect(result.current.pending).toBe(false));
    expect(result.current).toMatchObject(expected);
  });
});

describe("newSandboxModelOptions", () => {
  it("offers the sandbox catalog to any harness", () => {
    expect(newSandboxModelOptions("codex-native", gatewayRows, false)).toBe(gatewayRows);
  });

  it.each([
    {
      harness: "claude-native",
      unavailable: false,
      expected: CLAUDE_NATIVE_MODELS.map((m) => m.id),
    },
    { harness: "claude-native", unavailable: true, expected: [] },
    { harness: "codex-native", unavailable: false, expected: [] },
  ])(
    "without a catalog offers $harness (unavailable: $unavailable) $expected",
    ({ harness, unavailable, expected }) => {
      expect(newSandboxModelOptions(harness, undefined, unavailable).map((row) => row.id)).toEqual(
        expected,
      );
    },
  );
});
