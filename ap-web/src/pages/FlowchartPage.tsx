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

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import {
  ActivityIcon,
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
import { Input } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { Tabs, TabsContent, TabsList, TabsTrigger } from "@/components/ui/tabs";
import { Link, useNavigate, useParams } from "@/lib/routing";
import { generateFlowText } from "@/lib/flowToText";
import type { FlowNodeType } from "@/lib/flowToText";
import {
  ADDABLE_TYPES,
  attach,
  attachAction,
  repairActionSteps,
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
import { getSession } from "@/lib/sessionsApi";
import { fetchLastAssistantText } from "@/lib/lastAssistantText";
import { useAvailableAgents } from "@/hooks/useAvailableAgents";
import { useHosts } from "@/hooks/useHosts";
import { getIconComponent } from "@/components/icons/iconRegistry";
import {
  useActionCatalog,
  type ActionDef,
  type ActionGroup,
  type ActionGroupIcon,
} from "@/lib/actionCatalog";
import { cn } from "@/lib/utils";

// ---------------------------------------------------------------------------
// Per-kind presentation
// ---------------------------------------------------------------------------
// Boxes use the app's neutral card surface so they sit in spirit with the rest
// of the UI (no rainbow fills). Node type is conveyed by a small accent dot
// (`chip`) and the uppercase tag, not the whole box. `chip` is also reused in
// the "+" menu's generic-node buttons.
/** Schedule interval units the UI offers; storage stays canonical in minutes. */
type ScheduleUnit = "minute" | "hour" | "day";
const MINUTES_PER_UNIT: Record<ScheduleUnit, number> = {
  minute: 1,
  hour: 60,
  day: 60 * 24,
};
const UNIT_LABELS: Record<ScheduleUnit, string> = {
  minute: "min",
  hour: "hours",
  day: "days",
};

const KIND_META: Record<FlowNodeType, { tag: string; chip: string }> = {
  start: { tag: "Start", chip: "bg-emerald-500" },
  process: { tag: "Process", chip: "bg-primary" },
  decision: { tag: "Decision", chip: "bg-amber-500" },
  io: { tag: "Input/Output", chip: "bg-violet-500" },
  end: { tag: "End", chip: "bg-red-500" },
};

// ---------------------------------------------------------------------------
// "+" add control — click reveals the addable box types plus any predefined
// action groups (from the catalog), pick one to append.
// ---------------------------------------------------------------------------
/**
 * Renders a group's icon — a bundled brand component (built-in groups, by key)
 * or an uploaded image (custom groups, by URL). Falls back to nothing when the
 * group has no icon (e.g. the plain "Entities" / "Jobs" groups).
 */
function ActionIcon({ icon, className }: { icon?: ActionGroupIcon; className?: string }) {
  if (!icon) return null;
  if (icon.kind === "component") {
    const Cmp = getIconComponent(icon.key);
    return Cmp ? <Cmp className={cn("shrink-0", className)} /> : null;
  }
  return <img src={icon.url} alt="" className={cn("shrink-0 object-contain", className)} />;
}

function AddStep({
  onPick,
  onPickAction,
  groups,
  loadingGroups,
}: {
  onPick: (type: FlowNodeType) => void;
  onPickAction: (action: ActionDef, group: ActionGroup) => void;
  groups: ActionGroup[];
  loadingGroups: boolean;
}) {
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
    <div className="flex w-60 flex-col gap-1 rounded-lg border border-border bg-card p-1.5 shadow-md">
      {/* Generic node types */}
      <div className="flex flex-wrap gap-1">
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
      </div>

      {/* Predefined action groups (from the catalog DB) */}
      {loadingGroups ? (
        <div className="flex items-center gap-1.5 px-2 py-1 text-xs text-muted-foreground">
          <Loader2Icon className="size-3.5 animate-spin" /> Loading integrations…
        </div>
      ) : (
        groups.map((group) => (
          <div key={group.id} className="border-t border-border pt-1">
            <div className="flex items-center gap-1.5 px-2 py-0.5 text-[10px] font-bold tracking-wide text-muted-foreground uppercase">
              <ActionIcon icon={group.icon} className="size-3" />
              {group.name}
            </div>
            {group.actions.map((action) => (
              <button
                key={action.id}
                type="button"
                title={action.description}
                onClick={() => {
                  onPickAction(action, group);
                  setOpen(false);
                }}
                className="flex w-full items-center gap-1.5 rounded-md px-2 py-1 text-left text-sm hover:bg-muted"
              >
                <ActionIcon icon={group.icon} className="size-3.5 text-muted-foreground" />
                {action.label}
              </button>
            ))}
          </div>
        ))
      )}

      <Button variant="ghost" size="sm" className="mt-0.5" onClick={() => setOpen(false)}>
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
  onAddAction: (parentId: string, slot: Slot, action: ActionDef, group: ActionGroup) => void;
  onRename: (id: string, label: string) => void;
  onRenameBranch: (id: string, branch: "yes" | "no", label: string) => void;
  onDelete: (id: string) => void;
  groups: ActionGroup[];
  loadingGroups: boolean;
}

function Connector() {
  return <div className="h-6 w-0.5 bg-muted-foreground/60" />;
}

/**
 * Click-to-edit text. Renders `value` as a span until clicked, then swaps to an
 * autofocused input. Commits on Enter or blur, cancels (restores) on Escape.
 * `onCommit` receives the trimmed text; an empty result is ignored by callers
 * via their fallback. `stopPropagation` keeps clicks/keys from reaching the
 * canvas (deselect, delete shortcuts, etc.).
 */
function InlineEdit({
  value,
  onCommit,
  className,
  inputClassName,
}: {
  value: string;
  onCommit: (next: string) => void;
  className?: string;
  inputClassName?: string;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const ref = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      ref.current?.focus();
      ref.current?.select();
    }
  }, [editing]);

  const start = () => {
    setDraft(value);
    setEditing(true);
  };
  const commit = () => {
    setEditing(false);
    const next = draft.trim();
    if (next && next !== value) onCommit(next);
  };

  if (!editing) {
    return (
      <span
        className={cn("cursor-text", className)}
        title="Click to edit"
        onClick={(e) => {
          e.stopPropagation();
          start();
        }}
      >
        {value}
      </span>
    );
  }
  return (
    <input
      ref={ref}
      value={draft}
      onChange={(e) => setDraft(e.target.value)}
      onClick={(e) => e.stopPropagation()}
      onMouseDown={(e) => e.stopPropagation()}
      onBlur={commit}
      onKeyDown={(e) => {
        e.stopPropagation();
        if (e.key === "Enter") commit();
        if (e.key === "Escape") {
          setDraft(value);
          setEditing(false);
        }
      }}
      className={cn(
        "w-full rounded border border-primary bg-background px-1 text-center outline-none",
        inputClassName,
      )}
    />
  );
}

function StepView({
  step,
  isRoot,
  onAdd,
  onAddAction,
  onRename,
  onRenameBranch,
  onDelete,
  groups,
  loadingGroups,
}: StepViewProps) {
  const meta = KIND_META[step.type];
  // Shared props for every nested StepView so the catalog threads down.
  const childProps = { onAdd, onAddAction, onRename, onRenameBranch, onDelete, groups, loadingGroups };
  // An action step carries its source group's name; find that group in the
  // catalog to render its icon (built-in component or uploaded image) on the
  // box's left, matching how the picker presents it.
  const stepGroup = step.actionGroup
    ? groups.find((g) => g.name === step.actionGroup)
    : undefined;
  return (
    <div className="flex flex-col items-center">
      {/* The box. `relative z-10` lifts it above the branch-connector
          pseudo-elements (which sit at the lane's top-0) so its label input and
          delete button always receive clicks. */}
      <div className="group relative z-10 flex min-w-[180px] max-w-[280px] items-center gap-2.5 rounded-lg border border-border bg-card px-3 py-2.5 text-left shadow-sm">
        {/* Left: the action's group icon, or a node-type accent dot for generic
            steps — so the box reads as "what kind of step" at a glance. */}
        {stepGroup?.icon ? (
          <ActionIcon icon={stepGroup.icon} className="size-5 text-muted-foreground" />
        ) : (
          <span className={cn("size-2 shrink-0 rounded-full", meta.chip)} />
        )}
        <div className="flex min-w-0 flex-1 flex-col">
          <span className="text-[9.5px] font-bold tracking-wide text-muted-foreground uppercase">
            {/* Action steps show their integration group (e.g. "Jira"); generic
                steps show their node-type tag. */}
            {step.actionGroup ?? meta.tag}
          </span>
          {/* A wired-in job's label IS the job's name — it must mirror the source
              job, so it's shown read-only (editing it here would desync the two).
              Entity and generic steps stay inline-editable. */}
          {step.actionId?.startsWith("job:") ? (
            <span className="text-sm break-words">{step.label}</span>
          ) : (
            <InlineEdit
              value={step.label}
              onCommit={(next) => onRename(step.id, next || defaultLabel(step.type))}
              className="text-sm break-words"
              inputClassName="text-sm"
            />
          )}
        </div>
        {/* Delete (not on the Start root — a flow always has a Start). Shown on
            hover/focus-within; z-20 keeps it clickable above everything. */}
        {!isRoot && (
          <button
            type="button"
            aria-label="Delete step"
            onClick={(e) => {
              e.stopPropagation();
              onDelete(step.id);
            }}
            className="absolute -top-2 -right-2 z-20 hidden size-5 items-center justify-center rounded-full border border-border bg-background text-muted-foreground hover:text-red-500 group-hover:flex group-focus-within:flex"
          >
            <Trash2Icon className="size-3" />
          </button>
        )}
      </div>

      {step.type === "decision" ? (
        // Two branch lanes (Yes / No) joined by a fork, drawn with the classic
        // CSS-tree connector technique:
        //   • trunk = the <Connector/> straight down from the box.
        //   • each lane sizes to ITS OWN content (no flex-1 / no grid-fr) so
        //     dense or deeply-nested branches spread out instead of shrinking
        //     and overlapping — the canvas then scrolls horizontally.
        //   • connectors are pseudo-elements positioned at `left-1/2` of each
        //     lane, so they track each lane's real center at any width:
        //       before:* = the short vertical stub down into the lane;
        //       after:*  = the horizontal bar across the lane's top, clipped to
        //                  the inner half on the first/last lane so the bar runs
        //                  exactly between the two lane centers.
        <>
          <Connector />
          <div className="flex items-start">
            {(["yes", "no"] as const).map((branch) => {
              const child = step[branch];
              const label = branch === "yes" ? step.yesLabel : step.noLabel;
              return (
                <div
                  key={branch}
                  className={cn(
                    "relative flex flex-col items-center px-6 pt-4",
                    "before:absolute before:top-0 before:left-1/2 before:h-4 before:w-0.5 before:bg-muted-foreground/60",
                    "after:absolute after:top-0 after:right-0 after:left-0 after:h-0.5 after:bg-muted-foreground/60",
                    branch === "yes" ? "after:left-1/2" : "after:right-1/2",
                  )}
                >
                  <div className="relative z-10 rounded-full border border-border bg-muted px-2 py-0.5 text-[11px] font-medium text-muted-foreground">
                    <InlineEdit
                      value={label}
                      onCommit={(next) =>
                        onRenameBranch(step.id, branch, next || (branch === "yes" ? "Yes" : "No"))
                      }
                      inputClassName="text-[11px] w-16"
                    />
                  </div>
                  <Connector />
                  {child ? (
                    <StepView step={child} isRoot={false} {...childProps} />
                  ) : (
                    <AddStep
                      groups={groups}
                      loadingGroups={loadingGroups}
                      onPick={(type) => onAdd(step.id, branch, type)}
                      onPickAction={(action, group) => onAddAction(step.id, branch, action, group)}
                    />
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
            <StepView step={step.next} isRoot={false} {...childProps} />
          ) : (
            <AddStep
              groups={groups}
              loadingGroups={loadingGroups}
              onPick={(type) => onAdd(step.id, "next", type)}
              onPickAction={(action, group) => onAddAction(step.id, "next", action, group)}
            />
          )}
        </>
      )}
    </div>
  );
}

// ---------------------------------------------------------------------------
// Page
// ---------------------------------------------------------------------------
type OutputTab = "narrative" | "mermaid" | "runs";

export function FlowchartPage() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  // Reactive read so the Runs tab updates live as runs progress. The job loads
  // from the API after mount, so `loading` distinguishes "fetching" from "404".
  const { job, loading: jobLoading } = useJob(jobId);

  // Pickable groups: saved entities (Jira, …) + other jobs wired in as steps.
  // Exclude this job so a flow can't be wired into itself. Local stores → sync.
  const { groups, loading: loadingGroups } = useActionCatalog(jobId);

  // Lookup from action id → {label, instruction}, the source of truth for
  // repairing action-backed steps (see seeding below).
  const actionLookup = useMemo(() => {
    const map = new Map<string, { label: string; instruction: string }>();
    for (const g of groups)
      for (const a of g.actions) map.set(a.id, { label: a.label, instruction: a.instruction });
    return (id: string) => map.get(id);
  }, [groups]);

  // The builder's working copy of the step tree. Seeded from the job once it
  // resolves; Save writes it back. Re-seeds when the job (id or loaded tree)
  // changes. On seed we also repair legacy action steps (older builds stored
  // the full instruction as the label, which garbled the narrative).
  const [tree, setTree] = useState<FlowStep>(() => job?.tree ?? newStep("start"));
  const [seededFor, setSeededFor] = useState<string | undefined>(undefined);
  // The builder's working copy of the job name, edited inline in the header and
  // persisted on blur/Enter. Seeded from the job alongside the tree.
  const [name, setName] = useState<string>(() => job?.name ?? "");
  useEffect(() => {
    if (job && job.id !== seededFor) {
      setTree(repairActionSteps(job.tree, actionLookup));
      setName(job.name);
      setSeededFor(job.id);
    }
  }, [job, seededFor, actionLookup]);

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

  // Host picker: a job's runs launch their runner on the chosen host (persisted
  // on the job). Empty = let the run pick any online host. Persisted as hostId.
  const { data: hosts } = useHosts();
  const onPickHost = useCallback(
    (hostId: string) => {
      if (!jobId) return;
      void updateJob(jobId, { hostId: hostId || null });
    },
    [jobId],
  );

  // Inline name editing in the header: persist on blur / Enter. Empty falls
  // back to the job's current name (never save a blank title).
  const commitName = useCallback(() => {
    if (!jobId) return;
    const trimmed = name.trim();
    if (!trimmed || trimmed === job?.name) {
      setName(job?.name ?? "");
      return;
    }
    void updateJob(jobId, { name: trimmed });
  }, [jobId, name, job?.name]);

  // Schedule (time trigger): a job can fire automatically every N minutes/hours/
  // days. Storage stays canonical in minutes (the server poll cadence, 1-min
  // minimum); the UI picks a value + unit and converts. Persisted on the job.
  const schedule = job?.schedule ?? null;
  const scheduleEnabled = !!schedule?.enabled;
  const intervalMinutes = schedule?.intervalMinutes ?? 5;
  // Display the stored minutes as the largest unit it divides cleanly into, so
  // "120 min" shows as "2 hours" — but never below the stored granularity.
  const intervalUnit: ScheduleUnit =
    intervalMinutes % MINUTES_PER_UNIT.day === 0
      ? "day"
      : intervalMinutes % MINUTES_PER_UNIT.hour === 0
        ? "hour"
        : "minute";
  const intervalValue = intervalMinutes / MINUTES_PER_UNIT[intervalUnit];
  const onToggleSchedule = useCallback(
    (enabled: boolean) => {
      if (!jobId) return;
      void updateJob(jobId, {
        schedule: { enabled, intervalMinutes: Math.max(intervalMinutes, 1) },
      });
    },
    [jobId, intervalMinutes],
  );
  // Persist a new value/unit pair as canonical minutes (≥ 1).
  const onChangeInterval = useCallback(
    (value: number, unit: ScheduleUnit) => {
      if (!jobId || Number.isNaN(value)) return;
      const minutes = Math.max(Math.round(value * MINUTES_PER_UNIT[unit]), 1);
      void updateJob(jobId, {
        schedule: { enabled: scheduleEnabled, intervalMinutes: minutes },
      });
    },
    [jobId, scheduleEnabled],
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
  const onAddAction = useCallback(
    (parentId: string, slot: Slot, action: ActionDef, group: ActionGroup) =>
      // The step's *label* is the concise title (one clean narrative line); the
      // full instruction is stored separately for when the flow actually runs.
      setTree((t) =>
        attachAction(t, parentId, slot, action.id, action.label, group.name, action.instruction),
      ),
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
    // Surface the run in the Runs tab but DON'T navigate into its session —
    // the run executes in the background; the user opens it from the Runs tab
    // when they choose to.
    setTab("runs");
    try {
      // Persist the on-screen tree first so the run uses the current flow.
      await updateJob(jobId, { tree });
      await runJob(jobId);
    } catch (err) {
      window.alert(err instanceof Error ? err.message : "Failed to run job");
    } finally {
      setRunning(false);
    }
  }, [jobId, tree, running]);

  const copyOutput = useCallback(() => {
    // The "Narrative" tab renders the outline text (the old free-form narrative
    // tab was removed and this one renamed).
    const text = tab === "mermaid" ? result.mermaid : result.outline;
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
        <Input
          aria-label="Flow name"
          data-testid="job-name-input"
          value={name}
          placeholder="Flow builder"
          onChange={(e) => setName(e.target.value)}
          onBlur={commitName}
          onKeyDown={(e) => {
            if (e.key === "Enter") e.currentTarget.blur();
            if (e.key === "Escape") {
              setName(job?.name ?? "");
              e.currentTarget.blur();
            }
          }}
          disabled={!jobId}
          className="h-8 min-w-0 flex-1 border-transparent bg-transparent px-2 text-sm font-medium hover:border-input focus-visible:border-input"
        />
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
        {/* Host picker: where the run's runner launches. Persisted on the job;
            empty means "any online host" (chosen at run time). */}
        <select
          aria-label="Run on host"
          data-testid="job-host-select"
          value={job?.hostId ?? ""}
          onChange={(e) => onPickHost(e.target.value)}
          disabled={!jobId}
          className="h-8 rounded-md border border-input bg-background px-2 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
        >
          <option value="">Any host</option>
          {(hosts ?? []).map((h) => (
            <option key={h.host_id} value={h.host_id}>
              {h.name}
              {h.status === "offline" ? " (offline)" : ""}
            </option>
          ))}
        </select>
        {/* Schedule (time trigger): run automatically every N minutes. */}
        <div
          className="flex items-center gap-2 rounded-md border border-input px-2 py-1"
          title="Run this flow automatically on a fixed interval"
        >
          <Switch
            aria-label="Enable schedule"
            data-testid="job-schedule-toggle"
            checked={scheduleEnabled}
            onCheckedChange={onToggleSchedule}
            disabled={!jobId}
          />
          <span className="text-xs whitespace-nowrap text-muted-foreground">every</span>
          <Input
            type="number"
            min={1}
            step={1}
            aria-label="Schedule interval value"
            data-testid="job-schedule-interval"
            value={intervalValue}
            onChange={(e) => onChangeInterval(Number(e.target.value), intervalUnit)}
            disabled={!jobId || !scheduleEnabled}
            className="h-7 w-16 px-2 text-sm"
          />
          <select
            aria-label="Schedule interval unit"
            data-testid="job-schedule-unit"
            value={intervalUnit}
            onChange={(e) => onChangeInterval(intervalValue, e.target.value as ScheduleUnit)}
            disabled={!jobId || !scheduleEnabled}
            className="h-7 rounded-md border border-input bg-background px-1.5 text-sm outline-none focus-visible:ring-2 focus-visible:ring-ring"
          >
            {(["minute", "hour", "day"] as const).map((u) => (
              <option key={u} value={u}>
                {UNIT_LABELS[u]}
              </option>
            ))}
          </select>
        </div>
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
        {/* Stepper canvas. `overflow-auto` scrolls both axes; the inner track
            is `w-max` (sized to the widest row) with `mx-auto`, so the tree is
            centered when it fits and scrolls — without clipping the left edge —
            once dense branches make it wider than the viewport. */}
        <div className="min-w-0 flex-1 overflow-auto bg-[radial-gradient(circle,var(--border)_1px,transparent_1px)] [background-size:22px_22px]">
          <div className="mx-auto flex min-h-full w-max flex-col items-center p-10">
            <StepView
              step={tree}
              isRoot
              onAdd={onAdd}
              onAddAction={onAddAction}
              onRename={onRename}
              onRenameBranch={onRenameBranch}
              onDelete={onDelete}
              groups={groups}
              loadingGroups={loadingGroups}
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

/**
 * A brief, on-demand status of where a run's flow execution currently is.
 *
 * Clicking toggles a one-shot fetch of the run's session: the session status
 * (idle / running / waiting / failed) plus, when available, the agent's latest
 * output text — which, under the execution-engine system prompt, narrates the
 * current step ("Step 3 — …"). Lazy by design: nothing fetches until clicked,
 * and each open re-fetches so the status is fresh without background polling.
 *
 * Native runs persist no assistant items, so the progress line may be absent —
 * the session status still gives a useful "running vs done vs failed" signal.
 */
function RunStatusButton({ run }: { run: Run }) {
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [status, setStatus] = useState<string | null>(null);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!run.sessionId) return;
    setLoading(true);
    setError(null);
    try {
      const [session, latest] = await Promise.all([
        getSession(run.sessionId).catch(() => null),
        fetchLastAssistantText(run.sessionId, 240).catch(() => undefined),
      ]);
      setStatus(session?.status ?? null);
      setProgress(latest ?? null);
      if (!session) setError("Couldn't reach the session.");
    } finally {
      setLoading(false);
    }
  }, [run.sessionId]);

  const onToggle = useCallback(() => {
    setOpen((wasOpen) => {
      if (!wasOpen) void load(); // refetch on each open
      return !wasOpen;
    });
  }, [load]);

  if (!run.sessionId) return null;
  return (
    <div className="mt-2 inline-flex flex-col">
      <button
        type="button"
        onClick={onToggle}
        className="inline-flex items-center gap-1 text-xs font-medium text-primary hover:underline"
      >
        <ActivityIcon className="size-3.5" /> Status
      </button>
      {open && (
        <div className="mt-1.5 max-w-[360px] rounded-md border border-border bg-card p-2 text-xs">
          {loading ? (
            <span className="inline-flex items-center gap-1 text-muted-foreground">
              <Loader2Icon className="size-3.5 animate-spin" /> Checking…
            </span>
          ) : error ? (
            <span className="text-muted-foreground">{error}</span>
          ) : (
            <div className="flex flex-col gap-1">
              <div className="flex items-center gap-1.5">
                <span className="text-muted-foreground">Session:</span>
                <span className="font-medium capitalize">{status ?? "unknown"}</span>
              </div>
              <div>
                <span className="text-muted-foreground">Currently: </span>
                {progress ? (
                  <span className="whitespace-pre-wrap">{progress}</span>
                ) : (
                  <span className="text-muted-foreground italic">
                    {run.status === "running"
                      ? "Executing — no step reported yet (open the session to watch live)."
                      : "No progress detail available."}
                  </span>
                )}
              </div>
            </div>
          )}
        </div>
      )}
    </div>
  );
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
          <div key={run.id} className="rounded-md border border-border bg-background/40 p-2.5">
            <details>
              <summary className="flex cursor-pointer list-none items-center gap-2 text-sm">
                <RunStatusIcon status={run.status} />
                <span className="font-medium">Run #{run.number}</span>
                <span className="text-xs text-muted-foreground capitalize">{run.status}</span>
                <span
                  className={cn(
                    "rounded px-1.5 py-0.5 text-[10px] font-medium",
                    run.trigger === "scheduled"
                      ? "bg-violet-500/15 text-violet-600 dark:text-violet-400"
                      : "bg-muted text-muted-foreground",
                  )}
                >
                  {run.trigger === "scheduled" ? "Scheduled" : "Manual"}
                </span>
                <span className="ml-auto text-xs text-muted-foreground tabular-nums">
                  {duration}
                </span>
              </summary>
              <pre className="mt-2 border-t border-border pt-2 font-mono text-[11px] leading-relaxed whitespace-pre-wrap text-muted-foreground">
                {run.logs.join("\n")}
              </pre>
            </details>
            {/* The run executes in its own session in the background; opening
                it is always one explicit click away (never auto-navigated).
                A run is a native Claude-CLI session whose conversation lives in
                the terminal, so seed the AppShell's panel-key (the empty
                PANEL_NO_TERMINAL_KEY sentinel → terminal view, which resolves
                to the agent's own terminal) before navigating, landing the
                session on its terminal instead of the empty chat surface. */}
            {run.sessionId ? (
              <div className="flex items-start gap-4">
                <Link
                  to={`/c/${run.sessionId}`}
                  onClick={() => {
                    try {
                      sessionStorage.setItem(
                        `omnigent.ap-web.panel-key:${run.sessionId}`,
                        "",
                      );
                    } catch {
                      // sessionStorage unavailable (private mode / quota) — fall
                      // back to the default chat view; not worth blocking the nav.
                    }
                  }}
                  className="mt-2 inline-block text-xs font-medium text-primary hover:underline"
                >
                  Open session →
                </Link>
                <RunStatusButton run={run} />
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
