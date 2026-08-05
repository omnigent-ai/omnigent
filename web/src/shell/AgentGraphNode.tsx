// One node of the Agents-pane graph view. Split out of SubagentsGraphView so
// it can be rendered in tests without importing ReactFlow's barrel + CSS
// bundle, which exhausts the jsdom worker.

import type { NodeProps, Node } from "@xyflow/react";
import { Handle, Position } from "@xyflow/react";
import { RunningDot } from "@/components/RunningDot";
import { Badge } from "@/components/ui/badge";
import { cn } from "@/lib/utils";
import { NODE_WIDTH, type AgentActivity, type AgentNodeData } from "./subagentGraphLayout";
import { AGENT_ICON_CLASS, iconForChildAgent, iconForSessionAgent } from "./subagentIcons";
import { activityDotClassName } from "./subagentStatus";

// Per-activity node border + background tint. The status DOT color is NOT
// defined here — it comes from the shared ``activityDotClassName`` so the
// graph dot always matches the list dot (the source of truth).
const ACTIVITY_TINT: Record<AgentActivity, { border: string; bg: string }> = {
  working: { border: "border-brand-accent", bg: "bg-brand-accent/5" },
  awaiting: { border: "border-warning", bg: "bg-warning/5" },
  failed: { border: "border-destructive", bg: "bg-destructive/5" },
  launching: { border: "border-muted-foreground/40", bg: "bg-muted/30" },
  disconnected: { border: "border-muted-foreground/30", bg: "bg-card" },
  done: { border: "border-muted-foreground/30", bg: "bg-card" },
  idle: { border: "border-muted-foreground/30", bg: "bg-card" },
  other: { border: "border-muted-foreground/30", bg: "bg-card" },
};

function NodeStatusDot({ activity }: { activity: AgentActivity }) {
  if (activity === "working") return <RunningDot />;
  if (activity === "awaiting") {
    return (
      <Badge className="border-transparent bg-warning/15 text-warning text-[9px] px-1 py-0">
        !
      </Badge>
    );
  }
  return (
    <span
      className={cn("inline-block size-2 shrink-0 rounded-full", activityDotClassName(activity))}
    />
  );
}

export function AgentGraphNode({ data }: NodeProps<Node<AgentNodeData>>) {
  const { label, activity, statusLabel, isActive, preview, wrapper, tool, agentName, harness } =
    data;
  const tint = ACTIVITY_TINT[activity];
  // Same glyph the list view shows for this agent: the root reads as a whole
  // session (brand → nessie → bot), children as sub-agents (brand → role →
  // Otto). Decorative — the node label beside it carries the name.
  const Icon = data.isRoot
    ? iconForSessionAgent({ wrapper, agentName, harness })
    : iconForChildAgent({ wrapper, tool });

  return (
    <>
      <Handle
        type="target"
        position={Position.Top}
        className="!bg-muted-foreground/40 !w-1.5 !h-1.5 !border-0"
      />
      <div
        className={cn(
          "rounded-lg border px-3 py-2 shadow-sm transition-colors hover:shadow-md cursor-pointer",
          tint.border,
          tint.bg,
          isActive && "ring-2 ring-ring ring-offset-1 ring-offset-background",
        )}
        style={{ width: NODE_WIDTH }}
      >
        <div className="flex items-center gap-1.5">
          <Icon aria-hidden="true" className={AGENT_ICON_CLASS} />
          <span className="truncate text-xs font-medium leading-tight">{label}</span>
          <span className="flex-1" />
          <NodeStatusDot activity={activity} />
        </div>
        {preview && (
          <p className="mt-1 truncate text-[10px] leading-tight text-muted-foreground">{preview}</p>
        )}
        {!["idle", "done"].includes(activity) && (
          <p className="mt-0.5 text-[10px] text-muted-foreground">{statusLabel}</p>
        )}
      </div>
      <Handle
        type="source"
        position={Position.Bottom}
        className="!bg-muted-foreground/40 !w-1.5 !h-1.5 !border-0"
      />
    </>
  );
}
