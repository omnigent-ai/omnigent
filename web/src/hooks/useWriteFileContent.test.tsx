import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, cleanup, renderHook, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { ReactNode } from "react";
import { useWriteFileContent } from "./useWriteFileContent";

const fetchMock = vi.fn();

function okResponse(): Response {
  return { ok: true, status: 200, statusText: "OK" } as unknown as Response;
}

function errorResponse(status: number): Response {
  return { ok: false, status, statusText: "Error" } as unknown as Response;
}

function wrapper(qc: QueryClient) {
  return function Wrap({ children }: { children: ReactNode }) {
    return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
  };
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("useWriteFileContent", () => {
  it("PUTs to the correct filesystem URL with utf-8 body", async () => {
    fetchMock.mockResolvedValue(okResponse());
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderHook(() => useWriteFileContent("sess_1"), { wrapper: wrapper(qc) });

    await act(async () => {
      await result.current.mutateAsync({ path: "src/notes.md", content: "# Hello" });
    });

    expect(fetchMock).toHaveBeenCalledOnce();
    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe("/v1/sessions/sess_1/resources/environments/default/filesystem/src/notes.md");
    expect(init.method).toBe("PUT");
    expect(JSON.parse(init.body as string)).toEqual({ content: "# Hello", encoding: "utf-8" });
  });

  it("encodes special characters in path segments", async () => {
    fetchMock.mockResolvedValue(okResponse());
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderHook(() => useWriteFileContent("sess_1"), { wrapper: wrapper(qc) });

    await act(async () => {
      await result.current.mutateAsync({ path: "my dir/file name.md", content: "" });
    });

    const [url] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(
      "/v1/sessions/sess_1/resources/environments/default/filesystem/my%20dir/file%20name.md",
    );
  });

  it("invalidates file-content and workspace-changed-files on success", async () => {
    fetchMock.mockResolvedValue(okResponse());
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useWriteFileContent("sess_2"), { wrapper: wrapper(qc) });

    await act(async () => {
      await result.current.mutateAsync({ path: "README.md", content: "hi" });
    });

    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["file-content", "sess_2", "README.md"],
    });
    expect(invalidateSpy).toHaveBeenCalledWith({
      queryKey: ["workspace-changed-files", "sess_2"],
    });
  });

  it("updates file-content cache immediately on success when prior data exists", async () => {
    fetchMock.mockResolvedValue(okResponse());
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    // Seed the cache with the pre-save content.
    qc.setQueryData(["file-content", "sess_2", "README.md"], {
      object: "session.environment.filesystem.file_content",
      path: "README.md",
      content_type: "text/markdown",
      encoding: "utf-8",
      content: "old content",
      bytes: 11,
    });
    const { result } = renderHook(() => useWriteFileContent("sess_2"), { wrapper: wrapper(qc) });

    await act(async () => {
      await result.current.mutateAsync({ path: "README.md", content: "new content" });
    });

    const cached = qc.getQueryData(["file-content", "sess_2", "README.md"]) as { content: string };
    expect(cached.content).toBe("new content");
  });

  it("does not invalidate queries when the PUT fails", async () => {
    fetchMock.mockResolvedValue(errorResponse(500));
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const invalidateSpy = vi.spyOn(qc, "invalidateQueries");
    const { result } = renderHook(() => useWriteFileContent("sess_3"), { wrapper: wrapper(qc) });

    await act(async () => {
      await result.current.mutateAsync({ path: "a.md", content: "x" }).catch(() => {});
    });

    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(invalidateSpy).not.toHaveBeenCalled();
  });

  it("throws on non-ok response", async () => {
    fetchMock.mockResolvedValue(errorResponse(403));
    const qc = new QueryClient({ defaultOptions: { mutations: { retry: false } } });
    const { result } = renderHook(() => useWriteFileContent("sess_4"), { wrapper: wrapper(qc) });

    let thrown: Error | null = null;
    await act(async () => {
      try {
        await result.current.mutateAsync({ path: "a.md", content: "x" });
      } catch (e) {
        thrown = e as Error;
      }
    });

    expect(thrown).not.toBeNull();
    expect(thrown!.message).toMatch("403");
  });
});
