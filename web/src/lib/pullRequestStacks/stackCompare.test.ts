import { beforeEach, describe, expect, it, vi } from "vitest";

import type { CodeHostPullRequest } from "@/lib/codeHosts";
import type * as DesktopPullRequestsModule from "@/lib/desktopPullRequests";
import type * as DesktopReadPathModule from "@/lib/desktopReadPath";
import { DesktopPullRequestError } from "@/lib/desktopPullRequests";
import { macOffers } from "@/lib/desktopReadPath";
import {
  COMPARE_FILE_LIMIT,
  descriptionDigest,
  fetchStackComparison,
  PULL_REQUEST_STACK_COMPARE_OPERATION,
  stackCompareOffered,
  type StackCompareRequest,
} from "@/lib/pullRequestStacks/stackCompare";

// A plain function rather than a mock, so a rejected read settles only where the code awaits it.
const shell = vi.hoisted(() => ({
  calls: [] as unknown[][],
  answer: (): Promise<unknown> => Promise.resolve({ data: {}, login: null }),
}));

vi.mock("@/lib/desktopPullRequests", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopPullRequestsModule>()),
  readOnThisMac: (...args: unknown[]) => {
    shell.calls.push(args);
    return shell.answer();
  },
}));
vi.mock("@/lib/desktopReadPath", async (importOriginal) => ({
  ...(await importOriginal<typeof DesktopReadPathModule>()),
  macOffers: vi.fn(),
}));

const TARGET: CodeHostPullRequest = {
  provider: "github",
  host: "git.example.com",
  repository: "acme/app",
  number: 102,
  url: "https://git.example.com/acme/app/pull/102",
};
const REQUEST: StackCompareRequest = {
  parentNumber: 100,
  childHead: "c".repeat(40),
  nativeBase: "a".repeat(40),
  parentHead: "b".repeat(40),
  childBodyDigest: "d".repeat(64),
};
const FILE = {
  path: "src/app.ts",
  previous_path: null,
  status: "modified",
  additions: 1,
  deletions: 1,
  patch: "@@ -1 +1 @@\n-old\n+new\n",
  patch_state: "complete",
};

function wire(comparison: Record<string, unknown> = {}, files: unknown[] = [FILE]) {
  return {
    viewer: "octo",
    comparison: {
      parent: { repository: "acme/app", number: 100 },
      parent_head: REQUEST.parentHead,
      child_head: REQUEST.childHead,
      native_base: REQUEST.nativeBase,
      child_body_digest: REQUEST.childBodyDigest,
      merge_base: REQUEST.parentHead,
      separable: true,
      files_truncated: false,
      patches_truncated: false,
      ...comparison,
    },
    files,
  };
}

const manyFiles = (count: number) =>
  Array.from({ length: count }, (_value, index) => ({ ...FILE, path: `src/file${index}.ts` }));

const answerWith = (data: unknown) => {
  shell.answer = () => Promise.resolve({ data, login: "octo" });
};

beforeEach(() => {
  shell.calls = [];
});

describe("descriptionDigest", () => {
  it("digests as the shell does, an absent description as the empty string", async () => {
    const empty = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
    expect(await descriptionDigest("")).toBe(empty);
    expect(await descriptionDigest(null)).toBe(empty);
    // The shell's digest of the same non-ASCII, CRLF description, so both sides agree.
    expect(await descriptionDigest("Stacked on #100 ✓\r\n")).toBe(
      "584da8f61760328d8cbe7a3274634656a489d69bff1ae6a0b141da987c1b0596",
    );
  });
});

describe("stackCompareOffered", () => {
  it("asks whether the shell offers the comparison for the PR's provider", () => {
    vi.mocked(macOffers).mockReturnValue(false);
    expect(stackCompareOffered(TARGET)).toBe(false);
    vi.mocked(macOffers).mockReturnValue(true);
    expect(stackCompareOffered(TARGET)).toBe(true);
    expect(macOffers).toHaveBeenLastCalledWith("github", PULL_REQUEST_STACK_COMPARE_OPERATION);
  });
});

describe("fetchStackComparison", () => {
  it("asks for exactly the commits and description shown, as the PR reads", async () => {
    answerWith(wire());
    const comparison = await fetchStackComparison(TARGET, REQUEST);
    expect(shell.calls).toEqual([
      [
        TARGET,
        PULL_REQUEST_STACK_COMPARE_OPERATION,
        {
          repository: "acme/app",
          number: 102,
          parent_number: 100,
          expected_child_head: REQUEST.childHead,
          expected_native_base: REQUEST.nativeBase,
          expected_parent_head: REQUEST.parentHead,
          expected_child_body_digest: REQUEST.childBodyDigest,
        },
        {},
      ],
    ]);
    expect(comparison).toMatchObject({
      viewer: "octo",
      separable: true,
      reason: null,
      mergeBase: REQUEST.parentHead,
      files: [FILE],
    });
  });

  it("passes on why the parent's changes can't be told apart", async () => {
    answerWith(wire({ separable: false, reason: "in_base", merge_base: null }, []));
    expect(await fetchStackComparison(TARGET, REQUEST)).toMatchObject({
      separable: false,
      reason: "in_base",
      files: [],
    });
  });

  it("keeps the gaps a separable comparison owns up to", async () => {
    const files = [
      ...manyFiles(COMPARE_FILE_LIMIT - 1),
      { ...FILE, path: "src/big.ts", patch: null, patch_state: "incomplete" },
    ];
    answerWith(wire({ files_truncated: true, patches_truncated: true }, files));
    expect(await fetchStackComparison(TARGET, REQUEST)).toMatchObject({
      filesTruncated: true,
      patchesTruncated: true,
    });
  });

  it("answers null when a partial shell refuses the read as unsupported", async () => {
    shell.answer = () =>
      Promise.reject(
        new DesktopPullRequestError({ code: "unsupported", message: "Not here" }, TARGET),
      );
    expect(await fetchStackComparison(TARGET, REQUEST)).toBeNull();
  });

  it("passes on that the PR changed since it was shown", async () => {
    shell.answer = () =>
      Promise.reject(
        new DesktopPullRequestError({ code: "pull_request_changed", message: "Moved" }, TARGET),
      );
    await expect(fetchStackComparison(TARGET, REQUEST)).rejects.toMatchObject({
      code: "pull_request_changed",
    });
  });

  for (const [name, data] of [
    ["another child head", wire({ child_head: "e".repeat(40) })],
    ["another parent", wire({ parent: { repository: "acme/app", number: 99 } })],
    ["another description", wire({ child_body_digest: "f".repeat(64) })],
    ["a refusal it doesn't know", wire({ separable: false, reason: "unknown" })],
    ["a separable answer with a refusal", wire({ reason: "in_base" })],
    [
      "a patch on a file not proven whole",
      wire({ patches_truncated: true }, [{ ...FILE, patch_state: "incomplete" }]),
    ],
    ["no files", { ...wire(), files: undefined }],
    ["a separable answer without a merge base", wire({ merge_base: undefined })],
    ["a separable answer from another merge base", wire({ merge_base: "0".repeat(40) })],
    ["no word on whether files were cut", wire({ files_truncated: undefined })],
    ["no word on whether patches were cut", wire({ patches_truncated: undefined })],
    [
      "an incomplete file it calls whole",
      wire({}, [{ ...FILE, patch: null, patch_state: "incomplete" }]),
    ],
    ["GitHub's file limit it calls whole", wire({}, manyFiles(COMPARE_FILE_LIMIT))],
    ["a refusal that brings files", wire({ separable: false, reason: "in_base" })],
    ["a refusal without a reason", wire({ separable: false, merge_base: null }, [])],
    ["a file without a path", wire({}, [{ ...FILE, path: "" }])],
    ["a file with a status it doesn't know", wire({}, [{ ...FILE, status: "exploded" }])],
    ["a file renamed from nowhere", wire({}, [{ ...FILE, previous_path: 3 }])],
    ["a file with negative counts", wire({}, [{ ...FILE, additions: -1 }])],
    ["a file with fractional counts", wire({}, [{ ...FILE, deletions: 1.5 }])],
    ["a whole file without its patch", wire({}, [{ ...FILE, patch: null }])],
    ["no viewer", { ...wire(), viewer: "" }],
  ] as const) {
    it(`never shows an answer with ${name}`, async () => {
      answerWith(data);
      await expect(fetchStackComparison(TARGET, REQUEST)).rejects.toMatchObject({
        code: "query_failed",
      });
    });
  }
});
