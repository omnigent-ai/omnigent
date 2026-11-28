// What clicks inside a Files, Changes or pull request tab do to the workspace:
// move this tab (an in-tab step) or open another place (a tab switch). Pure, so
// each rule is tested without the views; the views only run the result.

import type { OpenOptions } from "../planOpen";
import type { ComparisonChoice } from "@/lib/comparisonChoice";
import type { FilePosition } from "@/shell/FileViewerContext";

import { pullRequestTabPlace } from "../pullRequestSubpage";
import type { LineRange, WorkspaceLink, WorkspacePlace } from "../workspacePlace";

export type FilesPlace = Extract<WorkspacePlace, { kind: "files" }>;
export type ChangesPlace = Extract<WorkspacePlace, { kind: "changes" }>;
export type PullRequestPlace = Extract<WorkspacePlace, { kind: "pulls" | "pull" }>;

export type PlaceViewAction =
  | { type: "navigate"; place: WorkspacePlace; replace: boolean }
  | { type: "open"; link: WorkspaceLink; options: OpenOptions }
  | { type: "none" };

const NONE: PlaceViewAction = { type: "none" };
const USER: OpenOptions = { opener: "user" };

/** A file chosen in the Files tab's own tree: shown there, or in a new tab when asked. */
export function treeFileChosen(path: string, options: { newTab?: boolean } = {}): PlaceViewAction {
  return {
    type: "open",
    link: { place: { kind: "files", path } },
    options: options.newTab
      ? { opener: "user", disposition: "new" }
      : { opener: "user", origin: "files-tree" },
  };
}

/** A file picked from a file tab's breadcrumbs: that tab steps to it. */
export function fileTabFileChosen(current: FilesPlace, path: string): PlaceViewAction {
  if (path === current.path) return NONE;
  return { type: "navigate", place: { kind: "files", path }, replace: false };
}

/**
 * The file view asked for its diff ("Show changes") or another file. A citation it hasn't
 * shown yet, as a diff link's, goes along to the review; one the reader moved on from doesn't.
 */
export function fileViewDiffRequested(
  current: FilesPlace,
  active: boolean,
  path: string,
  pendingCitation?: FilePosition,
): PlaceViewAction {
  if (active) {
    const line =
      path === current.path && pendingCitation ? lineRangeOf(pendingCitation) : undefined;
    const place: ChangesPlace = { kind: "changes", path, ...(line ? { line } : {}) };
    return { type: "open", link: { place }, options: USER };
  }
  if (path === current.path) return NONE;
  return { type: "open", link: { place: { kind: "files", path } }, options: USER };
}

function lineRangeOf(position: FilePosition): LineRange {
  return {
    start: position.line,
    ...(position.endLine !== undefined ? { end: position.endLine } : {}),
    ...(position.column !== undefined ? { column: position.column } : {}),
  };
}

/** Changes review's View File: the file itself in a Files tab, reusing one that shows it. */
export function changedFileViewed(path: string): PlaceViewAction {
  return { type: "open", link: { place: { kind: "files", path } }, options: USER };
}

/**
 * The comparison the session shows, kept on the visit with the folder whose review shows it
 * (`root`, null for the workspace): a step when the person picked it.
 */
export function changesComparisonShown(
  current: ChangesPlace,
  comparison: ComparisonChoice,
  { step, root }: { step: boolean; root: string | null },
): PlaceViewAction {
  // The recorded comparison replaces a link's range, which would otherwise outlive it.
  const { range: _range, comparisonRoot: _root, ...rest } = current;
  const place: ChangesPlace = { ...rest, comparison };
  if (root !== null) place.comparisonRoot = root;
  return { type: "navigate", place, replace: !step };
}

/** Leaves a comparison a link asked for, for the session's own, keeping the file. */
export function changesDefaultComparison(current: ChangesPlace): PlaceViewAction {
  if (!current.range) return NONE;
  const place: ChangesPlace = { kind: "changes" };
  if (current.path !== undefined) place.path = current.path;
  return { type: "navigate", place, replace: false };
}

/**
 * A changed file asked for in a tab of its own, as a middle click or ⌘Enter on its
 * review row asks: a Diffs tab named for that file, reusing one only if it shows
 * exactly that, behind this tab unless the gesture added Shift.
 */
export function changesFileInNewTab(
  current: ChangesPlace,
  path: string,
  { background }: { background: boolean },
): PlaceViewAction {
  const place: ChangesPlace = { kind: "changes", path, fileTab: true };
  if (current.range) place.range = current.range;
  if (current.comparison) place.comparison = current.comparison;
  if (current.comparisonRoot !== undefined) place.comparisonRoot = current.comparisonRoot;
  return {
    type: "open",
    link: { place },
    options: {
      opener: "user",
      disposition: "focus",
      reuse: "same-place",
      keepSelection: background,
    },
  };
}

/** Back from a file's diff to the changes overview. */
export function changesOverview(current: ChangesPlace): PlaceViewAction {
  if (current.path === undefined) return NONE;
  return {
    type: "navigate",
    place: {
      kind: "changes",
      ...(current.range ? { range: current.range } : {}),
      ...(current.comparison ? { comparison: current.comparison } : {}),
      ...(current.comparisonRoot !== undefined ? { comparisonRoot: current.comparisonRoot } : {}),
    },
    replace: false,
  };
}

/** A section picked on a pull request's page: a step in its tab, unless it shows already. */
/** A pull request chosen in the pull requests tab's list: a step to it, so Back returns. */
export function pullRequestChosen(
  url: string,
  section?: Extract<PullRequestPlace, { kind: "pull" }>["view"],
): PlaceViewAction {
  return { type: "navigate", place: pullRequestTabPlace(url, section), replace: false };
}

/** The session's pull request list, asked for from one pull request: its own tab, shown. */
export function allPullRequestsChosen(): PlaceViewAction {
  return {
    type: "open",
    link: { place: { kind: "pulls" } },
    options: { opener: "user", disposition: "focus" },
  };
}

export function pullRequestSectionChosen(
  current: Extract<PullRequestPlace, { kind: "pull" }>,
  section: Extract<PullRequestPlace, { kind: "pull" }>["view"],
): PlaceViewAction {
  if (section === current.view) return NONE;
  return {
    type: "navigate",
    place: { kind: "pull", url: current.url, view: section },
    replace: false,
  };
}

const SECTION_NAMES = { summary: "summary", comments: "comments" } as const;

/**
 * Parts of a pull request place its page can't show: a file or line in its changes, one
 * commit, or the checks. The tab names them and what it shows instead, rather than quietly
 * showing something else.
 */
export function unshownPullRequestParts(place: PullRequestPlace): string | null {
  if (place.kind !== "pull") return null;
  const parts: string[] = [];
  if (place.path !== undefined) {
    parts.push(place.line ? `${place.path} line ${place.line.start}` : place.path);
  } else if (place.line) {
    parts.push(`line ${place.line.start}`);
  }
  if (place.commit !== undefined) parts.push(`commit ${place.commit.slice(0, 7)}`);
  if (place.focus === "checks") parts.push("the checks");
  if (parts.length === 0) return null;
  const named =
    parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
  const shown =
    place.view === "changes" || place.view === "commits"
      ? "all of the pull request's changes"
      : `the pull request's ${SECTION_NAMES[place.view]}`;
  return `This link points at ${named}, which this tab can't open yet. It shows ${shown} instead.`;
}

/** The place without the parts its page can't show, which is what the tab shows. */
export function shownPullRequestPlace(place: PullRequestPlace): PullRequestPlace {
  if (place.kind !== "pull") return place;
  return { kind: "pull", url: place.url, view: place.view };
}
