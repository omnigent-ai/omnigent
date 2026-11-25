import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { PageScroll } from "@/components/PageScroll";
import { Spinner } from "@/components/ui/spinner";
import { Link } from "@/lib/routing";
import {
  formatTelemetryNumber,
  getTelemetryOverview,
  sumMeasured,
  telemetryOverviewKey,
  type ProjectTelemetry,
} from "@/lib/projectTelemetryApi";
import type { ProjectStatusWindow } from "@/lib/projectStatusApi";
import { InsightsPanels, type InsightsNumbers } from "@/projectHome/ProjectInsights";
import { WindowPicker } from "@/projectHome/ProjectSummary";
import { TicketLoadError } from "@/projectHome/TicketControls";
import {
  PROJECT_SUMMARY_TAB,
  PROJECT_TAB_PARAM,
  projectHomePath,
} from "@/projectHome/projectHomeNavigation";

/** Every owned project's numbers added up; medians and rates don't add, so they're left out. */
export function totalInsights(projects: readonly ProjectTelemetry[]): InsightsNumbers {
  const sum = (pick: (project: ProjectTelemetry) => number) =>
    projects.reduce((total, project) => total + pick(project), 0);
  const measured = (pick: (project: ProjectTelemetry) => number | null) =>
    sumMeasured(projects.map(pick));
  return {
    workStart: {
      sessions_started_by_people: sum((p) => p.work_start.sessions_started_by_people),
      tickets_filed_directly: sum((p) => p.work_start.tickets_filed_directly),
      tickets_filed_from_a_chat: sum((p) => p.work_start.tickets_filed_from_a_chat),
      tickets_filed_by_agents: sum((p) => p.work_start.tickets_filed_by_agents),
      people: {
        session_first: sum((p) => p.work_start.people.session_first),
        ticket_first: sum((p) => p.work_start.people.ticket_first),
        both: sum((p) => p.work_start.people.both),
      },
    },
    roles: {
      messages_to_lead: measured((p) => p.role_interaction.messages_to_lead),
      messages_to_roles: measured((p) => p.role_interaction.messages_to_roles),
      people_messaging_roles: measured((p) => p.role_interaction.people_messaging_roles),
      side_chats_opened: sum((p) => p.role_interaction.side_chats_opened),
      side_chats_with_roles: sum((p) => p.role_interaction.side_chats_with_roles),
      ticket_comments_by_people: sum((p) => p.role_interaction.ticket_comments_by_people),
      people_ticket_only: measured((p) => p.role_interaction.people_ticket_only),
    },
    team: {
      roles: sum((p) => p.team_building.roles),
      roles_created_by_lead: sum((p) => p.team_building.roles_created_by_lead),
      roles_created_by_hand: sum((p) => p.team_building.roles_created_by_hand),
      roles_created_in_window: sum((p) => p.team_building.roles_created_in_window),
      roles_created_in_window_by_lead: sum((p) => p.team_building.roles_created_in_window_by_lead),
      roles_edited_after_creation: sum((p) => p.team_building.roles_edited_after_creation),
      median_hours_to_last_role_edit: null,
    },
    shipping: {
      tickets_shipped: measured((p) => p.throughput.tickets_shipped),
      merged_prs: measured((p) => p.throughput.merged_prs),
      rework_tickets: measured((p) => p.throughput.rework_tickets),
      time_to_merged_median_hours: null,
      spend_usd: measured((p) => p.throughput.spend_usd),
      review_rounds_per_shipped_ticket: null,
      stalled_tickets: sum((p) => p.throughput.stalled_tickets),
    },
  };
}

/** Insights across every project the signed-in person owns. */
export function InsightsPage() {
  const [statusWindow, setStatusWindow] = useState<ProjectStatusWindow>("30d");
  const overview = useQuery({
    queryKey: telemetryOverviewKey(statusWindow),
    queryFn: ({ signal }) => getTelemetryOverview(statusWindow, signal),
    staleTime: 60_000,
  });
  return (
    <PageScroll>
      <div className="mx-auto w-full max-w-6xl space-y-6 px-4 py-8" data-testid="insights-page">
        <div className="flex flex-wrap items-center justify-between gap-3">
          <div>
            <h1 className="text-lg font-semibold">Insights</h1>
            <p className="text-sm text-muted-foreground">
              How people work across the projects you own. Each panel names the product decision it
              informs.
            </p>
          </div>
          <WindowPicker value={statusWindow} onChange={setStatusWindow} />
        </div>
        <TicketLoadError
          error={overview.error}
          hasData={overview.data !== undefined}
          onRetry={() => void overview.refetch()}
        />
        {overview.data ? (
          overview.data.projects.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              You don&rsquo;t own any projects yet. Insights cover the projects you own.
            </p>
          ) : (
            <>
              <p className="text-xs text-muted-foreground">
                {overview.data.projects.length}{" "}
                {overview.data.projects.length === 1 ? "project" : "projects"} ·{" "}
                {overview.data.since_day_utc
                  ? `${overview.data.since_day_utc} to ${overview.data.today_utc}, UTC`
                  : `All time to ${overview.data.today_utc}, UTC`}
              </p>
              <InsightsPanels numbers={totalInsights(overview.data.projects)} />
              <ByProjectTable projects={overview.data.projects} />
            </>
          )
        ) : (
          !overview.error && <Spinner aria-label="Loading insights" />
        )}
      </div>
    </PageScroll>
  );
}

function ByProjectTable({ projects }: { projects: ProjectTelemetry[] }) {
  return (
    <section aria-label="By project" className="space-y-2">
      <h2 className="font-medium">By project</h2>
      <div className="overflow-x-auto rounded-lg border border-border/60">
        <table className="w-full text-sm">
          <thead className="border-b border-border/60 text-muted-foreground">
            <tr>
              {[
                "Project",
                "Sessions by people",
                "Tickets filed",
                "Messages to roles",
                "Roles by the Lead",
                "Shipped",
                "Median to merged",
                "Stalled",
              ].map((label, index) => (
                <th
                  key={label}
                  scope="col"
                  className={
                    index ? "px-3 py-2 text-right font-medium" : "px-3 py-2 text-left font-medium"
                  }
                >
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border/60">
            {projects.map((project) => {
              const filed =
                project.work_start.tickets_filed_directly +
                project.work_start.tickets_filed_from_a_chat +
                project.work_start.tickets_filed_by_agents;
              return (
                <tr key={project.project_id}>
                  <th scope="row" className="px-3 py-2 text-left font-normal">
                    <Link
                      to={`${projectHomePath(project.project_name)}?${PROJECT_TAB_PARAM}=${PROJECT_SUMMARY_TAB}`}
                      className="underline-offset-4 hover:underline"
                    >
                      {project.project_name}
                    </Link>
                  </th>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatTelemetryNumber(project.work_start.sessions_started_by_people)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatTelemetryNumber(filed)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatTelemetryNumber(project.role_interaction.messages_to_roles)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {project.team_building.roles_created_by_lead} of {project.team_building.roles}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatTelemetryNumber(project.throughput.tickets_shipped)}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatTelemetryNumber(project.throughput.time_to_merged_median_hours, " h")}
                  </td>
                  <td className="px-3 py-2 text-right tabular-nums">
                    {formatTelemetryNumber(project.throughput.stalled_tickets)}
                  </td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
    </section>
  );
}
