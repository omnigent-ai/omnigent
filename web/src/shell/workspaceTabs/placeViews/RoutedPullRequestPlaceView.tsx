// The new pull request page, behind Pull Requests v2: loaded on its own only once the flag is
// on, so the page's code never reaches a client that has it off.

import type { ComponentType, ReactNode } from "react";

import { usePullRequestPageRoute } from "@/pullRequestPage/pullRequestPageRoute";

import type { PullRequestPlace } from "./placeViewActions";
import { PullRequestPlaceView as NewPullRequestPlaceView } from "./PullRequestPlaceView";
import type { WorkspacePlaceViewProps } from "./WorkspacePlaceView";

type PullRequestPlaceViewProps = WorkspacePlaceViewProps & { place: PullRequestPlace };

/**
 * The new page where the session's route serves the PR methods, else `classic`; `pending` while
 * the route is asked, so neither flashes.
 */
export default function RoutedPullRequestPlaceView({
  classic: ClassicPullRequestPlaceView,
  pending,
  ...props
}: PullRequestPlaceViewProps & {
  classic: ComponentType<PullRequestPlaceViewProps>;
  pending: ReactNode;
}) {
  const route = usePullRequestPageRoute(props.conversationId);
  if (route === "loading") return pending;
  return route ? (
    <NewPullRequestPlaceView {...props} />
  ) : (
    <ClassicPullRequestPlaceView {...props} />
  );
}
