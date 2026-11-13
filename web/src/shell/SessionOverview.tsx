// Compact session overview beside chat: what this session has spent and
// changed, and what each sub-agent cost. Configuration and diagnostics stay
// in Agent info.

import { XIcon } from "lucide-react";
import { Card, CardAction, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { RunningDot } from "@/components/RunningDot";
import { Spinner } from "@/components/ui/spinner";
import { useChildSessions } from "@/hooks/useChildSessions";
import { agentCostLabel, agentCostTitle, useAgentCosts } from "@/hooks/useAgentCosts";
import {
  useWorkspaceChangedFiles,
  useWorkspaceEnvironment,
} from "@/hooks/useWorkspaceChangedFiles";
import { formatSessionCostUsd, formatTokenCount } from "@/lib/formatCost";
import { cn } from "@/lib/utils";
import { useChatStore } from "@/store/chatStore";
import { childPrimaryLabel } from "./subagentLabel";
import { activityDotClassName, childStatus } from "./subagentStatus";

const UNAVAILABLE = "Unavailable";
const OPEN_STORAGE_KEY = "omnigent:session-overview-open";

/** Device-local preference for showing the docked overview; defaults to shown. */
export function readSessionOverviewOpen(): boolean {
  try {
    return window.localStorage.getItem(OPEN_STORAGE_KEY) !== "false";
  } catch {
    return true;
  }
}

export function writeSessionOverviewOpen(open: boolean): void {
  try {
    window.localStorage.setItem(OPEN_STORAGE_KEY, open ? "true" : "false");
  } catch {
    // Storage errors only lose the preference.
  }
}

/** The bound session's total (itself plus sub-agents): spinner while loading. */
export function SessionCostValue({ unavailable = UNAVAILABLE }: { unavailable?: string }) {
  const cost = useChatStore((s) => s.sessionCostUsd);
  const loading = useChatStore((s) => s.loadingConversation || s.sessionUsageLoading);
  if (cost !== null) return formatSessionCostUsd(cost);
  return loading ? <Spinner aria-label="Loading cost" className="size-3.5" /> : unavailable;
}

function TokensValue() {
  const models = useChatStore((s) => s.sessionUsageByModel);
  const loading = useChatStore((s) => s.loadingConversation || s.sessionUsageLoading);
  const counts = Object.values(models ?? {}).map((usage) => usage.totalTokens);
  // A model without a token count makes any sum partial, so show none.
  if (counts.length > 0 && counts.every((count) => count !== null)) {
    return formatTokenCount(counts.reduce((sum, count) => sum + count, 0));
  }
  return models === null && loading ? (
    <Spinner aria-label="Loading tokens" className="size-3.5" />
  ) : (
    UNAVAILABLE
  );
}

function ChangedLinesValue({ sessionId }: { sessionId: string }) {
  const environment = useWorkspaceEnvironment(sessionId);
  const { data, isFetching } = useWorkspaceChangedFiles(sessionId);
  if (data?.available) {
    // Binary files carry no line counts; they still count as changed files.
    const added = data.data.reduce((sum, file) => sum + (file.lines_added ?? 0), 0);
    const removed = data.data.reduce((sum, file) => sum + (file.lines_removed ?? 0), 0);
    const files = data.data.length;
    return (
      <>
        +{added} −{removed}
        <span className="text-muted-foreground">
          {" "}
          · {files === 1 ? "1 file" : `${files} files`}
        </span>
      </>
    );
  }
  // The changes read waits on the environment read; neither running means no answer.
  return isFetching || environment.isFetching ? (
    <Spinner aria-label="Loading changes" className="size-3.5" />
  ) : (
    UNAVAILABLE
  );
}

function Row({
  label,
  title,
  children,
}: {
  label: string;
  title?: string;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted-foreground" title={title}>
        {label}
      </dt>
      <dd className="flex items-center font-mono tabular-nums">{children}</dd>
    </div>
  );
}

function SubagentCosts({ sessionId }: { sessionId: string }) {
  const { children, isLoading } = useChildSessions(sessionId);
  const costs = useAgentCosts(sessionId);
  if (isLoading) return <Spinner aria-label="Loading sub-agents" className="size-3.5" />;
  if (children.length === 0) return <p className="text-muted-foreground">No sub-agents</p>;
  return (
    <ul className="flex flex-col gap-1" data-testid="session-overview-subagents">
      {children.map((child) => {
        const { activity, label } = childStatus(child);
        const cost = costs.data?.get(child.id);
        return (
          <li key={child.id} className="flex items-center gap-2" data-child-session-id={child.id}>
            <span title={label} className="flex size-3.5 shrink-0 items-center justify-center">
              {activity === "working" ? (
                <RunningDot className="size-3" />
              ) : (
                <span
                  className={cn(
                    "size-2 rounded-full",
                    activity === "awaiting" ? "bg-warning" : activityDotClassName(activity),
                  )}
                />
              )}
            </span>
            <span className="min-w-0 flex-1 truncate">{childPrimaryLabel(child)}</span>
            <span
              className="shrink-0 font-mono tabular-nums text-muted-foreground"
              title={agentCostTitle(cost)}
            >
              {agentCostLabel(cost) ??
                (costs.isPending ? <Spinner aria-label="Loading cost" className="size-3" /> : "—")}
            </span>
          </li>
        );
      })}
    </ul>
  );
}

const COST_CHIP =
  "h-6 shrink-0 items-center rounded-[6px] px-2 font-mono text-xs tabular-nums text-muted-foreground";

/**
 * Header cost, always visible on desktop. It toggles the overview where the
 * overview can dock (wide windows, workspace collapsed) and is plain text elsewhere.
 */
export function SessionCostButton({
  expanded,
  canToggle,
  onToggle,
}: {
  expanded: boolean;
  canToggle: boolean;
  onToggle: () => void;
}) {
  return (
    <>
      <span
        title="Session cost, including sub-agents"
        data-testid="header-session-cost"
        className={cn(COST_CHIP, "hidden md:inline-flex", canToggle && "xl:hidden")}
      >
        <SessionCostValue unavailable="—" />
      </span>
      {canToggle && (
        <button
          type="button"
          aria-pressed={expanded}
          title={expanded ? "Hide session overview" : "Show session overview"}
          data-testid="header-session-overview-toggle"
          className={cn(COST_CHIP, "hidden hover:bg-muted hover:text-foreground xl:inline-flex")}
          onClick={onToggle}
        >
          <SessionCostValue unavailable="—" />
          <span className="sr-only"> Session overview</span>
        </button>
      )}
    </>
  );
}

/** Operational summary of the bound session, docked beside chat. */
export function SessionOverview({
  sessionId,
  showChangedLines,
  onClose,
}: {
  sessionId: string;
  /** Whether the session has a browsable workspace to count changes in. */
  showChangedLines: boolean;
  onClose: () => void;
}) {
  return (
    <Card size="sm" aria-label="Session overview" data-testid="session-overview">
      <CardHeader>
        <CardTitle className="text-ui">Session overview</CardTitle>
        <CardAction>
          <button
            type="button"
            aria-label="Hide session overview"
            className="cursor-pointer rounded p-1 text-muted-foreground hover:bg-muted hover:text-foreground"
            onClick={onClose}
          >
            <XIcon className="size-3.5" />
          </button>
        </CardAction>
      </CardHeader>
      <CardContent className="flex flex-col gap-4 text-sm">
        <dl className="flex flex-col gap-1.5">
          <Row label="Total cost" title="This session and its sub-agents">
            <SessionCostValue />
          </Row>
          <Row label="Tokens" title="This session and its sub-agents">
            <TokensValue />
          </Row>
          {showChangedLines && (
            <Row label="Changed lines">
              <ChangedLinesValue sessionId={sessionId} />
            </Row>
          )}
        </dl>
        <section className="flex flex-col gap-1.5">
          <h3 className="text-muted-foreground">Sub-agents</h3>
          <SubagentCosts sessionId={sessionId} />
        </section>
      </CardContent>
    </Card>
  );
}
