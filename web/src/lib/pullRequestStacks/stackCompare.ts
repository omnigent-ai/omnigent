// A stacked PR's own changes since the PR it's stacked on, read through the session's host as
// the PR's changes after its parent's head. The host answers only for the head asked about; a
// parent head that isn't one of the PR's commits isn't carried, and all changes show.

import { PullRequestApiError, readPullRequestRpc } from "@/lib/pullRequestRpc";

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
  /** Only a whole patch; null otherwise. */
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

/** A comparison that no longer matches the PR shown: its head moved under the read. */
export class StackCompareChangedError extends Error {
  constructor() {
    super("The pull request changed; refresh to see its latest revision.");
    this.name = "StackCompareChangedError";
  }
}

/** The SHA-256 of a description; an absent one is the empty string's. */
export async function descriptionDigest(body: string | null | undefined): Promise<string> {
  const bytes = new TextEncoder().encode(body ?? "");
  const digest = await crypto.subtle.digest("SHA-256", bytes);
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
}

const unprefixed = (path: string, prefix: string) =>
  path.startsWith(prefix) ? path.slice(prefix.length) : null;

/** One file's section of a unified patch, or null where its paths can't be read plainly. */
function fileOfSection(section: string[]): StackCompareFile | null {
  const hunkStart = section.findIndex((line) => line.startsWith("@@"));
  const header = hunkStart === -1 ? section : section.slice(0, hunkStart);
  const field = (prefix: string) =>
    header.find((line) => line.startsWith(prefix))?.slice(prefix.length) ?? null;
  const renamedFrom = field("rename from ");
  const copiedFrom = field("copy from ");
  const minus = field("--- ");
  const plus = field("+++ ");
  const [, gitOld, gitNew] = /^diff --git a\/(.+) b\/(.+)$/.exec(section[0] ?? "") ?? [];
  const status: StackCompareFile["status"] = header.some((line) => line.startsWith("new file mode"))
    ? "added"
    : header.some((line) => line.startsWith("deleted file mode"))
      ? "removed"
      : renamedFrom !== null
        ? "renamed"
        : copiedFrom !== null
          ? "copied"
          : "modified";
  const path =
    field("rename to ") ??
    field("copy to ") ??
    (plus && plus !== "/dev/null" ? unprefixed(plus, "b/") : null) ??
    (minus && minus !== "/dev/null" ? unprefixed(minus, "a/") : null) ??
    gitNew ??
    null;
  if (!path || path.startsWith('"')) return null;
  const previous = renamedFrom ?? copiedFrom;
  const body = hunkStart === -1 ? [] : section.slice(hunkStart);
  let additions = 0;
  let deletions = 0;
  for (const line of body) {
    if (line.startsWith("+")) additions += 1;
    else if (line.startsWith("-")) deletions += 1;
  }
  const whole = body.length > 0;
  return {
    path,
    previous_path: previous ?? (gitOld && gitOld !== path ? gitOld : null),
    status,
    additions,
    deletions,
    patch: whole ? body.join("\n") : null,
    patch_state: whole ? "complete" : "unavailable",
  };
}

/** The files a unified patch changes, each with its own hunks; null for one it can't read. */
export function stackCompareFilesFromPatch(patch: string): StackCompareFile[] | null {
  const sections: string[][] = [];
  for (const line of patch.replace(/\n$/, "").split("\n")) {
    if (line.startsWith("diff --git ")) sections.push([line]);
    else if (sections.length > 0) sections.at(-1)!.push(line);
    else if (line !== "") return null;
  }
  const files = sections.map(fileOfSection);
  return files.every((file): file is StackCompareFile => file !== null) ? files : null;
}

/**
 * `request`'s comparison for `prUrl` through the session's host: the PR's changes after its
 * parent's head, up to the head shown. A closed parent, or one whose head isn't among the PR's
 * commits, can't be told apart from the PR's own changes.
 */
export async function fetchStackComparisonOnHost(
  conversationId: string,
  prUrl: string,
  request: StackCompareRequest & { parentRepository: string; parentOpen: boolean },
): Promise<StackComparison> {
  const base = {
    viewer: "",
    parent: { repository: request.parentRepository, number: request.parentNumber },
    parentHead: request.parentHead,
    childHead: request.childHead,
    nativeBase: request.nativeBase,
    childBodyDigest: request.childBodyDigest,
    filesTruncated: false,
  };
  const refused = (reason: StackCompareRefusal): StackComparison => ({
    ...base,
    mergeBase: null,
    separable: false,
    reason,
    patchesTruncated: false,
    files: [],
  });
  if (!request.parentOpen) return refused("parent_closed");
  let answer: { patch?: unknown };
  try {
    answer = await readPullRequestRpc<{ patch?: unknown }>(
      conversationId,
      "comparePullRequestRange",
      (client) =>
        client.comparePullRequestRange({
          prUrl,
          headSha: request.childHead,
          fromSha: request.parentHead,
        }),
    );
  } catch (error) {
    // The host refuses a moved head, and a parent head that isn't one of the PR's commits.
    if (error instanceof PullRequestApiError && error.status === 400) {
      if (/not part of the selected pull request/i.test(error.message)) {
        return refused("not_carried");
      }
      if (/pull request changed/i.test(error.message)) throw new StackCompareChangedError();
    }
    throw error;
  }
  const files = typeof answer.patch === "string" ? stackCompareFilesFromPatch(answer.patch) : null;
  if (!files) throw new Error("The comparison didn't match the PR that was shown.");
  return {
    ...base,
    mergeBase: request.parentHead,
    separable: true,
    reason: null,
    patchesTruncated: files.some((file) => file.patch_state !== "complete"),
    files,
  };
}
