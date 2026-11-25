import { useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { ArrowDownIcon, ArrowUpIcon, PencilIcon } from "lucide-react";
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
import { ProjectKpiTiles } from "./ProjectKpis";
import { useProjectMandate } from "./ProjectMandateDialog";
import { TicketError } from "./TicketControls";
import { TicketMarkdown } from "./ticketImages";
import { useTicketProject } from "./useProjectTickets";

/** The project's Summary tab: its charter, headline numbers, KPIs and per-member table. */
export function ProjectSummary({ projectName }: { projectName: string }) {
  const { projectId, projects } = useTicketProject(projectName);
  const [statusWindow, setStatusWindow] = useState<ProjectStatusWindow>("7d");
  const summary = useQuery({
    queryKey: projectStatusSummaryKey(projectId, statusWindow),
    queryFn: ({ signal }) => getProjectStatusSummary(projectId as string, statusWindow, signal),
    enabled: projectId !== null,
    staleTime: 30_000,
  });
  if (projects.isLoading) return <Spinner aria-label="Loading project" />;
  if (projectId === null)
    return <TicketError error={projects.error ?? new Error("Project not found.")} />;
  return (
    <div className="mx-auto w-full max-w-5xl space-y-8 px-1 pb-10" data-testid="project-summary">
      <ProjectCharter projectName={projectName} projectId={projectId} summary={summary.data} />
      <section aria-label="Headline numbers" className="space-y-3">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Status</h2>
          <WindowPicker value={statusWindow} onChange={setStatusWindow} />
        </div>
        {summary.isPending ? (
          <Spinner aria-label="Loading summary" />
        ) : summary.isError ? (
          <div className="space-y-2">
            <TicketError error={summary.error} />
            <Button variant="ghost" size="sm" onClick={() => void summary.refetch()}>
              Try again
            </Button>
          </div>
        ) : (
          <>
            <HeadlineNumbers summary={summary.data} />
            <p className="text-xs text-muted-foreground">
              {summary.data.since_day_utc
                ? `${summary.data.since_day_utc} to ${summary.data.today_utc}, UTC`
                : `All time to ${summary.data.today_utc}, UTC`}
            </p>
          </>
        )}
      </section>
      <ProjectKpiTiles projectId={projectId} />
      <section aria-label="Members" className="space-y-3">
        <h2 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Members</h2>
        {summary.isPending ? (
          <Spinner aria-label="Loading members" />
        ) : summary.isError ? (
          <p className="text-sm text-muted-foreground">Members are unavailable right now.</p>
        ) : (
          <MembersTable members={summary.data.members} />
        )}
      </section>
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

function HeadlineNumbers({ summary }: { summary: ProjectStatusSummary }) {
  const { totals, sources } = summary;
  return (
    <dl className="grid grid-cols-2 gap-3 sm:grid-cols-4">
      <StatTile
        label="PRs merged"
        value={totals.prs_merged === null ? "—" : String(totals.prs_merged)}
        note={sources.prs_merged === "unavailable" ? "Not tracked on this server" : undefined}
      />
      <StatTile
        label="Spent"
        value={formatCents(totals.spend_cents)}
        note={sources.spend === "unavailable" ? "Not tracked on this server" : undefined}
      />
      <StatTile label="Team members" value={String(totals.members)} />
      <StatTile label="Tickets done" value={String(totals.tickets_done)} />
    </dl>
  );
}

function StatTile({ label, value, note }: { label: string; value: string; note?: string }) {
  return (
    <div className="rounded-lg border border-border/60 p-3">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums">{value}</dd>
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

function MembersTable({ members }: { members: ProjectStatusMember[] }) {
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
                  <span className="wrap-anywhere">{member.name}</span>
                  <Badge variant="outline" className="shrink-0 text-xs font-normal">
                    {member.kind === "human" ? "Human" : "Agent"}
                  </Badge>
                </span>
              </th>
              <NumberCell>{String(member.tickets_done)}</NumberCell>
              <NumberCell>
                {member.prs_merged === null ? "—" : String(member.prs_merged)}
              </NumberCell>
              <NumberCell>{formatCents(member.spend_cents)}</NumberCell>
              <NumberCell>{formatCents(member.cost_per_merged_pr_cents)}</NumberCell>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function NumberCell({ children }: { children: ReactNode }) {
  return <td className="px-3 py-2 text-right tabular-nums">{children}</td>;
}

/** Switches the project page between its ticket board and its Summary. */
/**
 * Board / Summary, one row in the same place on both views, with an optional
 * control (the board's ticket filter) at its right end.
 */
export function ProjectHomeTabs({
  summaryOpen,
  onChange,
  className,
  trailing,
}: {
  summaryOpen: boolean;
  onChange: (summary: boolean) => void;
  className?: string;
  trailing?: ReactNode;
}) {
  return (
    <div
      className={cn(
        "mx-4 mb-3 flex shrink-0 items-center gap-3 border-b border-border/70",
        className,
      )}
    >
      <div role="tablist" aria-label="Project views" className="flex gap-4">
        {[
          { summary: false, label: "Board" },
          { summary: true, label: "Summary" },
        ].map((tab) => (
          <Button
            key={tab.label}
            role="tab"
            variant="ghost"
            size="sm"
            aria-selected={summaryOpen === tab.summary}
            // One weight for both, so selecting a tab never changes its width.
            className={cn(
              // A transparent top border balances the underline, so the label sits centred.
              "-mb-px inline-flex h-10 items-center rounded-none border-0 border-y-2 border-t-transparent px-0.5 leading-none font-medium hover:bg-transparent",
              summaryOpen === tab.summary
                ? "border-b-foreground text-foreground"
                : "border-b-transparent text-muted-foreground hover:text-foreground",
            )}
            onClick={() => onChange(tab.summary)}
          >
            {tab.label}
          </Button>
        ))}
      </div>
      {trailing && (
        <>
          <span aria-hidden className="h-4 w-px bg-border" />
          <div className="flex items-center">{trailing}</div>
        </>
      )}
    </div>
  );
}
