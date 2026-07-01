// Structured card for a `sys_advise_models` tool call: the orchestrator
// asking Intelligent routing to size a planned sub-agent fan-out. Instead of
// the generic ToolCard's raw-JSON dump, this renders the plan as rows —
// one per dispatch, with the target worker, the recommended model as a
// pill, and the judge's rationale — readable at a glance in the demo.
// The raw response JSON stays one chevron away for debugging.

import { BrainIcon, ChevronRightIcon } from "lucide-react";
import { useMemo } from "react";
import { CodeBlock, CodeBlockHeader, CodeBlockTitle } from "@/components/ai-elements/code-block";
import { Shimmer } from "@/components/ai-elements/shimmer";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import { shortModelName } from "@/components/CostRoutingControl";
import type { ToolState } from "@/lib/renderItems";
import { cn } from "@/lib/utils";
import { TOOL_SURFACE_WIDTH_CLASS } from "./toolSurface";

/** One dispatch the orchestrator planned (from the tool's `tasks` args). */
interface PlannedTask {
  title: string;
  agent: string;
}

/** One sized recommendation (from the tool's response JSON). */
interface Recommendation {
  model: string;
  rationale: string;
  title: string;
  /** Target worker as echoed in the response; `""` when absent. */
  agent: string;
}

/**
 * Extract the planned dispatches from the tool-call args, dropping
 * malformed entries. An empty result means the args were unusable —
 * the card then leans on the response alone.
 */
export function parsePlannedTasks(args: Record<string, unknown>): PlannedTask[] {
  const raw = args.tasks;
  if (!Array.isArray(raw)) return [];
  const tasks: PlannedTask[] = [];
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const rec = entry as Record<string, unknown>;
    if (typeof rec.title !== "string" || rec.title.length === 0) continue;
    // One row per task. Collect agent names for display during judging
    // (the recommendation will have the definitive chosen agent).
    let agentHint = "";
    if (Array.isArray(rec.agents) && rec.agents.length > 0) {
      agentHint = (rec.agents as Record<string, unknown>[])
        .map((a) => (typeof a.agent === "string" ? a.agent : ""))
        .filter(Boolean)
        .join(", ");
    }
    tasks.push({ title: rec.title, agent: agentHint });
  }
  return tasks;
}

/**
 * Parse the tool response into a title → recommendation map.
 *
 * Returns `null` when the output is not the success-shaped JSON (the
 * dispatcher returns plain `"Error: …"` strings for every failure mode:
 * router off, judge unavailable, malformed tasks) — the card then shows
 * the output verbatim as the failure line.
 */
export function parseRecommendations(output: string): Map<string, Recommendation> | null {
  let payload: unknown;
  try {
    payload = JSON.parse(output);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const raw = (payload as Record<string, unknown>).recommendations;
  if (!Array.isArray(raw)) return null;
  const map = new Map<string, Recommendation>();
  for (const entry of raw) {
    if (typeof entry !== "object" || entry === null) continue;
    const rec = entry as Record<string, unknown>;
    if (
      typeof rec.title === "string" &&
      rec.title.length > 0 &&
      typeof rec.model === "string" &&
      rec.model.length > 0
    ) {
      const agent = typeof rec.agent === "string" ? rec.agent : "";
      const title = rec.title as string;
      map.set(title, {
        model: rec.model,
        title,
        rationale: typeof rec.rationale === "string" ? rec.rationale : "",
        agent,
      });
    }
  }
  return map;
}

interface SmartRoutingCardProps {
  /** Full `sys_advise_models` args dict (`{tasks: [{title, agent, task}]}`). */
  arguments: Record<string, unknown>;
  /** Tool output (success JSON or an `Error: …` string); null while judging. */
  output: string | null;
  state: ToolState;
}

/** Shimmer verbs cycled per task row while the router judges (wraps past 4). */
const ROUTING_VERBS = ["weighing", "matching", "tuning", "sizing up"] as const;

/**
 * Render a `sys_advise_models` call as a Smart-routing plan card.
 *
 * Three states: while judging (`input-available`) the rows show their
 * worker with a shimmering per-task verb placeholder; on success each row gets
 * its recommended {@link ModelTierPill} plus the judge's rationale; on
 * failure (error string / unparseable output / no output recorded) the
 * dispatcher's message renders as a muted line — the orchestrator
 * dispatches with its own model choices in that case, so the card must
 * not pretend a plan exists.
 */
export function SmartRoutingCard({ arguments: args, output, state }: SmartRoutingCardProps) {
  const plannedTasks = useMemo(() => parsePlannedTasks(args), [args]);
  const recommendations = useMemo(
    () => (output === null ? null : parseRecommendations(output)),
    [output],
  );
  // Args are the row source (they exist from the moment the call starts);
  // when they didn't parse, fall back to the response's own task list so
  // a sized plan still renders rows.
  const tasks = useMemo(() => {
    if (plannedTasks.length > 0 || recommendations === null) return plannedTasks;
    return [...recommendations.values()].map((rec) => ({ title: rec.title, agent: "" }));
  }, [plannedTasks, recommendations]);
  const judging = state === "input-available";
  // Any terminal state without parseable recommendations is a failure:
  // an Error-string output, a cancelled turn, or no output recorded.
  const failed = !judging && recommendations === null;
  const prettyOutput = useMemo(() => {
    if (output === null) return null;
    try {
      return JSON.stringify(JSON.parse(output), null, 2);
    } catch {
      return output;
    }
  }, [output]);

  const taskNoun = tasks.length === 1 ? "task" : "tasks";
  return (
    <Collapsible
      defaultOpen={false}
      className={cn(
        "group not-prose my-1 flex flex-col gap-1.5 rounded-md border border-border bg-muted/30 px-3 py-2",
        TOOL_SURFACE_WIDTH_CLASS,
      )}
      data-testid="smart-routing-card"
      data-state-kind={judging ? "judging" : failed ? "failed" : "sized"}
    >
      <div className="flex items-center gap-1.5 text-xs">
        <BrainIcon className="size-3.5 shrink-0 text-muted-foreground" />
        <span className="font-medium">Intelligent routing</span>
        {judging ? (
          <Shimmer as="span" className="text-xs">
            {`Weighing ${tasks.length} ${taskNoun}…`}
          </Shimmer>
        ) : (
          <span className="text-muted-foreground">
            {failed ? "· unavailable" : `· sized ${recommendations!.size} ${taskNoun}`}
          </span>
        )}
        {prettyOutput !== null && (
          <CollapsibleTrigger
            className="ml-auto cursor-pointer rounded p-0.5 text-muted-foreground hover:text-foreground"
            aria-label="Show raw routing response"
            data-testid="smart-routing-raw-toggle"
          >
            <ChevronRightIcon className="size-3 transition-transform group-data-[state=open]:rotate-90" />
          </CollapsibleTrigger>
        )}
      </div>
      {failed ? (
        <p className="text-xs text-muted-foreground" data-testid="smart-routing-error">
          {output ?? "No routing decision was recorded for this fan-out."}
        </p>
      ) : (
        tasks.map((task, i) => {
          const rec = recommendations?.get(task.title) ?? null;
          return (
            <div key={task.title} className="flex flex-col gap-0.5">
              <div className="flex items-center gap-2 text-xs">
                <span className="min-w-0 truncate font-mono text-foreground">{task.title}</span>
                {(rec?.agent ?? task.agent).length > 0 && (
                  <span className="shrink-0 text-muted-foreground/70">
                    → {rec?.agent ?? task.agent}
                  </span>
                )}
                <span className="ml-auto shrink-0">
                  {rec !== null ? (
                    <span className="inline-flex items-center whitespace-nowrap rounded-full border border-border bg-muted px-1.5 py-0.5 font-mono text-[10px] font-medium leading-none text-foreground">
                      {shortModelName(rec.model)}
                    </span>
                  ) : judging ? (
                    <Shimmer as="span" className="text-xs">
                      {`${ROUTING_VERBS[i % ROUTING_VERBS.length]}…`}
                    </Shimmer>
                  ) : (
                    <span className="text-muted-foreground/60">—</span>
                  )}
                </span>
              </div>
              {rec !== null && rec.rationale.length > 0 && (
                <p className="text-xs leading-snug text-muted-foreground">{rec.rationale}</p>
              )}
            </div>
          );
        })
      )}
      {prettyOutput !== null && (
        <CollapsibleContent className="data-[state=closed]:fade-out-0 data-[state=open]:fade-in-0 data-[state=closed]:animate-out data-[state=open]:animate-in">
          <CodeBlock code={prettyOutput} language="json">
            <CodeBlockHeader>
              <CodeBlockTitle className="min-w-0">
                <span className="truncate font-medium uppercase tracking-wide">Response</span>
              </CodeBlockTitle>
            </CodeBlockHeader>
          </CodeBlock>
        </CollapsibleContent>
      )}
    </Collapsible>
  );
}
