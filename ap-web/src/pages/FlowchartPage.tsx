/**
 * Flow builder (`/jobs/flow/:jobId`) — guided top-down stepper.
 *
 * The chart is built as a vertical sequence of steps: it always begins with a
 * Start node, and at every open point a "+" reveals the available box types
 * (Process / Decision / Input-Output / End); picking one appends it as the next
 * step. A Decision splits into two labelled branch lanes (Yes / No), each its
 * own downward "+"-chain. This replaces the earlier free-form drag canvas —
 * there are no manual edges; the tree's structure *is* the connections.
 *
 * The editable model is a {@link FlowStep} tree (see `@/lib/flowTree`),
 * persisted on the job. For the output panel (Narrative / Outline / Mermaid)
 * and runs, the tree is converted to the flat {@link FlowGraph} the generator
 * consumes via {@link treeToGraph}, so nothing downstream changed.
 *
 * Double-click a step to rename it; double-click a branch label to edit it;
 * each step has a delete (which removes it and everything below).
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  ArrowLeftIcon,
  CheckCircle2Icon,
  CopyIcon,
  Loader2Icon,
  PlayIcon,
  PlusIcon,
  SaveIcon,
  Trash2Icon,
  XCircleIcon,
} from "lucide-react";
import { MessageResponse } from "@/components/ai-elements/message";
import { Button } from "@/components/ui/button";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { generateFlowText } from "@/lib/flowToText";
import type { FlowNodeType } from "@/lib/flowToText";
import {
  ADDABLE_TYPES,
  attach,
  countSteps,
  defaultLabel,
  deleteStep,
  newStep,
  setBranchLabel,
  setLabel,
  treeToGraph,
  type FlowStep,
  type Slot,
} from "@/lib/flowTree";
import { runJob, updateJob, useJob, type Run } from "@/lib/jobsStore";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Per-kind presentation
// ---------------------------------------------------------------------------
const KIND_META: Record<FlowNodeType, { tag: string; box: string; chip: string }> = {
  start: { tag: "Start", box: "border-emerald-500 bg-emerald-500/10", chip: "bg-emerald-500" },
  process: { tag: "Process", box: "border-blue-500 bg-blue-500/10", chip: "bg-blue-500" },
  decision: { tag: "Decision", box: "border-amber-500 bg-amber-500/10", chip: "bg-amber-500" },
  io: { tag: "Input/Output", box: "border-purple-500 bg-purple-500/10", chip: "bg-purple-500" },
  end: { tag: "End", box: "border-red-500 bg-red-500/10", chip: "bg-red-500" },
};

// ---------------------------------------------------------------------------
// "+" add control — click reveals the addable box types, pick one to append.
// ---------------------------------------------------------------------------
function AddStep({ onPick }: { onPick: (type: FlowNodeType) => void }) {
  const [open, setOpen] = useState(false);
  if (!open) {
    return (
      <button
        type="button"
        aria-label="Add step"
        onClick={() => setOpen(true)}
        className="flex size-8 items-center justify-center rounded-full border border-dashed border-border bg-background text-muted-foreground transition-colors hover:border-primary hover:text-primary"
      >
        <PlusIcon className="size-4" />
      </button>
    );
  }
  return (
    <div className="flex flex-wrap items-center justify-center gap-1 rounded-lg border border-border bg-card p-1.5 shadow-md">
      {ADDABLE_TYPES.map((type) => (
        <Button
          key={type}
          variant="ghost"
          size="sm"
          onClick={() => {
            onPick(type);
            setOpen(false);
          }}
        >
          <span className={cn("mr-1.5 inline-block size-2.5 rounded-sm", KIND_META[type].chip)} />
          {KIND_META[type].tag}
        </Button>
      ))}
      <Button variant="ghost" size="sm" onClick={() => setOpen(false)}>
        Cancel
      </Button>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Recursive step renderer — a box, a connector, then either the next "+"-chain
// or (for decisions) two labelled branch lanes side by side.
// ---------------------------------------------------------------------------
interface StepViewProps {
  step: FlowStep;
  isRoot: boolean;
  onAdd: (parentId: string, slot: Slot, type: FlowNodeType) => void;
  onRename: (id: string, label: string) => void;
  onRenameBranch: (id: string, branch: "yes" | "no", label: string) => void;
  onDelete: (id: string) => void;
}

function Connector() {
  return <div className="h-6 w-0.5 bg-muted-foreground/60" />;
}

function StepView({ step, isRoot, onAdd, onRename, onRenameBranch, onDelete }: StepViewProps) {
  const meta = KIND_META[step.type];
  return (
    <div className="flex flex-col items-center">
      {/* The box */}
      <div
        className={cn(
          "group relative flex min-w-[160px] max-w-[260px] flex-col items-center rounded-md border-2 px-4 py-2.5 text-center shadow-sm",
          meta.box,
        )}
        onDoubleClick={() => {
          const v = window.prompt("Label:", step.label);
          if (v !== null) onRename(step.id, v.trim() || defaultLabel(step.type));
        }}
      >
        <span className="text-[9.5px] font-bold tracking-wide text-muted-foreground uppercase">
          {meta.tag}
        </span>
        <span className="text-sm break-words">{step.label}</span>
        {/* Delete (not on the Start root — a flow always has a Start). */}
        {!isRoot && (
          <button
            type="button"
            aria-label="Delete step"
            onClick={() => onDelete(step.id)}
            className="absolute -top-2 -right-2 hidden size-5 items-center justify-center rounded-full border border-border bg-background text-muted-foreground hover:text-red-500 group-hover:flex"
          >
            <Trash2Icon className="size-3" />
          </button>
        )}
      </div>

      {step.type === "decision" ? (
        // Two branch lanes (Yes / No) joined by a fork:
        //   trunk (down from the box) → horizontal bar → a stub into each lane.
        // Lanes are equal-width (flex-1) with no gap, so their centers sit at
        // 25% / 75% of the row — exactly where the fork's vertical stubs land,
        // and the trunk meets the bar's midpoint at 50%. Spacing between lanes
        // comes from per-lane padding so it doesn't shift those centers.
        <>
          <Connector />
          <div className="flex w-full">
            <div className="flex-1">
              <div className="ml-auto h-4 w-1/2 border-t-2 border-l-2 border-muted-foreground/60" />
            </div>
            <div className="flex-1">
              <div className="mr-auto h-4 w-1/2 border-t-2 border-r-2 border-muted-foreground/60" />
            </div>
          </div>
          <div className="flex items-start">
            {(["yes", "no"] as const).map((branch) => {
              const child = step[branch];
              const label = branch === "yes" ? step.yesLabel : step.noLabel;
              return (
                <div key={branch} className="flex flex-1 flex-col items-center px-6">
                  <button
                    type="button"
                    onDoubleClick={(e) => {
                      e.stopPropagation();
                      const v = window.prompt("Branch label:", label);
                      if (v !== null) onRenameBranch(step.id, branch, v.trim() || (branch === "yes" ? "Yes" : "No"));
                    }}
                    className="rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground"
                    title="Double-click to rename branch"
                  >
                    {label}
                  </button>
                  <Connector />
                  {child ? (
                    <StepView
                      step={child}
                      isRoot={false}
                      onAdd={onAdd}
                      onRename={onRename}
                      onRenameBranch={onRenameBranch}
                      onDelete={onDelete}
                    />
                  ) : (
                    <AddStep onPick={(type) => onAdd(step.id, branch, type)} />
                  )}
                </div>
              );
            })}
          </div>
        </>
      ) : step.type === "end" ? null : (
        // Linear: next step or an open "+"
        <>
          <Connector />
          {step.next ? (
            <StepView
              step={step.next}
              isRoot={false}
              onAdd={onAdd}
              onRename={onRename}
              onRenameBranch={onRenameBranch}
              onDelete={onDelete}
            />
          ) : (
            <AddStep onPick={(type) => onAdd(step.id, "next", type)} />
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
type OutputTab = "narrative" | "outline" | "mermaid" | "runs";

export function FlowchartPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  // Reactive read so the Runs tab updates live as runs progress. The job loads
  // from the API after mount, so `loading` distinguishes "fetching" from "404".
  const { job, loading: jobLoading } = useJob(jobId);

  // The builder's working copy of the step tree. Seeded from the job once it
  // resolves; Save writes it back. Re-seeds when the job (id or loaded tree)
  // changes.
  const [tree, setTree] = useState<FlowStep>(() => job?.tree ?? newStep("start"));
  const [seededFor, setSeededFor] = useState<string | undefined>(undefined);
  useEffect(() => {
    // Seed once per job, when its tree first arrives.
    if (job && job.id !== seededFor) {
      setTree(job.tree);
      setSeededFor(job.id);
    }
  }, [job, seededFor]);

  const [tab, setTab] = useState<OutputTab>("narrative");
  const [copied, setCopied] = useState(false);
  const [saved, setSaved] = useState(false);
  const [running, setRunning] = useState(false);

  // Agent picker: a job runs as the chosen agent (its narrative becomes that
  // agent's first prompt). Persisted on the job.
  const { data: agents } = useAvailableAgents();
  const onPickAgent = useCallback(
    (agentId: string) => {
      if (!jobId) return;
      void updateJob(jobId, { agentId: agentId || null });
    },
    [jobId],
  );

  // Tree → flat graph → text. Pure & cheap, recompute on every edit.
  const result = useMemo(() => generateFlowText(treeToGraph(tree)), [tree]);
  const stepCount = countSteps(tree);
  const hasSteps = stepCount > 1; // more than the lone Start

  // ---- tree edits ----
  const onAdd = useCallback(
    (parentId: string, slot: Slot, type: FlowNodeType) =>
      setTree((t) => attach(t, parentId, slot, type)),
    [],
  );
  const onRename = useCallback(
    (id: string, label: string) => setTree((t) => setLabel(t, id, label)),
    [],
  );
  const onRenameBranch = useCallback(
    (id: string, branch: "yes" | "no", label: string) =>
      setTree((t) => setBranchLabel(t, id, branch, label)),
    [],
  );
  const onDelete = useCallback(
    (id: string) => setTree((t) => deleteStep(t, id) ?? t),
    [],
  );

  // ---- job actions ----
  const onSave = useCallback(() => {
    if (!jobId) return;
    void updateJob(jobId, { tree });
    setSaved(true);
    setTimeout(() => setSaved(false), 1200);
  }, [jobId, tree]);

  const onRun = useCallback(async () => {
    if (!jobId || running) return;
    setRunning(true);
    setTab("runs");
    try {
      // Persist the on-screen tree first so the run uses the current flow.
      await updateJob(jobId, { tree });
      const run = await runJob(jobId);
      if (run?.sessionId) navigate(`/c/${run.sessionId}`);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Failed to run job");
    } finally {
      setRunning(false);
    }
  }, [jobId, tree, running, navigate]);

  const copyOutput = useCallback(() => {
    const text =
      tab === "mermaid" ? result.mermaid : tab === "outline" ? result.outline : result.narrative;
    void navigator.clipboard.writeText(text).then(() => {
      setCopied(true);
      setTimeout(() => setCopied(false), 1200);
    });
  }, [tab, result]);

  // Stale/deleted job → bounce back to the list. Wait for the API fetch to
  // settle first so the loading window doesn't flash this.
  if (jobId && !job && !jobLoading) {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 text-center">
        <p className="text-sm font-medium">This flow no longer exists.</p>
        <Button variant="outline" onClick={() => navigate("/jobs")}>
          <ArrowLeftIcon className="size-4" /> Back to Jobs
        </Button>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Builder header — offset below the AppShell's absolute ChatHeader
          overlay (transparent 56px bar at top-0, z-30) so its buttons receive
          clicks; z-40 keeps it above that overlay where they meet. */}
      <div
        className="relative z-40 flex items-center gap-3 border-b border-border px-4 py-2"
        style={{ marginTop: "var(--omnigent-header-height, 0px)" }}
      >
        <Button asChild variant="ghost" size="sm">
          <Link to="/jobs">
            <ArrowLeftIcon className="size-4" /> Jobs
          </Link>
        </Button>
        <span className="min-w-0 flex-1 truncate text-sm font-medium">
          {job?.name ?? "Flow builder"}
        </span>
        <select
          aria-label="Run as agent"
          data-testid="job-agent-select"
          value={job?.agentId ?? ""}
          onChange={(e) => onPickAgent(e.target.value)}
          disabled={!jobId}
          className="h-8 rounded-md border border-input bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <option value="">Pick an agent…</option>
          {(agents ?? []).map((a) => (
            <option key={a.id} value={a.id}>
              {a.display_name}
            </option>
          ))}
        </select>
        <Button variant="outline" size="sm" onClick={onSave} disabled={!jobId}>
          <SaveIcon className="size-3.5" /> {saved ? "Saved!" : "Save"}
        </Button>
        <Button
          size="sm"
          onClick={onRun}
          disabled={!jobId || running || !hasSteps || !job?.agentId}
          data-testid="job-run-button"
        >
          {running ? (
            <Loader2Icon className="size-3.5 animate-spin" />
          ) : (
            <PlayIcon className="size-3.5" />
          )}
          {running ? "Running…" : "Run now"}
        </Button>
      </div>

      <div className="flex min-h-0 flex-1">
        {/* Stepper canvas */}
        <div className="min-w-0 flex-1 overflow-auto bg-[radial-gradient(circle,var(--border)_1px,transparent_1px)] [background-size:22px_22px]">
          <div className="flex min-h-full justify-center p-10">
            <StepView
              step={tree}
              isRoot
              onAdd={onAdd}
              onRename={onRename}
              onRenameBranch={onRenameBranch}
              onDelete={onDelete}
            />
          </div>
        </div>

        {/* Output panel */}
        <aside className="flex w-[420px] min-w-[300px] flex-col border-l border-border bg-card">
          <Tabs
            value={tab}
            onValueChange={(v) => setTab(v as OutputTab)}
            className="flex min-h-0 flex-1 flex-col gap-0"
          >
            <div className="flex items-center gap-2 border-b border-border px-3 py-2">
              <TabsList variant="line" className="flex-1">
                <TabsTrigger value="narrative">Narrative</TabsTrigger>
                <TabsTrigger value="outline">Outline</TabsTrigger>
                <TabsTrigger value="mermaid">Mermaid</TabsTrigger>
                <TabsTrigger value="runs">
                  Runs{job && job.runs.length > 0 ? ` (${job.runs.length})` : ""}
                </TabsTrigger>
              </TabsList>
              {tab !== "runs" && (
                <Button variant="outline" size="sm" onClick={copyOutput} disabled={!hasSteps}>
                  <CopyIcon className="size-3.5" /> {copied ? "Copied!" : "Copy"}
                </Button>
              )}
            </div>

            <div className="border-b border-border px-4 py-2 text-xs text-muted-foreground">
              {hasSteps ? result.summary : "Click the + below Start to add your first step."}
            </div>

            <div className="min-h-0 flex-1 overflow-auto">
              <TabsContent value="narrative" className="p-4">
                {result.narrative ? (
                  <pre className="font-sans text-sm leading-relaxed whitespace-pre-wrap">
                    {result.narrative}
                  </pre>
                ) : (
                  <p className="text-sm text-muted-foreground italic">Nothing to show yet.</p>
                )}
              </TabsContent>
              <TabsContent value="outline" className="p-4">
                {result.outline ? (
                  <pre className="font-mono text-xs leading-relaxed whitespace-pre-wrap">
                    {result.outline}
                  </pre>
                ) : (
                  <p className="text-sm text-muted-foreground italic">Nothing to show yet.</p>
                )}
              </TabsContent>
              <TabsContent value="mermaid" className="flex flex-col gap-3 p-4">
                {result.mermaid ? (
                  <>
                    <MessageResponse>{"```mermaid\n" + result.mermaid + "\n```"}</MessageResponse>
                    <pre className="rounded-md border border-border bg-muted/40 p-3 font-mono text-xs whitespace-pre-wrap">
                      {result.mermaid}
                    </pre>
                  </>
                ) : (
                  <p className="text-sm text-muted-foreground italic">Nothing to show yet.</p>
                )}
              </TabsContent>
              <TabsContent value="runs" className="p-4">
                <RunsList runs={job?.runs ?? []} />
              </TabsContent>
            </div>
          </Tabs>
        </aside>
      </div>
    </div>
  );
}

// ---------------------------------------------------------------------------
// Runs history (newest first) with a per-run status icon and expandable logs.
// ---------------------------------------------------------------------------
function RunStatusIcon({ status }: { status: Run["status"] }) {
  if (status === "running")
    return <Loader2Icon className="size-4 animate-spin text-muted-foreground" />;
  if (status === "succeeded") return <CheckCircle2Icon className="size-4 text-emerald-500" />;
  return <XCircleIcon className="size-4 text-red-500" />;
}

function RunsList({ runs }: { runs: Run[] }) {
  const ordered = [...runs].sort((a, b) => b.startedAt - a.startedAt);
  if (!ordered.length) {
    return (
      <p className="text-sm text-muted-foreground italic">
        No runs yet. Press “Run now” to execute this flow.
      </p>
    );
  }
  return (
    <div className="flex flex-col gap-2">
      {ordered.map((run) => {
        const duration =
          run.finishedAt != null
            ? `${((run.finishedAt - run.startedAt) / 1000).toFixed(1)}s`
            : "…";
        return (
          <details key={run.id} className="rounded-md border border-border bg-background/40 p-2.5">
            <summary className="flex cursor-pointer list-none items-center gap-2 text-sm">
              <RunStatusIcon status={run.status} />
              <span className="font-medium">Run #{run.number}</span>
              <span className="text-xs text-muted-foreground capitalize">{run.status}</span>
              <span className="ml-auto text-xs text-muted-foreground tabular-nums">{duration}</span>
            </summary>
            <pre className="mt-2 border-t border-border pt-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-muted-foreground">
              {run.logs.join("\n")}
            </pre>
          </details>
        );
      })}
    </div>
  );
}
