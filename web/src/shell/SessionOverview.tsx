import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ChartNoAxesColumnIcon, ChevronDownIcon, ChevronRightIcon, XIcon } from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import { Dialog, DialogContent, DialogTitle, DialogTrigger } from "@/components/ui/dialog";
import { ModelUsageBreakdown } from "@/components/ModelUsageBreakdown";
import { RunningDot } from "@/components/RunningDot";
import { useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { useIsMobileViewport } from "@/hooks/useIsMobileViewport";
import type { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { formatSessionCostUsd, formatTokenCount } from "@/lib/formatCost";
import { Link, useLocation } from "@/lib/routing";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import { useChatStore } from "@/store/chatStore";
import { childPrimaryLabel } from "./subagentLabel";
import { activityDotClassName, childStatus } from "./subagentStatus";
import { useOpenSubagentInPanel } from "./subagentPanelOpener";
import { SubagentCost } from "./SubagentCost";

const HIDDEN_STORAGE_KEY = "omnigent:session-overview-hidden";

function UsageValue({
  value,
  loading,
  label,
}: {
  value: string | null;
  loading: boolean;
  label: string;
}) {
  if (value !== null) return value;
  if (loading) return <Spinner className="size-3.5" aria-label={`Loading ${label}`} />;
  return "Unavailable";
}

function OverviewSubagent({
  child,
  onNavigate,
}: {
  child: ChildSessionInfo;
  onNavigate?: () => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const openInPanel = useOpenSubagentInPanel();
  const location = useLocation();
  const label = childPrimaryLabel(child);
  const status = childStatus(child);
  return (
    <li>
      <div className="flex items-center gap-1">
        <Button
          variant="ghost"
          size="icon-xs"
          className="shrink-0 max-md:size-11"
          aria-expanded={expanded}
          aria-label={`${expanded ? "Hide" : "Show"} nested costs for ${label}`}
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? (
            <ChevronDownIcon className="size-3.5" />
          ) : (
            <ChevronRightIcon className="size-3.5" />
          )}
        </Button>
        <Link
          to={{ pathname: `/c/${child.id}`, search: sessionNavigationSearch(location.search) }}
          onClick={(event) => {
            openInPanel(child.id, event);
            onNavigate?.();
          }}
          className="flex min-w-0 flex-1 items-center gap-2 rounded-md p-1 hover:bg-accent focus-visible:outline-ring"
        >
          {status.activity === "working" ? (
            <RunningDot />
          ) : (
            <span
              className={`size-2 shrink-0 rounded-full ${status.activity === "awaiting" ? "bg-warning" : activityDotClassName(status.activity)}`}
            />
          )}
          <span className="min-w-0 flex-1">
            <span className="block truncate" title={label}>
              {label}
            </span>
            <span className="block text-xs text-muted-foreground">{status.label}</span>
          </span>
          <SubagentCost costUsd={child.own_cost_usd} />
        </Link>
      </div>
      {expanded && (
        <div className="ml-2 border-l border-border/50 pl-2 pt-2">
          <OverviewSubagents sessionId={child.id} onNavigate={onNavigate} />
        </div>
      )}
    </li>
  );
}

function OverviewSubagents({
  sessionId,
  onNavigate,
}: {
  sessionId: string;
  onNavigate?: () => void;
}) {
  const [showAll, setShowAll] = useState(false);
  // Nested queries mount only when expanded; closing a branch stops its polling.
  const { children, isLoading, error } = useChildSessions(sessionId, 10_000);
  if (isLoading && children.length === 0) return <Spinner aria-label="Loading subagent costs" />;
  return (
    <>
      {error && <p className="text-muted-foreground">Couldn’t refresh subagents.</p>}
      {!error && children.length === 0 && <p className="text-muted-foreground">No subagents yet</p>}
      <ul className="space-y-2">
        {(showAll ? children : children.slice(0, 5)).map((child) => (
          <OverviewSubagent key={child.id} child={child} onNavigate={onNavigate} />
        ))}
      </ul>
      {children.length > 5 && (
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          onClick={() => setShowAll(!showAll)}
        >
          {showAll ? "Show fewer subagents" : `Show all ${children.length} subagents`}
        </Button>
      )}
    </>
  );
}

interface OverviewContentProps {
  sessionId: string;
  title: string;
  changedFiles: Pick<ReturnType<typeof useWorkspaceChangedFiles>, "data" | "isError">;
  workspaceUnavailable?: boolean;
  showChanges: boolean;
  onOpenChanges: () => void;
  onOpenAgents: () => void;
  onNavigate?: () => void;
}

function OverviewContent({
  sessionId,
  title,
  changedFiles,
  showChanges,
  workspaceUnavailable = false,
  onOpenChanges,
  onOpenAgents,
  onNavigate,
  usageLoading,
}: OverviewContentProps & { usageLoading: boolean }) {
  const cost = useChatStore((s) => (s.conversationId === sessionId ? s.sessionCostUsd : null));
  const usage = useChatStore((s) =>
    s.conversationId === sessionId ? s.sessionUsageByModel : null,
  );
  const models = Object.values(usage ?? {});
  const tokens =
    models.length > 0 && models.every((model) => model.totalTokens != null)
      ? models.reduce((total, model) => total + model.totalTokens!, 0)
      : null;
  const files = changedFiles.data?.data;
  const counts = files?.reduce(
    (total, file) => ({
      added: total.added + (file.lines_added ?? 0),
      removed: total.removed + (file.lines_removed ?? 0),
      unknown: total.unknown + (file.lines_added == null || file.lines_removed == null ? 1 : 0),
    }),
    { added: 0, removed: 0, unknown: 0 },
  );

  return (
    <div className="flex flex-col divide-y divide-border/50 text-sm">
      <section className="space-y-2 pb-4">
        <p className="truncate text-xs text-muted-foreground" title={title}>
          {title}
        </p>
        <div className="flex items-baseline justify-between gap-3">
          <span>Session total</span>
          <span className="font-medium tabular-nums">
            <UsageValue
              value={cost === null ? null : formatSessionCostUsd(cost)}
              loading={usageLoading}
              label="session cost"
            />
          </span>
        </div>
        <p className="text-xs text-muted-foreground">Includes subagents</p>
        <div className="flex justify-between gap-3">
          <span>Tokens</span>
          <span className="tabular-nums">
            <UsageValue
              value={tokens === null ? null : formatTokenCount(tokens)}
              loading={usageLoading}
              label="tokens"
            />
          </span>
        </div>
        {usage && models.length > 0 && (
          <ModelUsageBreakdown usageByModel={usage} label="Usage details" />
        )}
      </section>
      <section className="space-y-2 py-4">
        <h3 className="font-medium">Subagents</h3>
        <p className="text-xs text-muted-foreground">Each agent’s own cost</p>
        <OverviewSubagents sessionId={sessionId} onNavigate={onNavigate} />
        <Button
          variant="link"
          size="sm"
          className="h-auto p-0 text-xs"
          onClick={() => {
            onNavigate?.();
            onOpenAgents();
          }}
        >
          View all agents →
        </Button>
      </section>
      {showChanges && (
        <section className="space-y-2 pt-4">
          <h3 className="font-medium">Workspace changes</h3>
          {workspaceUnavailable ||
          changedFiles.isError ||
          changedFiles.data?.available === false ? (
            <p className="text-muted-foreground">Changes unavailable</p>
          ) : !files || !counts ? (
            <p className="text-muted-foreground">Loading changes…</p>
          ) : (
            <>
              <div className="flex justify-between gap-3">
                <span>
                  {files.length} {files.length === 1 ? "file" : "files"} changed
                </span>
                {counts.unknown < files.length || files.length === 0 ? (
                  <span
                    className="tabular-nums"
                    aria-label={`${counts.added} lines added, ${counts.removed} lines removed`}
                  >
                    <span className="text-green-600 dark:text-green-400">+{counts.added}</span>{" "}
                    <span className="text-destructive">−{counts.removed}</span>
                  </span>
                ) : (
                  <span className="text-muted-foreground">Lines unavailable</span>
                )}
              </div>
              {counts.unknown > 0 && (
                <p className="text-xs text-muted-foreground">
                  Line counts unavailable for {counts.unknown}{" "}
                  {counts.unknown === 1 ? "file" : "files"}.
                </p>
              )}
              <p className="text-xs text-muted-foreground">Compared with HEAD</p>
            </>
          )}
          <Button
            variant="link"
            size="sm"
            className="h-auto p-0 text-xs"
            onClick={() => {
              onNavigate?.();
              onOpenChanges();
            }}
          >
            View changes →
          </Button>
        </section>
      )}
    </div>
  );
}

interface SessionOverviewProps extends OverviewContentProps {
  dockTarget: HTMLElement | null;
  chatWidthMeasure: HTMLElement | null;
  dockAllowed: boolean;
}

/** One overview, docked beside a roomy chat or opened from its header. */
export function SessionOverview({
  dockTarget,
  chatWidthMeasure,
  dockAllowed,
  ...contentProps
}: SessionOverviewProps) {
  const mobile = useIsMobileViewport();
  const [hasRoom, setHasRoom] = useState(false);
  const [hidden, setHidden] = useState(() => {
    try {
      return localStorage.getItem(HIDDEN_STORAGE_KEY) === "true";
    } catch {
      return false;
    }
  });
  const [overlayOpen, setOverlayOpen] = useState(false);
  const triggerRef = useRef<HTMLButtonElement>(null);
  const cost = useChatStore((s) =>
    s.conversationId === contentProps.sessionId ? s.sessionCostUsd : null,
  );
  const usageLoading = useChatStore(
    (s) =>
      s.conversationId !== contentProps.sessionId || s.loadingConversation || s.loadingSessionUsage,
  );
  const costLabel = cost === null ? "unavailable" : formatSessionCostUsd(cost);
  const canDock = dockAllowed && !mobile && hasRoom;
  const docked = canDock && !hidden;

  useLayoutEffect(() => {
    const container = dockTarget?.parentElement;
    if (!container || !chatWidthMeasure) return;
    const measure = () =>
      setHasRoom(
        container.getBoundingClientRect().width >=
          chatWidthMeasure.getBoundingClientRect().width + 280,
      );
    measure();
    const observer = new ResizeObserver(measure);
    observer.observe(container);
    observer.observe(chatWidthMeasure);
    return () => observer.disconnect();
  }, [dockTarget, chatWidthMeasure]);

  useEffect(() => {
    if (canDock) setOverlayOpen(false);
  }, [canDock]);

  function setOverviewHidden(next: boolean) {
    setHidden(next);
    try {
      localStorage.setItem(HIDDEN_STORAGE_KEY, String(next));
    } catch {
      /* Storage may be disabled. */
    }
  }

  const trigger = (
    <Button
      ref={triggerRef}
      variant="ghost"
      size="sm"
      className="shrink-0 gap-1.5 px-2 text-muted-foreground max-md:min-h-11"
      aria-label={`Session overview, cost ${cost === null && usageLoading ? "loading" : costLabel}`}
      aria-expanded={docked || overlayOpen}
      title="Session overview"
    >
      <ChartNoAxesColumnIcon className="size-4" />
      <span className="tabular-nums">
        <UsageValue
          value={cost === null ? null : costLabel}
          loading={usageLoading}
          label="session cost"
        />
      </span>
    </Button>
  );
  const content = (
    <OverviewContent
      {...contentProps}
      usageLoading={usageLoading}
      onNavigate={() => setOverlayOpen(false)}
    />
  );
  return (
    <>
      {mobile ? (
        <Dialog open={overlayOpen} onOpenChange={setOverlayOpen}>
          <DialogTrigger asChild>{trigger}</DialogTrigger>
          <DialogContent
            aria-describedby={undefined}
            style={{
              top: "var(--omnigent-safe-top, 0px)",
              maxHeight:
                "calc(var(--omnigent-viewport-height, 100dvh) - var(--omnigent-safe-top, 0px) - var(--omnigent-safe-bottom, 0px))",
            }}
            className="max-md:inset-0 max-md:h-dvh max-md:w-screen max-md:max-w-none max-md:translate-x-0 max-md:translate-y-0 max-md:rounded-none flex flex-col overflow-y-auto"
          >
            <DialogTitle>Session overview</DialogTitle>
            {content}
          </DialogContent>
        </Dialog>
      ) : (
        <Popover
          open={overlayOpen && !canDock}
          onOpenChange={(open) => (canDock ? setOverviewHidden(!hidden) : setOverlayOpen(open))}
        >
          <PopoverTrigger asChild>{trigger}</PopoverTrigger>
          <PopoverContent
            align="end"
            className="w-72 max-h-[80dvh] overflow-y-auto p-4"
            aria-label="Session overview"
          >
            <h2 className="font-medium">Session overview</h2>
            {content}
          </PopoverContent>
        </Popover>
      )}
      {docked &&
        dockTarget &&
        createPortal(
          <aside aria-label="Session overview" className="w-[280px] shrink-0 px-3 pt-16 pb-4">
            <div className="max-h-[calc(100dvh-5rem)] overflow-y-auto rounded-xl border border-border/60 bg-card/60 p-4">
              <div className="mb-3 flex items-center justify-between gap-2">
                <h2 className="text-sm font-medium">Session overview</h2>
                <Button
                  variant="ghost"
                  size="icon-xs"
                  aria-label="Hide session overview"
                  onClick={() => {
                    setOverviewHidden(true);
                    triggerRef.current?.focus();
                  }}
                >
                  <XIcon className="size-3.5" />
                </Button>
              </div>
              {content}
            </div>
          </aside>,
          dockTarget,
        )}
    </>
  );
}
