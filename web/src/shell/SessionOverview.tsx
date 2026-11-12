import { useEffect, useRef, useState } from "react";
import { Link, useLocation, useNavigate } from "@/lib/routing";
import { XIcon } from "lucide-react";
import { useChatStore } from "@/store/chatStore";
import { MAX_TREE_DEPTH, useChildSessions, type ChildSessionInfo } from "@/hooks/useChildSessions";
import { useWorkspaceChangedFiles } from "@/hooks/useWorkspaceChangedFiles";
import { ModelUsageBreakdown } from "@/components/AgentInfo";
import { Spinner } from "@/components/ui/spinner";
import { formatSessionCostUsd, formatTokenCount } from "@/lib/formatCost";
import { sessionNavigationSearch } from "@/lib/sessionNavigation";
import type { FileNavigationGuard } from "./FileViewerContext";

export function SessionCost({ sessionId }: { sessionId: string }) {
  const id = useChatStore((s) => s.conversationId);
  const cost = useChatStore((s) => s.sessionCostUsd);
  const loading = useChatStore((s) => s.sessionUsageLoading);
  return id !== sessionId || (cost === null && loading) ? (
    <Spinner />
  ) : cost === null ? (
    "Unavailable"
  ) : (
    formatSessionCostUsd(cost)
  );
}

/** Operational summary; navigation commits through the mounted file viewer. */
export function SessionOverview({
  sessionId,
  canBrowseFiles,
  guard,
  onDismiss,
  requested = false,
  onOpenAgents,
  onOpenChanges,
}: {
  sessionId: string;
  requested?: boolean;
  canBrowseFiles: boolean;
  guard: FileNavigationGuard;
  onDismiss: () => void;
  onOpenAgents?: () => void;
  onOpenChanges?: () => void;
}) {
  const region = useRef<HTMLElement>(null);
  useEffect(() => {
    if (requested) region.current?.focus();
  }, [requested]);
  const id = useChatStore((s) => s.conversationId);
  const models = useChatStore((s) => s.sessionUsageByModel);
  const loading = useChatStore((s) => s.sessionUsageLoading);
  const { children, isLoading, error } = useChildSessions(sessionId);
  const files = useWorkspaceChangedFiles(sessionId, { enabled: canBrowseFiles });
  const [showAll, setShowAll] = useState(false);
  const tokenValues =
    id === sessionId
      ? Object.values(models ?? {})
          .map((model) => model.totalTokens)
          .filter((value): value is number => value !== null)
      : [];
  const tokens = tokenValues.length ? tokenValues.reduce((sum, value) => sum + value, 0) : null;
  const changed = files.data?.available ? files.data.data : null;
  const knownLines = changed?.every(
    (file) => file.lines_added !== null && file.lines_removed !== null,
  );
  return (
    <section
      ref={region}
      tabIndex={-1}
      aria-label="Session overview"
      className="flex max-h-full flex-col gap-4 overflow-y-auto rounded-xl border bg-background p-4 text-sm shadow-lg"
      onKeyDown={(event) => {
        if (event.key === "Escape") onDismiss();
      }}
    >
      <div className="flex items-center justify-between gap-3">
        <h2 className="font-medium">Session overview</h2>
        <button type="button" aria-label="Hide session overview" onClick={onDismiss}>
          <XIcon className="size-4" />
        </button>
      </div>
      <dl className="grid grid-cols-2 gap-2">
        <dt>Total cost</dt>
        <dd className="text-right font-mono">
          <SessionCost sessionId={sessionId} />
        </dd>
        <dt>Cumulative tokens</dt>
        <dd className="text-right font-mono">
          {tokens !== null ? (
            formatTokenCount(tokens)
          ) : loading || id !== sessionId ? (
            <Spinner />
          ) : (
            "Unavailable"
          )}
        </dd>
        <dt>Changed lines</dt>
        <dd className="text-right font-mono">
          {changed && knownLines ? (
            `+${changed.reduce((sum, file) => sum + (file.lines_added ?? 0), 0)} −${changed.reduce((sum, file) => sum + (file.lines_removed ?? 0), 0)}`
          ) : files.isLoading && canBrowseFiles ? (
            <Spinner />
          ) : (
            "Unavailable"
          )}
        </dd>
      </dl>
      {canBrowseFiles && onOpenChanges && (
        <button
          type="button"
          className="text-left underline"
          onClick={() => guard(null, undefined, onOpenChanges)}
        >
          View changes
        </button>
      )}
      {id === sessionId && models && Object.keys(models).length > 0 && (
        <ModelUsageBreakdown usageByModel={models} />
      )}
      <div>
        <h3 className="mb-2 font-medium">Subagents</h3>
        <p className="mb-2 text-xs text-muted-foreground">
          Own-agent costs; total cost includes descendants.
        </p>
        {isLoading ? (
          <Spinner />
        ) : error ? (
          <p>Subagents unavailable</p>
        ) : children.length === 0 ? (
          <p className="text-muted-foreground">No subagents</p>
        ) : (
          <ul className="space-y-2">
            {(showAll ? children : children.slice(0, 5)).map((child) => (
              <OverviewChild key={child.id} child={child} depth={1} guard={guard} />
            ))}
          </ul>
        )}
        {children.length > 5 && (
          <button type="button" className="mt-2 underline" onClick={() => setShowAll(!showAll)}>
            {showAll ? "Show fewer" : `Show all ${children.length}`}
          </button>
        )}
        {onOpenAgents && (
          <button
            type="button"
            className="mt-2 block underline"
            onClick={() => guard(null, undefined, onOpenAgents)}
          >
            View all agents
          </button>
        )}
      </div>
    </section>
  );
}

function OverviewChild({
  child,
  depth,
  guard,
}: {
  child: ChildSessionInfo;
  depth: number;
  guard: FileNavigationGuard;
}) {
  const [expanded, setExpanded] = useState(false);
  const canExpand = depth < MAX_TREE_DEPTH;
  const { children, isLoading, error } = useChildSessions(expanded && canExpand ? child.id : null);
  const navigate = useNavigate();
  const search = sessionNavigationSearch(useLocation().search);
  const label = child.task_summary ?? child.tool ?? child.title ?? "Agent";
  return (
    <li>
      <Link
        to={{ pathname: `/c/${encodeURIComponent(child.id)}`, search }}
        className="flex items-center justify-between gap-2 hover:underline"
        onClick={(event) => {
          if (
            event.button !== 0 ||
            event.ctrlKey ||
            event.metaKey ||
            event.altKey ||
            event.shiftKey
          )
            return;
          event.preventDefault();
          const destination = event.currentTarget.getAttribute("href")!;
          guard(null, undefined, () => {
            navigate(destination);
          });
        }}
      >
        <span className="truncate">
          {child.task_summary ?? child.tool ?? child.title ?? "Agent"}
        </span>
        <span className="shrink-0 font-mono">
          {child.own_cost_usd == null ? "Unavailable" : formatSessionCostUsd(child.own_cost_usd)}
        </span>
      </Link>
      {canExpand && (
        <button
          type="button"
          aria-label={`${expanded ? "Collapse" : "Expand"} ${label}`}
          aria-expanded={expanded}
          className="text-xs text-muted-foreground underline"
          onClick={() => setExpanded(!expanded)}
        >
          {expanded ? "Hide nested agents" : "Nested agents"}
        </button>
      )}
      {expanded &&
        canExpand &&
        (isLoading ? (
          <Spinner />
        ) : error ? (
          <p>Subagents unavailable</p>
        ) : children.length === 0 ? (
          <p>No nested agents</p>
        ) : (
          <ul className="mt-2 space-y-2 border-l pl-2">
            {children.map((nested) => (
              <OverviewChild key={nested.id} child={nested} depth={depth + 1} guard={guard} />
            ))}
          </ul>
        ))}
    </li>
  );
}
