import { useState, type FormEvent } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { PencilIcon, PlusIcon, Trash2Icon } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import { Spinner } from "@/components/ui/spinner";
import {
  PROJECT_KPIS_MAX,
  UNMEASURED_METRIC,
  formatKpiTarget,
  formatKpiValue,
  getProjectKpis,
  kpiDefinition,
  projectKpisKey,
  setProjectKpis,
  type KpiDirection,
  type KpiMetric,
  type KpiMetricId,
  type KpiStatus,
  type ProjectKpiDefinition,
  type ProjectKpiValue,
  type ProjectKpis,
} from "@/lib/projectKpisApi";
import type { ProjectStatusWindow } from "@/lib/projectStatusApi";

const KPI_WINDOW: ProjectStatusWindow = "7d";
import { formatUploadTime } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import type { StatusItemMetric } from "@/lib/projectStatusItemsApi";
import type { StatusDrilldown } from "./ProjectStatusItems";
import { TicketError } from "./TicketControls";

const TIME_METRICS = new Set<KpiMetricId>([
  "time_to_merged_median_hours",
  "time_to_merged_p90_hours",
]);

/** The Summary's KPI section: one tile per KPI, who set them, and the owner's Edit. */
export function ProjectKpiTiles({
  projectId,
  onDrill,
}: {
  projectId: string;
  /** Opens the items behind a tile. */
  onDrill?: (drilldown: StatusDrilldown) => void;
}) {
  const [editing, setEditing] = useState(false);
  // Tiles always show the last 7 full days, whatever window the page shows.
  const kpis = useQuery({
    queryKey: projectKpisKey(projectId, KPI_WINDOW),
    queryFn: ({ signal }) => getProjectKpis(projectId, KPI_WINDOW, signal),
    staleTime: 30_000,
  });
  return (
    <section aria-label="KPIs" className="space-y-3" data-testid="project-kpi-slot">
      <div className="flex items-center gap-2">
        <h2 className="min-w-0 flex-1 text-[length:calc(var(--text-ui)+2px)] font-bold">KPIs</h2>
        {kpis.data?.can_edit && (
          <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
            <PencilIcon aria-hidden />
            {kpis.data.kpis.length ? "Edit" : "Add KPIs"}
          </Button>
        )}
      </div>
      {kpis.isPending ? (
        <Spinner aria-label="Loading KPIs" />
      ) : kpis.isError ? (
        <div className="space-y-2">
          <TicketError error={kpis.error} />
          <Button variant="ghost" size="sm" onClick={() => void kpis.refetch()}>
            Try again
          </Button>
        </div>
      ) : kpis.data.kpis.length === 0 ? (
        <p className="rounded-lg border border-dashed border-border/70 p-4 text-sm text-muted-foreground">
          {kpis.data.can_edit
            ? "No KPIs yet. Add 3 or 4 numbers that show whether this project is working."
            : "No KPIs yet. The project owner or its Lead sets them."}
        </p>
      ) : (
        <>
          <ul className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
            {kpis.data.kpis.map((kpi) => (
              <KpiTile key={kpi.position} kpi={kpi} onDrill={onDrill} />
            ))}
          </ul>
          <KpiFooter kpis={kpis.data} />
        </>
      )}
      {editing && kpis.data && (
        <ProjectKpisDialog
          projectId={projectId}
          current={kpis.data}
          onClose={() => setEditing(false)}
        />
      )}
    </section>
  );
}

/** "Just now" and "Today 2:41 PM" read mid-sentence as "just now", "today 2:41 PM". */
function lowerFirst(text: string): string {
  return /^(Just|Today|Yesterday)\b/.test(text) ? text[0].toLowerCase() + text.slice(1) : text;
}

function KpiFooter({ kpis }: { kpis: ProjectKpis }) {
  if (!kpis.updated_at) return null;
  const who = kpis.updated_by_session_id
    ? "the Lead"
    : (kpis.updated_by_user_id ?? "the project owner");
  return (
    <p className="text-xs text-muted-foreground" data-testid="project-kpi-footer">
      Set by {who}, {lowerFirst(formatUploadTime(kpis.updated_at))}. Each tile covers the 7 full UTC
      days ending yesterday; today is shown separately.
    </p>
  );
}

const STATUS_BADGE: Record<KpiStatus, { label: string; className?: string }> = {
  met: { label: "On target", className: "bg-success/10 text-success" },
  missed: { label: "Off target", className: "bg-destructive/10 text-destructive" },
  no_data: { label: "No data yet" },
  unavailable: { label: "Not tracked here" },
  not_measured: { label: "Not measured yet" },
};

function StatusBadge({ status }: { status: KpiStatus }) {
  const badge = STATUS_BADGE[status];
  return (
    <Badge
      variant={badge.className ? "secondary" : "outline"}
      className={cn("text-xs font-normal", badge.className)}
      data-testid="project-kpi-status"
    >
      {badge.label}
    </Badge>
  );
}

export function KpiTile({
  kpi,
  onDrill,
}: {
  kpi: ProjectKpiValue;
  onDrill?: (drilldown: StatusDrilldown) => void;
}) {
  const trend = kpi.trend;
  const target = formatKpiTarget(kpi);
  const measured = kpi.metric !== UNMEASURED_METRIC && trend !== null;
  const status = measured ? trend.status : "not_measured";
  const sample = trend?.last_7_days_sample_size;
  return (
    <li
      className="flex min-w-0 flex-col gap-2 rounded-lg border border-border/60 p-3"
      data-testid="project-kpi-tile"
    >
      <div className="flex items-start gap-2">
        <h3 className="min-w-0 flex-1 font-medium wrap-anywhere">{kpi.name}</h3>
        <StatusBadge status={status} />
      </div>
      {target && <p className="text-xs text-muted-foreground">Target: {target}</p>}
      {measured ? (
        <>
          <div className="flex items-end justify-between gap-3">
            <div>
              <p className="text-2xl font-semibold tabular-nums">
                {formatKpiValue(trend.last_7_days, kpi.unit)}
              </p>
              <p className="text-xs text-muted-foreground">
                Last 7 days
                {TIME_METRICS.has(kpi.metric) && sample
                  ? `, from ${sample} ${sample === 1 ? "ticket" : "tickets"}`
                  : ""}
              </p>
            </div>
            <p className="text-right text-xs text-muted-foreground">
              Today so far
              <span className="block text-sm font-medium text-foreground tabular-nums">
                {formatKpiValue(trend.today, kpi.unit)}
              </span>
            </p>
          </div>
          <KpiBars kpi={kpi} />
          {onDrill && trend.status !== "unavailable" && (
            <Button
              variant="ghost"
              size="sm"
              className="-ml-2 self-start"
              onClick={() =>
                onDrill({
                  label: kpi.name,
                  metric: kpi.metric as StatusItemMetric,
                  range: "trend",
                })
              }
            >
              See the items
            </Button>
          )}
        </>
      ) : (
        <p className="text-sm text-muted-foreground">
          {kpi.not_measured_reason || "There's no metric for it yet."}
        </p>
      )}
      <p className="line-clamp-2 text-xs text-muted-foreground" title={kpi.why}>
        {kpi.why}
      </p>
    </li>
  );
}

/** One bar per UTC day, oldest first; today's bar is lighter because it's still running. */
function KpiBars({ kpi }: { kpi: ProjectKpiValue }) {
  const days = kpi.trend?.days ?? [];
  const max = Math.max(0, ...days.map((day) => day.value ?? 0));
  return (
    <div
      className="flex h-10 items-end gap-1"
      role="img"
      aria-label={`Per day: ${days
        .map((day) => `${day.day_utc} ${formatKpiValue(day.value, kpi.unit)}`)
        .join(", ")}`}
      data-testid="project-kpi-bars"
    >
      {days.map((day, index) => {
        const today = index === days.length - 1;
        const height = max > 0 && day.value ? Math.max(6, (day.value / max) * 100) : 0;
        return (
          <span
            key={day.day_utc}
            title={`${day.day_utc}${today ? " (today)" : ""}: ${formatKpiValue(day.value, kpi.unit)}`}
            className="flex h-full flex-1 items-end rounded-sm bg-muted/60"
          >
            <span
              className={cn("w-full rounded-sm bg-primary", today && "opacity-40")}
              style={{ height: `${height}%` }}
            />
          </span>
        );
      })}
    </div>
  );
}

interface DraftKpi {
  /** Stable row key while editing. */
  key: number;
  name: string;
  metric: KpiMetricId;
  why: string;
  target: string;
  direction: KpiDirection;
  reason: string;
}

let nextDraftKey = 0;

function toDraft(kpi: ProjectKpiDefinition): DraftKpi {
  return {
    key: nextDraftKey++,
    name: kpi.name,
    metric: kpi.metric,
    why: kpi.why,
    target: kpi.target === null || kpi.target === undefined ? "" : String(kpi.target),
    direction: kpi.direction ?? "at_least",
    reason: kpi.not_measured_reason ?? "",
  };
}

function emptyDraft(): DraftKpi {
  return toDraft({ name: "", metric: "merged_prs", why: "" });
}

/** The draft as the server's definition, or the first problem to fix. */
export function draftToDefinition(draft: Omit<DraftKpi, "key">): ProjectKpiDefinition | string {
  const name = draft.name.trim();
  const why = draft.why.trim();
  if (!name) return "Give every KPI a name.";
  if (!why) return `Say why "${name}" matters.`;
  if (draft.metric === UNMEASURED_METRIC) {
    if (!draft.reason.trim()) return `Say why "${name}" isn't measured yet.`;
    return kpiDefinition({ name, why, metric: draft.metric, not_measured_reason: draft.reason });
  }
  const target = Number(draft.target);
  if (draft.target.trim() === "" || !Number.isFinite(target) || target < 0)
    return `Give "${name}" a target of zero or more.`;
  return kpiDefinition({ name, why, metric: draft.metric, target, direction: draft.direction });
}

function ProjectKpisDialog({
  projectId,
  current,
  onClose,
}: {
  projectId: string;
  current: ProjectKpis;
  onClose: () => void;
}) {
  const [drafts, setDrafts] = useState<DraftKpi[]>(() =>
    current.kpis.length ? current.kpis.map(toDraft) : [emptyDraft()],
  );
  const [problem, setProblem] = useState<string | null>(null);
  const client = useQueryClient();
  const save = useMutation({
    mutationFn: (kpis: ProjectKpiDefinition[]) =>
      setProjectKpis(projectId, kpis, current.revision, KPI_WINDOW),
    onSuccess: (saved) => {
      client.setQueryData(projectKpisKey(projectId, KPI_WINDOW), saved);
      void client.invalidateQueries({ queryKey: projectKpisKey(projectId) });
      onClose();
    },
  });
  const busy = save.isPending;
  const update = (index: number, change: Partial<DraftKpi>) =>
    setDrafts((rows) => rows.map((row, at) => (at === index ? { ...row, ...change } : row)));
  const onSubmit = (event: FormEvent) => {
    event.preventDefault();
    const definitions = drafts.map(draftToDefinition);
    const first = definitions.find((entry) => typeof entry === "string");
    if (typeof first === "string") {
      setProblem(first);
      return;
    }
    setProblem(null);
    save.mutate(definitions as ProjectKpiDefinition[]);
  };
  return (
    <Dialog
      open
      onOpenChange={(open) => {
        if (!open && !busy) onClose();
      }}
    >
      <DialogContent className="max-h-[85dvh] overflow-y-auto sm:max-w-2xl">
        <DialogHeader>
          <DialogTitle>Project KPIs</DialogTitle>
          <DialogDescription>
            Pick 3 or 4 numbers that show whether this project is working, each with a target.
            Saving keeps the earlier set in the history. Only you and the project&rsquo;s Lead can
            change them.
          </DialogDescription>
        </DialogHeader>
        <form onSubmit={onSubmit} className="flex flex-col gap-4">
          {drafts.map((draft, index) => (
            <KpiDraftFields
              key={draft.key}
              index={index}
              draft={draft}
              metrics={current.metrics}
              disabled={busy}
              onChange={(change) => update(index, change)}
              onRemove={() => setDrafts((rows) => rows.filter((_, at) => at !== index))}
            />
          ))}
          {drafts.length < PROJECT_KPIS_MAX && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              className="self-start"
              disabled={busy}
              onClick={() => setDrafts((rows) => [...rows, emptyDraft()])}
            >
              <PlusIcon aria-hidden />
              Add a KPI
            </Button>
          )}
          {problem && (
            <p className="text-sm text-destructive" role="alert">
              {problem}
            </p>
          )}
          <TicketError error={save.error} />
          <DialogFooter className="border-t-0 bg-transparent">
            <Button type="button" variant="ghost" onClick={onClose} disabled={busy}>
              Cancel
            </Button>
            <Button type="submit" loading={busy} data-testid="project-kpis-save">
              Save
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}

function KpiDraftFields({
  index,
  draft,
  metrics,
  disabled,
  onChange,
  onRemove,
}: {
  index: number;
  draft: DraftKpi;
  metrics: KpiMetric[];
  disabled: boolean;
  onChange: (change: Partial<DraftKpi>) => void;
  onRemove: () => void;
}) {
  const label = (field: string) => `KPI ${index + 1} ${field}`;
  const unmeasured = draft.metric === UNMEASURED_METRIC;
  const unit = metrics.find((metric) => metric.id === draft.metric)?.unit;
  return (
    <fieldset
      className="grid gap-2 rounded-lg border border-border/60 p-3 sm:grid-cols-2"
      disabled={disabled}
    >
      <legend className="sr-only">KPI {index + 1}</legend>
      <Input
        aria-label={label("name")}
        placeholder="Name, e.g. Weekly PRs"
        value={draft.name}
        maxLength={80}
        onChange={(event) => onChange({ name: event.target.value })}
      />
      <Select
        value={draft.metric}
        onValueChange={(metric) => onChange({ metric: metric as KpiMetricId })}
      >
        <SelectTrigger aria-label={label("metric")} className="w-full">
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {metrics.map((metric) => (
            <SelectItem key={metric.id} value={metric.id}>
              {metric.label}
            </SelectItem>
          ))}
          <SelectItem value={UNMEASURED_METRIC}>Not measured yet</SelectItem>
        </SelectContent>
      </Select>
      <Input
        aria-label={label("why it matters")}
        placeholder="Why it matters"
        className="sm:col-span-2"
        value={draft.why}
        maxLength={500}
        onChange={(event) => onChange({ why: event.target.value })}
      />
      {unmeasured ? (
        <Input
          aria-label={label("reason it isn't measured")}
          placeholder="Why it isn't measured yet"
          className="sm:col-span-2"
          value={draft.reason}
          maxLength={300}
          onChange={(event) => onChange({ reason: event.target.value })}
        />
      ) : (
        <>
          <Select
            value={draft.direction}
            onValueChange={(direction) => onChange({ direction: direction as KpiDirection })}
          >
            <SelectTrigger aria-label={label("direction")} className="w-full">
              <SelectValue />
            </SelectTrigger>
            <SelectContent>
              <SelectItem value="at_least">At least</SelectItem>
              <SelectItem value="at_most">At most</SelectItem>
            </SelectContent>
          </Select>
          <Input
            aria-label={label("target")}
            placeholder={
              unit === "usd" ? "Target in dollars" : unit === "hours" ? "Target in hours" : "Target"
            }
            inputMode="decimal"
            value={draft.target}
            onChange={(event) => onChange({ target: event.target.value })}
          />
        </>
      )}
      <Button
        type="button"
        variant="ghost"
        size="sm"
        className="justify-self-start text-muted-foreground sm:col-span-2"
        onClick={onRemove}
      >
        <Trash2Icon aria-hidden />
        Remove
      </Button>
    </fieldset>
  );
}
