import { afterEach, describe, expect, it, vi } from "vitest";
import * as host from "@/lib/host";
import { basenamedRouting, reactRouterRouting } from "@/lib/routing";
import { getShareableSessionLink } from "./shareLink";

vi.mock("@/lib/host", async (importOriginal) => ({
  ...(await importOriginal<typeof host>()),
  getOmnigentTransformShareLink: vi.fn(() => undefined),
}));

const standalone = reactRouterRouting.rebasePath;
const embedded = basenamedRouting("/ml/omnigent-embed").rebasePath;

afterEach(() => {
  delete window.__OMNIGENT_BASE_PATH__;
  vi.mocked(host.getOmnigentTransformShareLink).mockReturnValue(undefined);
});

describe("getShareableSessionLink", () => {
  it("encodes a fullscreen file target under the deployment base path", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const filePath = "artifacts/财务 report #1 & + %.html";
    const url = new URL(getShareableSessionLink("conv_1", standalone, filePath));

    expect(url.pathname).toBe("/proxy/6767/c/conv_1");
    expect([...url.searchParams]).toEqual([
      ["file", filePath],
      ["fullscreen", "1"],
    ]);
  });

  it("preserves host query parameters when adding a file target", () => {
    vi.mocked(host.getOmnigentTransformShareLink).mockReturnValue(
      (path: string) => `https://host.example.com${path}?o=42`,
    );
    const url = new URL(getShareableSessionLink("conv_1", embedded, "report.html"));

    expect(url.pathname).toBe("/ml/omnigent-embed/c/conv_1");
    expect([...url.searchParams]).toEqual([
      ["o", "42"],
      ["file", "report.html"],
      ["fullscreen", "1"],
    ]);
  });

  it("falls back to the deployment URL when the host returns an invalid URL", () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    vi.mocked(host.getOmnigentTransformShareLink).mockReturnValue(() => "https://[");

    const url = new URL(getShareableSessionLink("conv_1", embedded, "report #1.html"));

    expect(url.origin).toBe(window.location.origin);
    expect(url.pathname).toBe("/proxy/6767/ml/omnigent-embed/c/conv_1");
    expect([...url.searchParams]).toEqual([
      ["file", "report #1.html"],
      ["fullscreen", "1"],
    ]);
  });

  it("preserves a relative host transform's prefix and query", () => {
    vi.mocked(host.getOmnigentTransformShareLink).mockReturnValue((path) => `/embed${path}?host=1`);

    const url = new URL(getShareableSessionLink("conv_1", embedded, "report #1.html"));

    expect(url.origin).toBe(window.location.origin);
    expect(url.pathname).toBe("/embed/ml/omnigent-embed/c/conv_1");
    expect([...url.searchParams]).toEqual([
      ["host", "1"],
      ["file", "report #1.html"],
      ["fullscreen", "1"],
    ]);
  });

  it("preserves hash-router queries and outer host queries for file links", () => {
    vi.mocked(host.getOmnigentTransformShareLink).mockReturnValue(
      (path: string) => `https://host.example.com/embed?host=1#${path}?o=42`,
    );
    const url = new URL(getShareableSessionLink("conv_1", embedded, "report #1.html"));
    const route = new URL(url.hash.slice(1), url.origin);

    expect(url.searchParams.get("host")).toBe("1");
    expect(route.pathname).toBe("/ml/omnigent-embed/c/conv_1");
    expect([...route.searchParams]).toEqual([
      ["o", "42"],
      ["file", "report #1.html"],
      ["fullscreen", "1"],
    ]);
  });

  it("prefixes a standalone deployment's origin and base path", () => {
    expect(getShareableSessionLink("conv_1", standalone)).toBe(
      `${window.location.origin}/c/conv_1`,
    );
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    expect(getShareableSessionLink("conv_1", standalone)).toBe(
      `${window.location.origin}/proxy/6767/c/conv_1`,
    );
  });

  it("keeps an embed's mount path when the host has no transform", () => {
    expect(getShareableSessionLink("conv_1", embedded)).toBe(
      `${window.location.origin}/ml/omnigent-embed/c/conv_1`,
    );
  });

  it("hands the whole rebased path to the host's transform", () => {
    // A different origin, hash routing, and a query after the path: not a prefix.
    vi.mocked(host.getOmnigentTransformShareLink).mockReturnValue(
      (path: string) => `https://host.example.com/embed#${path}?o=42`,
    );
    expect(getShareableSessionLink("conv_1", embedded)).toBe(
      "https://host.example.com/embed#/ml/omnigent-embed/c/conv_1?o=42",
    );
  });
});
