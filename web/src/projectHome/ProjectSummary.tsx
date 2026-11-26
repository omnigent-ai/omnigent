import { useMemo, useState, type ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlignLeftIcon,
  ArrowDownIcon,
  ArrowUpIcon,
  PencilIcon,
  ScrollTextIcon,
  SquareKanbanIcon,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { useServerInfo } from "@/lib/CapabilitiesContext";
import {
  PROJECT_STATUS_WINDOWS,
  formatCents,
  getProjectStatusSummary,
  projectStatusSummaryKey,
  prsMergedZeroNote,
  sortStatusMembers,
  type ProjectStatusMember,
  type ProjectStatusSortKey,
  type ProjectStatusSummary,
  type ProjectStatusWindow,
} from "@/lib/projectStatusApi";
import { Link } from "@/lib/routing";
import { cn } from "@/lib/utils";
import { ProjectDescriptionDialog } from "./ProjectDescriptionDialog";
import { MemberName, memberDisplayName } from "./MemberName";
import { ProjectInsightsSection } from "./ProjectInsights";
import { ProjectKpiTiles } from "./ProjectKpis";
import { useProjectIdentities } from "./useProjectIdentities";
import { ProjectStatusItemsDialog, type StatusDrilldown } from "./ProjectStatusItems";
import { useProjectMandate } from "./ProjectMandateDialog";
import { type ProjectHomeView, projectRulesPath } from "./projectHomeNavigation";
import { TicketLoadError } from "./TicketControls";
import { TicketMarkdown } from "./ticketImages";
import { ProjectMilestoneNote } from "./ProjectMilestoneNote";
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
  const projectRow = useProjectMandate(projectId);
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
      <ProjectMilestoneNote projectId={projectId} />
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
      <RoleKpis projectId={projectId} />
      <section aria-label="Agents" className="space-y-3">
        <div>
          <h2 className="text-[length:calc(var(--text-ui)+2px)] font-bold">Agents</h2>
          <p className="text-xs text-muted-foreground">
            The work agents did and what it cost. People&rsquo;s own chats count here too, since
            that&rsquo;s where their spend comes from.
          </p>
        </div>
        {summary.data ? (
          <AgentsTable summary={summary.data} statusWindow={statusWindow} onDrill={setDrilldown} />
        ) : summary.isPending ? (
          <Spinner aria-label="Loading agents" />
        ) : (
          <p className="text-sm text-muted-foreground">Agents are unavailable right now.</p>
        )}
      </section>
      <section aria-label="People" className="space-y-3">
        <div>
          <h2 className="text-[length:calc(var(--text-ui)+2px)] font-bold">People</h2>
          <p className="text-xs text-muted-foreground">
            Tickets each person asked for, including ones an agent filed for them.
          </p>
        </div>
        {summary.data ? (
          <PeopleTable
            members={summary.data.members}
            statusWindow={statusWindow}
            onDrill={setDrilldown}
          />
        ) : summary.isPending ? (
          <Spinner aria-label="Loading people" />
        ) : (
          <p className="text-sm text-muted-foreground">People are unavailable right now.</p>
        )}
      </section>
      <ProjectInsightsSection
        projectId={projectId}
        statusWindow={statusWindow}
        isOwner={projectRow.data !== undefined && projectRow.data.shared !== true}
      />
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
        <Button asChild variant="ghost" size="sm" data-testid="project-summary-rules-link">
          <Link to={projectRulesPath(projectName)}>
            <ScrollTextIcon aria-hidden />
            How this project works
          </Link>
        </Button>
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

export function WindowPicker({
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
        note={
          sources.prs_merged === "unavailable"
            ? "Not tracked on this server"
            : prsMergedZeroNote(totals.prs_merged, totals.tickets_done)
        }
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
      <StatTile label="Team" value={<TeamValue members={summary.members} />} />
      <StatTile
        label="Tickets done"
        value={String(totals.tickets_done)}
        onOpen={() => onDrill({ label: "Tickets done", metric: "tickets_shipped", range })}
      />
    </dl>
  );
}

/** People and team members (the Lead and named roles; the agents pool doesn't count). */
function teamParts(members: readonly ProjectStatusMember[]) {
  const people = members.filter((member) => member.kind === "human").length;
  const roles = members.filter((member) => member.kind === "identity").length;
  return [
    { count: people, noun: people === 1 ? "person" : "people", narrowDrops: "" },
    { count: roles, noun: roles === 1 ? "team member" : "team members", narrowDrops: "team " },
  ];
}

/** "2 people · 10 team members". */
export function teamCount(members: readonly ProjectStatusMember[]): string {
  return teamParts(members)
    .map((part) => `${part.count} ${part.noun}`)
    .join(" · ");
}

/** The team tile's value: big numbers, small words, on one line in a quarter-width tile. */
function TeamValue({ members }: { members: readonly ProjectStatusMember[] }) {
  const words = "text-sm font-medium text-muted-foreground";
  return (
    <span className="whitespace-nowrap">
      {teamParts(members).map((part, index) => (
        <span key={part.noun}>
          {index > 0 && <span className={words}> · </span>}
          {part.count}
          <span className={words}>
            {" "}
            {part.narrowDrops ? (
              <>
                {/* Read in full, shown only when the tile has room for it. */}
                <span className="@max-[16rem]/tile:sr-only">{part.narrowDrops}</span>
                {part.noun.slice(part.narrowDrops.length)}
              </>
            ) : (
              part.noun
            )}
          </span>
        </span>
      ))}
    </span>
  );
}

function StatTile({
  label,
  value,
  note,
  onOpen,
}: {
  label: string;
  value: ReactNode;
  note?: string;
  onOpen?: () => void;
}) {
  return (
    <div className="@container/tile rounded-lg border border-border/60 p-3">
      <dt className="text-sm text-muted-foreground">{label}</dt>
      <dd className="mt-1 text-2xl font-semibold tabular-nums">
        {onOpen ? (
          <button
            type="button"
            className="rounded-sm underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            aria-label={`${label}${typeof value === "string" ? `: ${value}` : ""}. Show the items behind it`}
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
  { key: "name", label: "Agent", numeric: false },
  { key: "tickets_done", label: "Tickets serviced", numeric: true },
  { key: "prs_merged", label: "PRs merged", numeric: true },
  { key: "spend_cents", label: "Spend", numeric: true },
  { key: "cost_per_merged_pr_cents", label: "Cost per merged PR", numeric: true },
];

/** Agent rows plus each person's own chats that did work: the rows spend and tickets come from. */
export function agentRows(members: readonly ProjectStatusMember[]): ProjectStatusMember[] {
  return members.filter(
    (member) =>
      member.kind !== "human" ||
      member.tickets_done > 0 ||
      (member.spend_cents ?? 0) > 0 ||
      (member.prs_merged ?? 0) > 0,
  );
}

function AgentsTable({
  summary,
  statusWindow,
  onDrill,
}: {
  summary: ProjectStatusSummary;
  statusWindow: ProjectStatusWindow;
  onDrill: (drilldown: StatusDrilldown) => void;
}) {
  const members = agentRows(summary.members);
  const { totals } = summary;
  const [sort, setSort] = useState<{ key: ProjectStatusSortKey; direction: "asc" | "desc" }>({
    key: "tickets_done",
    direction: "desc",
  });
  if (!members.length)
    return <p className="text-sm text-muted-foreground">No agents have activity yet.</p>;
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
                <span className="flex items-center">
                  <MemberName member={member} />
                  {member.kind === "human" && (
                    <span className="shrink-0 text-muted-foreground">&rsquo;s own chats</span>
                  )}
                </span>
              </th>
              <NumberCell
                label={`Tickets serviced by ${memberDisplayName(member)}`}
                onOpen={() =>
                  onDrill({
                    label: "Tickets serviced",
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
        <tfoot className="border-t border-border/60 font-medium">
          <tr data-testid="agents-total">
            <th scope="row" className="px-3 py-2 text-left">
              Total
            </th>
            <NumberCell>{String(totals.tickets_done)}</NumberCell>
            <NumberCell>{totals.prs_merged === null ? "—" : String(totals.prs_merged)}</NumberCell>
            <NumberCell>{formatCents(totals.spend_cents)}</NumberCell>
            <NumberCell>{formatCents(totals.cost_per_merged_pr_cents)}</NumberCell>
          </tr>
        </tfoot>
      </table>
    </div>
  );
}

/** Everyone on the project with the tickets they asked for, most first. */
function PeopleTable({
  members,
  statusWindow,
  onDrill,
}: {
  members: ProjectStatusMember[];
  statusWindow: ProjectStatusWindow;
  onDrill: (drilldown: StatusDrilldown) => void;
}) {
  const people = members
    .filter((member) => member.kind === "human")
    .sort(
      (left, right) =>
        (right.tickets_submitted ?? 0) - (left.tickets_submitted ?? 0) ||
        memberDisplayName(left).localeCompare(memberDisplayName(right)),
    );
  if (!people.length) return <p className="text-sm text-muted-foreground">No people yet.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-border/60">
      <table className="w-full text-sm">
        <thead className="border-b border-border/60 text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Person
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              Tickets submitted
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/60">
          {people.map((person) => (
            <tr key={person.key}>
              <th scope="row" className="px-3 py-2 text-left font-normal">
                <MemberName member={person} />
              </th>
              <NumberCell
                label={`Tickets submitted by ${memberDisplayName(person)}`}
                onOpen={() =>
                  onDrill({
                    label: "Tickets submitted",
                    metric: "tickets_submitted",
                    range: statusWindow,
                    member: person.key,
                  })
                }
              >
                {String(person.tickets_submitted ?? 0)}
              </NumberCell>
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
 * The project's view toolbar, in the same place on every view: the Board /
 * Summary / Rules switch, an optional control after it (the board's ticket
 * filter) and optional tools at the right end.
 */
export function ProjectHomeTabs({
  view,
  onChange,
  className,
  trailing,
  end,
  rulesBadge = 0,
}: {
  view: ProjectHomeView;
  onChange: (view: ProjectHomeView) => void;
  className?: string;
  trailing?: ReactNode;
  end?: ReactNode;
  /** Mandate proposals waiting on the viewer, flagged on the Rules tab. */
  rulesBadge?: number;
}) {
  return (
    <div
      className={cn(
        // Named like the top bar so tools moved here collapse at the same widths.
        // One line from `sm` up; phones give the filters their own row below the tabs.
        "@container/topbar mx-4 mb-3 flex shrink-0 items-center gap-3 overflow-x-auto border-b border-border/70 pt-0.5 pb-2.5 [scrollbar-width:none] max-sm:flex-wrap max-sm:gap-y-2.5 [&::-webkit-scrollbar]:hidden",
        className,
      )}
    >
      <div
        role="tablist"
        aria-label="Project views"
        className="flex shrink-0 rounded-[9px] bg-muted p-0.5"
      >
        {(
          [
            { id: "board", label: "Board", Icon: SquareKanbanIcon },
            { id: "summary", label: "Summary", Icon: AlignLeftIcon },
            { id: "rules", label: "Rules", Icon: ScrollTextIcon },
          ] as const
        ).map(({ id, label, Icon }) => {
          const selected = view === id;
          return (
            <button
              key={id}
              type="button"
              role="tab"
              aria-selected={selected}
              aria-label={
                id === "rules" && rulesBadge > 0
                  ? `Rules, ${rulesBadge} proposed mandate ${rulesBadge === 1 ? "change" : "changes"}`
                  : undefined
              }
              className={cn(
                "inline-flex h-7 items-center gap-1.5 rounded-[7px] px-2.5 text-[13px] font-medium whitespace-nowrap transition-colors focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none",
                selected
                  ? "bg-background text-foreground shadow-xs dark:bg-white/12"
                  : "text-muted-foreground hover:text-foreground",
              )}
              onClick={() => onChange(id)}
            >
              {/* Phones drop the icons so the whole toolbar fits on one line. */}
              <Icon aria-hidden className="size-3.5 max-sm:hidden" />
              <span className="text-optical">{label}</span>
              {id === "rules" && rulesBadge > 0 && (
                <span
                  aria-hidden
                  data-testid="project-rules-tab-badge"
                  className="inline-flex h-4 min-w-4 items-center justify-center rounded-full bg-brand-accent px-1 text-[10px] leading-none font-semibold text-white tabular-nums"
                >
                  {rulesBadge}
                </span>
              )}
            </button>
          );
        })}
      </div>
      {trailing && (
        <>
          <span aria-hidden className="h-4 w-px shrink-0 bg-border max-sm:hidden" />
          <div className="flex shrink-0 items-center max-sm:order-last max-sm:basis-full">
            {trailing}
          </div>
        </>
      )}
      {end && <div className="ml-auto flex min-w-0 items-center gap-1">{end}</div>}
    </div>
  );
}

/** Each role's own KPIs, by role; roles without KPIs are left out. */
function RoleKpis({ projectId }: { projectId: string }) {
  const identities = useProjectIdentities(projectId);
  const roles = (identities.data ?? []).filter((identity) => !identity.builtin);
  if (roles.length === 0) return null;
  return (
    <div className="space-y-6" data-testid="project-role-kpis">
      {roles.map((role) => (
        <ProjectKpiTiles
          key={role.id}
          projectId={projectId}
          identityId={role.id}
          title={`${role.name} KPIs`}
          hideWhenEmpty
        />
      ))}
    </div>
  );
}
