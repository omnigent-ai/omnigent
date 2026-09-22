import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { authenticatedFetch } from "@/lib/identity";
import type { ExtensionCatalogItem, ExtensionPage } from "../types";
import { buildExtensionDocument, loadExtensionBundle } from "./host";
import { EXTENSION_RPC_SOURCE, EXTENSION_RPC_VERSION } from "./protocol";
import { isExtensionInboundMessage } from "./validation";

vi.mock("@/lib/identity", () => ({ authenticatedFetch: vi.fn() }));

const page: ExtensionPage = {
  id: "acme.review.dashboard",
  title: "Dashboard",
  route: "dashboard",
  view: "dashboard",
};

const extension: ExtensionCatalogItem = {
  object: "extension",
  id: "acme.review",
  display_name: "Review",
  distribution: "acme-review",
  version: "1.0.0",
  extension_api: 1,
  status: "enabled",
  permissions: [],
  pages: [page],
  primary_navigation: [],
  browser: {
    declared: true,
    has_styles: true,
    digest: "old",
    script_url: "/old/extension.js",
    style_url: "/old/extension.css",
  },
};

beforeEach(() => vi.mocked(authenticatedFetch).mockReset());

describe("loadExtensionBundle", () => {
  it("refetches the catalog once after a stale digest 404", async () => {
    const refreshed = {
      ...extension,
      browser: {
        ...extension.browser,
        digest: "new",
        script_url: "/new/extension.js",
        style_url: null,
      },
    };
    vi.mocked(authenticatedFetch)
      .mockResolvedValueOnce(new Response("", { status: 404 }))
      .mockResolvedValueOnce(new Response("new script", { status: 200 }));
    const refresh = vi.fn(async () => [refreshed]);

    const bundle = await loadExtensionBundle(extension, refresh);

    expect(refresh).toHaveBeenCalledOnce();
    expect(bundle.extension.browser.digest).toBe("new");
    expect(bundle.script).toBe("new script");
    expect(authenticatedFetch).toHaveBeenCalledTimes(2);
  });

  it("does not refresh non-404 failures", async () => {
    vi.mocked(authenticatedFetch).mockResolvedValue(new Response("", { status: 500 }));
    const refresh = vi.fn(async () => [extension]);

    await expect(loadExtensionBundle(extension, refresh)).rejects.toThrow("(500)");
    expect(refresh).not.toHaveBeenCalled();
  });
});

describe("buildExtensionDocument", () => {
  it("builds an opaque-origin document with no network egress", () => {
    const { srcDoc, identity } = buildExtensionDocument(
      { extension, script: "globalThis.ok=true;</script>", styles: "body{color:red}</style>" },
      page,
      "nonce123",
    );

    expect(identity).toEqual({
      extensionId: extension.id,
      pageId: page.id,
      view: page.view,
      nonce: "nonce123",
      apiVersion: EXTENSION_RPC_VERSION,
    });
    expect(srcDoc).toContain("default-src 'none'");
    expect(srcDoc).toContain("connect-src 'none'");
    expect(srcDoc).toContain("img-src data: blob:");
    expect(srcDoc).toContain("form-action 'none'");
    expect(srcDoc).toContain("base-uri 'none'");
    expect(srcDoc).toContain("webrtc 'none'");
    expect(srcDoc).toContain('nonce="nonce123"');
    expect(srcDoc).toContain("atob(");
    expect(srcDoc).not.toContain("globalThis.ok=true;</script>");
    expect(srcDoc).not.toContain("body{color:red}</style>");
  });
});

describe("protocol parity", () => {
  it("pins the browser SDK to the host protocol constants", () => {
    const sdkProtocol = readFileSync(
      resolve(process.cwd(), "../sdks/web-extension/src/protocol.ts"),
      "utf8",
    );
    expect(sdkProtocol).toContain(`EXTENSION_RPC_SOURCE = "${EXTENSION_RPC_SOURCE}"`);
    expect(sdkProtocol).toContain(`EXTENSION_RPC_VERSION = ${EXTENSION_RPC_VERSION}`);
  });
});

describe("isExtensionInboundMessage", () => {
  const identity = {
    extensionId: extension.id,
    pageId: page.id,
    view: page.view,
    nonce: "nonce",
    apiVersion: EXTENSION_RPC_VERSION,
  };

  it("accepts only bounded messages matching the mount identity", () => {
    const ready = { ...identity, source: EXTENSION_RPC_SOURCE, type: "ready" };
    expect(isExtensionInboundMessage(ready, identity)).toBe(true);
    expect(isExtensionInboundMessage({ ...ready, nonce: "wrong" }, identity)).toBe(false);
    expect(isExtensionInboundMessage({ ...ready, extra: "x".repeat(70_000) }, identity)).toBe(
      false,
    );
  });
});
