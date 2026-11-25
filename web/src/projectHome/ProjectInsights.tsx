import type { ReactNode } from "react";
import { useQuery } from "@tanstack/react-query";
import { Button } from "@/components/ui/button";
import { Spinner } from "@/components/ui/spinner";
import { Link } from "@/lib/routing";
import {
  formatTelemetryNumber,
  getProjectTelemetry,
  projectTelemetryKey,
  type ProjectTelemetry,
  type TelemetryPerson,
} from "@/lib/projectTelemetryApi";
import type { ProjectStatusWindow } from "@/lib/projectStatusApi";
import { MemberName } from "./MemberName";
import { TicketLoadError } from "./TicketControls";

/** Numbers an insights panel shows; each panel answers one product question. */
export interface InsightsNumbers {
  workStart: ProjectTelemetry["work_start"];
  roles: ProjectTelemetry["role_interaction"];
  team: ProjectTelemetry["team_building"];
  shipping: ProjectTelemetry["throughput"];
}

export function insightsNumbers(telemetry: ProjectTelemetry): InsightsNumbers {
  return {
    workStart: telemetry.work_start,
    roles: telemetry.role_interaction,
    team: telemetry.team_building,
    shipping: telemetry.throughput,
  };
}

function Panel({
  title,
  question,
  decision,
  children,
}: {
  title: string;
  question: string;
  decision: string;
  children: ReactNode;
}) {
  return (
    <section
      aria-label={title}
      className="flex flex-col gap-2.5 rounded-lg border border-border/60 p-3"
      data-testid="insights-panel"
    >
      <div>
        <h3 className="font-medium">{title}</h3>
        <p className="text-xs text-muted-foreground">{question}</p>
      </div>
      <dl className="flex flex-col gap-1.5 text-sm">{children}</dl>
      <p className="mt-auto rounded-md bg-muted/60 px-2 py-1.5 text-xs text-muted-foreground">
        Decision: {decision}
      </p>
    </section>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3">
      <dt className="text-muted-foreground">{label}</dt>
      <dd className="font-medium tabular-nums">{value}</dd>
    </div>
  );
}

/** A two- or three-part share bar with its legend, e.g. roles by the Lead vs by hand. */
function ShareBar({ parts }: { parts: { label: string; value: number; className: string }[] }) {
  const total = parts.reduce((sum, part) => sum + part.value, 0);
  if (total === 0) return null;
  return (
    <div className="space-y-1">
      <div className="flex h-2 overflow-hidden rounded-full bg-muted" aria-hidden>
        {parts.map((part) => (
          <span
            key={part.label}
            className={part.className}
            style={{ width: `${(part.value / total) * 100}%` }}
          />
        ))}
      </div>
      <p className="flex flex-wrap gap-x-3 text-xs text-muted-foreground">
        {parts.map((part) => (
          <span key={part.label}>
            {part.label} {Math.round((part.value / total) * 100)}%
          </span>
        ))}
      </p>
    </div>
  );
}

const NOT_MEASURED =
  "Messages to the Lead and roles aren't measured yet: stored messages can't tell a person's words from the server's own notices. They'll appear once the usage log records each message as it's sent.";

/** The question-led panels shared by a project's Summary and the Insights page. */
export function InsightsPanels({ numbers }: { numbers: InsightsNumbers }) {
  const { workStart, roles, team, shipping } = numbers;
  const unmeasured = roles.messages_to_lead === null;
  return (
    <div className="space-y-2">
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2 xl:grid-cols-4">
        <Panel
          title="How people start work"
          question="Is chat or the board the real front door?"
          decision="what the project page leads with, the board or a chat."
        >
          <ShareBar
            parts={[
              {
                label: "Chat first",
                value: workStart.people.session_first,
                className: "bg-primary",
              },
              {
                label: "Tickets first",
                value: workStart.people.ticket_first,
                className: "bg-primary/40",
              },
              { label: "Both", value: workStart.people.both, className: "bg-muted-foreground/40" },
            ]}
          />
          <Row
            label="Sessions started by people"
            value={formatTelemetryNumber(workStart.sessions_started_by_people)}
          />
          <Row
            label="Tickets filed on the board"
            value={formatTelemetryNumber(workStart.tickets_filed_directly)}
          />
          <Row
            label="Tickets filed from a chat"
            value={formatTelemetryNumber(workStart.tickets_filed_from_a_chat)}
          />
          <Row
            label="Tickets filed by agents"
            value={formatTelemetryNumber(workStart.tickets_filed_by_agents)}
          />
        </Panel>
        <Panel
          title="Lead and roles"
          question="Do people talk to roles, or route everything through tickets?"
          decision="whether roles get their own chat entry points, or stay behind tickets."
        >
          <Row label="Messages to the Lead" value={formatTelemetryNumber(roles.messages_to_lead)} />
          <Row label="Messages to roles" value={formatTelemetryNumber(roles.messages_to_roles)} />
          <Row
            label="People who message roles"
            value={formatTelemetryNumber(roles.people_messaging_roles)}
          />
          <Row
            label="People who only use ticket comments"
            value={formatTelemetryNumber(roles.people_ticket_only)}
          />
          <Row label="Side chats opened" value={formatTelemetryNumber(roles.side_chats_opened)} />
        </Panel>
        <Panel
          title="Building the team"
          question="Do people trust the Lead to staff the project?"
          decision="invest in better role defaults if roles keep being reworked."
        >
          <ShareBar
            parts={[
              {
                label: "Created by the Lead",
                value: team.roles_created_by_lead,
                className: "bg-primary",
              },
              {
                label: "By hand",
                value: team.roles_created_by_hand,
                className: "bg-muted-foreground/40",
              },
            ]}
          />
          <Row label="Roles" value={formatTelemetryNumber(team.roles)} />
          <Row
            label="Roles edited after creation"
            value={formatTelemetryNumber(team.roles_edited_after_creation)}
          />
          <Row
            label="Median time to last edit"
            value={formatTelemetryNumber(team.median_hours_to_last_role_edit, " h")}
          />
        </Panel>
        <Panel
          title="Shipping"
          question="Is the factory productive, and where does work get stuck?"
          decision="where to point performance and workflow fixes."
        >
          <Row label="Tickets shipped" value={formatTelemetryNumber(shipping.tickets_shipped)} />
          <Row label="PRs merged" value={formatTelemetryNumber(shipping.merged_prs)} />
          <Row
            label="Median time to merged"
            value={formatTelemetryNumber(shipping.time_to_merged_median_hours, " h")}
          />
          <Row
            label="Review rounds per ticket"
            value={formatTelemetryNumber(shipping.review_rounds_per_shipped_ticket)}
          />
          <Row
            label="Tickets stalled 3+ days"
            value={formatTelemetryNumber(shipping.stalled_tickets)}
          />
        </Panel>
      </div>
      {unmeasured && <p className="text-xs text-muted-foreground">{NOT_MEASURED}</p>}
    </div>
  );
}

const PEOPLE_COLUMNS: { key: keyof TelemetryPerson; label: string }[] = [
  { key: "sessions_started", label: "Sessions" },
  { key: "tickets_filed", label: "Tickets filed" },
  { key: "messages_to_lead", label: "To the Lead" },
  { key: "messages_to_roles", label: "To roles" },
  { key: "side_chats_opened", label: "Side chats" },
  { key: "ticket_comments", label: "Comments" },
];

export function InsightsPeopleTable({ people }: { people: TelemetryPerson[] }) {
  if (!people.length)
    return <p className="text-sm text-muted-foreground">No activity from people yet.</p>;
  return (
    <div className="overflow-x-auto rounded-lg border border-border/60">
      <table className="w-full text-sm">
        <thead className="border-b border-border/60 text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 text-left font-medium">
              Person
            </th>
            {PEOPLE_COLUMNS.map((column) => (
              <th key={column.key} scope="col" className="px-3 py-2 text-right font-medium">
                {column.label}
              </th>
            ))}
          </tr>
        </thead>
        <tbody className="divide-y divide-border/60">
          {people.map((person) => (
            <tr key={person.user_id}>
              <th scope="row" className="px-3 py-2 text-left font-normal">
                <MemberName
                  member={{ kind: "human", name: person.user_id, user_id: person.user_id }}
                />
              </th>
              {PEOPLE_COLUMNS.map((column) => (
                <td key={column.key} className="px-3 py-2 text-right tabular-nums">
                  {formatTelemetryNumber(person[column.key] as number | null)}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The Summary's Insights section for one project, over the Summary's window. */
export function ProjectInsightsSection({
  projectId,
  statusWindow,
  isOwner,
}: {
  projectId: string;
  statusWindow: ProjectStatusWindow;
  isOwner: boolean;
}) {
  const telemetry = useQuery({
    queryKey: projectTelemetryKey(projectId, statusWindow),
    queryFn: ({ signal }) => getProjectTelemetry(projectId, statusWindow, signal),
    staleTime: 60_000,
  });
  return (
    <section aria-label="Insights" className="space-y-3" data-testid="project-insights">
      <div className="flex items-center gap-2">
        <h2 className="min-w-0 flex-1 text-[length:calc(var(--text-ui)+2px)] font-bold">
          Insights
        </h2>
        {isOwner && (
          <Button variant="ghost" size="sm" asChild>
            <Link to="/insights">All your projects</Link>
          </Button>
        )}
      </div>
      <TicketLoadError
        error={telemetry.error}
        hasData={telemetry.data !== undefined}
        onRetry={() => void telemetry.refetch()}
      />
      {telemetry.data ? (
        <>
          <InsightsPanels numbers={insightsNumbers(telemetry.data)} />
          {telemetry.data.people && (
            <div className="space-y-2">
              <h3 className="text-sm font-medium">{isOwner ? "By person" : "Your activity"}</h3>
              <InsightsPeopleTable people={telemetry.data.people} />
            </div>
          )}
        </>
      ) : (
        !telemetry.error && <Spinner aria-label="Loading insights" />
      )}
    </section>
  );
}
