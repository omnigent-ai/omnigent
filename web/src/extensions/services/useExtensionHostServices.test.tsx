import "fake-indexeddb/auto";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import type { ReactNode } from "react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import type { ExtensionCatalogItem } from "../types";
import { HOST_METHOD_PERMISSIONS } from "./registry";
import { resetExtensionStorageForTests } from "./storage";

const { navigate, identityRef, serverRef, authenticatedFetchMock } = vi.hoisted(() => ({
  navigate: vi.fn(),
  authenticatedFetchMock: vi.fn(),
  identityRef: { current: "user@example.com" as string | null },
  serverRef: { current: "server-a" as string | null },
}));
vi.mock("@/lib/routing", () => ({ useNavigate: () => navigate }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: authenticatedFetchMock,
  resolveIdentity: async () => identityRef.current,
}));
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
  permissions: ["navigation", "projects.read", "projects.write", "sessions.read", "storage.user"],
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
const queryClient = new QueryClient();
const wrapper = ({ children }: { children: ReactNode }) => (
  <QueryClientProvider client={queryClient}>{children}</QueryClientProvider>
);

beforeEach(async () => {
  navigate.mockReset();
  authenticatedFetchMock.mockReset();
  identityRef.current = "user@example.com";
  serverRef.current = "server-a";
  await resetExtensionStorageForTests();
});

describe("useExtensionHostServices", () => {
  it("declares a permission rule for every exposed host method", () => {
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });
    expect(Object.keys(result.current.methods).sort()).toEqual(
      Object.keys(HOST_METHOD_PERMISSIONS).sort(),
    );
  });

  it("routes only to pages owned by the extension and preserves params", () => {
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });
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
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });
    act(() => {
      result.current.methods["navigation.openSession"]?.({ sessionId: "conv_123" }, signal());
      result.current.methods["navigation.openNewSession"]?.({}, signal());
    });

    expect(navigate).toHaveBeenNthCalledWith(1, "/c/conv_123");
    expect(navigate).toHaveBeenNthCalledWith(2, "/");
  });

  it("returns theme snapshots and emits theme state", () => {
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });

    expect(result.current.methods["theme.getCurrent"]?.({}, signal())).toEqual({ theme: "dark" });
    expect(result.current.events).toEqual({ "theme.changed": { theme: "dark" } });
  });

  it("lists only the projected session summary through the host method", async () => {
    authenticatedFetchMock.mockResolvedValue(
      new Response(
        JSON.stringify({
          data: [
            {
              id: "conv_1",
              title: "One",
              status: "idle",
              workspace: "/workspace",
              created_at: 1,
              updated_at: 2,
              owner: "hidden",
            },
          ],
          has_more: false,
          last_id: "conv_1",
        }),
        { status: 200 },
      ),
    );
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });

    await expect(result.current.methods["sessions.listPage"]?.({}, signal())).resolves.toEqual({
      sessions: [
        {
          id: "conv_1",
          title: "One",
          status: "idle",
          titleProvisional: false,
          unread: false,
          workspace: "/workspace",
          gitBranch: null,
          projectId: null,
          createdAt: 1,
          updatedAt: 2,
        },
      ],
      nextCursor: null,
      hasMore: false,
    });
  });

  it("opens a project-scoped new session by resolving the project name", async () => {
    const projects = new Response(
      JSON.stringify({ object: "list", data: [{ id: "p1", name: "Alpha & co" }] }),
      { status: 200 },
    );
    authenticatedFetchMock.mockResolvedValueOnce(projects).mockResolvedValueOnce(projects.clone());
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });

    await result.current.methods["navigation.openNewSession"]?.({ projectId: "p1" }, signal());
    expect(navigate).toHaveBeenCalledWith({ pathname: "/", search: "?project=Alpha%20%26%20co" });
    await expect(
      result.current.methods["navigation.openNewSession"]?.({ projectId: "missing" }, signal()),
    ).rejects.toMatchObject({ code: "InvalidParams", message: "Project not found" });
  });

  it("returns a session's pull request and only opens URLs it handed out", async () => {
    authenticatedFetchMock.mockResolvedValueOnce(
      new Response(
        JSON.stringify({
          object: "session.github.info",
          available: true,
          pr: {
            number: 42,
            title: "Add canvas",
            state: "OPEN",
            url: "https://github.com/acme/repo/pull/42",
            is_draft: false,
            author: "me",
            base_ref: "main",
            head_ref: "feat",
            checks: { passing: 0, failing: 0, pending: 0, total: 0, runs: [] },
          },
        }),
        { status: 200 },
      ),
    );
    const open = vi.spyOn(window, "open").mockReturnValue(null);
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });

    await expect(
      result.current.methods["navigation.openExternal"]?.(
        { url: "https://github.com/acme/repo/pull/42" },
        signal(),
      ),
    ).rejects.toMatchObject({ code: "PermissionDenied" });

    await expect(
      result.current.methods["sessions.pullRequest"]?.({ sessionId: "conv_1" }, signal()),
    ).resolves.toEqual({
      number: 42,
      title: "Add canvas",
      state: "OPEN",
      url: "https://github.com/acme/repo/pull/42",
    });
    expect(authenticatedFetchMock.mock.calls[0][0]).toBe("/v1/sessions/conv_1/resources/github");

    await result.current.methods["navigation.openExternal"]?.(
      { url: "https://github.com/acme/repo/pull/42" },
      signal(),
    );
    expect(open).toHaveBeenCalledWith(
      "https://github.com/acme/repo/pull/42",
      "_blank",
      "noopener,noreferrer",
    );
    open.mockRestore();
  });

  it("lists projects and creates one while refreshing the sidebar's project list", async () => {
    authenticatedFetchMock
      .mockResolvedValueOnce(
        new Response(
          JSON.stringify({
            object: "list",
            data: [{ id: "p1", name: "Alpha", config: { icon: "🅰️" } }],
          }),
          { status: 200 },
        ),
      )
      .mockResolvedValueOnce(
        new Response(JSON.stringify({ id: "p2", name: "Beta", config: {} }), { status: 200 }),
      );
    const invalidate = vi.spyOn(queryClient, "invalidateQueries").mockResolvedValue(undefined);
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });

    await expect(result.current.methods["projects.list"]?.({}, signal())).resolves.toEqual([
      { id: "p1", name: "Alpha", icon: "🅰️" },
    ]);
    await expect(
      result.current.methods["projects.create"]?.({ name: "  Beta " }, signal()),
    ).resolves.toEqual({ id: "p2", name: "Beta", icon: null });
    const [, init] = authenticatedFetchMock.mock.calls[1] as [string, RequestInit];
    expect(JSON.parse(String(init.body))).toEqual({ name: "Beta" });
    expect(invalidate).toHaveBeenCalledWith({ queryKey: ["projects"] });
    invalidate.mockRestore();
  });

  it("keeps sessions methods absent for existing extension permissions", () => {
    const existing = { ...extension, permissions: ["navigation", "storage.user"] };
    const { result } = renderHook(() => useExtensionHostServices(existing), { wrapper });

    expect(result.current.methods["sessions.listPage"]).toBeUndefined();
    expect(result.current.methods["projects.list"]).toBeUndefined();
    expect(result.current.methods["projects.create"]).toBeUndefined();
    expect(result.current.methods["navigation.openSession"]).toBeDefined();
    expect(result.current.methods["storage.user.get"]).toBeDefined();
  });

  it("omits methods whose permissions were not granted", () => {
    const denied = { ...extension, permissions: [] };
    const { result } = renderHook(() => useExtensionHostServices(denied), { wrapper });

    expect(Object.keys(result.current.methods).sort()).toEqual([
      "theme.getCurrent",
      "theme.subscribe",
    ]);
  });

  it("refuses storage until both user and server identities resolve", async () => {
    identityRef.current = null;
    serverRef.current = null;
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });

    await expect(
      result.current.methods["storage.user.get"]?.({ key: "layout" }, signal()),
    ).rejects.toMatchObject({ code: "Unavailable" });
  });

  it("persists extension-scoped values through the storage service", async () => {
    const { result } = renderHook(() => useExtensionHostServices(extension), { wrapper });
    await act(() =>
      result.current.methods["storage.user.set"]?.({ key: "layout", value: { x: 1 } }, signal()),
    );

    await expect(
      result.current.methods["storage.user.get"]?.({ key: "layout" }, signal()),
    ).resolves.toEqual({ x: 1 });
  });
});
