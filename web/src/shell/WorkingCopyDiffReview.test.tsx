import type * as UseSessionModule from "@/hooks/useSession";
import { QueryClient, QueryClientProvider, useQuery } from "@tanstack/react-query";
import { act, render, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { DiffReviewProps } from "@/components/diff-review/DiffReview";
import { DiffContentLoader } from "@/components/diff-review/diffContentLoader";
import { createFileDiffComputer } from "@/components/diff-review/fileDiffComputation";
import type * as FileDiffModule from "@/hooks/useFileDiff";
import type * as WorkingCopyComparisonModule from "@/lib/workingCopyComparison";
import type * as WorkingCopyBaseModule from "./workingCopyBase";
import type { FileDiffResponse } from "@/hooks/useFileDiff";
import type { WorkspaceChangedFile } from "@/hooks/useWorkspaceChangedFiles";
import { hasNoTextDiff } from "./codeViewerHelpers";
import type { FileDiffRequest, FilePosition } from "./FileViewerContext";
import { ComparisonFileError, StaleComparisonReadError } from "@/lib/workingCopyComparison";
import { LEVEL_EDIT } from "@/lib/permissionsApi";
import { setSessionHost } from "@/lib/sessionHost";
import { setWorkingCopyComparisonsForTests } from "@/lib/workingCopyComparisons";
import { comparisonEntry, installFakeComparisonOwner } from "@/test/workingCopyComparisons";
import {
  changedFileFingerprint,
  fetchBranchPointText,
  WorkingCopyDiffReview,
  fetchWorkingCopyText,
  ORIGINAL_CONTENT_UNAVAILABLE,
  workingCopyReviewFile,
} from "./WorkingCopyDiffReview";

const fetchFileDiff = vi.hoisted(() => vi.fn());
const fetchComparisonFileText = vi.hoisted(() => vi.fn());
// A loaded session with permissions off, as in single-user mode, unless a case sets one.
const LOADED_SESSION = { permissionLevel: null };
const sessionAccess = vi.hoisted(() => ({
  session: { permissionLevel: null } as {
    permissionLevel: number | null;
    shareWorkspaceFiles?: boolean;
  } | null,
}));
vi.mock("@/hooks/useSession", async (importOriginal) => ({
  ...(await importOriginal<typeof UseSessionModule>()),
  useSession: () => ({ session: sessionAccess.session, isLoading: false, error: null }),
}));

vi.mock("@/lib/workingCopyComparison", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkingCopyComparisonModule>()),
  fetchComparisonFileText,
}));
vi.mock("@/hooks/useFileDiff", async (importOriginal) => ({
  ...(await importOriginal<typeof FileDiffModule>()),
  fetchFileDiff,
}));

// The listing is a real query whose data the test controls.
const listing = vi.hoisted(() => ({ files: [] as WorkspaceChangedFile[] }));
const workspaceReach = vi.hoisted(() => ({ serveable: true }));
vi.mock("@/hooks/useWorkspaceChangedFiles", () => ({
  useWorkspaceServeable: () => workspaceReach.serveable,
  useWorkspaceEnvironment: () => ({ data: { available: true } }),
  useWorkspaceChangedFiles: (conversationId: string) =>
    useQuery({
      queryKey: ["workspace-changed-files", conversationId],
      queryFn: async () => ({ data: listing.files.map((file) => ({ ...file })) }),
    }),
}));

vi.mock("@/hooks/usePermissions", () => ({ useCanEdit: () => true }));
vi.mock("./workingCopyBase", async (importOriginal) => ({
  ...(await importOriginal<typeof WorkingCopyBaseModule>()),
  useWorkingCopyBase: () => null,
}));

const reviewProps = vi.hoisted(() => ({ latest: undefined as DiffReviewProps | undefined }));
vi.mock("@/components/diff-review/DiffReview", () => ({
  DiffReview: (props: DiffReviewProps) => {
    reviewProps.latest = props;
    return null;
  },
}));

function deferredDiffs() {
  const pending: {
    resolve: (response: FileDiffResponse) => void;
    reject: (error: Error) => void;
  }[] = [];
  // Like the real fetch, each response carries when its request started.
  const startedAt: number[] = [];
  fetchFileDiff.mockImplementation(() => {
    startedAt.push(Date.now());
    return new Promise<FileDiffResponse>((resolve, reject) => {
      pending.push({ resolve, reject });
    });
  });
  const respond = (index: number, after: string) =>
    pending[index]!.resolve({
      object: "session.environment.filesystem.file_diff",
      path: "src/app.ts",
      before: "base\n",
      after,
      requestedAt: startedAt[index],
    });
  return { pending, respond };
}

const settle = () =>
  new Promise((resolve) => {
    setTimeout(resolve, 0);
  });

function entry(overrides: Partial<WorkspaceChangedFile> = {}): WorkspaceChangedFile {
  return {
    path: "src/app.ts",
    name: "app.ts",
    status: "modified",
    bytes: 10,
    modified_at: 100,
    lines_added: 2,
    lines_removed: 1,
    ...overrides,
  };
}

afterEach(() => fetchFileDiff.mockReset());

describe("workingCopyReviewFile", () => {
  it("maps statuses and line counts", () => {
    expect(workingCopyReviewFile(entry())).toMatchObject({
      change: "modified",
      additions: 2,
      deletions: 1,
      isBinary: false,
    });
    expect(workingCopyReviewFile(entry({ status: "created" })).change).toBe("added");
    expect(workingCopyReviewFile(entry({ status: "deleted" })).change).toBe("deleted");
  });

  it("treats media and binary files as having no text diff", () => {
    for (const path of ["logo.png", "doc.pdf", "clip.mp4", "model.stl", "lib.so"]) {
      expect(hasNoTextDiff(path)).toBe(true);
      expect(workingCopyReviewFile(entry({ path })).isBinary).toBe(true);
    }
    expect(hasNoTextDiff("src/app.ts")).toBe(false);
  });
});

describe("changedFileFingerprint", () => {
  it("changes when anything suggesting new content changes", () => {
    const base = changedFileFingerprint(entry());
    expect(changedFileFingerprint(entry())).toBe(base);
    expect(changedFileFingerprint(entry({ modified_at: 101 }))).not.toBe(base);
    expect(changedFileFingerprint(entry({ bytes: 11 }))).not.toBe(base);
    expect(changedFileFingerprint(entry({ status: "deleted" }))).not.toBe(base);
    expect(changedFileFingerprint(entry({ lines_added: 3 }))).not.toBe(base);
  });
});

describe("fetchWorkingCopyText", () => {
  it("reuses the cached diff unless the file changed", async () => {
    const queryClient = new QueryClient();
    fetchFileDiff.mockResolvedValue({ path: "a.ts", before: "1\n", after: "2\n" });
    await expect(
      fetchWorkingCopyText(queryClient, "conv", "a.ts", { fresh: false, listedAt: 0 }),
    ).resolves.toEqual({
      before: "1\n",
      after: "2\n",
    });
    await fetchWorkingCopyText(queryClient, "conv", "a.ts", { fresh: false, listedAt: 0 });
    expect(fetchFileDiff).toHaveBeenCalledTimes(1);
    await fetchWorkingCopyText(queryClient, "conv", "a.ts", { fresh: true, listedAt: 0 });
    expect(fetchFileDiff).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryData(["file-diff", "conv", "a.ts"])).toMatchObject({ after: "2\n" });
  });

  it("refetches a cached diff whose request predates the listing or was invalidated", async () => {
    const queryClient = new QueryClient();
    const key = ["file-diff", "conv", "b.ts"];
    queryClient.setQueryData(key, {
      path: "b.ts",
      before: "1\n",
      after: "old\n",
      requestedAt: 100,
    });
    fetchFileDiff.mockResolvedValue({
      path: "b.ts",
      before: "1\n",
      after: "new\n",
      requestedAt: 300,
    });

    // Reopened after the file changed: the cached diff's request began before the listing.
    await expect(
      fetchWorkingCopyText(queryClient, "conv", "b.ts", { fresh: false, listedAt: 200 }),
    ).resolves.toEqual({ before: "1\n", after: "new\n" });
    expect(fetchFileDiff).toHaveBeenCalledTimes(1);

    // Requested after the listing: reused.
    await fetchWorkingCopyText(queryClient, "conv", "b.ts", { fresh: false, listedAt: 300 });
    expect(fetchFileDiff).toHaveBeenCalledTimes(1);

    // Invalidated by another diff surface: fetched again.
    await queryClient.invalidateQueries({ queryKey: key, refetchType: "none" });
    await fetchWorkingCopyText(queryClient, "conv", "b.ts", { fresh: false, listedAt: 0 });
    expect(fetchFileDiff).toHaveBeenCalledTimes(2);
    expect(queryClient.getQueryState(key)!.isInvalidated).toBe(false);
  });

  it("reports an unknown baseline as unavailable", async () => {
    fetchFileDiff.mockResolvedValue({
      path: "a.ts",
      before: null,
      after: "2\n",
      baseline_state: "unknown",
    });
    await expect(
      fetchWorkingCopyText(new QueryClient(), "conv", "a.ts", { fresh: false, listedAt: 0 }),
    ).resolves.toEqual({
      unavailable: ORIGINAL_CONTENT_UNAVAILABLE,
    });
  });

  it("passes fetch failures on to the loader", async () => {
    fetchFileDiff.mockRejectedValue(new Error("git status timed out"));
    await expect(
      fetchWorkingCopyText(new QueryClient(), "conv", "a.ts", { fresh: false, listedAt: 0 }),
    ).rejects.toThrow("git status timed out");
  });
});

describe("the working-copy review with a real query cache", () => {
  it("fetches a change made while an older request was in flight", async () => {
    const { pending, respond } = deferredDiffs();
    const queryClient = new QueryClient();
    const loader = new DiffContentLoader({
      computer: createFileDiffComputer(null),
      fetchText: (path, { fresh }) =>
        fetchWorkingCopyText(queryClient, "conv", path, { fresh, listedAt: 0 }),
    });
    const revision = (fingerprint: string) => [{ path: "src/app.ts", fingerprint }];
    loader.setFiles(revision("1"));
    await settle();
    respond(0, "one\n");
    await settle();

    loader.setFiles(revision("2"));
    await settle();
    loader.setFiles(revision("3"));
    await settle();
    expect(pending).toHaveLength(2);
    respond(1, "two\n");
    await settle();
    // Revision 3 gets its own request rather than joining revision 2's.
    expect(pending).toHaveLength(3);
    respond(2, "three\n");
    await settle();
    const content = loader.contents.get("src/app.ts");
    expect(content?.status === "ready" && content.fileDiff.additionLines).toEqual(["three\n"]);

    loader.setFiles(revision("3"));
    await settle();
    expect(pending).toHaveLength(3);
    loader.dispose();
  });

  it("doesn't take an older review's in-flight response as fresh after reopening", async () => {
    const { pending, respond } = deferredDiffs();
    const queryClient = new QueryClient();
    const open = (listedAt: number) =>
      new DiffContentLoader({
        computer: createFileDiffComputer(null),
        fetchText: (path, { fresh }) =>
          fetchWorkingCopyText(queryClient, "conv", path, { fresh, listedAt }),
      });
    const first = open(0);
    first.setFiles([{ path: "src/app.ts", fingerprint: "1" }]);
    await settle();
    expect(pending).toHaveLength(1);
    // Closed while its request is in flight; the file changes; the review reopens.
    first.dispose();
    const reopened = open(Date.now());
    reopened.setFiles([{ path: "src/app.ts", fingerprint: "2" }]);
    await settle();
    respond(0, "before the change\n");
    await settle();
    expect(pending).toHaveLength(2);
    respond(1, "after the change\n");
    await settle();
    const content = reopened.contents.get("src/app.ts");
    expect(content?.status === "ready" && content.fileDiff.additionLines).toEqual([
      "after the change\n",
    ]);
    reopened.dispose();
  });

  it("doesn't take an older review's late response as fresh after reopening", async () => {
    const { pending, respond } = deferredDiffs();
    const queryClient = new QueryClient();
    const now = vi.spyOn(Date, "now").mockReturnValue(1_000);
    const open = (listedAt: number) =>
      new DiffContentLoader({
        computer: createFileDiffComputer(null),
        fetchText: (path, { fresh }) =>
          fetchWorkingCopyText(queryClient, "conv", path, { fresh, listedAt }),
      });
    const first = open(0);
    first.setFiles([{ path: "src/app.ts", fingerprint: "1" }]);
    await settle();
    first.dispose();
    // The file changes and the newer listing arrives, then the old response does.
    now.mockReturnValue(2_000);
    const listedAt = Date.now();
    now.mockReturnValue(3_000);
    respond(0, "before the change\n");
    await settle();
    expect(queryClient.getQueryState(["file-diff", "conv", "src/app.ts"])).toMatchObject({
      fetchStatus: "idle",
      dataUpdatedAt: 3_000,
    });

    const reopened = open(listedAt);
    reopened.setFiles([{ path: "src/app.ts", fingerprint: "2" }]);
    await settle();
    expect(pending).toHaveLength(2);
    respond(1, "after the change\n");
    await settle();
    const content = reopened.contents.get("src/app.ts");
    expect(content?.status === "ready" && content.fileDiff.additionLines).toEqual([
      "after the change\n",
    ]);
    reopened.dispose();
    now.mockRestore();
  });

  it("retries a failed refresh when an identical listing arrives", async () => {
    const { pending, respond } = deferredDiffs();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    listing.files = [entry({ modified_at: 1 })];
    render(
      <QueryClientProvider client={queryClient}>
        <WorkingCopyDiffReview
          conversationId="conv"
          revealPath="src/app.ts"
          sort="alpha"
          layout="unified"
          onLayoutChange={() => {}}
          wrapLines={false}
          onWrapLinesChange={() => {}}
          hideWhitespace={false}
          onHideWhitespaceChange={() => {}}
          findOpen={false}
          findFocusRequest={0}
          onFindClose={() => {}}
        />
      </QueryClientProvider>,
    );
    const content = () => reviewProps.latest?.contents.get("src/app.ts");
    await waitFor(() => expect(pending).toHaveLength(1));
    await act(async () => respond(0, "one\n"));
    await waitFor(() => expect(content()?.status).toBe("ready"));

    // The agent's final write: the refresh fails.
    listing.files = [entry({ modified_at: 2 })];
    await act(() => queryClient.refetchQueries({ queryKey: ["workspace-changed-files", "conv"] }));
    await waitFor(() => expect(pending).toHaveLength(2));
    await act(async () => pending[1]!.reject(new Error("timeout")));
    await waitFor(() => expect(content()).toHaveProperty("refreshError"));

    // A later refresh returns the same listing; its data keeps its identity.
    const before = queryClient.getQueryData(["workspace-changed-files", "conv"]);
    await act(() => queryClient.refetchQueries({ queryKey: ["workspace-changed-files", "conv"] }));
    expect(queryClient.getQueryData(["workspace-changed-files", "conv"])).toBe(before);
    await waitFor(() => expect(pending).toHaveLength(3));
    await act(async () => respond(2, "two\n"));
    await waitFor(() => {
      const shown = content();
      expect(shown?.status === "ready" && shown.fileDiff.additionLines).toEqual(["two\n"]);
    });
    expect(content()).not.toHaveProperty("refreshError");
  });

  it("retires a citation when the viewer moves to another file, loaded or not", async () => {
    deferredDiffs();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    listing.files = [entry(), entry({ path: "src/next.ts", name: "next.ts" })];
    const review = (revealPath: string, position?: FilePosition) => (
      <QueryClientProvider client={queryClient}>
        <WorkingCopyDiffReview
          conversationId="conv"
          revealPath={revealPath}
          position={position}
          sort="alpha"
          layout="unified"
          onLayoutChange={() => {}}
          wrapLines={false}
          onWrapLinesChange={() => {}}
          hideWhitespace={false}
          onHideWhitespaceChange={() => {}}
          findOpen={false}
          findFocusRequest={0}
          onFindClose={() => {}}
        />
      </QueryClientProvider>
    );
    // The cited file is still loading when the reader moves on.
    const view = render(review("src/app.ts", { line: 50 }));
    await waitFor(() =>
      expect(reviewProps.latest?.revealTarget).toMatchObject({ path: "src/app.ts", line: 50 }),
    );
    view.rerender(review("src/next.ts"));
    expect(reviewProps.latest?.revealTarget).toBeNull();
    // Coming back without a citation doesn't bring the old one back either.
    view.rerender(review("src/app.ts"));
    await act(settle);
    expect(reviewProps.latest?.revealTarget).toBeNull();
  });

  it("reveals a file opened by a fresh request instead of restoring the reader's place", async () => {
    deferredDiffs();
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    listing.files = [entry(), entry({ path: "src/next.ts", name: "next.ts" })];
    reviewProps.latest = undefined;
    const review = (revealRequest?: FileDiffRequest) => (
      <QueryClientProvider client={queryClient}>
        <WorkingCopyDiffReview
          conversationId="conv_anchor"
          revealPath="src/app.ts"
          revealRequest={revealRequest}
          sort="alpha"
          layout="unified"
          onLayoutChange={() => {}}
          wrapLines={false}
          onWrapLinesChange={() => {}}
          hideWhitespace={false}
          onHideWhitespaceChange={() => {}}
          findOpen={false}
          findFocusRequest={0}
          onFindClose={() => {}}
        />
      </QueryClientProvider>
    );
    const mountedAnchor = async (revealRequest?: FileDiffRequest) => {
      const view = render(review(revealRequest));
      await waitFor(() => expect(reviewProps.latest).toBeDefined());
      const anchor = reviewProps.latest?.initialScrollAnchor;
      view.unmount();
      reviewProps.latest = undefined;
      return anchor;
    };

    // The reader scrolls from app.ts down to next.ts, then leaves.
    const first = render(review());
    await waitFor(() => expect(reviewProps.latest).toBeDefined());
    act(() => reviewProps.latest?.onScrollAnchorChange?.({ path: "src/next.ts", offset: 0 }));
    first.unmount();
    reviewProps.latest = undefined;

    // Reopening its tab returns to where the reader was.
    expect(await mountedAnchor()).toEqual({ path: "src/next.ts", offset: 0 });
    // Clicking app.ts in Changes shows app.ts; a remount for that same click doesn't.
    const click: FileDiffRequest = { view: "diff" };
    expect(await mountedAnchor(click)).toBeUndefined();
    expect(await mountedAnchor(click)).toEqual({ path: "src/next.ts", offset: 0 });
  });
});

describe("the working-copy review under the branch point", () => {
  let fake: ReturnType<typeof installFakeComparisonOwner>;

  afterEach(() => {
    setWorkingCopyComparisonsForTests(null);
    fetchComparisonFileText.mockReset();
  });

  function comparisonText(after: string) {
    fetchComparisonFileText.mockImplementation(async (_id, comparisonId, path) => ({
      comparisonId,
      path,
      before: "base\n",
      after,
    }));
  }

  async function established(entries = [comparisonEntry("src/committed.ts")]) {
    fake = installFakeComparisonOwner();
    fake.owner.register("conv", {
      kind: "surface",
      enabled: true,
      sessionHostId: "host_1",
    });
    await waitFor(() => expect(fake.listings).toHaveLength(1));
    await fake.answer(entries);
    const { generation, established: current } = fake.owner.snapshot("conv");
    return {
      base: {
        comparisonId: current!.id,
        fingerprint: entries[0]!.fingerprint,
        generation,
      },
    };
  }

  it("seeds the shared cache only while the comparison and fingerprint are current", async () => {
    const { base } = await established();
    const queryClient = new QueryClient();
    comparisonText("one\n");
    const signal = new AbortController().signal;
    await expect(
      fetchBranchPointText(queryClient, fake.owner, "conv", "src/committed.ts", base, signal),
    ).resolves.toEqual({ before: "base\n", after: "one\n" });
    const key = ["file-diff", "conv", "src/committed.ts", base];
    expect(queryClient.getQueryData(key)).toMatchObject({ after: "one\n" });

    // The cached text for exactly this listing is reused.
    await fetchBranchPointText(queryClient, fake.owner, "conv", "src/committed.ts", base, signal);
    expect(fetchComparisonFileText).toHaveBeenCalledTimes(1);

    // A late answer after the owner forgot everything never brings text back.
    const stale = { ...base, fingerprint: "older" };
    let finish: (value: unknown) => void = () => {};
    fetchComparisonFileText.mockImplementation(
      () =>
        new Promise((resolve) => {
          finish = resolve;
        }),
    );
    const late = fetchBranchPointText(
      queryClient,
      fake.owner,
      "conv",
      "src/committed.ts",
      stale,
      signal,
    );
    fake.owner.reset();
    finish({
      comparisonId: base.comparisonId,
      path: "src/committed.ts",
      before: "",
      after: "late",
    });
    await expect(late).rejects.toBeInstanceOf(StaleComparisonReadError);
    expect(
      queryClient.getQueryData(["file-diff", "conv", "src/committed.ts", stale]),
    ).toBeUndefined();
  });

  it("shows a refusal that's the file's answer as text, and leaves the cache empty", async () => {
    const { base } = await established();
    const queryClient = new QueryClient();
    fetchComparisonFileText.mockRejectedValue(new ComparisonFileError("binary"));
    await expect(
      fetchBranchPointText(
        queryClient,
        fake.owner,
        "conv",
        "src/committed.ts",
        base,
        new AbortController().signal,
      ),
    ).resolves.toEqual({
      unavailable: "Binary file; no text to compare",
    });
    expect(queryClient.getQueryCache().getAll()).toHaveLength(0);
  });

  it("shows the branch's files with the branch-point label, and reads nothing on an unchanged re-list", async () => {
    await established([
      comparisonEntry("src/committed.ts"),
      comparisonEntry("src/added.ts", { status: "created" }),
      comparisonEntry("src/removed.ts", { status: "deleted" }),
    ]);
    comparisonText("one\n");
    const queryClient = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    listing.files = [];
    render(
      <QueryClientProvider client={queryClient}>
        <WorkingCopyDiffReview
          conversationId="conv"
          revealPath="src/committed.ts"
          sort="alpha"
          layout="unified"
          onLayoutChange={() => {}}
          wrapLines={false}
          onWrapLinesChange={() => {}}
          hideWhitespace={false}
          onHideWhitespaceChange={() => {}}
          findOpen={false}
          findFocusRequest={0}
          onFindClose={() => {}}
        />
      </QueryClientProvider>,
    );
    await waitFor(() =>
      expect(reviewProps.latest?.contents.get("src/committed.ts")?.status).toBe("ready"),
    );
    expect(reviewProps.latest?.files.map((file) => [file.path, file.change])).toEqual([
      ["src/added.ts", "added"],
      ["src/committed.ts", "modified"],
      ["src/removed.ts", "deleted"],
    ]);
    expect(reviewProps.latest?.base).toMatchObject({
      label: "vs origin/main",
      choices: [
        { label: "Automatic", checked: true },
        { label: "Uncommitted Changes", checked: false },
        { label: "All Branch Changes", checked: false },
        { label: "Last Commit", checked: false },
      ],
      actions: [{ label: "Refresh Changes" }],
    });
    const reads = fetchComparisonFileText.mock.calls.length;
    expect(reads).toBe(3);

    fake.owner.invalidate("conv");
    await waitFor(() => expect(fake.listings).toHaveLength(2));
    await fake.answer([
      comparisonEntry("src/committed.ts"),
      comparisonEntry("src/added.ts", { status: "created" }),
      comparisonEntry("src/removed.ts", { status: "deleted" }),
    ]);
    await settle();
    expect(fetchComparisonFileText).toHaveBeenCalledTimes(reads);

    // A new comparison is a new loader, so its files load under the new id.
    fake.owner.invalidate("conv");
    await waitFor(() => expect(fake.listings).toHaveLength(3));
    await fake.answer([comparisonEntry("src/committed.ts")], "cmp_2");
    await waitFor(() =>
      expect(fetchComparisonFileText).toHaveBeenLastCalledWith(
        "conv",
        "cmp_2",
        "src/committed.ts",
        expect.any(AbortSignal),
      ),
    );
  });
});

describe("a mounted working-copy review under the branch point", () => {
  let fake: ReturnType<typeof installFakeComparisonOwner>;

  beforeEach(() => {
    fake = installFakeComparisonOwner();
    setSessionHost("conv", "host_1");
    sessionAccess.session = { permissionLevel: LEVEL_EDIT };
    listing.files = [];
  });

  afterEach(() => {
    setWorkingCopyComparisonsForTests(null);
    setSessionHost("conv", null);
    sessionAccess.session = LOADED_SESSION;
    fetchComparisonFileText.mockReset();
  });

  function review(queryClient: QueryClient, active = true) {
    return (
      <QueryClientProvider client={queryClient}>
        <WorkingCopyDiffReview
          conversationId="conv"
          revealPath="src/a.ts"
          sort="alpha"
          layout="unified"
          onLayoutChange={() => {}}
          wrapLines={false}
          onWrapLinesChange={() => {}}
          hideWhitespace={false}
          onHideWhitespaceChange={() => {}}
          findOpen={false}
          findFocusRequest={0}
          onFindClose={() => {}}
          active={active}
        />
      </QueryClientProvider>
    );
  }

  it("stops its reads and drops the branch's text when the file permission is removed", async () => {
    const reads: { signal: AbortSignal; finish: (value: unknown) => void }[] = [];
    fetchComparisonFileText.mockImplementation(
      (_id, _comparisonId, _path, signal: AbortSignal) =>
        new Promise((resolve) => {
          reads.push({ signal, finish: resolve });
        }),
    );
    const queryClient = new QueryClient();
    const view = render(review(queryClient));
    await waitFor(() => expect(fake.listings).toHaveLength(1));
    await fake.answer([comparisonEntry("src/a.ts"), comparisonEntry("src/b.ts")]);
    await waitFor(() => expect(reads).toHaveLength(2));
    queryClient.setQueryData(
      [
        "file-diff",
        "conv",
        "src/b.ts",
        { comparisonId: "cmp_1", fingerprint: "fp-src/b.ts", generation: 0 },
      ],
      { before: "", after: "secret" },
    );

    // The owner shares files only with editors; this viewer is now view-only.
    sessionAccess.session = { permissionLevel: LEVEL_EDIT - 1 };
    view.rerender(review(queryClient));
    expect(await view.findByText("You don't have access to this session's files.")).toBeTruthy();
    expect(reads.every((read) => read.signal.aborted)).toBe(true);
    reads[0]!.finish({ comparisonId: "cmp_1", path: "src/a.ts", before: "", after: "late" });
    await settle();
    const cached = queryClient
      .getQueryCache()
      .findAll({ queryKey: ["file-diff", "conv"] })
      .map((query) => query.queryKey);
    expect(cached).toEqual([]);
    // Nothing lists for this session while nobody may read it.
    fake.owner.invalidate("conv");
    await settle();
    expect(fake.listings).toHaveLength(1);
  });

  it("re-lists once for a file not in the comparison, then holds its error until Retry", async () => {
    fetchComparisonFileText.mockRejectedValue(new ComparisonFileError("not_in_comparison"));
    render(review(new QueryClient()));
    await waitFor(() => expect(fake.listings).toHaveLength(1));
    await fake.answer([comparisonEntry("src/a.ts")]);
    await waitFor(() => expect(fake.listings).toHaveLength(2));
    // The same comparison comes back, so the file is tried once more under it.
    await fake.answer([comparisonEntry("src/a.ts")]);
    await waitFor(() =>
      expect(reviewProps.latest?.contents.get("src/a.ts")?.status).toBe("failed"),
    );
    await settle();
    expect(fetchComparisonFileText).toHaveBeenCalledTimes(2);

    // An unchanged poll doesn't try it again.
    fake.owner.invalidate("conv");
    await waitFor(() => expect(fake.listings).toHaveLength(3));
    await fake.answer([comparisonEntry("src/a.ts")]);
    await settle();
    expect(fetchComparisonFileText).toHaveBeenCalledTimes(2);

    // Retry starts one more chain: a read, one re-list, and its read.
    act(() => reviewProps.latest!.onRetry!("src/a.ts"));
    await waitFor(() => expect(fake.listings).toHaveLength(4));
    await fake.answer([comparisonEntry("src/a.ts")]);
    await waitFor(() => expect(fetchComparisonFileText).toHaveBeenCalledTimes(4));
    await settle();
    expect(fetchComparisonFileText).toHaveBeenCalledTimes(4);
  });

  it("keeps the comparison and its loaded text on screen while the workspace can't be reached", async () => {
    fetchComparisonFileText.mockImplementation(async (_id, comparisonId, path) => ({
      comparisonId,
      path,
      before: "base\n",
      after: "loaded\n",
    }));
    const queryClient = new QueryClient();
    const view = render(review(queryClient));
    await waitFor(() => expect(fake.listings).toHaveLength(1));
    await fake.answer([comparisonEntry("src/a.ts")]);
    const content = () => reviewProps.latest?.contents.get("src/a.ts");
    await waitFor(() => expect(content()?.status).toBe("ready"));

    // The runner and host both drop: nothing new starts, and nothing on screen goes away.
    workspaceReach.serveable = false;
    try {
      view.rerender(review(queryClient));
      expect(await view.findByText("Reconnecting to this session's machine…")).toBeTruthy();
      expect(reviewProps.latest?.files.map((file) => file.path)).toEqual(["src/a.ts"]);
      expect(content()?.status).toBe("ready");
    } finally {
      workspaceReach.serveable = true;
    }
    view.rerender(review(queryClient));
    // Reachable again: it lists at once, and the unchanged file isn't read again.
    await waitFor(() => expect(fake.listings).toHaveLength(2));
    await fake.answer([comparisonEntry("src/a.ts")]);
    await settle();
    expect(view.queryByText("Reconnecting to this session's machine…")).toBeNull();
    expect(content()?.status).toBe("ready");
    expect(fetchComparisonFileText).toHaveBeenCalledTimes(1);
  });

  it("starts no new reads while the workspace can't be reached, and finishes them once it's back", async () => {
    const reads: { path: string; finish: (value: unknown) => void }[] = [];
    fetchComparisonFileText.mockImplementation(
      (_id, comparisonId: string, path: string) =>
        new Promise((resolve) => {
          reads.push({
            path,
            finish: () => resolve({ comparisonId, path, before: "", after: `${path}\n` }),
          });
        }),
    );
    const paths = ["0.ts", "1.ts", "2.ts", "3.ts", "4.ts", "5.ts"];
    const queryClient = new QueryClient();
    const view = render(review(queryClient));
    await waitFor(() => expect(fake.listings).toHaveLength(1));
    await fake.answer(paths.map((path) => comparisonEntry(path)));
    // A few files load at a time; the rest wait their turn.
    await waitFor(() => expect(reads).toHaveLength(4));

    workspaceReach.serveable = false;
    try {
      view.rerender(review(queryClient));
      expect(await view.findByText("Reconnecting to this session's machine…")).toBeTruthy();
      for (const read of reads) read.finish(undefined);
      await settle();
      await settle();
      // Nothing new is read for the waiting files while it's unreachable.
      expect(reads.map((read) => read.path)).toEqual(["0.ts", "1.ts", "2.ts", "3.ts"]);
      expect(reviewProps.latest?.files.map((file) => file.path)).toEqual(paths);
    } finally {
      workspaceReach.serveable = true;
    }
    view.rerender(review(queryClient));
    await waitFor(() => expect(fake.listings).toHaveLength(2));
    await fake.answer(paths.map((path) => comparisonEntry(path)));
    await waitFor(() => expect(reads.length).toBeGreaterThan(4));
    // Resolve whatever loads now, until every file shows.
    await waitFor(async () => {
      for (const read of reads) read.finish(undefined);
      await settle();
      for (const path of paths) {
        expect(reviewProps.latest?.contents.get(path)?.status).toBe("ready");
      }
    });
  });

  it("keeps the comparison current only while it's on screen", async () => {
    const register = vi.spyOn(fake.owner, "register");
    const queryClient = new QueryClient();
    const view = render(review(queryClient, false));
    await waitFor(() => expect(register).toHaveBeenCalled());
    expect(register.mock.calls.map(([, consumer]) => consumer.kind)).toEqual(["file"]);
    view.rerender(review(queryClient, true));
    await waitFor(() =>
      expect(register.mock.calls.map(([, consumer]) => consumer.kind)).toEqual(["file", "surface"]),
    );
  });
});
