import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act, renderHook } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const authenticatedFetch = vi.hoisted(() => vi.fn());
const serveable = vi.hoisted(() => ({ current: true as boolean | undefined }));
vi.mock("@/lib/identity", () => ({ authenticatedFetch }));
// No host serves the workspace API here, so every load goes to REST.
vi.mock("@/lib/workspaceApi", () => ({
  listDirectoryViaWorkspaceApi: async () => null,
  readFileViaWorkspaceApi: async () => null,
}));
vi.mock("./useWorkspaceChangedFiles", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkspaceChangedFilesModule>()),
  useWorkspaceServeable: () => serveable.current,
}));

import type * as WorkspaceChangedFilesModule from "./useWorkspaceChangedFiles";
import {
  FILE_PREFETCH_MAX_BYTES,
  PREFETCH_DWELL_MS,
  prefetchableFile,
  useWorkspacePrefetch,
} from "./useWorkspacePrefetch";

const SESSION = "conv_prefetch";
const EMPTY_LISTING = { object: "list", data: [], has_more: false };

const listing = () => new Response(JSON.stringify(EMPTY_LISTING), { status: 200 });

/** The workspace paths of the REST requests made, from their URLs. */
function httpsPaths(): string[] {
  return authenticatedFetch.mock.calls.map(([url]) => {
    const { pathname } = new URL(String(url), "http://test");
    const [, rest = ""] = pathname.split(/\/(?:filesystem|files)/);
    return decodeURIComponent(rest);
  });
}

function renderPrefetch() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client }, children);
  const { result, rerender, unmount } = renderHook(
    () => useWorkspacePrefetch({ conversationId: SESSION, location: "", refreshToken: 0 }),
    { wrapper },
  );
  return { client, rerender, unmount, prefetch: () => result.current };
}

beforeEach(() => {
  vi.useFakeTimers();
  authenticatedFetch.mockImplementation(async () => listing());
});

afterEach(() => {
  vi.useRealTimers();
  authenticatedFetch.mockReset();
  serveable.current = true;
});

describe("useWorkspacePrefetch on pause", () => {
  it("loads a folder only once the pointer or focus rests on it", async () => {
    const { prefetch } = renderPrefetch();
    act(() => prefetch().intend({ kind: "directory", path: "src" }));
    await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS - 1));
    expect(authenticatedFetch).not.toHaveBeenCalled();
    await act(() => vi.advanceTimersByTimeAsync(1));
    expect(httpsPaths()).toEqual(["/src"]);
  });

  it("loads nothing when the pointer moves on before the pause ends", async () => {
    const { prefetch } = renderPrefetch();
    act(() => prefetch().intend({ kind: "directory", path: "src" }));
    await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS / 2));
    act(() => prefetch().cancel());
    // Passing over a second row restarts the pause for that row alone.
    act(() => prefetch().intend({ kind: "directory", path: "docs" }));
    await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS));
    expect(httpsPaths()).toEqual(["/docs"]);
  });

  it("doesn't ask again for a folder whose listing is still fresh", async () => {
    const { prefetch } = renderPrefetch();
    for (let visit = 0; visit < 2; visit += 1) {
      act(() => prefetch().intend({ kind: "directory", path: "src" }));
      // oxlint-disable-next-line no-await-in-loop -- each visit waits out its pause.
      await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS));
    }
    expect(httpsPaths()).toEqual(["/src"]);
  });

  it("loads a small file's contents, but not a large, unknown-size, or video file", async () => {
    const { prefetch } = renderPrefetch();
    for (const target of [
      { path: "big.log", bytes: FILE_PREFETCH_MAX_BYTES + 1 },
      { path: "unknown.txt", bytes: null },
      { path: "clip.mp4", bytes: 10 },
      { path: "README.md", bytes: 10 },
    ]) {
      act(() => prefetch().intend({ kind: "file", ...target }));
      // oxlint-disable-next-line no-await-in-loop -- each row waits out its pause.
      await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS));
    }
    expect(httpsPaths()).toEqual(["/README.md"]);
  });

  it("loads nothing while neither the runner nor the host can read the workspace", async () => {
    serveable.current = false;
    const { prefetch } = renderPrefetch();
    act(() => prefetch().intend({ kind: "directory", path: "src" }));
    await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS));
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("drops a pause under way when the workspace becomes unreadable", async () => {
    const { prefetch, rerender } = renderPrefetch();
    act(() => prefetch().intend({ kind: "directory", path: "src" }));
    await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS / 2));
    serveable.current = false;
    rerender();
    await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS));
    expect(authenticatedFetch).not.toHaveBeenCalled();
  });

  it("still loads while it's unknown whether the workspace can be read", async () => {
    serveable.current = undefined;
    const { prefetch } = renderPrefetch();
    act(() => prefetch().intend({ kind: "directory", path: "src" }));
    await act(() => vi.advanceTimersByTimeAsync(PREFETCH_DWELL_MS));
    expect(httpsPaths()).toEqual(["/src"]);
  });
});

describe("prefetchableFile", () => {
  it.each([
    ["a small text file", "a.ts", 1, true],
    ["a file at the limit", "a.ts", FILE_PREFETCH_MAX_BYTES, true],
    ["a file over the limit", "a.ts", FILE_PREFETCH_MAX_BYTES + 1, false],
    ["a file of unknown size", "a.ts", null, false],
    ["a video", "a.mov", 1, false],
  ])("treats %s as %s", (_label, path, bytes, expected) => {
    expect(prefetchableFile(path, bytes)).toBe(expected);
  });
});
