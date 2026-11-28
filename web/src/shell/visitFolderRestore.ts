// Landing on a Diffs visit (Back, Forward, a link or a page load) brings back the folder whose
// review showed the visit's comparison, before that comparison applies. A comparison or folder
// chosen since the landing wins, so this never pulls a folder back after the person moved on.

import { isWorktreeRoot } from "@/hooks/useHostWorktrees";

/** A tab's landing on a visit, stamped with the session's choice and folder counts then. */
export interface VisitLanding {
  settled: boolean;
  choices: number;
  folders?: number;
}

/** Whether `landing` still asks for its visit: nothing was chosen since it landed. */
export function landingStands(
  landing: VisitLanding | undefined,
  now: { choices: number; folders: number },
): boolean {
  return (
    landing !== undefined &&
    !landing.settled &&
    landing.choices === now.choices &&
    (landing.folders ?? now.folders) === now.folders
  );
}

export interface VisitFolderInput {
  /** The landing still stands. */
  stands: boolean;
  /** The review is on screen. */
  active: boolean;
  /** The visit recorded a comparison, with `visitRoot` the folder that showed it. */
  hasComparison: boolean;
  /** That folder, absolute, or null for the workspace. */
  visitRoot: string | null;
  /** The folder the review shows now, absolute, or null for the workspace. */
  shownRoot: string | null;
  /** The session's directory, which Files shares. */
  directory: string | null;
  /**
   * `shownRoot`, `visitRootShows` and `canChoose` rest on answers: false while a lookup is in
   * flight or failed, so the visit holds rather than being replaced by the workspace's.
   */
  resolved: boolean;
  /** Moving to `visitRoot` would review it: false once its worktree is gone. */
  visitRootShows: boolean;
  /** This viewer may choose the session's folder. */
  canChoose: boolean;
}

/**
 * What a review does about a standing landing's folder: nothing (it shows the visit's folder,
 * or can't move there), `hold` the visit's comparison until it knows, or `restore` the folder.
 */
export type VisitFolderStep =
  { kind: "none" } | { kind: "hold" } | { kind: "restore"; directory: string | null };

const NONE: VisitFolderStep = { kind: "none" };

/** Whether two folders, null for the workspace, are the same, however the host spells them. */
export function sameReviewFolder(left: string | null, right: string | null): boolean {
  return left === null || right === null ? left === right : isWorktreeRoot(left, right);
}

export function visitFolderStep(input: VisitFolderInput): VisitFolderStep {
  if (!input.stands || !input.active || !input.hasComparison) return NONE;
  // Until the worktrees load, a worktree's folder reads as the workspace, so roots can't compare.
  if (!input.resolved) return { kind: "hold" };
  if (sameReviewFolder(input.visitRoot, input.shownRoot)) return NONE;
  // A viewer who can't choose folders keeps what it shows, and so does Files when the visit's
  // worktree is gone or already chosen.
  if (
    !input.canChoose ||
    !input.visitRootShows ||
    sameReviewFolder(input.directory, input.visitRoot)
  ) {
    return NONE;
  }
  return { kind: "restore", directory: input.visitRoot };
}
