import { useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlignLeftIcon,
  ArrowDownIcon,
  ArrowUpIcon,
  PencilIcon,
  SquareKanbanIcon,
} from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  PROJECT_STATUS_WINDOWS,
  formatCents,
  getProjectStatusSummary,
  projectStatusSummaryKey,
  sortStatusMembers,
  type ProjectStatusMember,
  type ProjectStatusSortKey,
  type ProjectStatusSummary,
  type ProjectStatusWindow,
} from "@/lib/projectStatusApi";
import { cn } from "@/lib/utils";
import { ProjectDescriptionDialog } from "./ProjectDescriptionDialog";
import { MemberName, memberDisplayName } from "./MemberName";
import { ProjectKpiTiles } from "./ProjectKpis";
import { ProjectStatusItemsDialog, type StatusDrilldown } from "./ProjectStatusItems";
import { useProjectMandate } from "./ProjectMandateDialog";
import { TicketLoadError } from "./TicketControls";
import { TicketMarkdown } from "./ticketImages";
import { useTicketProject } from "./useProjectTickets";

/** The project's Summary tab: its charter, headline numbers, KPIs and per-member table. */
export function ProjectSummary({ projectName }: { projectName: string }) {
  const { projectId, projects } = useTicketProject(projectName);
  const [statusWindow, setStatusWindow] = useState<ProjectStatusWindow>("7d");
  const [drilldown, setDrilldown] = useState<StatusDrilldown | null>(null);
  const summary = useQuery({
    queryKey: projectStatusSummaryKey(projectId, statusWindow),
    queryFn: ({ signal }) => getProjectStatusSummary(projectId as string, statusWindow, signal),
    enabled: projectId !== null,
    staleTime: 30_000,
  });
  const memberNames = useMemo(
    () =>
      new Map(
        (summary.data?.members ?? []).map((member) => [member.key, memberDisplayName(member)]),
      ),
    [summary.data],
  );
  if (projects.isLoading) return <Spinner aria-label="Loading project" />;
  if (projectId === null)
    return (
      <div className="mx-auto w-full max-w-5xl px-1 pt-3">
        <TicketLoadError error={projects.error ?? new Error("Project not found.")} />
      </div>
    );
  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-1 pb-10" data-testid="project-summary">
      <ProjectCharter projectName={projectName} projectId={projectId} summary={summary.data} />
      <section aria-label="Headline numbers" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Status</h2>
          <WindowPicker value={statusWindow} onChange={setStatusWindow} />
        </div>
        <TicketLoadError
          error={summary.error}
          hasData={summary.data !== undefined}
          onRetry={() => void summary.refetch()}
        />
        {summary.data ? (
          <>
            <HeadlineNumbers summary={summary.data} onDrill={setDrilldown} />
            <p className="text-xs text-muted-foreground">
              {summary.data.since_day_utc
                ? `${summary.data.since_day_utc} to ${summary.data.today_utc}, UTC`
                : `All time to ${summary.data.today_utc}, UTC`}
            </p>
          </>
        ) : summary.isPending ? (
          <Spinner aria-label="Loading summary" />
        ) : null}
      </section>
      <ProjectKpiTiles projectId={projectId} onDrill={setDrilldown} />
      <section aria-label="Members" className="space-y-3">
        <h2 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Members</h2>
        {summary.data ? (
          <MembersTable
            members={summary.data.members}
            statusWindow={statusWindow}
            onDrill={setDrilldown}
          />
        ) : summary.isPending ? (
          <Spinner aria-label="Loading members" />
        ) : (
          <p className="text-sm text-muted-foreground">Members are unavailable right now.</p>
        )}
      </section>
      {drilldown && (
        <ProjectStatusItemsDialog
          projectId={projectId}
          projectName={projectName}
          drilldown={drilldown}
          memberNames={memberNames}
          onClose={() => setDrilldown(null)}
        />
      )}
    </div>
  );
}

function ProjectCharter({
  projectName,
  projectId,
  summary,
}: {
  projectName: string;
  projectId: string;
  summary: ProjectStatusSummary | undefined;
}) {
  const info = useServerInfo();
  const project = useProjectMandate(projectId);
  const [editing, setEditing] = useState(false);
  const descriptionEnabled = info !== "loading" && info.project_description === true;
  // Members read it; the owner edits it here, and the Lead through its tool.
  const canEdit = descriptionEnabled && project.data !== undefined && project.data.shared !== true;
  const description = (project.data?.description ?? summary?.description ?? "").trim();
  return (
    <section aria-label="Description" className="space-y-2">
      <div className="flex items-center gap-2">
        <h2 className="min-w-0 flex-1 text-[length:calc(var(--text-ui)+2px)] font-bold">
          About this project
        </h2>
        {canEdit && (
          <Button variant="ghost" size="sm" onClick={() => setEditing(true)}>
            <PencilIcon aria-hidden />
            {description ? "Edit" : "Add description"}
          </Button>
        )}
      </div>
      {project.isPending && !summary ? (
        <Spinner aria-label="Loading description" />
      ) : description ? (
        <TicketMarkdown className="text-ui leading-relaxed">{description}</TicketMarkdown>
      ) : (
        <p className="text-sm text-muted-foreground">
          {canEdit
            ? "No description yet. Add one so the team knows what this project is for."
            : "No description yet."}
        </p>
      )}
      {editing && (
        <ProjectDescriptionDialog projectName={projectName} onClose={() => setEditing(false)} />
      )}
    </section>
  );
}

function WindowPicker({
  value,
  onChange,
}: {
  value: ProjectStatusWindow;
  onChange: (statusWindow: ProjectStatusWindow) => void;
}) {
  return (
    <div role="group" aria-label="Time window" className="flex rounded-md border p-0.5">
      {PROJECT_STATUS_WINDOWS.map((option) => (
        <Button
          key={option.id}
          variant="ghost"
          size="sm"
          aria-pressed={value === option.id}
          className={cn("h-7 px-2.5", value === option.id && "bg-muted font-medium")}
          onClick={() => onChange(option.id)}
        >
          {option.label}
        </Button>
      ))}
    </div>
  );
}

function HeadlineNumbers({
  summary,
  onDrill,
}: {
  summary: ProjectStatusSummary;
  onDrill: (drilldown: StatusDrilldown) => void;
}) {
  const { totals, sources, window: range } = summary;
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatTile
        label="PRs merged"
        value={totals.prs_merged === null ? "—" : String(totals.prs_merged)}
        note={sources.prs_merged === "unavailable" ? "Not tracked on this server" : undefined}
        onOpen={
          totals.prs_merged === null
            ? undefined
            : () => onDrill({ label: "PRs merged", metric: "merged_prs", range })
        }
      />
      <StatTile
        label="Spent"
        value={formatCents(totals.spend_cents)}
        note={sources.spend === "unavailable" ? "Not tracked on this server" : undefined}
        onOpen={
          totals.spend_cents === null
            ? undefined
            : () => onDrill({ label: "Spent", metric: "spend_usd", range })
        }
      />
      <StatTile label="Team" value={teamCount(summary.members)} />
      <StatTile
        label="Tickets done"
        value={String(totals.tickets_done)}
        onOpen={() => onDrill({ label: "Tickets done", metric: "tickets_shipped", range })}
      />
    </dl>
  );
}

/** "2 people · 10 roles": the Lead and named roles count as roles; the agents pool doesn't. */
export function teamCount(members: readonly ProjectStatusMember[]): string {
  const people = members.filter((member) => member.kind === "human").length;
  const roles = members.filter((member) => member.kind === "identity").length;
  const plural = (count: number, one: string, many: string) =>
    `${count} ${count === 1 ? one : many}`;
  return `${plural(people, "person", "people")} · ${plural(roles, "role", "roles")}`;
}

function StatTile({
  label,
  value,
  note,
  onOpen,
}: {
  label: string;
  value: string;
  note?: string;
  onOpen?: () => void;
}) {
  return (
    <div className="rounded-lg border border-border/60 p-3">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums">
        {onOpen ? (
          <button
            type="button"
            className="rounded-sm underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            aria-label={`${label}: ${value}. Show the items behind it`}
            onClick={onOpen}
          >
            {value}
          </button>
        ) : (
          value
        )}
      </dd>
      {note && <dd className="mt-1 text-xs text-muted-foreground">{note}</dd>}
    </div>
  );
}

const COLUMNS: { key: ProjectStatusSortKey; label: string; numeric: boolean }[] = [
  { key: "name", label: "Member", numeric: false },
  { key: "tickets_done", label: "Tickets done", numeric: true },
  { key: "prs_merged", label: "PRs merged", numeric: true },
  { key: "spend_cents", label: "Spend", numeric: true },
  { key: "cost_per_merged_pr_cents", label: "Cost per merged PR", numeric: true },
];

function MembersTable({
  members,
  statusWindow,
  onDrill,
}: {
  members: ProjectStatusMember[];
  statusWindow: ProjectStatusWindow;
  onDrill: (drilldown: StatusDrilldown) => void;
}) {
  const [sort, setSort] = useState<{ key: ProjectStatusSortKey; direction: "asc" | "desc" }>({
    key: "tickets_done",
    direction: "desc",
  });
  if (!members.length)
    return <p className="text-sm text-muted-foreground">No members have activity yet.</p>;
  const rows = sortStatusMembers(members, sort.key, sort.direction);
  return (
    <div className="overflow-x-auto rounded-lg border border-border/60">
      <table className="w-full text-sm">
        <thead className="border-b border-border/60 text-muted-foreground">
          <tr>
            {COLUMNS.map((column) => {
              const active = sort.key === column.key;
              return (
                <th
                  key={column.key}
                  scope="col"
                  aria-sort={
                    active ? (sort.direction === "asc" ? "ascending" : "descending") : undefined
                  }
                  className={cn(
                    "px-3 py-2 font-medium",
                    column.numeric ? "text-right" : "text-left",
                  )}
                >
                  <button
                    type="button"
                    className="inline-flex items-center gap-1 rounded-sm hover:text-foreground focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
                    onClick={() =>
                      setSort({
                        key: column.key,
                        direction:
                          active && sort.direction === "desc"
                            ? "asc"
                            : active
                              ? "desc"
                              : column.numeric
                                ? "desc"
                                : "asc",
                      })
                    }
                  >
                    {column.label}
                    {active &&
                      (sort.direction === "asc" ? (
                        <ArrowUpIcon aria-hidden className="size-3.5" />
                      ) : (
                        <ArrowDownIcon aria-hidden className="size-3.5" />
                      ))}
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody className="divide-y divide-border/60">
          {rows.map((member) => (
            <tr key={member.key}>
              <th scope="row" className="px-3 py-2 text-left font-normal">
                <span className="flex items-center gap-2">
                  <MemberName member={member} />
                  <Badge variant="outline" className="shrink-0 text-xs font-normal">
                    {member.kind === "human" ? "Person" : "Agent"}
                  </Badge>
                </span>
              </th>
              <NumberCell
                label={`Tickets done by ${memberDisplayName(member)}`}
                onOpen={() =>
                  onDrill({
                    label: "Tickets done",
                    metric: "tickets_shipped",
                    range: statusWindow,
                    member: member.key,
                  })
                }
              >
                {String(member.tickets_done)}
              </NumberCell>
              <NumberCell
                label={`PRs merged by ${memberDisplayName(member)}`}
                onOpen={
                  member.prs_merged === null
                    ? undefined
                    : () =>
                        onDrill({
                          label: "PRs merged",
                          metric: "merged_prs",
                          range: statusWindow,
                          member: member.key,
                        })
                }
              >
                {member.prs_merged === null ? "—" : String(member.prs_merged)}
              </NumberCell>
              <NumberCell
                label={`Spend by ${memberDisplayName(member)}`}
                onOpen={
                  member.spend_cents === null
                    ? undefined
                    : () =>
                        onDrill({
                          label: "Spent",
                          metric: "spend_usd",
                          range: statusWindow,
                          member: member.key,
                        })
                }
              >
                {formatCents(member.spend_cents)}
              </NumberCell>
              <NumberCell>{formatCents(member.cost_per_merged_pr_cents)}</NumberCell>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NumberCell({
  children,
  label,
  onOpen,
}: {
  children: ReactNode;
  label?: string;
  onOpen?: () => void;
}) {
  return (
    <td className="px-3 py-2 text-right tabular-nums">
      {onOpen ? (
        <button
          type="button"
          className="rounded-sm underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
          aria-label={label ? `${label}: show the items behind it` : undefined}
          onClick={onOpen}
        >
          {children}
        </button>
      ) : (
        children
      )}
    </td>
  );
}

/**
 * The project's view toolbar, in the same place on both views: the Board /
 * Summary switch, an optional control after it (the board's ticket filter) and
 * optional tools at the right end.
 */
export function ProjectHomeTabs({
  summaryOpen,
  onChange,
  className,
  trailing,
  end,
}: {
  summaryOpen: boolean;
  onChange: (summary: boolean) => void;
  className?: string;
  trailing?: ReactNode;
  end?: ReactNode;
}) {
  return (
    <div
      className={cn(
        // Named like the top bar so tools moved here collapse at the same widths.
        "@container/topbar mx-4 mb-3 flex shrink-0 items-center gap-3 border-b border-border/70 pb-2.5",
        className,
      )}
    >
      <div role="tablist" aria-label="Project views" className="flex rounded-[9px] bg-muted p-0.5">
        {[
          { summary: false, label: "Board", Icon: SquareKanbanIcon },
          { summary: true, label: "Summary", Icon: AlignLeftIcon },
        ].map(({ summary, label, Icon }) => {
          const selected = summaryOpen === summary;
          return (
            <button
              key={label}
              type="button"
              role="tab"
              aria-selected={selected}
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-[7px] px-2.5 text-[13px] font-medium transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                selected
                  ? "bg-background text-foreground shadow-xs dark:bg-white/12"
                  : "text-muted-foreground hover:text-foreground",
              )}
              onClick={() => onChange(summary)}
            >
              <Icon aria-hidden className="size-3.5" />
              <span className="text-optical">{label}</span>
            </button>
          );
        })}
      </div>
      {trailing && (
        <>
          <span aria-hidden className="h-4 w-px bg-border" />
          <div className="flex items-center">{trailing}</div>
        </>
      )}
      {end && <div className="ml-auto flex min-w-0 items-center gap-1">{end}</div>}
    </div>
  );
}
