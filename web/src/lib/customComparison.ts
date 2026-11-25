// A base and head a person typed for one session's changes, such as "v1.2" …
// "Working Copy". It wins over the saved base for that session's workspace (its
// host and root) until a base is chosen again or the comparison is reset; a
// session moved to another workspace doesn't take it along. Kept per device, for
// the most recent ones only.

/** Named revisions; an empty head is the working copy. */
export interface RevisionSpec {
  base: string;
  head: string;
}

/** Git's empty tree, by object-name length: what a root commit is compared with. */
const EMPTY_TREES: Record<number, string> = {
  40: "4b825dc642cb6eb9a060e54bf8d69288fbee4904",
  64: "6ef19b41225c5369f1c104d45d8d85efa9b057b53b14b4b9b939dd74decc5321",
};

/**
 * What one commit changed: its first parent compared with it, or for a root
 * commit, which has no parent, the empty tree.
 */
export function commitRevisions(
  oid: string,
  { root = false }: { root?: boolean } = {},
): RevisionSpec {
  const emptyTree = EMPTY_TREES[oid.length];
  return { base: root && emptyTree ? emptyTree : `${oid}~1`, head: oid };
}

/** The commit a typed comparison shows on its own, as {@link commitRevisions} makes it; else null. */
export function shownCommit(spec: RevisionSpec | null): string | null {
  if (!spec || !/^([0-9a-f]{40}|[0-9a-f]{64})$/.test(spec.head)) return null;
  const ownBase = spec.base === `${spec.head}~1` || spec.base === EMPTY_TREES[spec.head.length];
  return ownBase ? spec.head : null;
}

/** A commit's abbreviated name, as Git shows it by default. */
export function shortCommit(oid: string): string {
  return oid.slice(0, 7);
}

/** The head field's text for the working copy; it never reaches the desktop app. */
export const WORKING_COPY_HEAD = "Working Copy";

/** Revisions longer than this are refused before they're sent, as the helper would. */
export const MAXIMUM_REVISION_BYTES = 1024;

const STORAGE_KEY = "omnigent.changes.custom-comparisons";
const CHANGE_EVENT = "omnigent:custom-comparison-change";
/** Session workspaces whose typed comparison is remembered; older ones are forgotten first. */
export const REMEMBERED_SESSIONS = 100;

/** The workspace a typed comparison was made in: the session's host and root. */
export interface ComparisonWorkspace {
  hostId: string | null;
  root: string;
}

function storageKey(sessionId: string, workspace: ComparisonWorkspace): string {
  return JSON.stringify([sessionId, workspace.hostId, workspace.root]);
}

interface StoredComparison extends RevisionSpec {
  savedAt: number;
}

function isRevisionSpec(value: unknown): value is StoredComparison {
  if (!value || typeof value !== "object") return false;
  const candidate = value as Record<string, unknown>;
  return (
    typeof candidate.base === "string" &&
    candidate.base !== "" &&
    typeof candidate.head === "string" &&
    typeof candidate.savedAt === "number"
  );
}

function readAll(): Record<string, StoredComparison> {
  try {
    const parsed: unknown = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? "{}");
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) return {};
    return Object.fromEntries(
      Object.entries(parsed as Record<string, unknown>).filter(
        (entry): entry is [string, StoredComparison] => isRevisionSpec(entry[1]),
      ),
    );
  } catch {
    return {};
  }
}

function writeAll(all: Record<string, StoredComparison>): boolean {
  const kept = Object.entries(all)
    .sort(([, left], [, right]) => right.savedAt - left.savedAt)
    .slice(0, REMEMBERED_SESSIONS);
  try {
    if (kept.length === 0) window.localStorage.removeItem(STORAGE_KEY);
    else window.localStorage.setItem(STORAGE_KEY, JSON.stringify(Object.fromEntries(kept)));
  } catch {
    return false;
  }
  window.dispatchEvent(new Event(CHANGE_EVENT));
  return true;
}

/** The session's typed comparison in `workspace`, or null when it uses the saved base there. */
export function readCustomComparison(
  sessionId: string,
  workspace: ComparisonWorkspace,
): RevisionSpec | null {
  const stored = readAll()[storageKey(sessionId, workspace)];
  return stored ? { base: stored.base, head: stored.head } : null;
}

/** Saves a typed comparison for the session in `workspace` and tells every window. */
export function writeCustomComparison(
  sessionId: string,
  workspace: ComparisonWorkspace,
  spec: RevisionSpec,
): boolean {
  return writeAll({
    ...readAll(),
    [storageKey(sessionId, workspace)]: { ...spec, savedAt: Date.now() },
  });
}

/** The session goes back to the saved base in `workspace`. */
export function clearCustomComparison(sessionId: string, workspace: ComparisonWorkspace): void {
  const key = storageKey(sessionId, workspace);
  const all = readAll();
  if (!(key in all)) return;
  writeAll(Object.fromEntries(Object.entries(all).filter(([stored]) => stored !== key)));
}

/** Calls `listener` whenever any session's typed comparison changes, in this window or another. */
export function subscribeCustomComparisons(listener: () => void): () => void {
  const onStorage = (event: StorageEvent) => {
    if (event.key === null || event.key === STORAGE_KEY) listener();
  };
  window.addEventListener(CHANGE_EVENT, listener);
  window.addEventListener("storage", onStorage);
  return () => {
    window.removeEventListener(CHANGE_EVENT, listener);
    window.removeEventListener("storage", onStorage);
  };
}

export function sameRevisions(left: RevisionSpec | null, right: RevisionSpec | null): boolean {
  return left?.base === right?.base && left?.head === right?.head;
}

/** Which typed field a problem is about. */
export type RevisionField = "base" | "head" | "both";

/** Why typed text can't be a revision, worded for its field; null when it can be sent. */
function revisionTextProblem(text: string, name: "Base" | "Head"): string | null {
  if (text.startsWith("-")) return `${name} can't start with “-”.`;
  // oxlint-disable-next-line no-control-regex -- control characters are what's refused
  if (/[\u0000-\u001f\u007f]/.test(text)) return `${name} can't contain control characters.`;
  if (new TextEncoder().encode(text).length > MAXIMUM_REVISION_BYTES) {
    return `${name} is too long.`;
  }
  return null;
}

/**
 * The comparison to ask for from the typed Base and Head, or the field to fix.
 * Head's "Working Copy", or nothing, is the working copy.
 */
export function typedRevisions(
  base: string,
  head: string,
): { spec: RevisionSpec } | { field: RevisionField; message: string } {
  const trimmedBase = base.trim();
  const trimmedHead = head.trim();
  if (trimmedBase === "") return { field: "base", message: "Enter a branch, tag or commit." };
  const baseProblem = revisionTextProblem(trimmedBase, "Base");
  if (baseProblem) return { field: "base", message: baseProblem };
  const workingCopy = trimmedHead === "" || trimmedHead === WORKING_COPY_HEAD;
  const headProblem = workingCopy ? null : revisionTextProblem(trimmedHead, "Head");
  if (headProblem) return { field: "head", message: headProblem };
  return { spec: { base: trimmedBase, head: workingCopy ? "" : trimmedHead } };
}
