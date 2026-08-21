import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import type { Payload } from "recharts/types/component/DefaultTooltipContent";
import type { DailyCost } from "@/lib/usageApi";

interface Props {
  dailyCosts: DailyCost[];
  forecastCosts?: DailyCost[];
}

function fillGaps(
  costs: DailyCost[],
  forecastCosts?: DailyCost[],
): { label: string; cost: number; isForecast: boolean }[] {
  if (costs.length === 0 && (!forecastCosts || forecastCosts.length === 0)) return [];

  const map = new Map(costs.map((d) => [d.day, d.costUsd]));
  const forecastMap = new Map((forecastCosts ?? []).map((d) => [d.day, d.costUsd]));

  const allDays = [...costs.map((d) => d.day), ...(forecastCosts ?? []).map((d) => d.day)];
  if (allDays.length === 0) return [];

  allDays.sort();
  const start = new Date(allDays[0] + "T00:00:00Z");
  const end = new Date(allDays[allDays.length - 1] + "T00:00:00Z");
  const today = new Date();
  today.setUTCHours(0, 0, 0, 0);
  const last = end > today ? end : today;

  const result: { label: string; cost: number; isForecast: boolean }[] = [];
  const lastTs = last.getTime();
  for (
    let cursor = new Date(start);
    cursor.getTime() <= lastTs;
    cursor.setUTCDate(cursor.getUTCDate() + 1)
  ) {
    const iso = cursor.toISOString().slice(0, 10);
    const label = `${cursor.toLocaleString(undefined, { month: "short", timeZone: "UTC" })} ${cursor.getUTCDate()}`;
    const actual = map.get(iso);
    const forecast = forecastMap.get(iso);
    if (actual !== undefined) {
      result.push({ label, cost: actual, isForecast: false });
    } else if (forecast !== undefined) {
      result.push({ label, cost: forecast, isForecast: true });
    } else {
      result.push({ label, cost: 0, isForecast: false });
    }
  }
  return result;
}

function CustomTooltip({
  active,
  payload,
  label,
}: {
  active?: boolean;
  payload?: readonly Payload<number, string>[];
  label?: string;
}) {
  if (!active || !payload?.length) return null;
  const entry = payload[0].payload as { isForecast: boolean };
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-1.5 text-sm shadow-md">
      <p className="text-muted-foreground">
        {label}
        {entry?.isForecast && " (projected)"}
      </p>
      <p className="font-medium tabular-nums">${payload[0].value?.toFixed(2)}</p>
    </div>
  );
}

export function CostTimelineChart({ dailyCosts, forecastCosts }: Props) {
  const data = fillGaps(dailyCosts, forecastCosts);
  if (data.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        No cost data yet
      </div>
    );
  }
  return (
    <div className="h-64">
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} margin={{ top: 8, right: 8, bottom: 0, left: 0 }}>
          <XAxis
            dataKey="label"
            tick={{ fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            interval="preserveStartEnd"
            className="fill-muted-foreground"
          />
          <YAxis
            tick={{ fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v: number) => `$${v}`}
            width={50}
            className="fill-muted-foreground"
          />
          <Tooltip content={<CustomTooltip />} cursor={{ fill: "var(--accent)", opacity: 0.3 }} />
          <Bar dataKey="cost" radius={[3, 3, 0, 0]}>
            {data.map((entry) => (
              <Cell
                key={entry.label}
                fill={entry.isForecast ? "var(--muted-foreground)" : "var(--primary)"}
                opacity={entry.isForecast ? 0.4 : 1}
              />
            ))}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
