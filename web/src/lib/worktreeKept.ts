/**
 * Build the "Worktree kept — …" note for an archived session whose git
 * worktree was left in place instead of removed.
 *
 * Archive-time cleanup stores its reason on the session's
 * ``omnigent.worktree_kept`` label as a small JSON object:
 *
 * - ``{"reason": "in_use" | "host_offline" | "unknown"}`` when no safety
 *   check could run, or
 * - ``{"dirty_files": N, "unpushed_commits": M, "merged": bool | null,
 *   "default_ref": string | null}`` when the host inspected the worktree
 *   and found work that removing it would lose.
 *
 * A missing, empty, or malformed value yields ``null`` (no note); an
 * inspection with nothing unsafe also yields ``null`` — that shape would
 * mean the worktree was removed, so there is nothing to explain.
 */

export const WORKTREE_KEPT_LABEL = "omnigent.worktree_kept";

interface WorktreeKeptInspection {
  dirty_files?: unknown;
  unpushed_commits?: unknown;
  merged?: unknown;
  default_ref?: unknown;
  reason?: unknown;
}

/** Pluralize a counted noun phrase: 1 → "1 commit", 3 → "3 commits". */
function counted(n: number, singular: string, plural: string): string {
  return `${n} ${n === 1 ? singular : plural}`;
}

/**
 * Render the label value as a human sentence, or ``null`` for no note.
 *
 * :param raw: The session's ``omnigent.worktree_kept`` label value.
 * :returns: Sentence like ``"Worktree kept — 2 uncommitted changes,
 *   branch not merged into origin/main."``, or ``null``.
 */
export function worktreeKeptNote(raw: string | undefined): string | null {
  if (!raw) return null;
  let parsed: WorktreeKeptInspection;
  try {
    parsed = JSON.parse(raw) as WorktreeKeptInspection;
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;

  // Reason-only shapes: no inspection ran, so there's one broad cause.
  switch (parsed.reason) {
    case "in_use":
      return "Worktree kept — another session is still using it.";
    case "host_offline":
      return "Worktree kept — its host is offline, so it couldn't be checked.";
    case "unknown":
      return "Worktree kept — Omnigent couldn't verify it was safe to remove.";
  }

  // Inspected-but-unsafe: list what removing the worktree would lose.
  const parts: string[] = [];
  const dirty = parsed.dirty_files;
  if (typeof dirty === "number" && dirty > 0) {
    parts.push(counted(dirty, "uncommitted change", "uncommitted changes"));
  }
  const unpushed = parsed.unpushed_commits;
  if (typeof unpushed === "number" && unpushed > 0) {
    parts.push(counted(unpushed, "unpushed commit", "unpushed commits"));
  }
  if (parsed.merged === false) {
    const ref =
      typeof parsed.default_ref === "string" && parsed.default_ref !== ""
        ? ` into ${parsed.default_ref}`
        : "";
    parts.push(`branch not merged${ref}`);
  } else if (parsed.merged === null) {
    parts.push("merge status couldn't be determined");
  }
  if (parts.length === 0) return null;
  return `Worktree kept — ${parts.join(", ")}.`;
}
