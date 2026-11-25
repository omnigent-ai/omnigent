// A stacked PR's own changes since the PR it's stacked on, read on this Mac as the account
// that read the PR. The shell answers only for the exact commits and description asked about;
// an older or partial shell can't answer, and the page keeps showing all changes.

import type { CodeHostPullRequest, CodeHostReadScheduling } from "@/lib/codeHosts";
import { DesktopPullRequestError, readOnThisMac } from "@/lib/desktopPullRequests";
import { macOffers } from "@/lib/desktopReadPath";

export const PULL_REQUEST_STACK_COMPARE_OPERATION = "pull_request_stack_compare";

/** The commits and description the page showed, which the comparison must still match. */
export interface StackCompareRequest {
  parentNumber: number;
  childHead: string;
  nativeBase: string;
  parentHead: string;
  childBodyDigest: string;
}

/** Why the parent's changes can't be told apart from the child's. */
export type StackCompareRefusal = "parent_closed" | "not_carried" | "in_base";

export type StackComparePatchState = "complete" | "incomplete" | "unavailable";

export interface StackCompareFile {
  path: string;
  previous_path: string | null;
  status: "added" | "removed" | "modified" | "renamed" | "copied" | "changed";
  additions: number;
  deletions: number;
  /** Only a patch proven whole; null otherwise. */
  patch: string | null;
  patch_state: StackComparePatchState;
}

export interface StackComparison {
  viewer: string;
  parent: { repository: string; number: number };
  parentHead: string;
  childHead: string;
  nativeBase: string;
  childBodyDigest: string;
  mergeBase: string | null;
  separable: boolean;
  reason: StackCompareRefusal | null;
  /** GitHub listed only its first files; there may be more. */
  filesTruncated: boolean;
  /** Some file has no whole patch to show. */
  patchesTruncated: boolean;
  files: StackCompareFile[];
}

/** GitHub's compare lists at most this many files; a list this long may be cut short. */
export const COMPARE_FILE_LIMIT = 300;

const REFUSALS = new Set<string>(["parent_closed", "not_carried", "in_base"]);
const PATCH_STATES = new Set<string>(["complete", "incomplete", "unavailable"]);
const FILE_STATUSES = new Set<string>([
  "added",
  "removed",
  "modified",
  "renamed",
  "copied",
  "changed",
]);

/** The SHA-256 of a description as the shell digests it; an absent one is the empty string's. */
export async function descriptionDigest(body: string | null | undefined): Promise<string> {
  const bytes = new TextEncoder().encode(body ?? "");
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

/** Whether this Mac's shell offers the comparison for `target`'s provider. */
export function stackCompareOffered(target: CodeHostPullRequest): boolean {
  return macOffers(target.provider, PULL_REQUEST_STACK_COMPARE_OPERATION);
}

interface WireComparison {
  viewer?: unknown;
  comparison?: {
    parent?: { repository?: unknown; number?: unknown };
    parent_head?: unknown;
    child_head?: unknown;
    native_base?: unknown;
    child_body_digest?: unknown;
    merge_base?: unknown;
    separable?: unknown;
    reason?: unknown;
    files_truncated?: unknown;
    patches_truncated?: unknown;
  };
  files?: unknown;
}

const isCount = (value: unknown): value is number =>
  typeof value === "number" && Number.isSafeInteger(value) && value >= 0;

/** One file exactly as the shell's contract describes it, or null for anything else. */
function parseFile(file: unknown): StackCompareFile | null {
  const entry = (file ?? {}) as Record<string, unknown>;
  const { path, previous_path, status, additions, deletions, patch, patch_state } = entry;
  if (
    typeof path !== "string" ||
    !path ||
    (previous_path !== null && (typeof previous_path !== "string" || !previous_path)) ||
    typeof status !== "string" ||
    !FILE_STATUSES.has(status) ||
    !isCount(additions) ||
    !isCount(deletions) ||
    typeof patch_state !== "string" ||
    !PATCH_STATES.has(patch_state)
  ) {
    return null;
  }
  // Only a patch proven whole comes with its text.
  const complete = patch_state === "complete";
  if (complete ? typeof patch !== "string" || !patch : patch !== null) return null;
  return {
    path,
    previous_path,
    status: status as StackCompareFile["status"],
    additions,
    deletions,
    patch: complete ? (patch as string) : null,
    patch_state: patch_state as StackComparePatchState,
  };
}

/**
 * `request`'s comparison for `target`, or null when a shell that offers the read refuses it
 * as unsupported. An answer for other commits than the ones asked about is never shown.
 */
export async function fetchStackComparison(
  target: CodeHostPullRequest,
  request: StackCompareRequest,
  scheduling: CodeHostReadScheduling = {},
): Promise<StackComparison | null> {
  let data: WireComparison | null;
  try {
    ({ data } = await readOnThisMac<WireComparison>(
      target,
      PULL_REQUEST_STACK_COMPARE_OPERATION,
      {
        repository: target.repository,
        number: target.number,
        parent_number: request.parentNumber,
        expected_child_head: request.childHead,
        expected_native_base: request.nativeBase,
        expected_parent_head: request.parentHead,
        expected_child_body_digest: request.childBodyDigest,
      },
      scheduling,
    ));
  } catch (error) {
    if (error instanceof DesktopPullRequestError && error.code === "unsupported") return null;
    throw error;
  }
  const comparison = data?.comparison;
  const refuse = () =>
    new DesktopPullRequestError(
      { code: "query_failed", message: "The comparison didn't match the PR that was shown." },
      target,
    );
  if (
    typeof data?.viewer !== "string" ||
    !data.viewer ||
    !comparison ||
    comparison.parent?.repository !== target.repository ||
    comparison.parent.number !== request.parentNumber ||
    comparison.parent_head !== request.parentHead ||
    comparison.child_head !== request.childHead ||
    comparison.native_base !== request.nativeBase ||
    comparison.child_body_digest !== request.childBodyDigest ||
    typeof comparison.separable !== "boolean" ||
    typeof comparison.files_truncated !== "boolean" ||
    typeof comparison.patches_truncated !== "boolean" ||
    !Array.isArray(data.files)
  ) {
    throw refuse();
  }
  const files = data.files.map(parseFile);
  if (!files.every((file): file is StackCompareFile => file !== null)) throw refuse();
  const reason = comparison.reason ?? null;
  if (comparison.separable) {
    // A separable comparison is from the parent's head, and owns up to every gap it has.
    if (
      reason !== null ||
      comparison.merge_base !== request.parentHead ||
      (files.length >= COMPARE_FILE_LIMIT && !comparison.files_truncated) ||
      (files.some((file) => file.patch_state !== "complete") && !comparison.patches_truncated)
    ) {
      throw refuse();
    }
  } else if (typeof reason !== "string" || !REFUSALS.has(reason) || files.length > 0) {
    throw refuse();
  }
  return {
    viewer: data.viewer,
    parent: { repository: target.repository, number: request.parentNumber },
    parentHead: request.parentHead,
    childHead: request.childHead,
    nativeBase: request.nativeBase,
    childBodyDigest: request.childBodyDigest,
    mergeBase: comparison.separable ? request.parentHead : null,
    separable: comparison.separable,
    reason: reason as StackCompareRefusal | null,
    filesTruncated: comparison.files_truncated,
    patchesTruncated: comparison.patches_truncated,
    files,
  };
}
