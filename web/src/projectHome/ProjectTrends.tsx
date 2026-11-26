import { useQuery } from "@tanstack/react-query";
import type { ProjectStatusWindow } from "@/lib/projectStatusApi";
import type { StatusItemMetric } from "@/lib/projectStatusItemsApi";
import {
  getProjectStatusTrends,
  projectStatusTrendsKey,
  type ProjectStatusTrendDay,
  type ProjectStatusTrends,
} from "@/lib/projectTrendsApi";
import { cn } from "@/lib/utils";
import type { StatusDrilldown } from "./ProjectStatusItems";

type TrendField = Exclude<keyof ProjectStatusTrendDay, "day_utc">;

interface TrendChart {
  field: TrendField;
  title: string;
  /** What the chart's headline number is: the window's sum, or the latest bucket's value. */
  headline: "sum" | "latest";
  metric?: StatusItemMetric;
  money?: boolean;
}

/** Which charts a project gets: changes landed where it lands commits, else PRs merged. */
export function trendCharts(trends: ProjectStatusTrends): TrendChart[] {
  const landed = trends.days.some((day) => day.changes_landed !== null);
  const charts: TrendChart[] = [
    {
      field: "tickets_filed",
      title: "Tickets filed",
      headline: "sum",
      metric: "tickets_submitted",
    },
    {
      field: "tickets_finished",
      title: "Tickets finished",
      headline: "sum",
      metric: "tickets_shipped",
    },
    { field: "open_tickets", title: "Open tickets", headline: "latest" },
    landed
      ? {
          field: "changes_landed",
          title: "Changes landed",
          headline: "sum",
          metric: "changes_landed",
        }
      : { field: "prs_merged", title: "PRs merged", headline: "sum", metric: "merged_prs" },
    { field: "spend_usd", title: "Spend", headline: "sum", metric: "spend_usd", money: true },
  ];
  return charts.filter((chart) => trends.days.some((day) => day[chart.field] !== null));
}

function format(value: number | null, money?: boolean): string {
  if (value === null) return "—";
  return money
    ? new Intl.NumberFormat("en-US", { style: "currency", currency: "USD" }).format(value)
    : new Intl.NumberFormat("en-US").format(value);
}

function headlineValue(chart: TrendChart, days: ProjectStatusTrendDay[]): number | null {
  if (chart.headline === "latest") return days.at(-1)?.[chart.field] ?? null;
  return days.reduce((sum, day) => sum + (day[chart.field] ?? 0), 0);
}

function Chart({
  chart,
  trends,
  onDrill,
}: {
  chart: TrendChart;
  trends: ProjectStatusTrends;
  onDrill: (drilldown: StatusDrilldown) => void;
}) {
  const { days } = trends;
  const max = Math.max(0, ...days.map((day) => day[chart.field] ?? 0));
  const unit = trends.bucket === "week" ? "week of " : "";
  const summary =
    chart.headline === "latest"
      ? `${format(headlineValue(chart, days))} now`
      : `${format(headlineValue(chart, days), chart.money)} total`;
  const drill = chart.metric
    ? () => onDrill({ label: chart.title, metric: chart.metric!, range: trends.window })
    : undefined;
  return (
    <figure className="flex flex-col gap-2 rounded-lg border border-border/60 p-3">
      <figcaption className="flex items-baseline justify-between gap-2 text-sm">
        <span className="text-muted-foreground">{chart.title}</span>
        {drill ? (
          <button
            type="button"
            className="rounded-sm font-medium tabular-nums underline-offset-4 hover:underline focus-visible:ring-2 focus-visible:ring-ring focus-visible:outline-none"
            aria-label={`${chart.title}: ${summary}. Show the items behind it`}
            onClick={drill}
          >
            {summary}
          </button>
        ) : (
          <span className="font-medium tabular-nums">{summary}</span>
        )}
      </figcaption>
      {max === 0 ? (
        <p className="flex h-16 items-center justify-center rounded-md bg-muted/40 text-xs text-muted-foreground">
          Nothing in this window
        </p>
      ) : (
        <div
          className={cn(
            "flex h-16 items-end border-b border-border/60",
            days.length > 31 ? "gap-px" : "gap-1",
          )}
          role="img"
          aria-label={`${chart.title} per ${trends.bucket}: ${days
            .map((day) => `${unit}${day.day_utc} ${format(day[chart.field], chart.money)}`)
            .join(", ")}`}
          data-testid="project-trend-bars"
        >
          {days.map((day, index) => {
            const value = day[chart.field] ?? 0;
            const height = value ? Math.max(6, (value / max) * 100) : 0;
            const current = index === days.length - 1;
            return (
              <span
                key={day.day_utc}
                title={`${unit}${day.day_utc}${current ? " (so far)" : ""}: ${format(day[chart.field], chart.money)}`}
                className="flex h-full flex-1 items-end justify-center"
              >
                <span
                  className={cn("w-full max-w-3 rounded-t-sm bg-primary", current && "opacity-50")}
                  style={{ height: `${height}%` }}
                />
              </span>
            );
          })}
        </div>
      )}
      <p className="flex justify-between text-[11px] text-muted-foreground tabular-nums">
        <span>{days[0]?.day_utc}</span>
        <span>{trends.bucket === "week" ? "this week" : "today"}</span>
      </p>
    </figure>
  );
}

/** The Status section's trend charts, over the same window as its numbers. */
export function ProjectTrends({
  projectId,
  statusWindow,
  onDrill,
}: {
  projectId: string;
  statusWindow: ProjectStatusWindow;
  onDrill: (drilldown: StatusDrilldown) => void;
}) {
  const trends = useQuery({
    queryKey: projectStatusTrendsKey(projectId, statusWindow),
    queryFn: ({ signal }) => getProjectStatusTrends(projectId, statusWindow, signal),
  });
  if (!trends.data) return null;
  const charts = trendCharts(trends.data);
  return (
    <div
      className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3"
      aria-label="Trends"
      role="group"
    >
      {charts.map((chart) => (
        <Chart key={chart.field} chart={chart} trends={trends.data} onDrill={onDrill} />
      ))}
    </div>
  );
}
