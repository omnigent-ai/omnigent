import { beforeEach, describe, expect, it, vi } from "vitest";
import { fetchWithBrowserSession } from "./identity";

const host = vi.hoisted(() => ({ fetch: vi.fn(), config: vi.fn() }));
vi.mock("./host", () => ({
  hostFetch: host.fetch,
  getOmnigentHostConfig: host.config,
  isDatabricksWorkspace: () => false,
}));

beforeEach(() => {
  host.fetch.mockReset();
  host.config.mockReturnValue({});
});

describe("browser-native notebook identity", () => {
  it("reports the browser session result without injecting JavaScript identity headers", async () => {
    const response = new Response("Login required", { status: 401 });
    host.fetch.mockResolvedValue(response);
    const signal = new AbortController().signal;
    expect(await fetchWithBrowserSession("/v1/sessions/example/docloop/jupyter", signal)).toBe(
      response,
    );
    expect(host.fetch).toHaveBeenCalledWith("/v1/sessions/example/docloop/jupyter", {
      signal,
      credentials: "same-origin",
      cache: "no-store",
    });
  });
  it("does not send iframe probes through an embed-only fetcher", () => {
    host.config.mockReturnValue({ fetcher: vi.fn() });
    expect(() => fetchWithBrowserSession("/v1/sessions/example/docloop/jupyter")).toThrow(
      "directly in Omnigent",
    );
    expect(host.fetch).not.toHaveBeenCalled();
  });
});
