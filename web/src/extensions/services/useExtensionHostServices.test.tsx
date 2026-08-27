import "fake-indexeddb/auto";
import { act, renderHook } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ExtensionCatalogItem } from "../types";
import { HOST_METHOD_PERMISSIONS } from "./registry";
import { resetExtensionStorageForTests } from "./storage";

const { navigate, identityRef, serverRef } = vi.hoisted(() => ({
  navigate: vi.fn(),
  identityRef: { current: "user@example.com" as string | null },
  serverRef: { current: "server-a" as string | null },
}));
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
vi.mock("@/lib/identity", () => ({ resolveIdentity: async () => identityRef.current }));
vi.mock("@/lib/host", () => ({ getOmnigentServerIdentity: () => serverRef.current }));
vi.mock("next-themes", () => ({ useTheme: () => ({ resolvedTheme: "dark" }) }));

import { useExtensionHostServices } from "./useExtensionHostServices";

const extension: ExtensionCatalogItem = {
  object: "extension",
  id: "acme.review",
  display_name: "Review",
  distribution: "acme-review",
  version: "1.0.0",
  extension_api: 1,
  status: "enabled",
  permissions: ["navigation", "storage.user"],
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
    digest: "digest",
    script_url: "/script",
    style_url: null,
  },
};
const signal = () => new AbortController().signal;

beforeEach(async () => {
  navigate.mockReset();
  identityRef.current = "user@example.com";
  serverRef.current = "server-a";
  await resetExtensionStorageForTests();
});

describe("useExtensionHostServices", () => {
  it("declares a permission rule for every exposed host method", () => {
    const { result } = renderHook(() => useExtensionHostServices(extension));
    expect(Object.keys(result.current.methods).sort()).toEqual(
      Object.keys(HOST_METHOD_PERMISSIONS).sort(),
    );
  });

  it("routes only to pages owned by the extension and preserves params", () => {
    const { result } = renderHook(() => useExtensionHostServices(extension));
    act(() => {
      result.current.methods["navigation.openPage"]?.(
        { pageId: "acme.review.dashboard", params: { tab: "files" } },
        signal(),
      );
    });
    expect(navigate).toHaveBeenCalledWith({
      pathname: "/extensions/acme.review/dashboard",
      search: "?tab=files",
    });

    expect(() =>
      result.current.methods["navigation.openPage"]?.({ pageId: "other.page" }, signal()),
    ).toThrow("Page is not owned by extension");
  });

  it("opens sessions and the new-session page through parent routing", () => {
    const { result } = renderHook(() => useExtensionHostServices(extension));
    act(() => {
      result.current.methods["navigation.openSession"]?.({ sessionId: "conv_123" }, signal());
      result.current.methods["navigation.openNewSession"]?.({}, signal());
    });

    expect(navigate).toHaveBeenNthCalledWith(1, "/c/conv_123");
    expect(navigate).toHaveBeenNthCalledWith(2, "/");
  });

  it("returns theme snapshots and emits theme state", () => {
    const { result } = renderHook(() => useExtensionHostServices(extension));

    expect(result.current.methods["theme.getCurrent"]?.({}, signal())).toEqual({ theme: "dark" });
    expect(result.current.events).toEqual({ "theme.changed": { theme: "dark" } });
  });

  it("omits methods whose permissions were not granted", () => {
    const denied = { ...extension, permissions: [] };
    const { result } = renderHook(() => useExtensionHostServices(denied));

    expect(Object.keys(result.current.methods).sort()).toEqual([
      "theme.getCurrent",
      "theme.subscribe",
    ]);
  });

  it("refuses storage until both user and server identities resolve", async () => {
    identityRef.current = null;
    serverRef.current = null;
    const { result } = renderHook(() => useExtensionHostServices(extension));

    await expect(
      result.current.methods["storage.user.get"]?.({ key: "layout" }, signal()),
    ).rejects.toMatchObject({ code: "Unavailable" });
  });

  it("persists extension-scoped values through the storage service", async () => {
    const { result } = renderHook(() => useExtensionHostServices(extension));
    await act(() =>
      result.current.methods["storage.user.set"]?.({ key: "layout", value: { x: 1 } }, signal()),
    );

    await expect(
      result.current.methods["storage.user.get"]?.({ key: "layout" }, signal()),
    ).resolves.toEqual({ x: 1 });
  });
});
