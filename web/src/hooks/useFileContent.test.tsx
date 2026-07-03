import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";

vi.mock("@/hooks/RunnerHealthProvider", () => ({
  useSessionRunnerOnline: vi.fn(),
}));
vi.mock("@/store/chatStore", () => ({
  useChatStore: vi.fn(),
}));

import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useChatStore } from "@/store/chatStore";
import {
  downloadWorkspaceFile,
  fileContentToBlob,
  triggerBrowserDownload,
  useFileContent,
} from "./useFileContent";
import type { FileContentResponse } from "./useFileContent";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

function jsonResponse(body: unknown): Response {
  return {
    ok: true,
    status: 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

function Wrap({ children }: { children: ReactNode }) {
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function Probe({ id, path }: { id: string | undefined; path: string | null }) {
  useFileContent(id, path);
  return null;
}

async function flushMicrotasks() {
  await new Promise((r) => setTimeout(r, 10));
}

type ChatStoreState = {
  conversationId: string | null;
  sessionStatus: "idle" | "running" | "waiting" | "failed";
};

// `useChatStore` is a selector hook: state in, derived value out.
// Our consumer reads `s.conversationId` and `s.sessionStatus`.
function stubChatStore(state: Partial<ChatStoreState> = {}) {
  const full: ChatStoreState = {
    conversationId: null,
    sessionStatus: "idle",
    ...state,
  };
  chatStoreMock.mockImplementation((selector: unknown) => {
    if (typeof selector === "function") {
      return (selector as (s: ChatStoreState) => unknown)(full);
    }
    return undefined;
  });
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  fetchMock.mockResolvedValue(
    jsonResponse({
      object: "session.environment.filesystem.file_content",
      path: "a.txt",
      content_type: "text/plain",
      encoding: "utf-8",
      content: "hi",
      bytes: 2,
    }),
  );
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  // restore (not just reset) so any `vi.spyOn` a test installs is removed even
  // if that test threw before its own cleanup — otherwise a leaked
  // `document.createElement` spy is recaptured as the "original" by the next
  // test and recurses infinitely.
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// fileContentToBlob
// ---------------------------------------------------------------------------

describe("fileContentToBlob", () => {
  it("produces a utf-8 blob with the server content_type", () => {
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "hello.txt",
      content_type: "text/plain",
      encoding: "utf-8",
      content: "hello world",
      bytes: 11,
    };
    const blob = fileContentToBlob(data);
    expect(blob.type).toBe("text/plain");
    expect(blob.size).toBe(11);
  });

  it("falls back to text/plain when content_type is null for utf-8 files", () => {
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "no-mime.txt",
      content_type: null,
      encoding: "utf-8",
      content: "x",
      bytes: 1,
    };
    expect(fileContentToBlob(data).type).toBe("text/plain");
  });

  it("decodes a base64 payload and uses the server content_type", () => {
    // "abc" in base64 is "YWJj"
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "img.png",
      content_type: "image/png",
      encoding: "base64",
      content: "YWJj",
      bytes: 3,
    };
    const blob = fileContentToBlob(data);
    expect(blob.type).toBe("image/png");
    expect(blob.size).toBe(3);
  });

  it("falls back to application/octet-stream for base64 files without content_type", () => {
    const data: FileContentResponse = {
      object: "session.environment.filesystem.file_content",
      path: "bin.dat",
      content_type: null,
      encoding: "base64",
      content: "YWJj",
      bytes: 3,
    };
    expect(fileContentToBlob(data).type).toBe("application/octet-stream");
  });
});

// ---------------------------------------------------------------------------
// triggerBrowserDownload
// ---------------------------------------------------------------------------

describe("triggerBrowserDownload", () => {
  it("creates and clicks a synthetic link then removes it", () => {
    const blob = new Blob(["data"], { type: "text/plain" });
    const createdLinks: HTMLAnchorElement[] = [];
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = origCreateElement(tag);
      if (tag === "a") {
        vi.spyOn(el as HTMLAnchorElement, "click");
        createdLinks.push(el as HTMLAnchorElement);
      }
      return el;
    });
    vi.stubGlobal("URL", {
      createObjectURL: vi.fn(() => "blob:fake"),
      revokeObjectURL: vi.fn(),
    });

    triggerBrowserDownload(blob, "output.txt");

    expect(createdLinks).toHaveLength(1);
    const link = createdLinks[0];
    expect(link.download).toBe("output.txt");
    expect(link.href).toContain("blob:fake");
    expect(link.click).toHaveBeenCalledOnce();
    // Element must have been removed from the DOM.
    expect(document.body.contains(link)).toBe(false);
    expect(URL.revokeObjectURL).toHaveBeenCalledWith("blob:fake");

    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });
});

// ---------------------------------------------------------------------------
// downloadWorkspaceFile
// ---------------------------------------------------------------------------

describe("downloadWorkspaceFile", () => {
  it("fetches the correct URL and triggers a download with the filename from the path", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        object: "session.environment.filesystem.file_content",
        path: "src/main.py",
        content_type: "text/x-python",
        encoding: "utf-8",
        content: "print('hi')",
        bytes: 11,
      }),
    );
    const clickedLinks: string[] = [];
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = origCreateElement(tag);
      if (tag === "a")
        vi.spyOn(el as HTMLAnchorElement, "click").mockImplementation(() => {
          clickedLinks.push((el as HTMLAnchorElement).download);
        });
      return el;
    });
    vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() });

    await downloadWorkspaceFile("sess_123", "src/main.py");

    // fetchFileContent goes through authenticatedFetch, which adds auth headers
    // and `cache: "no-store"` (see lib/identity.ts) — assert the URL plus that
    // cache-bypass init rather than a bare single-arg call.
    expect(fetchMock).toHaveBeenCalledWith(
      "/v1/sessions/sess_123/resources/environments/default/filesystem/src/main.py",
      expect.objectContaining({ cache: "no-store" }),
    );
    // The download filename is derived from the last path segment.
    expect(clickedLinks).toEqual(["main.py"]);

    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("logs a console warning when the server returns truncated: true", async () => {
    fetchMock.mockResolvedValueOnce(
      jsonResponse({
        object: "session.environment.filesystem.file_content",
        path: "big.txt",
        content_type: "text/plain",
        encoding: "utf-8",
        content: "partial content",
        bytes: 15,
        truncated: true,
      }),
    );
    const warnSpy = vi.spyOn(console, "warn").mockImplementation(() => {});
    const origCreateElement = document.createElement.bind(document);
    vi.spyOn(document, "createElement").mockImplementation((tag: string) => {
      const el = origCreateElement(tag);
      if (tag === "a") vi.spyOn(el as HTMLAnchorElement, "click").mockImplementation(() => {});
      return el;
    });
    vi.stubGlobal("URL", { createObjectURL: vi.fn(() => "blob:x"), revokeObjectURL: vi.fn() });

    await downloadWorkspaceFile("sess_abc", "big.txt");

    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("big.txt"));
    expect(warnSpy).toHaveBeenCalledWith(expect.stringContaining("truncated"));

    vi.restoreAllMocks();
    vi.unstubAllGlobals();
  });

  it("propagates fetch errors to the caller", async () => {
    fetchMock.mockResolvedValueOnce({
      ok: false,
      status: 404,
      statusText: "Not Found",
    } as Response);

    await expect(downloadWorkspaceFile("sess_x", "missing.txt")).rejects.toThrow("404");
  });
});

// ---------------------------------------------------------------------------
// useFileContent gating
// ---------------------------------------------------------------------------

describe("useFileContent gating", () => {
  it("does not fetch when the runner is offline", async () => {
    onlineMock.mockReturnValue(false);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_dead" path="src/a.txt" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not fetch when path is null", async () => {
    onlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_live" path={null} />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches when online and a path is given", async () => {
    onlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_live" path="src/a.txt" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default/filesystem/src/a.txt",
    );
  });

  it("fetches when status is unknown (undefined)", async () => {
    onlineMock.mockReturnValue(undefined);
    stubChatStore();

    render(
      <Wrap>
        <Probe id="conv_unknown" path="src/a.txt" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
  });

  it("does not fetch when conversationId is undefined", async () => {
    onlineMock.mockReturnValue(true);
    stubChatStore();

    render(
      <Wrap>
        <Probe id={undefined} path="src/a.txt" />
      </Wrap>,
    );
    await flushMicrotasks();

    // Undefined conversationId gates the enabled flag — no session to fetch from.
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

// ---------------------------------------------------------------------------
// useFileContent trailing invalidation
// ---------------------------------------------------------------------------
//
// Each test creates one QueryClient and one StableWrap component that closes
// over it.  Both render() and rerender() receive the *same* StableWrap
// reference, so React reconciles the provider in place (no remount, same
// client).  Using the Wrap helper instead would create a new QueryClient on
// every rerender call, racing with the invalidation under test.

describe("useFileContent trailing invalidation", () => {
  // Returns a wrapper component that always uses the given QueryClient.
  // Call once per test and reuse the returned reference for both render()
  // and rerender() so the QueryClient is stable across all renders.
  function stableWrap(qc: QueryClient) {
    return function StableWrap({ children }: { children: ReactNode }) {
      return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
    };
  }

  it("refetches once when session transitions from running to idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_a", sessionStatus: "running" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_a" path="src/a.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Transition: running → idle — trailing invalidation must fire one refetch.
    stubChatStore({ conversationId: "conv_a", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_a" path="src/a.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("refetches once when session transitions from waiting to idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_b", sessionStatus: "waiting" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_b" path="src/b.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    stubChatStore({ conversationId: "conv_b", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_b" path="src/b.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  });

  it("does not refetch when session stays idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_c", sessionStatus: "idle" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_c" path="src/c.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Still idle — no extra invalidation.
    rerender(
      <StableWrap>
        <Probe id="conv_c" path="src/c.txt" />
      </StableWrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("invalidates the current path when path changes mid-session and session goes idle", async () => {
    // If the user navigates to a different file while the agent is running,
    // the trailing invalidation must fire for the path that is open when
    // the session finishes — not for the path that was open when the session
    // started.
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_nav", sessionStatus: "running" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_nav" path="src/old.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // Navigate to a different file while the session is still running.
    rerender(
      <StableWrap>
        <Probe id="conv_nav" path="src/new.txt" />
      </StableWrap>,
    );
    // src/new.txt now fetches for the first time (fetch #2).
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Session goes idle — trailing invalidation fires for the current path (src/new.txt).
    stubChatStore({ conversationId: "conv_nav", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_nav" path="src/new.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    // Verify the last refetch targeted the current path, not the old one.
    expect(fetchMock.mock.calls[2][0]).toContain("src/new.txt");
  });

  it("does not refetch a second time when re-rendering while still idle", async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false, staleTime: 0 } } });
    const StableWrap = stableWrap(qc);
    onlineMock.mockReturnValue(true);
    stubChatStore({ conversationId: "conv_d", sessionStatus: "running" });

    const { rerender } = render(
      <StableWrap>
        <Probe id="conv_d" path="src/d.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));

    // First transition: running → idle — trailing invalidation fires (fetch #2).
    stubChatStore({ conversationId: "conv_d", sessionStatus: "idle" });
    rerender(
      <StableWrap>
        <Probe id="conv_d" path="src/d.txt" />
      </StableWrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Second re-render while still idle — must NOT fire a third fetch.
    rerender(
      <StableWrap>
        <Probe id="conv_d" path="src/d.txt" />
      </StableWrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});
