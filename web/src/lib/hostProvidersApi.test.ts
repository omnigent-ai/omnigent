// Tests for the host-providers API client: URL/method/body shapes and the
// FastAPI `detail` error extraction, with `authenticatedFetch` mocked.

import { afterEach, describe, expect, it, vi } from "vitest";
import {
  clearHostAgentPin,
  fetchHostAgentSpecs,
  fetchHostProviders,
  HostProvidersApiError,
  pinHostAgent,
  testHostProvider,
  upsertHostProvider,
} from "./hostProvidersApi";

vi.mock("./identity", () => ({ authenticatedFetch: vi.fn() }));

const fetchMock = vi.mocked((await import("./identity")).authenticatedFetch);

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: "X",
    json: async () => body,
  } as Response;
}

afterEach(() => {
  fetchMock.mockReset();
});

describe("hostProvidersApi", () => {
  it("fetchHostProviders GETs the providers route and unwraps the list", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ providers: [{ name: "gw", kind: "gateway", openai: { api_key_set: true } }] }),
    );
    const providers = await fetchHostProviders("host_1");
    expect(fetchMock).toHaveBeenCalledWith("/v1/hosts/host_1/providers");
    expect(providers).toHaveLength(1);
    expect(providers[0].name).toBe("gw");
  });

  it("upsertHostProvider PUTs the entry body", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));
    await upsertHostProvider("host_1", "gw", { kind: "gateway" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/hosts/host_1/providers/gw");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({ entry: { kind: "gateway" } });
  });

  it("pinHostAgent PUTs null-padded pin fields", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));
    await pinHostAgent("host_1", "my-agent", { model: "gpt-x" });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/hosts/host_1/agent-specs/my-agent/pin");
    expect(init?.method).toBe("PUT");
    expect(JSON.parse(String(init?.body))).toEqual({ provider: null, model: "gpt-x" });
  });

  it("clearHostAgentPin DELETEs", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({}));
    await clearHostAgentPin("host_1", "my-agent");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/hosts/host_1/agent-specs/my-agent/pin");
    expect(init?.method).toBe("DELETE");
  });

  it("testHostProvider POSTs and returns the probe result", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({ name: "gw", family: "openai", endpoint: "https://x/v1/models", ok: true }),
    );
    const result = await testHostProvider("host_1", "gw");
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe("/v1/hosts/host_1/providers/gw/test");
    expect(init?.method).toBe("POST");
    expect(result.ok).toBe(true);
  });

  it("surfaces the server's detail message on failure", async () => {
    fetchMock.mockResolvedValueOnce(jsonResponse({ detail: "no provider named 'x'" }, 404));
    await expect(fetchHostProviders("host_1")).rejects.toMatchObject({
      name: "HostProvidersApiError",
      status: 404,
      message: "no provider named 'x'",
    } satisfies Partial<HostProvidersApiError>);
  });

  it("fetchHostAgentSpecs unwraps the agents list", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        agents: [
          { name: "my-agent", harness: "pi", model: null, auth: null, spec_version: 1, path: "/x" },
        ],
      }),
    );
    const agents = await fetchHostAgentSpecs("host_1");
    expect(agents[0].name).toBe("my-agent");
  });
});
