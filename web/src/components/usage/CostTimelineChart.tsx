import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import type { Payload } from "recharts/types/component/DefaultTooltipContent";
import type { DailyCost } from "@/lib/usageApi";

interface Props {
  dailyCosts: DailyCost[];
  now?: Date;
  animate?: boolean;
}

function fillGaps(costs: DailyCost[], now: Date): { label: string; cost: number }[] {
  if (costs.length === 0) return [];
  const map = new Map(costs.map((d) => [d.day, d.costUsd]));
  const start = new Date(costs[0].day + "T00:00:00Z");
  const end = new Date(costs[costs.length - 1].day + "T00:00:00Z");
  const today = new Date(now);
  today.setUTCHours(0, 0, 0, 0);
  const last = end > today ? end : today;
  const result: { label: string; cost: number }[] = [];
  const lastTs = last.getTime();
  for (
    let cursor = new Date(start);
    cursor.getTime() <= lastTs;
    cursor.setUTCDate(cursor.getUTCDate() + 1)
  ) {
    const iso = cursor.toISOString().slice(0, 10);
    const label = `${cursor.toLocaleString(undefined, { month: "short", timeZone: "UTC" })} ${cursor.getUTCDate()}`;
    result.push({ label, cost: map.get(iso) ?? 0 });
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
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-1.5 text-sm shadow-md">
      <p className="text-muted-foreground">{label}</p>
      <p className="font-medium tabular-nums">${payload[0].value?.toFixed(2)}</p>
    </div>
  );
}

export function CostTimelineChart({ dailyCosts, now, animate = true }: Props) {
  const data = fillGaps(dailyCosts, now ?? new Date());
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
          <Bar
            dataKey="cost"
            fill="var(--primary)"
            radius={[3, 3, 0, 0]}
            isAnimationActive={animate}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}
