// A pull request panel shown in a sidebar, which a sidebar toggle shouldn't hold up.

import { type ComponentProps, memo, useDeferredValue } from "react";

import { PullRequestPanel } from "../newPullRequestPage/shell/PullRequestPanel";

type PullRequestPanelProps = ComponentProps<typeof PullRequestPanel>;

const MemoizedPullRequestPanel = memo(PullRequestPanel);

/**
 * Showing or hiding the sidebar only flips `active`, which the panel takes after the sidebar's
 * frame: a long review takes tens of milliseconds to re-render. Its other props must be stable.
 */
export const SidebarPullRequestPanel = memo(function SidebarPullRequestPanel({
  active = true,
  ...props
}: PullRequestPanelProps) {
  return <MemoizedPullRequestPanel {...props} active={useDeferredValue(active)} />;
});
