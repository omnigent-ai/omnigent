// One pull request as a workspace tab's page: the panel's own details for that pull
// request, with the tab's place owning which section shows, so Back and Forward step it.

import { useQueryClient } from "@tanstack/react-query";
import { RefreshCwIcon } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { usePullRequestInfo } from "@/hooks/usePullRequests";
import { cn } from "@/lib/utils";
import {
  PullRequestPanelDetails,
  type PullRequestSubtab,
  refreshSessionPullRequests,
} from "@/shell/PullRequestPanel";

import { useStoredPullRequestFallback } from "./useStoredPullRequestFallback";

/** The sections a pull request's tab can show; Commits shows in Changes, its list's home. */
export type PullRequestPageSection = PullRequestSubtab | "commits";

export function pullRequestPageTab(section: PullRequestPageSection): PullRequestSubtab {
  return section === "commits" ? "changes" : section;
}

export interface PullRequestPageBodyProps {
  conversationId: string;
  prUrl: string;
  section: PullRequestPageSection;
  onSectionChange: (section: PullRequestSubtab) => void;
  /** The tab is selected and the sidebar is open: only then does it poll. */
  active: boolean;
}

export function PullRequestPageBody({
  conversationId,
  prUrl,
  section,
  onSectionChange,
  active,
}: PullRequestPageBodyProps) {
  const queryClient = useQueryClient();
  const info = usePullRequestInfo(conversationId, { poll: active, prUrl });
  // Offline, the tab still shows the PR as the server stored it, as the panel does.
  const { storedPr, notSynced } = useStoredPullRequestFallback(conversationId, info, prUrl);
  const [refreshing, setRefreshing] = useState(false);
  const refresh = async () => {
    setRefreshing(true);
    try {
      await refreshSessionPullRequests(queryClient, conversationId);
    } finally {
      setRefreshing(false);
    }
  };
  const tab = pullRequestPageTab(section);
  return (
    <div className="flex min-h-0 flex-1 flex-col" data-testid="pull-request-page">
      <PullRequestPanelDetails
        conversationId={conversationId}
        info={info}
        storedPr={storedPr}
        notSynced={notSynced}
        active={active}
        showHeading={false}
        activeTab={tab}
        // Tabs report a change twice; only a real one is a step.
        setActiveTab={(next) => {
          if (next !== tab) onSectionChange(next);
        }}
        refreshButton={
          <Button size="sm" variant="ghost" disabled={refreshing} onClick={refresh}>
            <RefreshCwIcon
              className={cn("size-3.5", refreshing && "animate-spin")}
              aria-hidden="true"
            />
            Refresh
          </Button>
        }
      />
    </div>
  );
}
