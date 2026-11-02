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
