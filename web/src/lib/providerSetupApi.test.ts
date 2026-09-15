import { beforeEach, describe, expect, it, vi } from "vitest";
import {
  cancelSetupOperation,
  detectSetup,
  fetchSetupInventory,
  fetchSetupOperation,
  runSetupAction,
  setupOperationAttachUrl,
  startSetupOperation,
  verifySetupOperation,
} from "./providerSetupApi";
import { authenticatedFetch } from "./identity";
import { resolveWebSocketUrl } from "./host";

vi.mock("./identity", () => ({
  authenticatedFetch: vi.fn(),
}));

vi.mock("./host", () => ({
  resolveWebSocketUrl: vi.fn(),
}));

const mockAuthenticatedFetch = vi.mocked(authenticatedFetch);
const mockResolveWebSocketUrl = vi.mocked(resolveWebSocketUrl);

function mockJsonResponse(
  body: unknown,
  init: { ok?: boolean; status?: number; statusText?: string } = {},
): Response {
  return {
    ok: init.ok ?? true,
    status: init.status ?? 200,
    statusText: init.statusText ?? "OK",
    json: async () => body,
  } as unknown as Response;
}

beforeEach(() => {
  mockAuthenticatedFetch.mockReset();
  mockResolveWebSocketUrl.mockReset();
});

describe("provider setup API", () => {
  it("encodes host IDs and uses the expected read requests", async () => {
    const signal = new AbortController().signal;
    const inventory = { feature_enabled: true };
    const detection = { providers: [], imports: [], models: {} };
    mockAuthenticatedFetch
      .mockResolvedValueOnce(mockJsonResponse(inventory))
      .mockResolvedValueOnce(mockJsonResponse(detection));

    await expect(fetchSetupInventory("host /?", signal)).resolves.toEqual(inventory);
    await expect(detectSetup("host /?")).resolves.toEqual(detection);

    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(1, "/v1/hosts/host%20%2F%3F/setup", {
      signal,
    });
    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(
      2,
      "/v1/hosts/host%20%2F%3F/setup/detect",
      { method: "POST" },
    );
  });

  it("POSTs setup actions with JSON content and preserves the action body", async () => {
    const action = {
      action: "set_default" as const,
      name: "Gateway / one",
      surface: "openai" as const,
    };
    const result = { ok: true, message: "updated", inventory: {} };
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse(result));

    await expect(runSetupAction("host", action)).resolves.toEqual(result);

    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/hosts/host/setup/actions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(action),
    });
  });

  it("sends a typed source and path for an explicit ACP import preview", async () => {
    const detection = { providers: [], imports: [], models: {}, default_models: {} };
    mockAuthenticatedFetch.mockResolvedValueOnce(mockJsonResponse(detection));

    await expect(
      detectSetup("host", {
        import_source: "openclaw",
        import_path: "/Users/example/.openclaw/config.json",
      }),
    ).resolves.toEqual(detection);

    expect(mockAuthenticatedFetch).toHaveBeenCalledWith("/v1/hosts/host/setup/detect", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        import_source: "openclaw",
        import_path: "/Users/example/.openclaw/config.json",
      }),
    });
  });

  it("uses server error detail and ignores non-string detail values", async () => {
    mockAuthenticatedFetch
      .mockResolvedValueOnce(
        mockJsonResponse(
          { detail: "Provider rejected the credential", error: { message: "nested detail" } },
          { ok: false, status: 422, statusText: "Unprocessable Entity" },
        ),
      )
      .mockResolvedValueOnce(
        mockJsonResponse(
          { detail: { message: "do not stringify me" }, error: { message: 42 } },
          { ok: false, status: 502, statusText: "Bad Gateway" },
        ),
      );

    await expect(fetchSetupInventory("host")).rejects.toThrow("nested detail");
    await expect(fetchSetupInventory("host")).rejects.toThrow("502 Bad Gateway");
  });

  it("starts, fetches, and cancels an operation with encoded path values", async () => {
    const operation = { operation_id: "op/1", state: "running" };
    const parameters = { redirect_uri: "http://localhost/callback?x=1" };
    mockAuthenticatedFetch
      .mockResolvedValueOnce(mockJsonResponse(operation))
      .mockResolvedValueOnce(mockJsonResponse(operation))
      .mockResolvedValueOnce(mockJsonResponse({ ...operation, state: "cancelled" }));
    const signal = new AbortController().signal;

    await expect(startSetupOperation("host /?", "claude-login", parameters)).resolves.toEqual(
      operation,
    );
    await expect(fetchSetupOperation("host /?", "op/1", signal)).resolves.toEqual(operation);
    await expect(cancelSetupOperation("host /?", "op/1")).resolves.toMatchObject({
      state: "cancelled",
    });

    const base = "/v1/hosts/host%20%2F%3F/setup-operations";
    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(1, base, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "claude-login", parameters }),
    });
    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(2, `${base}/op%2F1`, { signal });
    expect(mockAuthenticatedFetch).toHaveBeenNthCalledWith(3, `${base}/op%2F1`, {
      method: "DELETE",
    });
  });

  it("verifies on the selected host and preserves a retryable connection failure", async () => {
    mockAuthenticatedFetch
      .mockResolvedValueOnce(
        mockJsonResponse({ detail: "Sign-in is not complete" }, { ok: false, status: 409 }),
      )
      .mockResolvedValueOnce(
        mockJsonResponse({ operation_id: "op/1", state: "succeeded", already_connected: true }),
      );

    await expect(verifySetupOperation("host /?", "op/1")).rejects.toMatchObject({
      status: 409,
      message: "Sign-in is not complete",
    });
    await expect(verifySetupOperation("host /?", "op/1")).resolves.toMatchObject({
      state: "succeeded",
      already_connected: true,
    });
    expect(mockAuthenticatedFetch).toHaveBeenLastCalledWith(
      "/v1/hosts/host%20%2F%3F/setup-operations/op%2F1/verify",
      { method: "POST" },
    );
  });

  it("resolves the attach URL with encoded host and operation and the host slice key", () => {
    mockResolveWebSocketUrl.mockImplementation((path) => `ws://resolved${path}`);

    expect(setupOperationAttachUrl("host /?", "op/1 ?")).toBe(
      "ws://resolved/v1/hosts/host%20%2F%3F/setup-operations/op%2F1%20%3F/attach?omnigent_slice_key=host%20%2F%3F",
    );
    expect(mockResolveWebSocketUrl).toHaveBeenCalledWith(
      "/v1/hosts/host%20%2F%3F/setup-operations/op%2F1%20%3F/attach?omnigent_slice_key=host%20%2F%3F",
    );
  });
});
