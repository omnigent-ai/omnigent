// What a step inside the new PR page's tab does: move this tab or open another place.

import {
  type PullRequestNewTabTarget,
  type PullRequestPanelView,
  type PullRequestSubtab,
  viewForChosenPullRequest,
} from "@/shell/pullRequestPanelView";

import type { WorkspacePlace } from "../workspacePlace";
import type { PlaceViewAction, PullRequestPlace } from "./placeViewActions";

const NONE: PlaceViewAction = { type: "none" };

/** The pull request panel's view for a place. */
export function pullRequestPanelViewFor(place: PullRequestPlace): PullRequestPanelView {
  if (place.kind === "pulls") return { showingDetails: false, subtab: "summary" };
  const view: PullRequestPanelView = {
    selected: place.url,
    showingDetails: true,
    subtab: place.view,
  };
  if (place.commit !== undefined && place.view === "commits") view.commit = place.commit;
  if (showsFileCursor(place)) {
    view.file = { path: place.path! };
  }
  return view;
}

/** A file cursor the Changes tab shows: a path in the pull request's own changes. */
function showsFileCursor(place: PullRequestPlace): boolean {
  return (
    place.kind === "pull" &&
    place.view === "changes" &&
    place.path !== undefined &&
    place.commit === undefined
  );
}

/** The place the panel's view shows; a list view is the pull requests overview. */
export function pullRequestPlaceFor(view: PullRequestPanelView): PullRequestPlace {
  if (!view.showingDetails || !view.selected) return { kind: "pulls" };
  const place: PullRequestPlace = { kind: "pull", url: view.selected, view: view.subtab };
  if (view.subtab === "commits" && view.commit !== undefined) place.commit = view.commit;
  if (view.subtab === "changes" && view.file) place.path = view.file.path;
  return place;
}

/** The place a commit or file of `url`'s pull request shows in a tab of its own. */
export function pullRequestNewTabPlace(
  url: string,
  target: PullRequestNewTabTarget,
): PullRequestPlace {
  return "commit" in target
    ? { kind: "pull", url, view: "commits", commit: target.commit }
    : { kind: "pull", url, view: "changes", path: target.path };
}

/**
 * The panel's view changed: move this tab within what it shows (a sub-view,
 * commit or file of its pull request) unless that is already shown. A tab never
 * becomes another pull request, or the overview, this way; those open their own tab.
 */
export function pullRequestViewChanged(
  current: PullRequestPlace,
  view: PullRequestPanelView,
  replace: boolean,
  isSamePlace: (left: WorkspacePlace, right: WorkspacePlace) => boolean,
): PlaceViewAction {
  const next = withListOf(current, pullRequestPlaceFor(view));
  if (next.kind !== current.kind) return NONE;
  if (next.kind === "pull" && current.kind === "pull" && next.url !== current.url) return NONE;
  // A focus or column in the place has no panel equivalent; keep them while the view matches.
  const comparable =
    current.kind === "pull"
      ? withListOf(current, pullRequestPlaceFor(pullRequestPanelViewFor(current)))
      : current;
  if (isSamePlace(comparable, next)) return NONE;
  return { type: "navigate", place: next, replace };
}

/** `place`, keeping the list beside it when `current` shows one. */
function withListOf(current: PullRequestPlace, place: PullRequestPlace): PullRequestPlace {
  return current.kind === "pull" && current.list && place.kind === "pull"
    ? { ...place, list: true }
    : place;
}

/**
 * The section a pull request chosen from a list opens on: the one the tab shows, or the one it
 * showed last (`lastShown`) while only the list shows.
 */
export function chosenPullRequestSection(
  current: PullRequestPlace,
  lastShown: PullRequestSubtab = "summary",
): PullRequestSubtab {
  return current.kind === "pull" ? current.view : lastShown;
}

/**
 * A pull request chosen in a list that shows pull requests in this tab: the tab steps to it,
 * beside the list, on the same sub-view as the one it leaves so a review moves file to file.
 */
export function listedPullRequestChosen(
  current: PullRequestPlace,
  url: string,
  lastShown?: PullRequestSubtab,
): PlaceViewAction {
  if (current.kind === "pull" && current.url === url) return NONE;
  const chosen = pullRequestPlaceFor(
    viewForChosenPullRequest(url, chosenPullRequestSection(current, lastShown)),
  );
  if (chosen.kind !== "pull") return NONE;
  return { type: "navigate", place: { ...chosen, list: true }, replace: false };
}

/** A pull request's tab opened on its own gains the list beside it, in place. */
export function pullRequestListAdded(current: PullRequestPlace): PlaceViewAction {
  if (current.kind !== "pull" || current.list) return NONE;
  return { type: "navigate", place: { ...current, list: true }, replace: true };
}

/** The list a pull request's tab returns to: all of them, in this tab. */
export function pullRequestListShown(): PlaceViewAction {
  return { type: "navigate", place: { kind: "pulls" }, replace: false };
}

const SUBVIEW_NAMES = {
  summary: "overview",
  comments: "comments",
  commits: "commits",
  changes: "changes",
} as const;

/**
 * Parts of a pull request place the panel can't show yet: a file or line in its
 * changes, the checks section, or one commit outside Commits. The tab names all
 * of them and what it shows instead, rather than quietly showing something else.
 */
export function unshownPullRequestParts(place: PullRequestPlace): string | null {
  if (place.kind !== "pull") return null;
  const parts: string[] = [];
  // The Changes tab opens its file cursor but can't reach a line yet; other files it can't open.
  if (showsFileCursor(place)) {
    if (place.line) parts.push(`line ${place.line.start} of ${place.path}`);
  } else {
    if (place.path !== undefined) {
      parts.push(place.line ? `${place.path} line ${place.line.start}` : place.path);
    } else if (place.line) {
      parts.push(`line ${place.line.start}`);
    }
  }
  if (place.commit !== undefined && place.view !== "commits")
    parts.push(`commit ${place.commit.slice(0, 7)}`);
  if (place.focus === "checks") parts.push("the checks");
  if (parts.length === 0) return null;
  const named =
    parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join(", ")} and ${parts.at(-1)}`;
  const shown = showsFileCursor(place)
    ? `the top of ${place.path}`
    : place.view === "changes"
      ? "all of the pull request's changes"
      : `the pull request's ${SUBVIEW_NAMES[place.view]}`;
  return `This link points at ${named}, which this tab can't open yet. It shows ${shown} instead.`;
}

/** The place without the parts the panel can't show, which is what the tab shows. */
export function shownPullRequestPlace(place: PullRequestPlace): PullRequestPlace {
  if (place.kind !== "pull") return place;
  const shown: PullRequestPlace = { kind: "pull", url: place.url, view: place.view };
  if (place.list) shown.list = true;
  if (place.commit !== undefined && place.view === "commits") shown.commit = place.commit;
  if (showsFileCursor(place)) shown.path = place.path;
  return shown;
}
