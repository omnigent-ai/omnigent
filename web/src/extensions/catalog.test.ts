import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import { fetchExtensionCatalog, resolveExtensionPageFromPath } from "./catalog";
import type { ExtensionCatalogItem } from "./types";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const extension: ExtensionCatalogItem = {
  object: "extension",
  id: "acme.review",
  display_name: "Review",
  distribution: "acme-review",
  version: "1.0.0",
  extension_api: 1,
  status: "enabled",
  permissions: [],
  pages: [
    {
      id: "acme.review.dashboard",
      title: "Dashboard",
      route: "dashboard",
      view: "dashboard",
    },
  ],
  primary_navigation: [],
  browser: {
    declared: true,
    has_styles: false,
    digest: "abc",
    script_url: "/script",
    style_url: null,
  },
};

beforeEach(() => vi.mocked(authenticatedFetch).mockReset());

describe("fetchExtensionCatalog", () => {
  it("keeps enabled extensions and suppresses unavailable ones", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(
      new Response(
        JSON.stringify({
          object: "list",
          data: [extension, { ...extension, id: "acme.broken", status: "unavailable" }],
        }),
        { status: 200 },
      ),
    );

    await expect(fetchExtensionCatalog()).resolves.toEqual([extension]);
  });

  it("rejects failed and malformed responses", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValueOnce(new Response("", { status: 500 }));
    await expect(fetchExtensionCatalog()).rejects.toThrow("Failed to load extensions (500)");

    vi.mocked(authenticatedFetch).mockResolvedValueOnce(
      new Response(JSON.stringify({ object: "wrong", data: [] }), { status: 200 }),
    );
    await expect(fetchExtensionCatalog()).rejects.toThrow("Invalid extension catalog response");
  });
});

describe("resolveExtensionPageFromPath", () => {
  it("resolves standalone and embedded paths", () => {
    expect(
      resolveExtensionPageFromPath([extension], "/extensions/acme.review/dashboard")?.page.id,
    ).toBe("acme.review.dashboard");
    expect(
      resolveExtensionPageFromPath([extension], "/mount/extensions/acme.review/dashboard")?.page.id,
    ).toBe("acme.review.dashboard");
  });

  it("rejects unknown pages", () => {
    expect(resolveExtensionPageFromPath([extension], "/extensions/acme.review/missing")).toBeNull();
  });
});
