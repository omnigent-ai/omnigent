// Tests for downloadWorkspaceFile's two transports under a configured base path,
// and for workspaceFileMediaUrl's host-resolved direct media URL.
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

const isDatabricksWorkspace = vi.fn();
const resolveMediaUrl = vi.fn();
const authenticatedFetch = vi.fn();
const isIOSShell = vi.fn(() => false);
const isAndroidShell = vi.fn(() => false);

vi.mock("@/lib/host", () => ({
  isDatabricksWorkspace: () => isDatabricksWorkspace(),
  resolveMediaUrl: (p: string) => resolveMediaUrl(p),
  isHostRoutingEnabled: () => true,
}));
vi.mock("@/lib/identity", () => ({ authenticatedFetch: (u: string) => authenticatedFetch(u) }));
vi.mock("@/lib/nativeBridge", () => ({
  isIOSShell: () => isIOSShell(),
  isAndroidShell: () => isAndroidShell(),
}));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  browseLocationBase: () => "",
  browseLocationSegment: (p: string) => p,
  useWorkspaceServeable: () => ({}),
}));
vi.mock("@/store/chatStore", () => ({ useChatStore: () => undefined }));

import { clearHostKeyless, markHostKeyless, setSessionHost } from "@/lib/sessionHost";
import { downloadWorkspaceFile, workspaceFileMediaUrl } from "./useFileContent";

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

    expect(authenticatedFetch).toHaveBeenCalledWith(RAW_URL);
  });
});

describe("workspaceFileMediaUrl", () => {
  const MEDIA_URL =
    "/v1/sessions/sess_abc/resources/environments/default/filesystem/demo.mp4?download=true";

  beforeEach(() => {
    resolveMediaUrl.mockReset();
    resolveMediaUrl.mockImplementation((p: string) => `https://ws.example${p}`);
  });

  afterEach(() => {
    setSessionHost("sess_abc", null);
    clearHostKeyless("host_1");
  });

  it("is null when the host has no media resolver, so callers fall back to fetching", () => {
    setSessionHost("sess_abc", "host_1");
    resolveMediaUrl.mockReturnValue(null);

    expect(workspaceFileMediaUrl("sess_abc", "demo.mp4")).toBeNull();
    expect(resolveMediaUrl).toHaveBeenCalled();
  });

  it("routes the headerless media load to the session host's replica", () => {
    setSessionHost("sess_abc", "host_1");

    expect(workspaceFileMediaUrl("sess_abc", "demo.mp4")).toBe(
      `https://ws.example${MEDIA_URL}&omnigent_slice_key=host_1`,
    );
  });

  it("fetches an unknown host's load and leaves a keyless host's unkeyed", () => {
    // The authenticated fetch recovers from a wrong-replica miss; a bare <video src> can't.
    expect(workspaceFileMediaUrl("sess_abc", "demo.mp4")).toBeNull();

    setSessionHost("sess_abc", "host_1");
    markHostKeyless("host_1");
    expect(workspaceFileMediaUrl("sess_abc", "demo.mp4")).toBe(`https://ws.example${MEDIA_URL}`);
  });
});
