// The width of the list beside a pull request: the person drags its edge or uses the arrow
// keys, within limits that keep both the list and the pull request readable.

import type { RefObject } from "react";

import {
  clampPaneWidth,
  resetRememberedPaneWidthForTesting,
  useRememberedPaneWidth,
  type PaneWidthSpec,
} from "@/hooks/useRememberedPaneWidth";

export const PULL_REQUEST_LIST_DEFAULT_WIDTH = 320;
export const PULL_REQUEST_LIST_MIN_WIDTH = 240;
/** The widest before the tab is measured; a measured tab lets the list grow with it. */
export const PULL_REQUEST_LIST_MAX_WIDTH = 520;
/** The pull request beside the list always keeps at least this much. */
export const PULL_REQUEST_DETAILS_MIN_WIDTH = 320;
/** And at least this share of a wide window, so a wide list never crowds it. */
export const PULL_REQUEST_DETAILS_MIN_SHARE = 0.4;

/** The widest a list may be beside a pull request in a container this wide. */
export function pullRequestListRoomIn(containerWidth: number): number {
  return Math.floor(
    containerWidth -
      Math.max(PULL_REQUEST_DETAILS_MIN_WIDTH, containerWidth * PULL_REQUEST_DETAILS_MIN_SHARE),
  );
}
/**
 * Below this width the pull request takes the whole space, and its way back is the list: the
 * list's narrowest plus the least the pull request beside it keeps.
 */
export const PULL_REQUEST_LIST_BESIDE_MIN_WIDTH =
  PULL_REQUEST_LIST_MIN_WIDTH + PULL_REQUEST_DETAILS_MIN_WIDTH;

const PULL_REQUEST_LIST_WIDTH: PaneWidthSpec = {
  preference: "pullRequestListWidthPx",
  defaultWidth: PULL_REQUEST_LIST_DEFAULT_WIDTH,
  minWidth: PULL_REQUEST_LIST_MIN_WIDTH,
  maxWidth: PULL_REQUEST_LIST_MAX_WIDTH,
  roomIn: pullRequestListRoomIn,
  growsWithRoom: true,
  label: "Resize the pull request list",
  keyboardStep: 20,
};

/** `width` within the list's limits, leaving the pull request its share of `containerWidth`. */
export function clampPullRequestListWidth(width: number, containerWidth: number | null): number {
  return clampPaneWidth(PULL_REQUEST_LIST_WIDTH, width, containerWidth);
}

/** Reload the shared choice from storage. Only for tests. */
export function resetPullRequestListWidthStoreForTesting(): void {
  resetRememberedPaneWidthForTesting("pullRequestListWidthPx");
}

/**
 * The list's width in `container`, whose left edge the list starts at, and the props for its
 * resize handle. The chosen width is remembered on this device; a narrower tab shows less of
 * it until there's room again.
 */
export function usePullRequestListWidth(
  container: RefObject<HTMLElement | null>,
  containerWidth: number | null,
) {
  return useRememberedPaneWidth(PULL_REQUEST_LIST_WIDTH, container, containerWidth);
}
