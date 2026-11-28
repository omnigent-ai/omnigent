import { beforeEach, describe, expect, it, vi } from "vitest";

import { PullRequestApiError } from "@/lib/pullRequestRpc";
import {
  descriptionDigest,
  fetchStackComparisonOnHost,
  StackCompareChangedError,
  stackCompareFilesFromPatch,
} from "@/lib/pullRequestStacks/stackCompare";

// A plain function rather than a mock, so a rejected read settles only where the code awaits it.
const host = vi.hoisted(() => ({
  calls: [] as { session: string; method: string; request: unknown }[],
  answer: (): Promise<unknown> => Promise.resolve({}),
}));
vi.mock("@/lib/pullRequestRpc", async (importOriginal) => ({
  ...(await importOriginal<Record<string, unknown>>()),
  readPullRequestRpc: async (
    session: string,
    method: string,
    call: (client: unknown) => Promise<unknown>,
  ) => {
    let request: unknown;
    await call({
      comparePullRequestRange: async (asked: unknown) => {
        request = asked;
        return {};
      },
    });
    host.calls.push({ session, method, request });
    return host.answer();
  },
}));

const PR = "https://github.com/acme/app/pull/102";
const REQUEST = {
  parentNumber: 101,
  parentRepository: "acme/app",
  parentOpen: true,
  childHead: "c".repeat(40),
  nativeBase: "b".repeat(40),
  parentHead: "p".repeat(40),
  childBodyDigest: "d".repeat(64),
};
const PATCH = [
  "diff --git a/src/app.ts b/src/app.ts",
  "index 1111111..2222222 100644",
  "--- a/src/app.ts",
  "+++ b/src/app.ts",
  "@@ -1,2 +1,2 @@",
  " keep",
  "-old",
  "+new",
  "diff --git a/docs/new.md b/docs/new.md",
  "new file mode 100644",
  "--- /dev/null",
  "+++ b/docs/new.md",
  "@@ -0,0 +1 @@",
  "+hello",
  "diff --git a/old/name.ts b/new/name.ts",
  "similarity index 100%",
  "rename from old/name.ts",
  "rename to new/name.ts",
  "diff --git a/logo.png b/logo.png",
  "index 3333333..4444444 100644",
  "Binary files a/logo.png and b/logo.png differ",
  "",
].join("\n");

beforeEach(() => {
  host.calls = [];
});

describe("descriptionDigest", () => {
  it("digests a description, an absent one as the empty string", async () => {
    const empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    expect(await descriptionDigest("")).toBe(empty);
    expect(await descriptionDigest(null)).toBe(empty);
    expect(await descriptionDigest("Stacked on #100 ✓\r\n")).toBe(
      "584da8f61760328d8cbe7a3274634656a489d69bff1ae6a0b141da987c1b0596",
    );
  });
});

describe("stackCompareFilesFromPatch", () => {
  it("lists each file with its status, counts and own hunks", () => {
    expect(stackCompareFilesFromPatch(PATCH)).toEqual([
      {
        path: "src/app.ts",
        previous_path: null,
        status: "modified",
        additions: 1,
        deletions: 1,
        patch: "@@ -1,2 +1,2 @@\n keep\n-old\n+new",
        patch_state: "complete",
      },
      {
        path: "docs/new.md",
        previous_path: null,
        status: "added",
        additions: 1,
        deletions: 0,
        patch: "@@ -0,0 +1 @@\n+hello",
        patch_state: "complete",
      },
      {
        path: "new/name.ts",
        previous_path: "old/name.ts",
        status: "renamed",
        additions: 0,
        deletions: 0,
        patch: null,
        patch_state: "unavailable",
      },
      {
        path: "logo.png",
        previous_path: null,
        status: "modified",
        additions: 0,
        deletions: 0,
        patch: null,
        patch_state: "unavailable",
      },
    ]);
  });

  it("reads an empty patch as no files, and text that isn't one as unreadable", () => {
    expect(stackCompareFilesFromPatch("")).toEqual([]);
    expect(stackCompareFilesFromPatch("not a patch")).toBeNull();
  });
});

describe("fetchStackComparisonOnHost", () => {
  it("asks the host for the PR's changes after its parent's head, up to the head shown", async () => {
    host.answer = () => Promise.resolve({ object: "session.github.pr_diff", patch: PATCH });
    const comparison = await fetchStackComparisonOnHost("s1", PR, REQUEST);
    expect(host.calls).toEqual([
      {
        session: "s1",
        method: "comparePullRequestRange",
        request: { prUrl: PR, headSha: REQUEST.childHead, fromSha: REQUEST.parentHead },
      },
    ]);
    expect(comparison).toMatchObject({
      parent: { repository: "acme/app", number: 101 },
      separable: true,
      reason: null,
      mergeBase: REQUEST.parentHead,
      filesTruncated: false,
      // The rename and the binary file have no whole patch to show.
      patchesTruncated: true,
    });
    expect(comparison.files.map((file) => file.path)).toEqual([
      "src/app.ts",
      "docs/new.md",
      "new/name.ts",
      "logo.png",
    ]);
  });

  it("can't tell a closed parent's changes apart, and asks nothing", async () => {
    const comparison = await fetchStackComparisonOnHost("s1", PR, {
      ...REQUEST,
      parentOpen: false,
    });
    expect(comparison).toMatchObject({ separable: false, reason: "parent_closed", files: [] });
    expect(host.calls).toEqual([]);
  });

  it("says a parent head that isn't one of the PR's commits isn't carried", async () => {
    host.answer = () =>
      Promise.reject(
        new PullRequestApiError("That commit is not part of the selected pull request.", 400),
      );
    expect(await fetchStackComparisonOnHost("s1", PR, REQUEST)).toMatchObject({
      separable: false,
      reason: "not_carried",
    });
  });

  it("says the PR changed when its head moved under the read", async () => {
    host.answer = () =>
      Promise.reject(
        new PullRequestApiError(
          "The pull request changed; refresh to see its latest revision.",
          400,
        ),
      );
    await expect(fetchStackComparisonOnHost("s1", PR, REQUEST)).rejects.toBeInstanceOf(
      StackCompareChangedError,
    );
  });

  it("passes on any other failure, and never shows an answer it can't read", async () => {
    const error = new PullRequestApiError("Sign in to this GitHub host with gh.", 400);
    host.answer = () => Promise.reject(error);
    await expect(fetchStackComparisonOnHost("s1", PR, REQUEST)).rejects.toBe(error);
    host.answer = () => Promise.resolve({ patch: 7 });
    await expect(fetchStackComparisonOnHost("s1", PR, REQUEST)).rejects.toThrow(
      "The comparison didn't match the PR that was shown.",
    );
  });
});
