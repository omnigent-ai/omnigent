export const WORKTREE_KEPT_LABEL = "omnigent.worktree_kept";

interface WorktreeKeptInspection {
  dirty_files?: unknown;
  unpushed_commits?: unknown;
  merged?: unknown;
  default_ref?: unknown;
  reason?: unknown;
}

function counted(count: number, singular: string, plural: string): string {
  return `${count} ${count === 1 ? singular : plural}`;
}

export function worktreeKeptNote(raw: string | undefined): string | null {
  if (!raw) return null;
  let parsed: WorktreeKeptInspection;
  try {
    parsed = JSON.parse(raw) as WorktreeKeptInspection;
  } catch {
    return null;
  }
  if (parsed === null || typeof parsed !== "object") return null;

  if (parsed.reason === "in_use") {
    return "Worktree kept — another session is still using it.";
  }
  if (parsed.reason === "host_offline") {
    return "Worktree kept — its host is offline, so it couldn't be checked.";
  }
  if (parsed.reason === "unknown") {
    return "Worktree kept — Omnigent couldn't verify it was safe to remove.";
  }

  const reasons: string[] = [];
  if (typeof parsed.dirty_files === "number" && parsed.dirty_files > 0) {
    reasons.push(counted(parsed.dirty_files, "uncommitted change", "uncommitted changes"));
  }
  if (typeof parsed.unpushed_commits === "number" && parsed.unpushed_commits > 0) {
    reasons.push(counted(parsed.unpushed_commits, "unpushed commit", "unpushed commits"));
  }
  if (parsed.merged === false) {
    const ref =
      typeof parsed.default_ref === "string" && parsed.default_ref !== ""
        ? ` into ${parsed.default_ref}`
        : "";
    reasons.push(`branch not merged${ref}`);
  } else if (parsed.merged === null) {
    reasons.push("merge status couldn't be determined");
  }
  if (reasons.length === 0) return null;
  return `Worktree kept — ${reasons.join(", ")}.`;
}
