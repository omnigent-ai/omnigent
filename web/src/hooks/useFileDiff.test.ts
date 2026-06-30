// Unit tests for useFileDiff: the enable gate (only fires when a session,
// a path, an online runner, and a changed-files match all line up), the
// per-segment path encoding of the diff URL, and the throw-on-non-2xx guard.
//
// The two source hooks are mocked so we exercise the gate and URL builder
// directly without standing up the RunnerHealthProvider or the changed-files
// query.

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderHook, waitFor } from "@testing-library/react";
import { createElement, type ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("@/hooks/RunnerHealthProvider", () => ({ useSessionRunnerOnline: vi.fn() }));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({ useWorkspaceChangedFiles: vi.fn() }));

import { useSessionRunnerOnline } from "@/hooks/RunnerHealthProvider";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { useFileDiff } from "./useFileDiff";

const runnerOnlineMock = vi.mocked(useSessionRunnerOnline);
const changedFilesMock = vi.mocked(useWorkspaceChangedFiles);

function mockResponse(body: unknown, init?: { ok?: boolean; status?: number }): Response {
  return {
    ok: init?.ok ?? true,
    status: init?.status ?? 200,
    statusText: "OK",
    json: async () => body,
  } as unknown as Response;
}

const fetchMock = vi.fn();

/** Wire the two source hooks: runner online state + changed-files paths. */
function setHooks(opts: {
  runnerOnline?: boolean | undefined;
  changedPaths?: string[] | undefined;
}) {
  runnerOnlineMock.mockReturnValue(opts.runnerOnline);
  changedFilesMock.mockReturnValue({
    data:
      opts.changedPaths === undefined
        ? undefined
        : { available: true, data: opts.changedPaths.map((path) => ({ path })) },
  } as unknown as ReturnType<typeof useWorkspaceChangedFiles>);
}

beforeEach(() => {
  fetchMock.mockReset();
  runnerOnlineMock.mockReset();
  changedFilesMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

function renderDiff(conversationId: string | undefined, path: string | null) {
  const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) =>
    createElement(QueryClientProvider, { client: queryClient }, children);
  return renderHook(() => useFileDiff(conversationId, path), { wrapper });
}

describe("useFileDiff — enable gate", () => {
  it("does not fetch when conversationId is missing", () => {
    // No session means there is no diff endpoint to call.
    setHooks({ runnerOnline: true, changedPaths: ["a.ts"] });
    renderDiff(undefined, "a.ts");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not fetch when path is null", () => {
    // Nothing is selected; firing a request would build a malformed URL.
    setHooks({ runnerOnline: true, changedPaths: ["a.ts"] });
    renderDiff("conv_1", null);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not fetch when the runner is offline", () => {
    // An offline runner has no live filesystem to diff against; the query
    // must stay disabled rather than hammer a dead endpoint.
    setHooks({ runnerOnline: false, changedPaths: ["a.ts"] });
    renderDiff("conv_1", "a.ts");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not fetch when the file is not in the changed-files list", () => {
    // Only created/modified/deleted files have diff data; an unchanged file
    // would 404, so the gate keeps it disabled.
    setHooks({ runnerOnline: true, changedPaths: ["other.ts"] });
    renderDiff("conv_1", "a.ts");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("treats an unresolved changed-files list as not-yet-matched (no fetch)", () => {
    // Before the changed-files query resolves, data is undefined; the gate
    // must wait rather than fetch a diff for a file it can't confirm changed.
    setHooks({ runnerOnline: true, changedPaths: undefined });
    renderDiff("conv_1", "a.ts");
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches when session + path + online runner + changed-file match all hold", async () => {
    setHooks({ runnerOnline: true, changedPaths: ["a.ts"] });
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        object: "session.environment.filesystem.file_diff",
        path: "a.ts",
        before: "old",
        after: "new",
      }),
    );
    const { result } = renderDiff("conv_1", "a.ts");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(result.current.data).toMatchObject({ before: "old", after: "new" });
  });

  it("still fetches when runnerOnline is undefined (unknown, not offline)", async () => {
    // The gate only blocks on an explicit `false`; an unknown (undefined)
    // runner state must not stop the diff, or the panel would stay blank
    // during the brief window before health resolves.
    setHooks({ runnerOnline: undefined, changedPaths: ["a.ts"] });
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        object: "session.environment.filesystem.file_diff",
        path: "a.ts",
        before: null,
        after: "new",
      }),
    );
    const { result } = renderDiff("conv_1", "a.ts");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe("useFileDiff — request URL", () => {
  it("encodes each path segment individually, preserving structural slashes", async () => {
    // Per-segment encoding keeps "/" as the directory separator while
    // escaping spaces/specials; whole-string encoding would turn slashes
    // into %2F and break the FastAPI {path:path} route.
    setHooks({ runnerOnline: true, changedPaths: ["src/a b.ts"] });
    fetchMock.mockResolvedValueOnce(
      mockResponse({
        object: "session.environment.filesystem.file_diff",
        path: "src/a b.ts",
        before: null,
        after: null,
      }),
    );
    const { result } = renderDiff("conv with space", "src/a b.ts");
    await waitFor(() => expect(result.current.isSuccess).toBe(true));
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv%20with%20space/resources/environments/default/diff/src/a%20b.ts",
    );
  });
});

describe("useFileDiff — error handling", () => {
  it("surfaces an error on non-2xx", async () => {
    setHooks({ runnerOnline: true, changedPaths: ["a.ts"] });
    fetchMock.mockResolvedValueOnce(mockResponse({}, { ok: false, status: 500 }));
    const { result } = renderDiff("conv_1", "a.ts");
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({ message: expect.stringContaining("500") });
  });

  it("surfaces the server's reason from the error body when present", async () => {
    // A git_status_failed 500 carries the real cause in error.message; the
    // hook must propagate it so the diff view shows what went wrong rather
    // than a bare status code.
    setHooks({ runnerOnline: true, changedPaths: ["a.ts"] });
    fetchMock.mockResolvedValueOnce(
      mockResponse(
        { error: { code: "git_status_failed", message: "git status timed out after 5.0s" } },
        { ok: false, status: 500 },
      ),
    );
    const { result } = renderDiff("conv_1", "a.ts");
    await waitFor(() => expect(result.current.isError).toBe(true));
    expect(result.current.error).toMatchObject({
      message: "git status timed out after 5.0s",
    });
  });
});
