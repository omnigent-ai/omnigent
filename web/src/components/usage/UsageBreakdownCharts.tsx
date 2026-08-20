import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import type { Payload } from "recharts/types/component/DefaultTooltipContent";
import type { SessionUsage } from "@/lib/usageApi";

interface Props {
  sessions: SessionUsage[];
}

function aggregateByKey(
  sessions: SessionUsage[],
  keyFn: (s: SessionUsage) => string | null,
): { name: string; cost: number }[] {
  const map = new Map<string, number>();
  for (const s of sessions) {
    const key = keyFn(s) ?? "Unknown";
    map.set(key, (map.get(key) ?? 0) + s.costUsd);
  }
  return Array.from(map.entries())
    .map(([name, cost]) => ({ name, cost }))
    .sort((a, b) => b.cost - a.cost);
}

function aggregateByModel(sessions: SessionUsage[]): { name: string; cost: number }[] {
  const map = new Map<string, number>();
  for (const s of sessions) {
    for (const [model, cost] of Object.entries(s.models)) {
      map.set(model, (map.get(model) ?? 0) + cost);
    }
  }
  return Array.from(map.entries())
    .map(([name, cost]) => ({ name, cost }))
    .sort((a, b) => b.cost - a.cost);
}

function BreakdownTooltip({
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
      <p className="truncate text-muted-foreground">{label}</p>
      <p className="font-medium tabular-nums">${payload[0].value?.toFixed(2)}</p>
    </div>
  );
}

function HorizontalBarChart({ data }: { data: { name: string; cost: number }[] }) {
  if (data.length === 0) {
    return (
      <div className="flex h-48 items-center justify-center text-sm text-muted-foreground">
        No data
      </div>
    );
  }
  const height = Math.max(160, data.length * 36 + 40);
  return (
    <div style={{ height }}>
      <ResponsiveContainer width="100%" height="100%">
        <BarChart data={data} layout="vertical" margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
          <XAxis
            type="number"
            tick={{ fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            tickFormatter={(v: number) => `$${v}`}
            className="fill-muted-foreground"
          />
          <YAxis
            type="category"
            dataKey="name"
            tick={{ fontSize: 11 }}
            tickLine={false}
            axisLine={false}
            width={120}
            className="fill-muted-foreground"
          />
          <Tooltip
            content={<BreakdownTooltip />}
            cursor={{ fill: "var(--accent)", opacity: 0.3 }}
          />
          <Bar dataKey="cost" fill="var(--primary)" radius={[0, 3, 3, 0]} />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function UsageBreakdownCharts({ sessions }: Props) {
  const byHarness = aggregateByKey(sessions, (s) => s.harness);
  const byModel = aggregateByModel(sessions);

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div>
        <h3 className="mb-2 text-sm font-medium text-muted-foreground">Cost by harness</h3>
        <HorizontalBarChart data={byHarness} />
      </div>
      <div>
        <h3 className="mb-2 text-sm font-medium text-muted-foreground">Cost by model</h3>
        <HorizontalBarChart data={byModel} />
      </div>
    </div>
  );
}
