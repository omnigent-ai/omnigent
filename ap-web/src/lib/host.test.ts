import { afterEach, describe, expect, it, vi } from "vitest";

import { getCliServerUrl, hostFetch, resolveWebSocketUrl, setOmnigentHostConfig } from "./host";

afterEach(() => {
  setOmnigentHostConfig({});
  delete window.__OMNIGENT_BASE_PATH__;
  vi.restoreAllMocks();
});

describe("getCliServerUrl", () => {
  it("returns window.location.origin when no suffix is configured", () => {
    setOmnigentHostConfig({});
    const url = getCliServerUrl();
    expect(url).toBe(window.location.origin);
  });

  it("appends the configured cliServerUrlSuffix", () => {
    setOmnigentHostConfig({ cliServerUrlSuffix: "/api/2.0/omnigent" });
    const url = getCliServerUrl();
    expect(url).toBe(`${window.location.origin}/api/2.0/omnigent`);
  });

  it("handles an empty string suffix the same as no suffix", () => {
    setOmnigentHostConfig({ cliServerUrlSuffix: "" });
    expect(getCliServerUrl()).toBe(window.location.origin);
  });

  it("includes the base path before the suffix when one is configured", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    setOmnigentHostConfig({ cliServerUrlSuffix: "/api" });
    expect(getCliServerUrl()).toBe(`${window.location.origin}/proxy/6767/api`);
  });
});

describe("hostFetch base path", () => {
  it("prepends the base path to standalone fetch calls", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 200 }));
    await hostFetch("/v1/sessions");
    expect(fetchSpy).toHaveBeenCalledWith("/proxy/6767/v1/sessions", undefined);
  });

  it("leaves the path unchanged when no base path is set", async () => {
    const fetchSpy = vi
      .spyOn(globalThis, "fetch")
      .mockResolvedValue(new Response(null, { status: 200 }));
    await hostFetch("/v1/sessions");
    expect(fetchSpy).toHaveBeenCalledWith("/v1/sessions", undefined);
  });

  it("passes the un-prefixed path to a host fetcher (host owns rebasing)", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const fetcher = vi.fn().mockResolvedValue(new Response(null, { status: 200 }));
    setOmnigentHostConfig({ fetcher });
    await hostFetch("/v1/sessions");
    expect(fetcher).toHaveBeenCalledWith("/v1/sessions", undefined);
  });
});

describe("resolveWebSocketUrl base path", () => {
  it("prepends the base path to the WebSocket path", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const url = resolveWebSocketUrl("/v1/sessions/updates");
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    expect(url).toBe(`${scheme}//${window.location.host}/proxy/6767/v1/sessions/updates`);
  });

  it("builds a root-relative WebSocket URL when no base path is set", () => {
    const url = resolveWebSocketUrl("/v1/sessions/updates");
    const scheme = window.location.protocol === "https:" ? "wss:" : "ws:";
    expect(url).toBe(`${scheme}//${window.location.host}/v1/sessions/updates`);
  });
});
