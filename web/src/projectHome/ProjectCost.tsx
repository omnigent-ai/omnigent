import { useQuery } from "@tanstack/react-query";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { authenticatedFetch } from "@/lib/identity";
import { apiErrorFromResponse } from "@/lib/sessionsApi";

/** The project's total spend over its trailing window. */
export interface ProjectCost {
  cost_usd: number;
  days: number;
  since_day_utc: string;
}

/** A ticket's linked-session spend; `cost_usd` is null for a session with no priced turn. */
export interface TicketCost {
  cost_usd: number;
  session_count: number;
  unpriced_sessions: number;
  sessions: { session_id: string; cost_usd: number | null; nested: boolean }[];
}

/** How often cost readouts refresh while visible; spend changes only at turn boundaries. */
const COST_REFRESH_MS = 60_000;
export const PROJECT_COST_DAYS = 7;

function ticketsPath(projectId: string): string {
  return `/v1/projects/${encodeURIComponent(projectId)}/tickets`;
}

async function getJson<T>(url: string): Promise<T> {
  const response = await authenticatedFetch(url);
  if (!response.ok) throw await apiErrorFromResponse(response);
  return (await response.json()) as T;
}

export function getProjectCost(projectId: string, days = PROJECT_COST_DAYS) {
  return getJson<ProjectCost>(`${ticketsPath(projectId)}/cost?days=${days}`);
}

export function getTicketCost(projectId: string, ticketId: string) {
  return getJson<TicketCost>(`${ticketsPath(projectId)}/${encodeURIComponent(ticketId)}/cost`);
}

/** Plain-language summary of a ticket's cost, saying when some sessions have no recorded cost. */
export function describeTicketCost(cost: TicketCost): string {
  if (cost.session_count === 0) return "No linked agent sessions yet";
  const priced = cost.session_count - cost.unpriced_sessions;
  const sessions = `${cost.session_count} linked session${cost.session_count === 1 ? "" : "s"}`;
  if (priced === 0) return `Cost unknown for ${sessions}`;
  const amount = formatSessionCostUsd(cost.cost_usd);
  if (cost.unpriced_sessions === 0) return `${amount} across ${sessions}`;
  return `${amount} across ${sessions}; cost unknown for ${cost.unpriced_sessions}`;
}

/** Short pill text for a ticket's cost; {@link describeTicketCost} explains it in full. */
export function ticketCostLabel(cost: TicketCost): string {
  if (cost.session_count > 0 && cost.session_count === cost.unpriced_sessions)
    return "Cost unknown";
  return formatSessionCostUsd(cost.cost_usd);
}

// The cost pill shared by the project header and a ticket's meta row.
const COST_PILL_CLASS =
  "shrink-0 rounded-md border border-border/60 px-1.5 py-0.5 text-xs tabular-nums text-muted-foreground";

/** The project header's trailing-7-day agent cost. */
export function ProjectCostBadge({
  projectId,
  plain = false,
}: {
  projectId: string | null;
  /** Plain text for the view toolbar, instead of a bordered pill. */
  plain?: boolean;
}) {
  const query = useQuery({
    queryKey: ["project-cost", projectId, PROJECT_COST_DAYS],
    queryFn: () => getProjectCost(projectId!),
    enabled: projectId !== null,
    refetchInterval: COST_REFRESH_MS,
    retry: false,
  });
  if (!query.data) return null;
  const amount = formatSessionCostUsd(query.data.cost_usd);
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        {plain ? (
          <span
            data-testid="project-cost-t7d"
            className="shrink-0 px-2 text-[13px] whitespace-nowrap text-muted-foreground"
          >
            <span className="font-medium text-foreground tabular-nums">{amount}</span> last 7 days
          </span>
        ) : (
          <span data-testid="project-cost-t7d" className={COST_PILL_CLASS}>
            {amount} · 7d
          </span>
        )}
      </TooltipTrigger>
      <TooltipContent side="bottom">
        Agent cost in this project over the last {query.data.days} days (UTC, since{" "}
        {query.data.since_day_utc}), including sub-agents
      </TooltipContent>
    </Tooltip>
  );
}

/** A ticket's all-time agent cost, the sum over its linked sessions and sub-agents, as a pill. */
export function TicketCostBadge({ projectId, ticketId }: { projectId: string; ticketId: string }) {
  const query = useQuery({
    queryKey: ["ticket-cost", projectId, ticketId],
    queryFn: () => getTicketCost(projectId, ticketId),
    refetchInterval: COST_REFRESH_MS,
    retry: false,
  });
  if (!query.data) return null;
  return (
    <Tooltip>
      <TooltipTrigger asChild>
        <span
          data-testid="ticket-cost"
          aria-label={`Ticket cost: ${describeTicketCost(query.data)}`}
          className={COST_PILL_CLASS}
        >
          {ticketCostLabel(query.data)}
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        Agent cost on this ticket, including sub-agents: {describeTicketCost(query.data)}
      </TooltipContent>
    </Tooltip>
  );
}
