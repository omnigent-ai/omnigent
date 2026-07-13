import { Link } from "@/lib/routing";
import { Badge } from "@/components/ui/badge";
import type { WorkflowNodeSummary } from "@/hooks/useWorkflows";

/**
 * Detail card for one workflow DAG node, shown in the node-click popover.
 *
 * Renders the node's metadata (agent, role, state, attempts, dependencies)
 * plus its error, and — when the node has dispatched a child session — a link
 * into that agent's chat (where the full result is rendered). Kept free of
 * ``@xyflow/react`` so it is unit-testable without loading ReactFlow (which
 * OOMs in jsdom).
 */
export function WorkflowNodeDetails({
  node,
  search,
}: {
  node: WorkflowNodeSummary;
  search: string;
}) {
  return (
    <div className="flex flex-col gap-2 text-xs">
      <div className="flex items-start gap-1.5">
        <span className="min-w-0 flex-1 font-medium">{node.title}</span>
        <Badge variant="outline" className="shrink-0 capitalize text-[10px]">
          {node.state}
        </Badge>
      </div>

      <dl className="grid grid-cols-[auto_1fr] gap-x-2 gap-y-0.5 text-[11px] text-muted-foreground">
        <dt>Agent</dt>
        <dd className="text-foreground">{node.agent}</dd>
        <dt>Role</dt>
        <dd className="capitalize text-foreground">{node.role}</dd>
        <dt>Attempts</dt>
        <dd className="text-foreground">{node.attempt_count}</dd>
        <dt>Depends on</dt>
        <dd className="text-foreground">{node.deps.length ? node.deps.join(", ") : "—"}</dd>
      </dl>

      {node.error && (
        <div className="rounded border border-destructive/40 bg-destructive/5 p-1.5 text-[11px] text-destructive">
          <span className="whitespace-pre-wrap break-words">{node.error}</span>
        </div>
      )}

      {node.child_session_id ? (
        <Link
          to={{ pathname: `/c/${node.child_session_id}`, search }}
          className="text-[11px] font-medium text-brand-accent hover:underline"
        >
          Open agent chat →
        </Link>
      ) : (
        <span className="text-[11px] text-muted-foreground">
          No agent session yet — this node hasn’t started.
        </span>
      )}
    </div>
  );
}
