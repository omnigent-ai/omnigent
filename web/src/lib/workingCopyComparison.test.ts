import { afterEach, describe, expect, it, vi } from "vitest";

import {
  ChangedFileProblem,
  ChangeStatus,
  type ChangedFile,
  ComparisonField,
  ComparisonKind,
  ComparisonProblem,
  GitEntryKind,
  type ListWorkingCopyChangesResponse,
  type WorkingCopyBase,
} from "@/lib/workspaceSidebarApi";
import {
  DEFAULT_WORKSPACE_SIDEBAR_SOURCE,
  setWorkspaceSidebarSourceForTests,
  type WorkspaceSidebarSource,
  WorkspaceMethodUnavailableError,
} from "@/lib/workspaceSidebarSource";

import {
  BRANCH_POINT_NOT_SERVED,
  type BranchPointComparison,
  type BranchPointSupport,
  branchPointLabel,
  branchPointListingFrom,
  branchPointSupport,
  COMPARING,
  ComparisonFileError,
  type EffectiveComparisonInput,
  effectiveComparison,
  fetchBranchPointListing,
  fetchComparisonFileText,
  fetchRepositoryRefs,
  RECONNECTING,
  forgetListedComparisons,
  resetListedComparisonsForTests,
  REVISIONS_NOT_SERVED,
  revisionSupport,
  comparisonHeadIsWorkingCopy,
} from "./workingCopyComparison";

const SESSION = "conv_cmp";

const comparison: BranchPointComparison = {
  id: "cmp_1",
  ref: "origin/main",
  baseOid: "abc1234def",
  headOid: "fff0000",
  refOid: "123",
  entries: [],
  unrepresentableNames: 0,
  listedAt: 0,
};

const AVAILABLE: BranchPointSupport = { kind: "available" };
const CHECKING: BranchPointSupport = { kind: "checking" };

function input(overrides: Partial<EffectiveComparisonInput>): EffectiveComparisonInput {
  return {
    intent: "automatic",
    support: AVAILABLE,
    multipleRepositories: false,
    established: null,
    problem: null,
    connectTimedOut: false,
    headOverride: false,
    ...overrides,
  };
}

describe("effectiveComparison", () => {
  it("shows the last commit when the person chose it, whatever else is known", () => {
    expect(effectiveComparison(input({ intent: "head", established: comparison }))).toEqual({
      base: "head",
      note: null,
      override: false,
      retry: false,
    });
  });

  it("shows the last commit for this session after Show uncommitted changes", () => {
    for (const intent of ["automatic", "branch-point"] as const) {
      expect(
        effectiveComparison(input({ intent, established: comparison, headOverride: true })),
      ).toMatchObject({ base: "head", override: true });
    }
  });

  it("shows an established comparison, under Automatic or Branch point", () => {
    for (const intent of ["automatic", "branch-point"] as const) {
      expect(effectiveComparison(input({ intent, established: comparison }))).toEqual({
        base: "branch-point",
        comparison,
        status: null,
      });
    }
  });

  it("waits for the first comparison rather than flashing last-commit changes", () => {
    for (const support of [AVAILABLE, CHECKING]) {
      expect(effectiveComparison(input({ support }))).toEqual({
        base: "pending",
        message: COMPARING,
      });
    }
  });

  it("falls back to the last commit under Automatic after the connect clock, never under Branch point", () => {
    expect(effectiveComparison(input({ support: CHECKING, connectTimedOut: true }))).toEqual({
      base: "head",
      note: "Showing uncommitted changes: couldn't reach this session's machine.",
      override: false,
      retry: true,
    });
    expect(
      effectiveComparison(
        input({ intent: "branch-point", support: CHECKING, connectTimedOut: true }),
      ),
    ).toMatchObject({ base: "pending" });
  });

  it("says why under Branch point, and falls back with the reason under Automatic, when it can't work here", () => {
    const cases: [Partial<EffectiveComparisonInput>, string][] = [
      [
        {
          support: { kind: "unsupported", message: BRANCH_POINT_NOT_SERVED, reason: "not_served" },
        },
        BRANCH_POINT_NOT_SERVED,
      ],
      [{ multipleRepositories: true }, "Branch point works for a single Git repository."],
      [
        { problem: { reason: "no_branch_point", transient: false } },
        "No branch point with the default branch.",
      ],
      [
        {
          problem: {
            reason: "no_branch_point",
            transient: false,
            ref: "origin/dev",
            shallow: true,
          },
        },
        "No branch point with origin/dev (the clone may be shallow).",
      ],
    ];
    for (const [overrides, message] of cases) {
      expect(effectiveComparison(input(overrides))).toEqual({
        base: "head",
        note: message,
        override: false,
        retry: false,
      });
      // Even an established comparison can't outlive a definitive answer.
      expect(
        effectiveComparison(
          input({ ...overrides, intent: "branch-point", established: comparison }),
        ),
      ).toEqual({ base: "unavailable", message, retry: false });
    }
  });

  it("falls back with Retry under Automatic, and offers Retry under Branch point, after a transient failure", () => {
    const problem = { reason: "timeout", transient: true };
    expect(effectiveComparison(input({ problem }))).toEqual({
      base: "head",
      note: "Showing uncommitted changes: Comparing took too long.",
      override: false,
      retry: true,
    });
    expect(effectiveComparison(input({ intent: "branch-point", problem }))).toEqual({
      base: "unavailable",
      message: "Comparing took too long.",
      retry: true,
    });
    const unreachable: BranchPointSupport = {
      kind: "unreachable",
      reason: "remote_host_offline",
      message: "This session's machine is offline.",
    };
    expect(effectiveComparison(input({ intent: "branch-point", support: unreachable }))).toEqual({
      base: "unavailable",
      message: "This session's machine is offline.",
      retry: true,
    });
  });

  it("keeps an established comparison through a transient failure, saying it's stale", () => {
    for (const intent of ["automatic", "branch-point"] as const) {
      expect(
        effectiveComparison(
          input({
            intent,
            established: comparison,
            problem: { reason: "git_failed", transient: true },
          }),
        ),
      ).toEqual({
        base: "branch-point",
        comparison,
        status: {
          kind: "stale",
          message: "Couldn't refresh: Couldn't compare with the branch point.",
        },
      });
    }
  });

  it("keeps an established comparison while the host is asked again", () => {
    expect(effectiveComparison(input({ support: CHECKING, established: comparison }))).toEqual({
      base: "branch-point",
      comparison,
      status: { kind: "reconnecting", message: RECONNECTING },
    });
  });
});

describe("comparisonHeadIsWorkingCopy", () => {
  const branchPoint = (revisions: unknown) =>
    ({
      base: "branch-point",
      status: null,
      comparison: {
        id: "c1",
        ref: "origin/main",
        baseOid: "a",
        headOid: "b",
        refOid: "a",
        entries: [],
        unrepresentableNames: 0,
        listedAt: 1,
        revisions,
      },
    }) as Parameters<typeof comparisonHeadIsWorkingCopy>[0];
  const typed = (headIsWorkingCopy: boolean) => ({
    spec: { base: "v1" },
    baseDisplay: "v1",
    headDisplay: headIsWorkingCopy ? "" : "v2",
    headIsWorkingCopy,
  });

  it("is the working copy against the last commit or the branch point", () => {
    expect(
      comparisonHeadIsWorkingCopy({ base: "head", note: null, override: false, retry: false }),
    ).toBe(true);
    expect(comparisonHeadIsWorkingCopy(branchPoint(null))).toBe(true);
    expect(comparisonHeadIsWorkingCopy(branchPoint(undefined))).toBe(true);
  });

  it("follows a typed comparison's head", () => {
    expect(comparisonHeadIsWorkingCopy(branchPoint(typed(true)))).toBe(true);
    expect(comparisonHeadIsWorkingCopy(branchPoint(typed(false)))).toBe(false);
  });

  it("isn't, while there's nothing to compare yet", () => {
    expect(comparisonHeadIsWorkingCopy({ base: "pending", message: "Comparing" })).toBe(false);
  });
});

describe("branchPointSupport and revisionSupport", () => {
  it("are still checking until the session's host answers", () => {
    expect(branchPointSupport({ route: null })).toEqual(CHECKING);
    expect(branchPointSupport({ route: { state: "checking" } })).toEqual(CHECKING);
    expect(revisionSupport({ route: { state: "checking" } })).toEqual(CHECKING);
  });

  it("are available once the host serves comparisons", () => {
    expect(branchPointSupport({ route: { state: "served" } })).toEqual(AVAILABLE);
    expect(revisionSupport({ route: { state: "served" } })).toEqual(AVAILABLE);
  });

  it("say each comparison isn't available on a host that doesn't serve them", () => {
    expect(branchPointSupport({ route: { state: "unserved" } })).toEqual({
      kind: "unsupported",
      reason: "not_served",
      message: BRANCH_POINT_NOT_SERVED,
    });
    expect(revisionSupport({ route: { state: "unserved" } })).toEqual({
      kind: "unsupported",
      reason: "not_served",
      message: REVISIONS_NOT_SERVED,
    });
  });

  it("pass on why the host couldn't be asked, as something that may pass", () => {
    const route = { state: "unreachable" as const, message: "The host is offline." };
    expect(branchPointSupport({ route })).toEqual({
      kind: "unreachable",
      reason: "request_failed",
      message: "The host is offline.",
    });
    expect(revisionSupport({ route })).toMatchObject({ kind: "unreachable" });
  });
});

const BASE: WorkingCopyBase = {
  kind: ComparisonKind.BRANCH_POINT,
  head: "h".repeat(40),
  base: "b".repeat(40),
  reference: "origin/main",
  referenceCommit: "r".repeat(40),
  mergeBase: "b".repeat(40),
  baseDisplay: "origin/main",
  headDisplay: "HEAD",
  headIsWorkingCopy: true,
};

function changedFile(path: string, extra: Partial<ChangedFile> = {}): ChangedFile {
  return {
    path,
    status: ChangeStatus.MODIFIED,
    baseKind: GitEntryKind.FILE,
    workingKind: GitEntryKind.FILE,
    baseObject: `base-${path}`,
    baseSize: 4n,
    linesAdded: 2n,
    linesRemoved: 1n,
    binary: false,
    problem: ChangedFileProblem.UNSPECIFIED,
    working: {
      size: 10n,
      modifiedUnixNanos: 1_500_000_000n,
      changedUnixNanos: 1n,
      inode: 7n,
      device: 1n,
    },
    ...extra,
  };
}

function listing(
  files: ChangedFile[],
  base: Partial<WorkingCopyBase> = {},
): ListWorkingCopyChangesResponse {
  return { base: { ...BASE, ...base }, files, unrepresentableNames: 0 };
}

const unavailable = (
  problem: ComparisonProblem,
  extra: Partial<WorkingCopyBase["unavailable"]> = {},
) =>
  listing([], {
    unavailable: {
      problem,
      reference: "",
      shallow: false,
      detail: "",
      field: ComparisonField.UNSPECIFIED,
      ...extra,
    },
  });

describe("branchPointListingFrom", () => {
  afterEach(() => resetListedComparisonsForTests());

  it("reads a comparison and its entries in the views' terms", () => {
    const listed = branchPointListingFrom(
      SESSION,
      {
        ...listing([
          changedFile("src/a.ts", {
            status: ChangeStatus.TYPE_CHANGED,
            baseKind: GitEntryKind.SYMLINK,
          }),
          changedFile("src/new.ts", {
            status: ChangeStatus.ADDED,
            baseKind: GitEntryKind.UNSPECIFIED,
            baseObject: "",
          }),
          changedFile("src/gone.ts", { status: ChangeStatus.DELETED, working: undefined }),
          changedFile("big.bin", { binary: true, problem: ChangedFileProblem.FILTERED }),
        ]),
        unrepresentableNames: 2,
      },
      42,
    );
    expect(listed).toMatchObject({
      kind: "comparison",
      comparison: {
        ref: "origin/main",
        baseOid: BASE.base,
        headOid: BASE.head,
        refOid: BASE.referenceCommit,
        unrepresentableNames: 2,
        listedAt: 42,
        revisions: null,
      },
    });
    const entries = listed?.kind === "comparison" ? listed.comparison.entries : [];
    expect(entries.map((entry) => [entry.path, entry.status, entry.change])).toEqual([
      ["src/a.ts", "modified", "type_changed"],
      ["src/new.ts", "created", null],
      ["src/gone.ts", "deleted", null],
      ["big.bin", "modified", null],
    ]);
    expect(entries[0]).toMatchObject({
      name: "a.ts",
      bytes: 10,
      modified_at: 1.5,
      lines_added: 2,
      lines_removed: 1,
      baseKind: "symlink",
      workingKind: "file",
      binary: false,
      problem: null,
    });
    expect(entries[1]?.baseKind).toBeNull();
    // A deleted file's size is its base's.
    expect(entries[2]).toMatchObject({ bytes: 4, modified_at: null });
    expect(entries[3]).toMatchObject({ binary: true, problem: "filtered" });
  });

  it("names the same commits the same comparison, and a changed file a new fingerprint", () => {
    const first = branchPointListingFrom(SESSION, listing([changedFile("a.ts")]), 1);
    const again = branchPointListingFrom(SESSION, listing([changedFile("a.ts")]), 2);
    const touched = branchPointListingFrom(
      SESSION,
      listing([
        changedFile("a.ts", {
          working: {
            size: 11n,
            modifiedUnixNanos: 2n,
            changedUnixNanos: 2n,
            inode: 7n,
            device: 1n,
          },
        }),
      ]),
      3,
    );
    const moved = branchPointListingFrom(SESSION, listing([], { base: "c".repeat(40) }), 4);
    const id = (value: typeof first) => (value?.kind === "comparison" ? value.comparison.id : null);
    const fingerprint = (value: typeof first) =>
      value?.kind === "comparison" ? value.comparison.entries[0]?.fingerprint : null;
    expect(id(again)).toBe(id(first));
    expect(fingerprint(again)).toBe(fingerprint(first));
    expect(id(touched)).toBe(id(first));
    expect(fingerprint(touched)).not.toBe(fingerprint(first));
    expect(id(moved)).not.toBe(id(first));
  });

  it("keeps the typed names with a typed listing, and the host's names for them", () => {
    const spec = { base: "v1", head: "feature" };
    const listed = branchPointListingFrom(
      SESSION,
      listing([], {
        kind: ComparisonKind.REVISIONS,
        baseDisplay: "v1 (tag)",
        headDisplay: "",
        headIsWorkingCopy: false,
      }),
      0,
      spec,
    );
    expect(listed).toMatchObject({
      kind: "comparison",
      comparison: {
        revisions: {
          spec,
          baseDisplay: "v1 (tag)",
          headDisplay: "feature",
          headIsWorkingCopy: false,
        },
      },
    });
  });

  it("never shows an answer for another kind of comparison than was asked for", () => {
    const spec = { base: "v1", head: "" };
    expect(branchPointListingFrom(SESSION, listing([]), 0, spec)).toBeNull();
    expect(
      branchPointListingFrom(SESSION, listing([], { kind: ComparisonKind.REVISIONS }), 0),
    ).toBeNull();
    expect(branchPointListingFrom(SESSION, { files: [], unrepresentableNames: 0 }, 0)).toBeNull();
  });

  it("reads why a comparison can't be made, with definitive reasons never transient", () => {
    expect(
      branchPointListingFrom(
        SESSION,
        unavailable(ComparisonProblem.NO_BRANCH_POINT, { reference: "origin/main", shallow: true }),
        0,
      ),
    ).toEqual({
      kind: "unavailable",
      problem: { reason: "no_branch_point", transient: false, ref: "origin/main", shallow: true },
    });
    expect(branchPointListingFrom(SESSION, unavailable(ComparisonProblem.TIMEOUT), 0)).toEqual({
      kind: "unavailable",
      problem: { reason: "timeout", transient: true },
    });
  });

  it("keeps which typed field a refusal is about, and the host's detail", () => {
    expect(
      branchPointListingFrom(
        SESSION,
        unavailable(ComparisonProblem.BASE_NOT_FOUND, { field: ComparisonField.BASE }),
        0,
        { base: "nope", head: "" },
      ),
    ).toEqual({
      kind: "unavailable",
      problem: { reason: "base_not_found", transient: false, field: "base" },
    });
    expect(
      branchPointListingFrom(
        SESSION,
        unavailable(ComparisonProblem.GIT_FAILED, { detail: "bad object" }),
        0,
      ),
    ).toEqual({
      kind: "unavailable",
      problem: { reason: "git_failed", transient: true, detail: "bad object" },
    });
  });
});

describe("branchPointLabel", () => {
  it("names the ref, and says when the branch point is the last commit", () => {
    expect(branchPointLabel(comparison)).toEqual({
      label: "vs origin/main",
      description:
        "Compared with where this branch left origin/main (abc1234), including uncommitted and untracked changes.",
    });
    expect(branchPointLabel({ ...comparison, headOid: comparison.baseOid }).label).toBe(
      "vs origin/main (same as last commit)",
    );
  });
});

describe("comparison fetchers", () => {
  /** Installs a source whose Git methods are `methods`; the rest are the default's. */
  function serving(methods: Partial<WorkspaceSidebarSource>) {
    setWorkspaceSidebarSourceForTests({ ...DEFAULT_WORKSPACE_SIDEBAR_SOURCE, ...methods });
  }

  const text = (content: string) => ({
    object: "session.environment.filesystem.file_content" as const,
    path: "",
    content_type: "text/plain",
    content,
    encoding: "utf-8" as "utf-8" | "base64",
    bytes: content.length,
    truncated: false,
  });

  afterEach(() => {
    setWorkspaceSidebarSourceForTests(null);
    resetListedComparisonsForTests();
  });

  it("asks the host for the branch point, or exactly the typed base and head", async () => {
    const asked = vi.fn(async () => listing([]));
    serving({ listWorkingCopyChanges: asked });
    await expect(fetchBranchPointListing(SESSION, { refresh: true })).resolves.toMatchObject({
      kind: "comparison",
    });
    expect(asked).toHaveBeenLastCalledWith(
      SESSION,
      { kind: ComparisonKind.BRANCH_POINT },
      undefined,
    );
    asked.mockResolvedValue(listing([], { kind: ComparisonKind.REVISIONS }));
    const spec = { base: "v1 & more", head: "feature" };
    await fetchBranchPointListing(SESSION, { spec });
    expect(asked).toHaveBeenLastCalledWith(
      SESSION,
      { kind: ComparisonKind.REVISIONS, baseRevision: "v1 & more", headRevision: "feature" },
      undefined,
    );
  });

  it("treats an answer it can't show as a failure worth retrying", async () => {
    serving({ listWorkingCopyChanges: async () => ({ files: [], unrepresentableNames: 0 }) });
    await expect(fetchBranchPointListing(SESSION)).resolves.toEqual({
      kind: "unavailable",
      problem: { reason: "git_failed", transient: true },
    });
  });

  it("says so while no route serves listings", async () => {
    await expect(fetchBranchPointListing(SESSION)).rejects.toBeInstanceOf(
      WorkspaceMethodUnavailableError,
    );
    await expect(fetchRepositoryRefs(SESSION)).rejects.toBeInstanceOf(
      WorkspaceMethodUnavailableError,
    );
  });

  it("lists branch and tag suggestions from the host", async () => {
    serving({
      listRepositoryRefs: async () => ({
        refs: ["main", "v1"],
        truncated: true,
        defaultBranchRef: "origin/main",
      }),
    });
    await expect(fetchRepositoryRefs(SESSION)).resolves.toEqual({
      refs: ["main", "v1"],
      truncated: true,
      defaultBranchRef: "origin/main",
    });
  });

  it("reads a working-copy file's base blob and its working text", async () => {
    const readBaseFile = vi.fn(async () => text("old\n"));
    const readFile = vi.fn(async () => text("new\n"));
    serving({ readBaseFile, readFile });
    const listed = branchPointListingFrom(SESSION, listing([changedFile("src/a b.ts")]), 0);
    const id = listed?.kind === "comparison" ? listed.comparison.id : "";
    await expect(fetchComparisonFileText(SESSION, id, "src/a b.ts")).resolves.toEqual({
      comparisonId: id,
      path: "src/a b.ts",
      before: "old\n",
      after: "new\n",
    });
    expect(readBaseFile).toHaveBeenCalledWith(
      SESSION,
      { commit: BASE.base, path: "src/a b.ts", object: "base-src/a b.ts" },
      undefined,
    );
    expect(readFile).toHaveBeenCalledWith(SESSION, "src/a b.ts", undefined);
  });

  it("keeps each session's listing when two sessions compare the same commits", async () => {
    const readFile = vi.fn(async (sessionId: string) => text(`${sessionId}\n`));
    serving({ readBaseFile: async () => text("old\n"), readFile });
    const first = branchPointListingFrom(SESSION, listing([changedFile("a.ts")]), 0);
    const other = branchPointListingFrom("conv_other", listing([changedFile("a.ts")]), 0);
    const firstId = first?.kind === "comparison" ? first.comparison.id : "";
    const otherId = other?.kind === "comparison" ? other.comparison.id : "";
    await expect(fetchComparisonFileText(SESSION, firstId, "a.ts")).resolves.toMatchObject({
      after: `${SESSION}\n`,
    });
    await expect(fetchComparisonFileText("conv_other", otherId, "a.ts")).resolves.toMatchObject({
      after: "conv_other\n",
    });
  });

  it("forgets one session's listings, as when its workspace moves, and keeps the others'", async () => {
    serving({ readBaseFile: async () => text("old\n"), readFile: async () => text("new\n") });
    const first = branchPointListingFrom(SESSION, listing([changedFile("a.ts")]), 0);
    const other = branchPointListingFrom("conv_other", listing([changedFile("a.ts")]), 0);
    forgetListedComparisons(SESSION);
    await expect(
      fetchComparisonFileText(
        SESSION,
        first?.kind === "comparison" ? first.comparison.id : "",
        "a.ts",
      ),
    ).rejects.toMatchObject({ reason: "comparison_expired" });
    await expect(
      fetchComparisonFileText(
        "conv_other",
        other?.kind === "comparison" ? other.comparison.id : "",
        "a.ts",
      ),
    ).resolves.toMatchObject({ after: "new\n" });
  });

  it("reads a committed head's blob, and no text for an added or deleted side", async () => {
    const readBaseFile = vi.fn(async (_sessionId: string, file: { object: string }) =>
      text(file.object),
    );
    const readFile = vi.fn();
    serving({ readBaseFile, readFile });
    const spec = { base: "v1", head: "feature" };
    const listed = branchPointListingFrom(
      SESSION,
      listing(
        [
          changedFile("a.ts", { committed: { object: "head-a" } }),
          changedFile("added.ts", {
            status: ChangeStatus.ADDED,
            baseObject: "",
            committed: { object: "head-added" },
          }),
          changedFile("gone.ts", { status: ChangeStatus.DELETED, working: undefined }),
        ],
        { kind: ComparisonKind.REVISIONS, headIsWorkingCopy: false },
      ),
      0,
      spec,
    );
    const id = listed?.kind === "comparison" ? listed.comparison.id : "";
    await expect(fetchComparisonFileText(SESSION, id, "a.ts")).resolves.toMatchObject({
      before: "base-a.ts",
      after: "head-a",
    });
    await expect(fetchComparisonFileText(SESSION, id, "added.ts")).resolves.toMatchObject({
      before: null,
      after: "head-added",
    });
    await expect(fetchComparisonFileText(SESSION, id, "gone.ts")).resolves.toMatchObject({
      before: "base-gone.ts",
      after: null,
    });
    expect(readFile).not.toHaveBeenCalled();
  });

  it("refuses with the file's own reason, and asks for a re-list when the comparison is gone", async () => {
    serving({ readBaseFile: async () => text("x"), readFile: async () => text("y") });
    const listed = branchPointListingFrom(
      SESSION,
      listing([
        changedFile("lfs.bin", { problem: ChangedFileProblem.FILTERED }),
        changedFile("image.png", { binary: true }),
        changedFile("vendor", { workingKind: GitEntryKind.SUBMODULE }),
      ]),
      0,
    );
    const id = listed?.kind === "comparison" ? listed.comparison.id : "";
    const refusal = (comparisonId: string, path: string, sessionId = SESSION) =>
      fetchComparisonFileText(sessionId, comparisonId, path).catch((error: unknown) => error);
    const filtered = await refusal(id, "lfs.bin");
    expect(filtered).toBeInstanceOf(ComparisonFileError);
    expect(filtered).toMatchObject({
      reason: "filtered",
      message: "Can't compare: this file uses a Git filter (such as LFS)",
    });
    expect(await refusal(id, "image.png")).toMatchObject({ reason: "binary" });
    expect(await refusal(id, "vendor")).toMatchObject({ reason: "submodule" });
    expect(await refusal(id, "unlisted.ts")).toMatchObject({ reason: "not_in_comparison" });
    expect(await refusal("cmp_unknown", "lfs.bin")).toMatchObject({ reason: "comparison_expired" });
    // Another session's listing is never read under this one.
    expect(await refusal(id, "lfs.bin", "conv_other")).toMatchObject({
      reason: "comparison_expired",
    });
  });

  it("names the host's refusals: a moved base, a blob the clone lacks, an unserved read", async () => {
    const connectError = (code: number) =>
      Object.assign(new Error("refused"), { name: "ConnectError", code });
    let failure: unknown = connectError(9);
    serving({
      readBaseFile: async () => {
        throw failure;
      },
    });
    const listed = branchPointListingFrom(SESSION, listing([changedFile("a.ts")]), 0);
    const id = listed?.kind === "comparison" ? listed.comparison.id : "";
    const refusal = () =>
      fetchComparisonFileText(SESSION, id, "a.ts").catch((error: unknown) => error);
    expect(await refusal()).toMatchObject({ reason: "comparison_expired" });
    failure = connectError(5);
    expect(await refusal()).toMatchObject({ reason: "missing_object" });
    failure = new WorkspaceMethodUnavailableError("readBaseFile");
    expect(await refusal()).toMatchObject({ reason: "not_served" });
    // Anything else is the reader's to show as it is.
    failure = new TypeError("Failed to fetch");
    expect(await refusal()).toBe(failure);
  });

  it("refuses text too large or not UTF-8", async () => {
    let base = { ...text("x"), truncated: true };
    serving({ readBaseFile: async () => base, readFile: async () => text("y") });
    const listed = branchPointListingFrom(SESSION, listing([changedFile("a.ts")]), 0);
    const id = listed?.kind === "comparison" ? listed.comparison.id : "";
    const refusal = () =>
      fetchComparisonFileText(SESSION, id, "a.ts").catch((error: unknown) => error);
    expect(await refusal()).toMatchObject({ reason: "too_large_text" });
    base = { ...text(btoa(String.fromCharCode(0xff, 0xfe))), encoding: "base64" };
    expect(await refusal()).toMatchObject({ reason: "binary" });
  });
});

describe("effectiveComparison with a typed base and head", () => {
  const V1 = { base: "v1", head: "" };
  const typed: BranchPointComparison = {
    ...comparison,
    id: "cmp_v1",
    revisions: { spec: V1, baseDisplay: "v1", headDisplay: "", headIsWorkingCopy: true },
  };

  it("shows only a listing of exactly what was typed, and never falls back to the last commit", () => {
    const typedInput = (overrides: Partial<EffectiveComparisonInput>) =>
      input({ custom: V1, revisionSupport: AVAILABLE, connectTimedOut: true, ...overrides });
    expect(effectiveComparison(typedInput({ established: typed }))).toMatchObject({
      base: "branch-point",
      comparison: { id: "cmp_v1" },
      status: null,
    });
    // The branch point's listing, or another typed one, waits for this one instead.
    for (const established of [
      comparison,
      { ...typed, revisions: { ...typed.revisions!, spec: { base: "v2", head: "" } } },
    ]) {
      expect(effectiveComparison(typedInput({ established }))).toEqual({
        base: "pending",
        message: "Comparing…",
      });
    }
    expect(
      effectiveComparison(
        typedInput({
          revisionSupport: {
            kind: "unsupported",
            reason: "update_app",
            message: "Update the app.",
          },
        }),
      ),
    ).toEqual({
      base: "unavailable",
      message:
        "Comparing “v1” with the working copy isn't available for this session right now. Update the app.",
      retry: false,
    });
  });

  it("names the field to fix for a revision the repository doesn't have", () => {
    expect(
      effectiveComparison(
        input({
          custom: { base: "v1", head: "nope" },
          revisionSupport: AVAILABLE,
          problem: { reason: "head_not_found", transient: false, field: "head" },
        }),
      ),
    ).toEqual({
      base: "unavailable",
      message: "Head “nope” wasn't found in this repository.",
      retry: false,
      field: "head",
    });
    expect(
      effectiveComparison(
        input({
          custom: V1,
          revisionSupport: AVAILABLE,
          problem: { reason: "no_common_history", transient: false, field: "both" },
        }),
      ),
    ).toMatchObject({
      message: "Base “v1” and Head “HEAD” don't share a common commit.",
      field: "both",
    });
  });

  it("keeps a typed listing out of the branch point once the typed comparison ends", () => {
    expect(effectiveComparison(input({ intent: "branch-point", established: typed }))).toEqual({
      base: "pending",
      message: COMPARING,
    });
  });
});
