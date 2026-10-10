// Tests for downloadWorkspaceFile's two transports under a configured base path.
//
//   - Standalone browser: a direct anchor click, so the URL must carry the
//     subpath prefix (withBasePath) or a stripping proxy misses the app.
//   - Managed / mobile: authenticatedFetch owns the transport and prefixes
//     internally, so it must receive the RAW url; prefixing here would double it.
//
// The seams (`@/lib/host`, `@/lib/identity`, `@/lib/nativeBridge`, the workspace
// path helpers, the chat store) are mocked; `withBasePath` is the real thing,
// reading `window.__OMNIGENT_BASE_PATH__`.

import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { renderHook, cleanup, act } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";

const isDatabricksWorkspace = vi.fn();
const authenticatedFetch = vi.fn();
const isIOSShell = vi.fn(() => false);
const isAndroidShell = vi.fn(() => false);

vi.mock("@/lib/host", () => ({ isDatabricksWorkspace: () => isDatabricksWorkspace() }));
vi.mock("@/lib/identity", () => ({
  authenticatedFetch: (...args: unknown[]) => authenticatedFetch(...args),
}));
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => isIOSShell(),
  isAndroidShell: () => isAndroidShell(),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  browseLocationBase: (path: string) => (path.startsWith("/") ? "host" : ""),
  browseLocationSegment: (path: string) =>
    path.replace(/^\//, "").split("/").map(encodeURIComponent).join("/"),
  useWorkspaceServeable: () => ({}),
}));
vi.mock("@/store/chatStore", () => ({ useChatStore: () => undefined }));

import {
  downloadWorkspaceFile,
  workspaceFileDownloadUrl,
  usesDirectFileDownload,
  fetchWorkspaceFileBlob,
  useFileContent,
} from "./useFileContent";

const RAW_URL =
  "/v1/sessions/sess_abc/resources/environments/default/filesystem/src/main.py?download=true";

beforeEach(() => {
  isDatabricksWorkspace.mockReset();
  authenticatedFetch.mockReset();
  isIOSShell.mockReturnValue(false);
  isAndroidShell.mockReturnValue(false);
  // Stub the click so jsdom doesn't attempt a navigation for the download anchor.
  vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => {});
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  vi.unstubAllGlobals();
  delete window.__OMNIGENT_BASE_PATH__;
});

describe("downloadWorkspaceFile base-path handling", () => {
  it("prefixes the standalone download anchor with the configured base path", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    isDatabricksWorkspace.mockReturnValue(false);
    const appendSpy = vi.spyOn(document.body, "append").mockImplementation(() => {});

    await downloadWorkspaceFile("sess_abc", "src/main.py");

    expect(appendSpy).toHaveBeenCalledTimes(1);
    const anchor = appendSpy.mock.calls[0]?.[0] as HTMLAnchorElement;
    expect(anchor.getAttribute("href")).toBe(`/proxy/6767${RAW_URL}`);
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("passes the raw url to authenticatedFetch on the managed branch (no double prefix)", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    isDatabricksWorkspace.mockReturnValue(true);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: async () => new Blob(["x"]) });
    vi.stubGlobal("URL", { createObjectURL: () => "blob:x", revokeObjectURL: () => {} });

    await downloadWorkspaceFile("sess_abc", "src/main.py");

    expect(authenticatedFetch).toHaveBeenCalledWith(RAW_URL, { signal: undefined });
  });
});

describe("raw file helpers", () => {
  it("does not fetch JSON when explicitly disabled", async () => {
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    const { result } = renderHook(() => useFileContent("sess_abc", "clip.mp4", false), {
      wrapper: ({ children }: { children: ReactNode }) =>
        createElement(QueryClientProvider, { client }, children),
    });
    await act(async () => {});
    expect(result.current.fetchStatus).toBe("idle");
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });
  it("encodes the session and absolute path with base=host", () => {
    expect(workspaceFileDownloadUrl("session space", "/tmp/my clip.mp4")).toBe(
      "/v1/sessions/session%20space/resources/environments/default/filesystem/tmp/my%20clip.mp4?download=true&base=host",
    );
  });
  it.each(["browser", "managed", "ios", "android"])("selects transport for %s", (surface) => {
    isDatabricksWorkspace.mockReturnValue(surface === "managed");
    isIOSShell.mockReturnValue(surface === "ios");
    isAndroidShell.mockReturnValue(surface === "android");
    expect(usesDirectFileDownload()).toBe(surface === "browser");
  });
  it("fetches a complete blob with the raw URL", async () => {
    window.__OMNIGENT_BASE_PATH__ = "/proxy/6767";
    const blob = new Blob(["complete"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: async () => blob });
    expect(await fetchWorkspaceFileBlob("sess_abc", "src/main.py")).toBe(blob);
    expect(authenticatedFetch).toHaveBeenCalledWith(RAW_URL, { signal: undefined });
  });
  it("forwards the optional abort signal to authenticatedFetch", async () => {
    const controller = new AbortController();
    const blob = new Blob(["video"]);
    authenticatedFetch.mockResolvedValue({ ok: true, blob: async () => blob });

    await expect(
      fetchWorkspaceFileBlob("sess_abc", "src/main.py", controller.signal),
    ).resolves.toBe(blob);
    expect(authenticatedFetch).toHaveBeenCalledWith(RAW_URL, { signal: controller.signal });
  });
  it("rejects a failed raw download", async () => {
    authenticatedFetch.mockResolvedValue({ ok: false, status: 404, statusText: "Not Found" });
    await expect(fetchWorkspaceFileBlob("sess_abc", "src/main.py")).rejects.toThrow(
      "404 Not Found",
    );
  });
});
