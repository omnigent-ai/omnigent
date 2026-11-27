// Comparisons of a stacked PR #102 with its parent #100, for the stack diff's tests.

import type { StackCompareFile, StackComparison } from "@/lib/pullRequestStacks/stackCompare";
import type { StackComparisonIdentity } from "./stackComparisonState";

export const CHILD_HEAD = "c".repeat(40);
export const PARENT_HEAD = "b".repeat(40);
export const NATIVE_BASE = "a".repeat(40);
export const DIGEST = "d".repeat(64);

export function compareFile(overrides: Partial<StackCompareFile> = {}): StackCompareFile {
  return {
    path: "src/app.ts",
    previous_path: null,
    status: "modified",
    additions: 1,
    deletions: 1,
    patch: "@@ -1,2 +1,2 @@\n line\n-old\n+new",
    patch_state: "complete",
    ...overrides,
  };
}

export function comparison(overrides: Partial<StackComparison> = {}): StackComparison {
  return {
    viewer: "alice",
    parent: { repository: "acme/app", number: 100 },
    parentHead: PARENT_HEAD,
    childHead: CHILD_HEAD,
    nativeBase: NATIVE_BASE,
    childBodyDigest: DIGEST,
    mergeBase: PARENT_HEAD,
    separable: true,
    reason: null,
    filesTruncated: false,
    patchesTruncated: false,
    files: [compareFile()],
    ...overrides,
  };
}

export function identity(
  overrides: Partial<StackComparisonIdentity> = {},
): StackComparisonIdentity {
  return {
    kind: "stack-comparison",
    reader: '["mac-read","user","github","git.example.com"]',
    repository: "acme/app",
    number: 102,
    parentNumber: 100,
    parentState: "OPEN",
    childHead: CHILD_HEAD,
    parentHead: PARENT_HEAD,
    nativeBase: NATIVE_BASE,
    childBody: "Stacked on #100",
    nativeStack: null,
    ...overrides,
  };
}
