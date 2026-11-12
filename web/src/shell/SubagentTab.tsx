import { useQueries } from "@tanstack/react-query";
import { BotIcon, Maximize2Icon } from "lucide-react";
import { SideChatPane } from "@/components/chat/SideChatPane";
import { useCanEdit } from "@/hooks/usePermissions";
import { sessionQueryOptions, useSession } from "@/hooks/useSession";
import { Link, useLocation } from "@/lib/routing";
import { readOnlyReasonForSessionLabels } from "@/lib/sessionCapabilities";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import { subagentSessionIdFromTab } from "@/lib/subagentTabs";
import { subAgentComposerLabel } from "./subagentLabel";

/** Shown until the sub-agent's session snapshot loads. */
export const SUBAGENT_TAB_FALLBACK_LABEL = "Sub-agent";

/**
 * Labels for the sub-agent tabs among `tabIds`, keyed by tab id. Reads the
 * shared session cache, so an already-loaded sub-agent needs no new request.
 */
export function useSubagentTabLabels(tabIds: readonly string[]): Record<string, string> {
  const subagentTabs = tabIds.flatMap((tabId) => {
    const sessionId = subagentSessionIdFromTab(tabId);
    return sessionId === null ? [] : [{ tabId, sessionId }];
  });
  const snapshots = useQueries({
    queries: subagentTabs.map(({ sessionId }) => sessionQueryOptions(sessionId)),
  });
  const labels: Record<string, string> = {};
  subagentTabs.forEach(({ tabId }, index) => {
    labels[tabId] =
      subAgentComposerLabel(snapshots[index]?.data ?? null) ?? SUBAGENT_TAB_FALLBACK_LABEL;
  });
  return labels;
}

/**
 * A sub-agent's full conversation in the parent's Workspace panel. Read-only
 * without edit access, until its snapshot loads, or when the full-screen chat
 * would be (e.g. a Claude Code sub-agent).
 */
export function SubagentTabPane({ sessionId, label }: { sessionId: string; label: string }) {
  const canEdit = useCanEdit(sessionId);
  const { session } = useSession(sessionId);
  const readOnlyReason = readOnlyReasonForSessionLabels(session, null);
  // The labels that decide read-only arrive with the snapshot, so don't offer a composer first.
  const readOnly = !canEdit || session === null || readOnlyReason !== null;
  // Open on Agents, as the transcript's sub-agent links do, so the page remembers that tab.
  const params = new URLSearchParams(sessionNavigationSearch(useLocation().search));
  params.set("panel", "agents");
  return (
    <div data-testid="subagent-tab-pane" className="flex h-full min-h-0 flex-1 flex-col">
      <div className="flex h-9 shrink-0 items-center gap-2 border-border border-b px-3">
        <BotIcon aria-hidden="true" className="size-4 shrink-0 text-muted-foreground" />
        <span className="min-w-0 flex-1 truncate text-sm font-medium">{label}</span>
        <Link
          to={{ pathname: `/c/${sessionId}`, search: `?${params.toString()}` }}
          data-testid="subagent-tab-open-full"
          componentId="workspace.subagent_tab.open_full_screen"
          className="flex shrink-0 items-center gap-1 rounded px-1.5 py-0.5 text-xs text-muted-foreground hover:bg-muted hover:text-foreground"
        >
          <Maximize2Icon aria-hidden="true" className="size-3" />
          Open full screen
        </Link>
      </div>
      <div className="min-h-0 flex-1">
        <SideChatPane key={sessionId} childId={sessionId} fullSession readOnly={readOnly} />
      </div>
      {readOnlyReason !== null && (
        <p
          data-testid="subagent-tab-read-only"
          className="mx-3 mb-5 shrink-0 rounded-md border border-border bg-muted/40 px-3 py-2 text-center text-sm text-muted-foreground"
        >
          {readOnlyReason}
        </p>
      )}
    </div>
  );
}
