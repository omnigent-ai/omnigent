import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer } from "recharts";
import type { Payload } from "recharts/types/component/DefaultTooltipContent";

interface Props {
  harnessBreakdown: Record<string, number>;
  modelBreakdown: Record<string, number>;
  animate?: boolean;
}

function breakdownToChartData(breakdown: Record<string, number>): { name: string; cost: number }[] {
  return Object.entries(breakdown)
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

function HorizontalBarChart({
  data,
  animate,
}: {
  data: { name: string; cost: number }[];
  animate: boolean;
}) {
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
          <Bar
            dataKey="cost"
            fill="var(--primary)"
            radius={[0, 3, 3, 0]}
            isAnimationActive={animate}
          />
        </BarChart>
      </ResponsiveContainer>
    </div>
  );
}

export function UsageBreakdownCharts({ harnessBreakdown, modelBreakdown, animate = true }: Props) {
  const byHarness = breakdownToChartData(harnessBreakdown);
  const byModel = breakdownToChartData(modelBreakdown);

  return (
    <div className="grid gap-6 md:grid-cols-2">
      <div>
        <h3 className="mb-2 text-sm font-medium text-muted-foreground">Cost by harness</h3>
        <HorizontalBarChart data={byHarness} animate={animate} />
      </div>
      <div>
        <h3 className="mb-2 text-sm font-medium text-muted-foreground">Cost by model</h3>
        <HorizontalBarChart data={byModel} animate={animate} />
      </div>
    </div>
  );
}
