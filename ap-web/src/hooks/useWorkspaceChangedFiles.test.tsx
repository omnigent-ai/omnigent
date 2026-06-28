import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { cleanup, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useEffect } from "react";
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
  MAX_RUNNER_OFFLINE_RETRIES,
  RunnerOfflineError,
  isRunnerUnavailable503,
  looksLikeWorkspaceFilePath,
  runnerOfflineRetryDelay,
  shouldRetryRunnerOffline,
  toWorkspaceRelativePath,
  useWorkspaceAllFiles,
  useWorkspaceChangedFiles,
  useWorkspaceDirectory,
  useWorkspaceEnvironment,
  useWorkspaceFileExists,
  useWorkspaceFileSearch,
  type WorkspaceChangedFilesResult,
} from "./useWorkspaceChangedFiles";

const onlineMock = vi.mocked(useSessionRunnerOnline);
const chatStoreMock = vi.mocked(useChatStore);
const fetchMock = vi.fn();

type StubStatus = "idle" | "running" | "waiting" | "failed";

function stubChatStore(conversationId: string | null = null, sessionStatus: StubStatus = "idle") {
  chatStoreMock.mockImplementation((selector: unknown) => {
    if (typeof selector === "function") {
      return (
        selector as (s: { conversationId: string | null; sessionStatus: StubStatus }) => unknown
      )({ conversationId, sessionStatus });
    }
    return undefined;
  });
}

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: status === 200 ? "OK" : "Error",
    json: async () => body,
  } as unknown as Response;
}

function environmentResponse(root = "/workspace"): Response {
  return jsonResponse({ metadata: { root } });
}

function changedFilesResponse(): Response {
  return jsonResponse({ object: "list", data: [], has_more: false });
}

function filesystemListResponse(): Response {
  return jsonResponse({ object: "list", data: [], has_more: false });
}

function Wrap({ children }: { children: ReactNode }) {
  // retry: false — otherwise mocked 503s spin past the assertion
  // window. staleTime: 0 — force re-eval of `enabled` per render.
  const qc = new QueryClient({
    defaultOptions: { queries: { retry: false, staleTime: 0 } },
  });
  return <QueryClientProvider client={qc}>{children}</QueryClientProvider>;
}

function ChangedFilesProbe({ id }: { id: string | undefined }) {
  useWorkspaceChangedFiles(id);
  return null;
}

function AllFilesProbe({ id }: { id: string | undefined }) {
  useWorkspaceAllFiles(id);
  return null;
}

function DisabledAllFilesProbe({ id }: { id: string | undefined }) {
  useWorkspaceAllFiles(id, { enabled: false });
  return null;
}

function DisabledChangedFilesProbe({ id }: { id: string | undefined }) {
  useWorkspaceChangedFiles(id, { enabled: false });
  return null;
}

function EnvironmentProbe({ id }: { id: string | undefined }) {
  useWorkspaceEnvironment(id);
  return null;
}

function EnvironmentDataProbe({
  id,
  onData,
}: {
  id: string | undefined;
  onData: (data: { available: boolean; root: string | null; home: string | null }) => void;
}) {
  const query = useWorkspaceEnvironment(id);
  useEffect(() => {
    if (query.isSuccess) onData(query.data);
  }, [query.isSuccess, query.data, onData]);
  return null;
}

function DisabledEnvironmentProbe({ id }: { id: string | undefined }) {
  useWorkspaceEnvironment(id, { enabled: false });
  return null;
}

function DirectoryProbe({ id, path }: { id: string | undefined; path: string | null }) {
  useWorkspaceDirectory(id, path);
  return null;
}

function FileSearchProbe({
  id,
  query,
  include,
  exclude,
  enabled = true,
}: {
  id: string | undefined;
  query: string;
  include?: string;
  exclude?: string;
  enabled?: boolean;
}) {
  useWorkspaceFileSearch(id, query, include, exclude, { enabled });
  return null;
}

async function flushMicrotasks() {
  await Promise.resolve();
  await Promise.resolve();
}

beforeEach(() => {
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  // Default: no focused conversation, idle session. Existing tests
  // assume sessionActive is false (initial fetch from `enabled`, no
  // polling). The trailing-invalidate test overrides per-call.
  stubChatStore();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetAllMocks();
});

describe("useWorkspaceChangedFiles gating", () => {
  it("does not fetch when the runner is offline", async () => {
    onlineMock.mockReturnValue(false);
    fetchMock.mockResolvedValue(jsonResponse({ object: "list", data: [] }));

    render(
      <Wrap>
        <ChangedFilesProbe id="conv_dead" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches when the runner is online", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock
      .mockResolvedValueOnce(environmentResponse())
      .mockResolvedValueOnce(changedFilesResponse());

    render(
      <Wrap>
        <ChangedFilesProbe id="conv_live" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default/changes",
    );
  });

  it("fetches when status is unknown (undefined)", async () => {
    // Don't block first render of healthy sessions before the
    // sidebar's /health batch has reported.
    onlineMock.mockReturnValue(undefined);
    fetchMock
      .mockResolvedValueOnce(environmentResponse())
      .mockResolvedValueOnce(changedFilesResponse());

    render(
      <Wrap>
        <ChangedFilesProbe id="conv_unknown" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_unknown/resources/environments/default",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/v1/sessions/conv_unknown/resources/environments/default/changes",
    );
  });

  it("does not fetch changed files when the default environment is unavailable", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse({ metadata: {} }));

    render(
      <Wrap>
        <ChangedFilesProbe id="conv_no_fs" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await flushMicrotasks();

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_no_fs/resources/environments/default",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("only preflights the environment when the runner is unavailable", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse({ error: { code: "runner_unavailable" } }, 503));

    render(
      <Wrap>
        <ChangedFilesProbe id="conv_asleep" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await flushMicrotasks();

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_asleep/resources/environments/default",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not fetch when conversationId is undefined", async () => {
    onlineMock.mockReturnValue(true);

    render(
      <Wrap>
        <ChangedFilesProbe id={undefined} />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not fetch when disabled by the caller", async () => {
    onlineMock.mockReturnValue(true);

    render(
      <Wrap>
        <DisabledChangedFilesProbe id="conv_live" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("useWorkspaceAllFiles gating", () => {
  it("does not fetch when the runner is offline", async () => {
    onlineMock.mockReturnValue(false);

    render(
      <Wrap>
        <AllFilesProbe id="conv_dead" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches the filesystem listing when the runner is online", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock
      .mockResolvedValueOnce(environmentResponse())
      .mockResolvedValueOnce(filesystemListResponse());

    render(
      <Wrap>
        <AllFilesProbe id="conv_live" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default",
    );
    expect(fetchMock.mock.calls[1][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default/filesystem?limit=1000&order=asc",
    );
  });

  it("does not fetch the filesystem listing when the default environment is unavailable", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse({ metadata: {} }));

    render(
      <Wrap>
        <AllFilesProbe id="conv_no_fs" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(1));
    await flushMicrotasks();

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_no_fs/resources/environments/default",
    );
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });

  it("does not poll the root filesystem listing while the session is active", async () => {
    onlineMock.mockReturnValue(true);
    stubChatStore("conv_live", "running");
    fetchMock
      .mockResolvedValueOnce(environmentResponse())
      .mockResolvedValueOnce(filesystemListResponse());

    render(
      <Wrap>
        <AllFilesProbe id="conv_live" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    try {
      vi.useFakeTimers();
      await vi.advanceTimersByTimeAsync(10_500);
    } finally {
      vi.useRealTimers();
    }
    await flushMicrotasks();

    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it("does not fetch when disabled by the caller", async () => {
    onlineMock.mockReturnValue(true);

    render(
      <Wrap>
        <DisabledAllFilesProbe id="conv_live" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("useWorkspaceEnvironment gating", () => {
  it("fetches the default environment when the runner is online", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse({ metadata: { root: "/workspace" } }));

    render(
      <Wrap>
        <EnvironmentProbe id="conv_live" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default",
    );
  });

  it("marks the environment unavailable when the server omits metadata.root", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse({ metadata: {} }));
    const results: Array<{ available: boolean; root: string | null; home: string | null }> = [];

    render(
      <Wrap>
        <EnvironmentDataProbe id="conv_live" onData={(data) => results.push(data)} />
      </Wrap>,
    );
    await waitFor(() =>
      expect(results.at(-1)).toEqual({ available: false, root: null, home: null }),
    );
  });

  it("surfaces metadata.home alongside root when the server reports it", async () => {
    // home drives "~" expansion for chat path-links; it must round-trip from
    // the environment payload into the query result.
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(
      jsonResponse({ metadata: { root: "/home/u/ws", home: "/home/u" } }),
    );
    const results: Array<{ available: boolean; root: string | null; home: string | null }> = [];

    render(
      <Wrap>
        <EnvironmentDataProbe id="conv_live" onData={(data) => results.push(data)} />
      </Wrap>,
    );
    await waitFor(() =>
      expect(results.at(-1)).toEqual({ available: true, root: "/home/u/ws", home: "/home/u" }),
    );
  });

  it("does not fetch when disabled by the caller", async () => {
    onlineMock.mockReturnValue(true);

    render(
      <Wrap>
        <DisabledEnvironmentProbe id="conv_live" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("does not fetch when the runner is offline", async () => {
    onlineMock.mockReturnValue(false);

    render(
      <Wrap>
        <EnvironmentProbe id="conv_dead" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("useWorkspaceDirectory gating", () => {
  it("does not fetch when the runner is offline (on-demand expand)", async () => {
    // Lazy expand path — not poll spam, but still 503s on a dead
    // runner. Gated for consistency with the other workspace hooks.
    onlineMock.mockReturnValue(false);

    render(
      <Wrap>
        <DirectoryProbe id="conv_dead" path="src" />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches the directory contents when the runner is online", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse({ object: "list", data: [] }));

    render(
      <Wrap>
        <DirectoryProbe id="conv_live" path="src/inner" />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default/filesystem/src/inner?limit=1000&order=asc",
    );
  });

  it("does not fetch when path is null (pre-existing gate)", async () => {
    onlineMock.mockReturnValue(true);

    render(
      <Wrap>
        <DirectoryProbe id="conv_live" path={null} />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("useWorkspaceFileSearch gating", () => {
  it("does not fetch when disabled by the caller", async () => {
    onlineMock.mockReturnValue(true);

    render(
      <Wrap>
        <FileSearchProbe id="conv_live" query="main" enabled={false} />
      </Wrap>,
    );
    await flushMicrotasks();

    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("fetches search results with query and glob filters when enabled", async () => {
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValue(jsonResponse({ object: "list", data: [], has_more: false }));

    render(
      <Wrap>
        <FileSearchProbe
          id="conv_live"
          query=" main "
          include=" *.ts "
          exclude=" **/node_modules "
        />
      </Wrap>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalled());

    expect(fetchMock.mock.calls[0][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default/search?limit=500&q=main&include=*.ts&exclude=**%2Fnode_modules",
    );
  });
});

describe("useWorkspaceChangedFiles trailing-edge invalidate", () => {
  it("refetches once when the focused session transitions running → idle", async () => {
    // Repro: a prior change cut polling on idle, but the agent's last writes
    // may have landed in the runner registry after the most recent poll.
    // Without a trailing refetch, the panel stays stuck on stale data
    // until the user reloads the page.
    onlineMock.mockReturnValue(true);
    stubChatStore("conv_live", "running");
    fetchMock
      .mockResolvedValueOnce(environmentResponse())
      .mockResolvedValue(changedFilesResponse());

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <ChangedFilesProbe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    // Status flips to idle: trailing invalidate should fire one more fetch.
    stubChatStore("conv_live", "idle");
    rerender(
      <QueryClientProvider client={qc}>
        <ChangedFilesProbe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(3));
    expect(fetchMock.mock.calls[2][0]).toBe(
      "/v1/sessions/conv_live/resources/environments/default/changes",
    );
  });

  it("does not invalidate when status stays idle across renders", async () => {
    // Guard against a spurious refetch on every render — the trailing
    // edge only triggers on the active → idle transition.
    onlineMock.mockReturnValue(true);
    stubChatStore("conv_live", "idle");
    fetchMock
      .mockResolvedValueOnce(environmentResponse())
      .mockResolvedValue(changedFilesResponse());

    const qc = new QueryClient({
      defaultOptions: { queries: { retry: false, staleTime: 0 } },
    });
    const { rerender } = render(
      <QueryClientProvider client={qc}>
        <ChangedFilesProbe id="conv_live" />
      </QueryClientProvider>,
    );
    await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));

    rerender(
      <QueryClientProvider client={qc}>
        <ChangedFilesProbe id="conv_live" />
      </QueryClientProvider>,
    );
    await flushMicrotasks();
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });
});

describe("isRunnerUnavailable503", () => {
  // The crux of distinguishing "runner asleep" from other 503s: a 503 is
  // only treated as the offline runner when the body carries the app's
  // runner_unavailable code. A new session whose runner is still
  // connecting 503s with this same code, but the hook's retries let it
  // resolve before the hint shows — see the retry config.
  it("is true for the app's runner_unavailable error body", async () => {
    const res = jsonResponse({ error: { code: "runner_unavailable" } }, 503);
    expect(await isRunnerUnavailable503(res)).toBe(true);
  });

  it("is false for a different app error code at 503", async () => {
    // e.g. runner_capability_mismatch is also 503 but is not "offline".
    const res = jsonResponse({ error: { code: "runner_capability_mismatch" } }, 503);
    expect(await isRunnerUnavailable503(res)).toBe(false);
  });

  it("is false for a non-JSON 503 (gateway / front-door restart)", async () => {
    // The Databricks Apps front door returns 503 with an HTML body while
    // the app restarts — must NOT be read as the runner being offline.
    const res = {
      ok: false,
      status: 503,
      statusText: "Service Unavailable",
      json: async () => {
        throw new SyntaxError("Unexpected token < in JSON");
      },
    } as unknown as Response;
    expect(await isRunnerUnavailable503(res)).toBe(false);
  });
});

describe("looksLikeWorkspaceFilePath", () => {
  it.each([
    // [input, expected, why]
    ["projects/dais-2026-outlines/foo.md", true, "nested relative path"],
    ["src/app/main.ts", true, "relative path with extension"],
    ["a/b", true, "minimal interior slash"],
    ["docs/Design Notes.md", true, "space inside a later segment is a valid filename"],
    ["", false, "empty string"],
    ["README", false, "bare filename, no directory segment"],
    ["git status", false, "command, no slash"],
    ["npm run build", false, "command, no slash"],
    ["git diff src/app", false, "whitespace before the first slash → command, not a path"],
    ["a/b?c=d", false, "query string → not a plain path"],
    ["a/b#frag", false, "fragment → not a plain path"],
    ["/etc/hosts", false, "absolute path (FileViewer rejects these)"],
    ["https://example.com/x", false, "URL"],
    ["file://a/b", false, "URL scheme"],
    ["a/", false, "trailing slash → empty basename"],
    ["a/b/", false, "trailing slash → empty final segment"],
    ["a//b", false, "empty interior segment"],
    ["../x/y", false, "parent-traversal segment"],
    ["a/./b", false, "current-dir segment"],
    ["/a", false, "leading slash → no parent segment"],
  ])("returns %o → %s (%s)", (input, expected) => {
    expect(looksLikeWorkspaceFilePath(input as string)).toBe(expected);
  });
});

describe("toWorkspaceRelativePath", () => {
  const ROOT = "/home/u/ws";
  const HOME = "/home/u";
  it.each([
    // [text, root, home, expected, why]
    ["src/app.ts", ROOT, HOME, "src/app.ts", "plain relative path → unchanged"],
    ["foo.md", ROOT, HOME, "foo.md", "bare relative basename → unchanged"],
    ["~/ws/foo.md", ROOT, HOME, "foo.md", "tilde under root → root-level relative"],
    ["~/ws/src/app.ts", ROOT, HOME, "src/app.ts", "tilde under root → nested relative"],
    ["/home/u/ws/src/app.ts", ROOT, HOME, "src/app.ts", "absolute under root → relative"],
    ["/home/u/ws/foo.md", ROOT, HOME, "foo.md", "absolute under root → root-level relative"],
    ["/etc/hosts", ROOT, HOME, null, "absolute outside root → unresolvable"],
    ["~/other/x.md", ROOT, HOME, null, "tilde outside root → unresolvable"],
    ["/home/u/ws", ROOT, HOME, null, "the root dir itself, not a file"],
    ["~/ws", ROOT, HOME, null, "tilde expands to the root dir itself"],
    ["~/ws/foo.md", ROOT, null, null, "tilde with no home → unresolvable"],
    ["/home/u/ws/foo.md", null, HOME, null, "absolute with no root → unresolvable"],
    ["", ROOT, HOME, null, "empty string"],
    ["~/ws/foo.md", "/home/u/ws/", HOME, "foo.md", "trailing-slash root tolerated"],
    // Interior traversal must not escape the workspace once stripped to relative.
    ["/home/u/ws/../etc/hosts", ROOT, HOME, null, "absolute with interior .. → unresolvable"],
    ["/home/u/ws/a/../../etc", ROOT, HOME, null, "absolute climbing above root → unresolvable"],
    ["/home/u/ws/./foo.md", ROOT, HOME, null, "absolute with '.' segment → unresolvable"],
    ["/home/u/ws/sub//foo.md", ROOT, HOME, null, "absolute with empty segment → unresolvable"],
    ["~/ws/../secret.md", ROOT, HOME, null, "tilde with interior .. → unresolvable"],
    ["a/../b.md", ROOT, HOME, null, "relative with '..' segment → unresolvable"],
    // URLs / query / fragment can't name a workspace file — rejected up-front
    // so a trusted absolute path doesn't strip to a non-matching candidate.
    ["/home/u/ws/foo.md#L12", ROOT, HOME, null, "absolute with #fragment → unresolvable"],
    ["/home/u/ws/foo.md?x=1", ROOT, HOME, null, "absolute with ?query → unresolvable"],
    ["https://example.com/x", ROOT, HOME, null, "URL → unresolvable"],
  ])("%o (root=%o, home=%o) → %o (%s)", (text, root, home, expected, _why) => {
    expect(
      toWorkspaceRelativePath(text as string, root as string | null, home as string | null),
    ).toBe(expected);
  });

  it("expands a root-user home ('/') without doubling the slash", () => {
    // home "/" + "/ws/foo.md" must not become "//ws/foo.md" (which wouldn't
    // match root "/ws"). Guards the trailing-slash strip on home expansion.
    expect(toWorkspaceRelativePath("~/ws/foo.md", "/ws", "/")).toBe("foo.md");
  });
});

function dirEntry(path: string, type: "file" | "directory" = "file") {
  return {
    id: path,
    name: path.split("/").pop(),
    path,
    type,
    bytes: type === "file" ? 5 : null,
    modified_at: 1,
  };
}

function FileExistsProbe({
  id,
  path,
  trusted = false,
  onResult,
}: {
  id: string | undefined;
  path: string | null;
  trusted?: boolean;
  onResult: (exists: boolean) => void;
}) {
  // Report from an effect, not during render: invoking onResult mid-render is
  // a side effect that double-fires under StrictMode and risks flaky probes.
  const exists = useWorkspaceFileExists(id, path, trusted);
  useEffect(() => {
    onResult(exists);
  }, [exists, onResult]);
  return null;
}

describe("useWorkspaceFileExists", () => {
  beforeEach(() => {
    onlineMock.mockReturnValue(true);
  });

  it("lists the parent directory and reports an existing file as present", async () => {
    fetchMock.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [dirEntry("projects/out/foo.md"), dirEntry("projects/out/bar.md")],
        has_more: false,
      }),
    );
    const results: boolean[] = [];
    render(
      <Wrap>
        <FileExistsProbe id="conv_1" path="projects/out/foo.md" onResult={(r) => results.push(r)} />
      </Wrap>,
    );

    // Resolves true once the parent listing arrives.
    await waitFor(() => expect(results.at(-1)).toBe(true));
    // Existence is checked via the PARENT directory listing, not a content
    // read or a recursive /search walk.
    expect(fetchMock).toHaveBeenCalledTimes(1);
    expect(fetchMock.mock.calls[0][0]).toContain(
      "/resources/environments/default/filesystem/projects/out?",
    );
  });

  it("reports false when the file is absent from the parent listing", async () => {
    // Sibling exists, target does not → must not be treated as present.
    fetchMock.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [dirEntry("projects/out/other.md")],
        has_more: false,
      }),
    );
    const results: boolean[] = [];
    render(
      <Wrap>
        <FileExistsProbe
          id="conv_1"
          path="projects/out/missing.md"
          onResult={(r) => results.push(r)}
        />
      </Wrap>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await flushMicrotasks();
    // The listing was actually consulted (parent dir, not a content read) —
    // proves the false result comes from a resolved-but-no-match query, not
    // from the query never running.
    expect(fetchMock.mock.calls[0][0]).toContain("/filesystem/projects/out?");
    // Never flips true — the basename isn't in the listing.
    expect(results.length).toBeGreaterThan(0);
    expect(results).not.toContain(true);
    expect(results.at(-1)).toBe(false);
  });

  it("reports false (and 404-tolerant) when the parent directory is missing", async () => {
    // 404 = directory/env absent. fetchDirEntriesTolerant swallows it as an
    // empty listing rather than throwing, so existence is simply false.
    fetchMock.mockResolvedValue(jsonResponse({ error: {} }, 404));
    const results: boolean[] = [];
    render(
      <Wrap>
        <FileExistsProbe id="conv_1" path="ghost/dir/file.md" onResult={(r) => results.push(r)} />
      </Wrap>,
    );

    await waitFor(() => expect(fetchMock).toHaveBeenCalled());
    await flushMicrotasks();
    expect(results.length).toBeGreaterThan(0);
    expect(results).not.toContain(true);
    expect(results.at(-1)).toBe(false);
  });

  it("checks a trusted root-level basename against the workspace root listing", async () => {
    // A path resolved from an absolute/"~" form can be a bare basename
    // ("foo.md", no slash) that looksLikeWorkspaceFilePath rejects. With
    // trusted=true it's checked anyway, listing the ROOT via bare /filesystem
    // (parent dir is "").
    fetchMock.mockResolvedValue(
      jsonResponse({
        object: "list",
        data: [dirEntry("foo.md"), dirEntry("README.md")],
        has_more: false,
      }),
    );
    const results: boolean[] = [];
    render(
      <Wrap>
        <FileExistsProbe id="conv_1" path="foo.md" trusted onResult={(r) => results.push(r)} />
      </Wrap>,
    );

    // Resolves true once the root listing arrives — proves the trusted bypass
    // and the root-level ("" parent) endpoint both work.
    await waitFor(() => expect(results.at(-1)).toBe(true));
    expect(fetchMock).toHaveBeenCalledTimes(1);
    // Bare /filesystem (no /<dir> suffix) is the workspace root listing.
    expect(fetchMock.mock.calls[0][0]).toContain("/environments/default/filesystem?");
  });

  it("does not check an untrusted bare basename (heuristic still gates)", async () => {
    // Without trusted=true, a slashless string fails looksLikeWorkspaceFilePath
    // → no listing fires. Guards against the trusted bypass leaking to the
    // default path and linkifying every bare word.
    fetchMock.mockResolvedValue(jsonResponse({ object: "list", data: [] }));
    const results: boolean[] = [];
    render(
      <Wrap>
        <FileExistsProbe id="conv_1" path="foo.md" onResult={(r) => results.push(r)} />
      </Wrap>,
    );

    await flushMicrotasks();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(results.at(-1)).toBe(false);
  });

  it("does not fetch for null or non-path candidates", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ object: "list", data: [] }));
    const results: boolean[] = [];
    render(
      <Wrap>
        <FileExistsProbe id="conv_1" path={null} onResult={(r) => results.push(r)} />
        <FileExistsProbe id="conv_1" path="git status" onResult={(r) => results.push(r)} />
      </Wrap>,
    );

    await flushMicrotasks();
    // Neither a null path nor a non-path-shaped string triggers a listing.
    expect(fetchMock).not.toHaveBeenCalled();
    expect(results.length).toBeGreaterThan(0);
    expect(results).not.toContain(true);
    expect(results.at(-1)).toBe(false);
  });
});

describe("shouldRetryRunnerOffline", () => {
  it("retries a still-connecting runner up to the cap, then stops", () => {
    // A new session whose runner is still booting answers RunnerOfflineError
    // (the app's runner_unavailable 503). ~2 minutes of backoff must outlast
    // a cold boot; beyond the cap the runner is genuinely offline and the
    // reconnect hint should show instead of retrying forever.
    const err = new RunnerOfflineError();
    expect(shouldRetryRunnerOffline(1, err)).toBe(true);
    expect(shouldRetryRunnerOffline(MAX_RUNNER_OFFLINE_RETRIES, err)).toBe(true);
    expect(shouldRetryRunnerOffline(MAX_RUNNER_OFFLINE_RETRIES + 1, err)).toBe(false);
  });

  it("fails fast on any non-runner-offline error", () => {
    // A generic failure (500, parse error, transport) is a real answer, not
    // a not-ready signal — retrying would only delay surfacing it.
    expect(shouldRetryRunnerOffline(1, new Error("boom"))).toBe(false);
  });
});

describe("runnerOfflineRetryDelay", () => {
  it("doubles per attempt then caps at 15s", () => {
    // Climbs (so a flapping runner isn't hammered) and caps (so late retries
    // stay responsive once the runner is nearly up).
    expect(runnerOfflineRetryDelay(0)).toBe(1000);
    expect(runnerOfflineRetryDelay(1)).toBe(2000);
    expect(runnerOfflineRetryDelay(2)).toBe(4000);
    expect(runnerOfflineRetryDelay(3)).toBe(8000);
    expect(runnerOfflineRetryDelay(4)).toBe(15_000);
    expect(runnerOfflineRetryDelay(10)).toBe(15_000);
  });
});

describe("useWorkspaceChangedFiles tracking signal", () => {
  function ChangedFilesDataProbe({
    id,
    onData,
  }: {
    id: string | undefined;
    onData: (data: WorkspaceChangedFilesResult) => void;
  }) {
    const query = useWorkspaceChangedFiles(id);
    useEffect(() => {
      if (query.isSuccess) onData(query.data);
    }, [query.isSuccess, query.data, onData]);
    return null;
  }

  it("surfaces incomplete tracking with the non-git reason", async () => {
    // A non-git workspace reports complete:false so the panel can warn that
    // the (possibly empty) list misses CLI/shell/external edits.
    onlineMock.mockReturnValue(true);
    fetchMock.mockResolvedValueOnce(environmentResponse()).mockResolvedValueOnce(
      jsonResponse({
        object: "list",
        data: [],
        has_more: false,
        tracking: { complete: false, reason: "non_git_workspace" },
      }),
    );

    const seen: WorkspaceChangedFilesResult[] = [];
    render(
      <Wrap>
        <ChangedFilesDataProbe id="conv_nongit" onData={(d) => seen.push(d)} />
      </Wrap>,
    );

    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    const last = seen.at(-1) as WorkspaceChangedFilesResult;
    expect(last.available).toBe(true);
    expect(last.trackingComplete).toBe(false);
    expect(last.trackingReason).toBe("non_git_workspace");
  });

  it("defaults to complete tracking when the runner omits the tracking field", async () => {
    // Older runners don't send `tracking`; treat that as complete so no
    // spurious "limited" notice appears against a server that can't report it.
    onlineMock.mockReturnValue(true);
    fetchMock
      .mockResolvedValueOnce(environmentResponse())
      .mockResolvedValueOnce(jsonResponse({ object: "list", data: [], has_more: false }));

    const seen: WorkspaceChangedFilesResult[] = [];
    render(
      <Wrap>
        <ChangedFilesDataProbe id="conv_legacy" onData={(d) => seen.push(d)} />
      </Wrap>,
    );

    await waitFor(() => expect(seen.length).toBeGreaterThan(0));
    const last = seen.at(-1) as WorkspaceChangedFilesResult;
    expect(last.trackingComplete).toBe(true);
    expect(last.trackingReason).toBeNull();
  });
});
