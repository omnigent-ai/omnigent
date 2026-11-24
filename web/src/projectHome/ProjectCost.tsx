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

/** The project header's trailing-7-day agent cost. */
export function ProjectCostBadge({ projectId }: { projectId: string | null }) {
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
        <span
          data-testid="project-cost-t7d"
          className="shrink-0 rounded-md border border-border/60 px-1.5 py-0.5 text-xs tabular-nums text-muted-foreground"
        >
          {amount} · 7d
        </span>
      </TooltipTrigger>
      <TooltipContent side="bottom">
        Agent cost in this project over the last {query.data.days} days (UTC, since{" "}
        {query.data.since_day_utc}), including sub-agents
      </TooltipContent>
    </Tooltip>
  );
}

/** A ticket's all-time agent cost: the sum over its linked sessions, sub-agents included. */
export function TicketCostSection({
  projectId,
  ticketId,
}: {
  projectId: string;
  ticketId: string;
}) {
  const query = useQuery({
    queryKey: ["ticket-cost", projectId, ticketId],
    queryFn: () => getTicketCost(projectId, ticketId),
    refetchInterval: COST_REFRESH_MS,
    retry: false,
  });
  if (query.isError) return null;
  return (
    <section aria-label="Cost" className="flex items-baseline gap-2 text-ui">
      <h3 className="font-bold">Cost</h3>
      <span data-testid="ticket-cost" className="tabular-nums text-muted-foreground">
        {query.data ? describeTicketCost(query.data) : "…"}
      </span>
    </section>
  );
}
