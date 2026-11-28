import { formatLines } from "@/lib/projectStatusApi";
import { useQuery } from "@tanstack/react-query";
import { Link } from "@/lib/routing";
import { Badge } from "@/components/ui/badge";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Spinner } from "@/components/ui/spinner";
import { formatKpiValue } from "@/lib/projectKpisApi";
import {
  getProjectStatusItems,
  projectStatusItemsKey,
  type ProjectStatusItem,
  type ProjectStatusItems,
  type StatusItemsQuery,
} from "@/lib/projectStatusItemsApi";
import { formatMessageTimestamp } from "@/lib/relativeTime";
import { cn } from "@/lib/utils";
import { projectHomePath, ticketPanelSearch } from "./projectHomeNavigation";
import { TicketLoadError } from "./TicketControls";

/** What opened the drill-down: the metric, its range and, for a member cell, the member. */
export interface StatusDrilldown extends StatusItemsQuery {
  /** The number's label, e.g. "PRs merged" or a KPI's name. */
  label: string;
}

const KIND_LABEL: Record<ProjectStatusItem["kind"], string> = {
  ticket: "Ticket",
  pull_request: "PR",
  session: "Session",
  person: "Person",
};

function rangeText(items: ProjectStatusItems): string {
  if (items.range === "trend")
    return `the 7 full UTC days from ${items.since_day_utc} plus today (${items.today_utc})`;
  return items.since_day_utc
    ? `${items.since_day_utc} to ${items.today_utc}, UTC`
    : `all time to ${items.today_utc}, UTC`;
}

/** Lists the tickets, PRs or sessions behind a Summary number or KPI tile, by day. */
export function ProjectStatusItemsDialog({
  projectId,
  projectName,
  drilldown,
  memberNames,
  onClose,
}: {
  projectId: string;
  projectName: string;
  drilldown: StatusDrilldown;
  /** Display names by member key, from the Summary's member table. */
  memberNames: ReadonlyMap<string, string>;
  onClose: () => void;
}) {
  const items = useQuery({
    queryKey: projectStatusItemsKey(projectId, drilldown),
    queryFn: ({ signal }) => getProjectStatusItems(projectId, drilldown, signal),
    staleTime: 30_000,
  });
  const member = drilldown.member ? (memberNames.get(drilldown.member) ?? drilldown.member) : null;
  return (
    <Dialog open onOpenChange={(open) => !open && onClose()}>
      <DialogContent
        className="max-h-[85dvh] overflow-y-auto sm:max-w-3xl"
        data-testid="project-status-items"
      >
        <DialogHeader>
          <DialogTitle>
            {drilldown.label}
            {member ? ` for ${member}` : ""}
          </DialogTitle>
          <DialogDescription>
            {items.data
              ? `What's counted over ${rangeText(items.data)}.`
              : "What's behind this number."}
          </DialogDescription>
        </DialogHeader>
        <TicketLoadError
          error={items.error}
          hasData={items.data !== undefined}
          onRetry={() => void items.refetch()}
        />
        {items.data ? (
          items.data.available ? (
            <StatusItemsBody
              items={items.data}
              projectName={projectName}
              memberNames={memberNames}
            />
          ) : (
            <p className="text-sm text-muted-foreground">
              This server doesn&rsquo;t track the items behind this number.
            </p>
          )
        ) : items.isPending ? (
          <Spinner aria-label="Loading items" />
        ) : null}
      </DialogContent>
    </Dialog>
  );
}

function StatusItemsBody({
  items,
  projectName,
  memberNames,
}: {
  items: ProjectStatusItems;
  projectName: string;
  memberNames: ReadonlyMap<string, string>;
}) {
  const { unit } = items;
  const counts = items.metric !== "spend_usd" && unit === "count";
  return (
    <div className="space-y-4">
      <p className="text-sm">
        Total:{" "}
        <span className="font-semibold tabular-nums" data-testid="project-status-items-total">
          {formatKpiValue(items.total, unit)}
        </span>
        {items.sample_size
          ? items.metric === "daily_active_humans"
            ? ` over ${items.sample_size} weekdays`
            : ` from ${items.sample_size} tickets`
          : ""}
      </p>
      {items.days.length > 0 && items.days.length <= 31 && (
        <ol className="flex flex-wrap gap-1.5" aria-label="By day">
          {items.days.map((day) => (
            <li
              key={day.day_utc}
              className="rounded-md border border-border/60 px-2 py-1 text-xs"
              title={`${day.items} ${day.items === 1 ? "item" : "items"}`}
            >
              <span className="text-muted-foreground">{day.day_utc.slice(5)}</span>{" "}
              <span className="font-medium tabular-nums">{formatKpiValue(day.value, unit)}</span>
            </li>
          ))}
        </ol>
      )}
      {items.items.length === 0 ? (
        <p className="text-sm text-muted-foreground">Nothing counted in this range.</p>
      ) : (
        <ItemTable
          rows={items.items}
          unit={unit}
          showValue={!counts}
          projectName={projectName}
          memberNames={memberNames}
        />
      )}
      {items.metric === "spend_usd" && items.items.length > 0 && (
        <p className="text-xs text-muted-foreground">
          Each total is rounded to the cent once, so rows can differ from it by a cent.
        </p>
      )}
      {items.excluded.length > 0 && (
        <section aria-label="Left out" className="space-y-2">
          <h3 className="text-sm font-medium">Left out ({items.excluded.length})</h3>
          <ItemTable
            rows={items.excluded}
            unit={unit}
            showValue={false}
            projectName={projectName}
            memberNames={memberNames}
            excluded
          />
        </section>
      )}
      {items.truncated && (
        <p className="text-xs text-muted-foreground">Showing the newest 500 rows.</p>
      )}
      {(items.hidden_items ?? 0) > 0 && (
        <p className="text-xs text-muted-foreground">
          {items.hidden_items} {items.hidden_items === 1 ? "row" : "rows"} about other people{" "}
          aren&apos;t shown to you; the numbers above still count them.
        </p>
      )}
    </div>
  );
}

function ItemTable({
  rows,
  unit,
  showValue,
  projectName,
  memberNames,
  excluded = false,
}: {
  rows: ProjectStatusItem[];
  unit: ProjectStatusItems["unit"];
  showValue: boolean;
  projectName: string;
  memberNames: ReadonlyMap<string, string>;
  excluded?: boolean;
}) {
  return (
    <div className="overflow-x-auto rounded-lg border border-border/60">
      <table className="w-full text-sm">
        <thead className="border-b border-border/60 text-left text-muted-foreground">
          <tr>
            <th scope="col" className="px-3 py-2 font-medium">
              Day
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Item
            </th>
            <th scope="col" className="px-3 py-2 font-medium">
              Member
            </th>
            <th scope="col" className="px-3 py-2 text-right font-medium">
              {excluded ? "Why it's left out" : showValue ? "Value" : ""}
            </th>
          </tr>
        </thead>
        <tbody className="divide-y divide-border/60">
          {rows.map((row) => (
            <tr
              key={`${row.day_utc}-${row.kind}-${row.id}`}
              className={cn(!row.counted_in_total && !excluded && "text-muted-foreground")}
              data-testid={excluded ? "project-status-excluded-row" : "project-status-item-row"}
            >
              <td className="px-3 py-2 whitespace-nowrap tabular-nums">
                {row.day_utc ?? "Undated"}
                {row.at !== null && (
                  <span className="block text-xs text-muted-foreground">
                    {formatMessageTimestamp(row.at)}
                  </span>
                )}
              </td>
              <td className="px-3 py-2">
                <span className="flex min-w-0 items-center gap-2">
                  <Badge variant="outline" className="shrink-0 text-xs font-normal">
                    {KIND_LABEL[row.kind]}
                  </Badge>
                  <ItemLink row={row} projectName={projectName} />
                </span>
                {row.detail?.from_stage_id && (
                  <span className="block text-xs text-muted-foreground">
                    {row.detail.from_stage_id} → {row.detail.to_stage_id}
                  </span>
                )}
                {row.detail?.added != null && (
                  <span className="block text-xs text-muted-foreground tabular-nums">
                    {formatLines(row.detail.added, row.detail.removed)} lines
                    {row.detail.raw_added != null &&
                      (row.detail.raw_added !== row.detail.added ||
                        row.detail.raw_removed !== row.detail.removed) &&
                      ` (${formatLines(row.detail.raw_added, row.detail.raw_removed)} with generated files)`}
                  </span>
                )}
                {!row.counted_in_total && !excluded && (
                  <span className="block text-xs">
                    Also counted on a later day; the total counts it once.
                  </span>
                )}
              </td>
              <td className="px-3 py-2 wrap-anywhere">
                {row.member ? (memberNames.get(row.member) ?? row.member) : "—"}
              </td>
              <td className="px-3 py-2 text-right">
                {excluded ? (
                  <span className="text-muted-foreground">{row.reason}</span>
                ) : showValue ? (
                  <span className="tabular-nums">{formatKpiValue(row.value, unit)}</span>
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ItemLink({ row, projectName }: { row: ProjectStatusItem; projectName: string }) {
  const className = "min-w-0 wrap-anywhere text-foreground underline-offset-4 hover:underline";
  if (row.kind === "pull_request" && row.url)
    return (
      <a href={row.url} target="_blank" rel="noreferrer" className={className}>
        {row.url.replace(/^https?:\/\//, "")}
      </a>
    );
  if (row.kind === "person") return <span className="text-foreground">Active that day</span>;
  if (row.kind === "session")
    return (
      <Link to={`/c/${encodeURIComponent(row.id)}`} className={className}>
        {row.title || "Untitled session"}
      </Link>
    );
  return (
    <Link to={`${projectHomePath(projectName)}${ticketPanelSearch(row.id)}`} className={className}>
      {row.title || "Untitled ticket"}
    </Link>
  );
}
