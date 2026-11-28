/** An automation's runs per day, stacked by outcome, above its run list. */

import type { ReactElement } from "react";
import { Bar, BarChart, CartesianGrid, ResponsiveContainer, Tooltip, XAxis, YAxis } from "recharts";
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select";
import {
  formatRunCount,
  formatRunHistoryDay,
  RUN_HISTORY_RANGES,
  RUN_OUTCOME_LABELS,
  runHistoryDayTotal,
  summarizeRunHistory,
} from "@/lib/runHistory";
import {
  RUN_OUTCOMES,
  type ScheduledTaskRunOutcome,
  type ScheduledTaskRunStats,
  type ScheduledTaskRunStatsDay,
} from "@/lib/scheduledTasksApi";
import { cn } from "@/lib/utils";

// Failed keeps the light theme's red in both themes: the dark theme's lighter
// red is too close to the succeeded green for red-green colorblind readers.
const OUTCOME_COLORS: Record<ScheduledTaskRunOutcome, string> = {
  succeeded: "var(--chart-3)",
  failed: "#c8324c",
  skipped: "var(--chart-5)",
  inProgress: "var(--chart-1)",
};

const AXIS_TICK = { fontSize: 11 };
const COLUMN_CORNER_RADIUS = 4;
// Space left between a column's outcomes, so neighbours don't rely on color alone.
const SEGMENT_GAP = 2;

/** A day's counts with its axis label. */
type ChartDay = ScheduledTaskRunStatsDay & { label: string };

interface RunHistoryChartProps {
  /** The counts to draw; `undefined` until they first load. */
  stats: ScheduledTaskRunStats | undefined;
  /** The last request for the counts failed; `stats` then holds those from before it, if any. */
  isError: boolean;
  /** `stats` is an earlier range's, shown while the chosen range loads. */
  isStale: boolean;
  /** The chosen range, in days ending today. */
  days: number;
  onDaysChange: (days: number) => void;
}

export function RunHistoryChart({
  stats,
  isError,
  isStale,
  days,
  onDaysChange,
}: RunHistoryChartProps) {
  const range =
    RUN_HISTORY_RANGES.find((option) => option.days === days)?.label ?? `Last ${days} days`;
  const counted = stats ? summarizeRunHistory(stats.days) : null;
  // An empty earlier range says nothing about the range still loading.
  const summary = counted !== null && !(isStale && counted.total === 0) ? counted : null;
  // Counts that aren't the chosen range's latest: it is still loading, or its refresh failed.
  const outdated = summary !== null && (isStale || isError);

  return (
    <section
      aria-label="Run history"
      aria-busy={summary === null ? !isError : isStale}
      data-testid="run-history-chart"
      className="mb-4 rounded-lg border border-border p-4"
    >
      <div className="flex flex-wrap items-center justify-between gap-2">
        <p
          className={cn(
            "text-sm text-muted-foreground transition-opacity",
            outdated && "opacity-50",
          )}
          data-testid="run-history-summary"
        >
          {summary === null ? (
            isError ? (
              "Couldn’t load the run chart."
            ) : (
              "Loading run history…"
            )
          ) : summary.total === 0 ? (
            `No runs in the ${range.toLowerCase()}.`
          ) : (
            <>
              <span className="font-medium text-foreground">{formatRunCount(summary.total)}</span>
              {summary.successRate !== null && ` · ${summary.successRate}% success rate`}
            </>
          )}
        </p>
        <Select
          value={String(days)}
          onValueChange={(value) => onDaysChange(Number(value))}
          componentId="tasks.scheduled.run_history_range"
          valueHasNoPii
        >
          <SelectTrigger size="sm" aria-label="Run history range" data-testid="run-history-range">
            <SelectValue />
          </SelectTrigger>
          <SelectContent align="end" position="popper">
            {RUN_HISTORY_RANGES.map((option) => (
              <SelectItem
                key={option.days}
                value={String(option.days)}
                data-testid={`run-history-range-${option.days}`}
              >
                {option.label}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      </div>
      {summary !== null && isError && (
        <p
          className="mt-1 text-xs text-muted-foreground"
          role="status"
          data-testid="run-history-refresh-error"
        >
          Couldn’t refresh the run chart. These counts may be out of date.
        </p>
      )}
      {stats?.truncated && summary && (
        <p
          className={cn(
            "mt-1 text-xs text-muted-foreground transition-opacity",
            outdated && "opacity-50",
          )}
          data-testid="run-history-truncated"
        >
          Only the most recent {formatRunCount(summary.total)} in this range are counted.
        </p>
      )}
      {stats && summary && summary.total > 0 && (
        // Outdated counts stay in place, dimmed, until the chosen range's arrive.
        <div className={cn("transition-opacity", outdated && "opacity-50")}>
          <div className="mt-3 h-44">
            <ResponsiveContainer width="100%" height="100%">
              <BarChart
                data={stats.days.map((day) => ({ ...day, label: formatRunHistoryDay(day.date) }))}
                margin={{ top: 8, right: 8, bottom: 0, left: 0 }}
              >
                <CartesianGrid stroke="var(--border)" vertical={false} />
                <XAxis
                  dataKey="label"
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  interval="preserveStartEnd"
                  minTickGap={24}
                  className="fill-muted-foreground"
                />
                <YAxis
                  allowDecimals={false}
                  tick={AXIS_TICK}
                  tickLine={false}
                  axisLine={false}
                  width={32}
                  className="fill-muted-foreground"
                />
                <Tooltip
                  content={<DayTooltip />}
                  cursor={{ fill: "var(--accent)", opacity: 0.3 }}
                />
                {RUN_OUTCOMES.map((outcome) => (
                  <Bar
                    key={outcome}
                    dataKey={outcome}
                    name={RUN_OUTCOME_LABELS[outcome]}
                    stackId="runs"
                    fill={OUTCOME_COLORS[outcome]}
                    maxBarSize={24}
                    isAnimationActive={false}
                    shape={SEGMENT_SHAPES[outcome]}
                  />
                ))}
              </BarChart>
            </ResponsiveContainer>
          </div>
          <ul
            className="mt-2 flex flex-wrap justify-center gap-x-4 gap-y-1 text-xs text-muted-foreground"
            data-testid="run-history-legend"
          >
            {RUN_OUTCOMES.map((outcome) => (
              <li key={outcome} className="flex items-center gap-1.5">
                <span
                  aria-hidden="true"
                  className="size-2 rounded-[2px]"
                  style={{ background: OUTCOME_COLORS[outcome] }}
                />
                {RUN_OUTCOME_LABELS[outcome]}
                <span className="text-foreground tabular-nums">
                  {summary.counts[outcome].toLocaleString()}
                </span>
              </li>
            ))}
          </ul>
          <DayTable days={stats.days} range={range} />
        </div>
      )}
    </section>
  );
}

/** What the chart passes a column segment's shape. */
interface SegmentProps {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
  fill?: string;
  /** The day the segment belongs to. */
  payload?: ChartDay;
}

/**
 * One outcome's part of a day's column. The top part gets the column's rounded
 * corners, and every other part stops short of the one above it.
 */
function ColumnSegment({
  x = 0,
  y = 0,
  width = 0,
  height = 0,
  fill,
  payload,
  outcome,
}: SegmentProps & { outcome: ScheduledTaskRunOutcome }) {
  if (width <= 0 || height <= 0) return null;
  const above = RUN_OUTCOMES.slice(RUN_OUTCOMES.indexOf(outcome) + 1);
  if (above.some((later) => (payload?.[later] ?? 0) > 0)) {
    // A part too short to give up the gap is drawn whole rather than dropped.
    const gap = height > SEGMENT_GAP ? SEGMENT_GAP : 0;
    return <rect x={x} y={y + gap} width={width} height={height - gap} fill={fill} />;
  }
  const radius = Math.min(COLUMN_CORNER_RADIUS, width / 2, height);
  const right = x + width;
  const bottom = y + height;
  return (
    <path
      fill={fill}
      d={
        `M${x},${bottom} L${x},${y + radius} Q${x},${y} ${x + radius},${y} ` +
        `L${right - radius},${y} Q${right},${y} ${right},${y + radius} L${right},${bottom} Z`
      }
    />
  );
}

// One shape per outcome, made once so the chart keeps the same one across renders.
const SEGMENT_SHAPES = Object.fromEntries(
  RUN_OUTCOMES.map((outcome) => [
    outcome,
    (segment: SegmentProps) => <ColumnSegment {...segment} outcome={outcome} />,
  ]),
) as Record<ScheduledTaskRunOutcome, (segment: SegmentProps) => ReactElement>;

/** The hovered or focused day: its date, then each outcome that had runs. */
function DayTooltip({
  active,
  payload,
}: {
  active?: boolean;
  payload?: readonly { payload?: ChartDay }[];
}) {
  const day = payload?.[0]?.payload;
  if (!active || !day) return null;
  return (
    // The app's menu surface: dark themes' popover color is translucent without its blur.
    <div className="menu-surface rounded-lg bg-popover px-3 py-2 text-sm text-popover-foreground shadow-md">
      <p className="text-muted-foreground">{formatRunHistoryDay(day.date, { weekday: true })}</p>
      {runHistoryDayTotal(day) === 0 ? (
        <p>No runs</p>
      ) : (
        RUN_OUTCOMES.filter((outcome) => day[outcome] > 0).map((outcome) => (
          <p key={outcome} className="flex items-center gap-2">
            <span
              aria-hidden="true"
              className="h-0.5 w-3 rounded-full"
              style={{ background: OUTCOME_COLORS[outcome] }}
            />
            <span className="font-medium tabular-nums">{day[outcome].toLocaleString()}</span>
            <span className="text-muted-foreground">
              {RUN_OUTCOME_LABELS[outcome].toLowerCase()}
            </span>
          </p>
        ))
      )}
    </div>
  );
}

/** The chart's numbers for screen readers: one row per day that had runs. */
function DayTable({ days, range }: { days: readonly ScheduledTaskRunStatsDay[]; range: string }) {
  return (
    <table className="sr-only" data-testid="run-history-table">
      <caption>Runs per day, {range.toLowerCase()}</caption>
      <thead>
        <tr>
          <th scope="col">Day</th>
          {RUN_OUTCOMES.map((outcome) => (
            <th key={outcome} scope="col">
              {RUN_OUTCOME_LABELS[outcome]}
            </th>
          ))}
        </tr>
      </thead>
      <tbody>
        {days
          .filter((day) => runHistoryDayTotal(day) > 0)
          .map((day) => (
            <tr key={day.date}>
              <th scope="row">{formatRunHistoryDay(day.date, { weekday: true })}</th>
              {RUN_OUTCOMES.map((outcome) => (
                <td key={outcome}>{day[outcome]}</td>
              ))}
            </tr>
          ))}
      </tbody>
    </table>
  );
}
