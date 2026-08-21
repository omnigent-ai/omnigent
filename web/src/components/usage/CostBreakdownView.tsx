import { useEffect, useState } from "react";
import { BarChart, Bar, XAxis, YAxis, Tooltip, ResponsiveContainer, Cell } from "recharts";
import type { Payload } from "recharts/types/component/DefaultTooltipContent";
import type { SessionCostBreakdown, CategoryBreakdown } from "@/lib/usageApi";
import { fetchSessionCostBreakdown } from "@/lib/usageApi";

interface Props {
  sessionId: string;
}

interface ChartDataItem {
  name: string;
  cost: number;
  tokens: number;
  category: string;
}

// Color palette for categories
const CATEGORY_COLORS: Record<string, string> = {
  tools: "hsl(var(--chart-1))",
  shell: "hsl(var(--chart-2))",
  model: "hsl(var(--chart-3))",
  system: "hsl(var(--chart-4))",
  user: "hsl(var(--chart-5))",
  images: "hsl(var(--primary))",
};

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
  const data = payload[0].payload as ChartDataItem;
  return (
    <div className="rounded-md border border-border bg-popover px-3 py-2 text-sm shadow-md">
      <p className="font-medium">{label}</p>
      <p className="text-muted-foreground">Cost: ${data.cost.toFixed(4)}</p>
      <p className="text-muted-foreground">Tokens: {data.tokens.toLocaleString()}</p>
    </div>
  );
}

function CategorySection({
  title,
  category,
  color,
}: {
  title: string;
  category: CategoryBreakdown;
  color: string;
}) {
  if (category.total.totalTokens === 0) {
    return null;
  }

  const chartData: ChartDataItem[] = category.items.map((item) => ({
    name: item.name,
    cost: item.usage.totalCostUsd ?? 0,
    tokens: item.usage.totalTokens,
    category: title,
  }));

  // Sort by cost descending
  chartData.sort((a, b) => b.cost - a.cost);

  const height = Math.max(160, chartData.length * 36 + 60);

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="mb-3 flex items-baseline justify-between">
        <h3 className="text-sm font-medium">{title}</h3>
        <div className="text-right text-xs text-muted-foreground">
          <div>${(category.total.totalCostUsd ?? 0).toFixed(4)}</div>
          <div>{category.total.totalTokens.toLocaleString()} tokens</div>
        </div>
      </div>

      {chartData.length === 0 ? (
        <div className="flex h-32 items-center justify-center text-sm text-muted-foreground">
          No data
        </div>
      ) : (
        <div style={{ height }}>
          <ResponsiveContainer width="100%" height="100%">
            <BarChart
              data={chartData}
              layout="vertical"
              margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
            >
              <XAxis
                type="number"
                tick={{ fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                tickFormatter={(v: number) => `$${v.toFixed(3)}`}
                className="fill-muted-foreground"
              />
              <YAxis
                type="category"
                dataKey="name"
                tick={{ fontSize: 11 }}
                tickLine={false}
                axisLine={false}
                width={150}
                className="fill-muted-foreground"
              />
              <Tooltip
                content={<BreakdownTooltip />}
                cursor={{ fill: "var(--accent)", opacity: 0.3 }}
              />
              <Bar dataKey="cost" fill={color} radius={[0, 3, 3, 0]} />
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}
    </div>
  );
}

function SummaryCard({ title, value, subtitle }: { title: string; value: string; subtitle: string }) {
  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <div className="text-sm text-muted-foreground">{title}</div>
      <div className="mt-1 text-2xl font-semibold tabular-nums">{value}</div>
      <div className="mt-0.5 text-xs text-muted-foreground">{subtitle}</div>
    </div>
  );
}

function OverallBreakdown({ breakdown }: { breakdown: SessionCostBreakdown }) {
  const categories = [
    { name: "Tools", data: breakdown.tools, color: CATEGORY_COLORS.tools },
    { name: "Shell", data: breakdown.shell, color: CATEGORY_COLORS.shell },
    { name: "Model Output", data: breakdown.model, color: CATEGORY_COLORS.model },
    { name: "System", data: breakdown.system, color: CATEGORY_COLORS.system },
    { name: "User Input", data: breakdown.user, color: CATEGORY_COLORS.user },
    { name: "Images", data: breakdown.images, color: CATEGORY_COLORS.images },
  ];

  const chartData = categories
    .map((cat) => ({
      name: cat.name,
      cost: cat.data.total.totalCostUsd ?? 0,
      tokens: cat.data.total.totalTokens,
      category: cat.name,
    }))
    .filter((item) => item.tokens > 0)
    .sort((a, b) => b.cost - a.cost);

  return (
    <div className="rounded-lg border border-border bg-card p-4">
      <h3 className="mb-3 text-sm font-medium">Overall Breakdown by Category</h3>
      <div style={{ height: Math.max(300, chartData.length * 40 + 60) }}>
        <ResponsiveContainer width="100%" height="100%">
          <BarChart
            data={chartData}
            layout="vertical"
            margin={{ top: 4, right: 8, bottom: 0, left: 0 }}
          >
            <XAxis
              type="number"
              tick={{ fontSize: 11 }}
              tickLine={false}
              axisLine={false}
              tickFormatter={(v: number) => `$${v.toFixed(3)}`}
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
            <Bar dataKey="cost" radius={[0, 3, 3, 0]}>
              {chartData.map((entry, index) => {
                const color =
                  categories.find((c) => c.name === entry.name)?.color || "var(--primary)";
                return <Cell key={`cell-${index}`} fill={color} />;
              })}
            </Bar>
          </BarChart>
        </ResponsiveContainer>
      </div>
    </div>
  );
}

export function CostBreakdownView({ sessionId }: Props) {
  const [breakdown, setBreakdown] = useState<SessionCostBreakdown | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;

    async function load() {
      try {
        setLoading(true);
        setError(null);
        const data = await fetchSessionCostBreakdown(sessionId);
        if (!cancelled) {
          setBreakdown(data);
        }
      } catch (err) {
        if (!cancelled) {
          setError(err instanceof Error ? err.message : "Failed to load cost breakdown");
        }
      } finally {
        if (!cancelled) {
          setLoading(false);
        }
      }
    }

    load();

    return () => {
      cancelled = true;
    };
  }, [sessionId]);

  if (loading) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="text-sm text-muted-foreground">Loading cost breakdown...</div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="flex h-96 items-center justify-center">
        <div className="text-sm text-destructive">{error}</div>
      </div>
    );
  }

  if (!breakdown) {
    return null;
  }

  return (
    <div className="space-y-6">
      {/* Summary cards */}
      <div className="grid gap-4 md:grid-cols-3">
        <SummaryCard
          title="Total Cost"
          value={`$${breakdown.totalCostUsd.toFixed(4)}`}
          subtitle="Session total"
        />
        <SummaryCard
          title="Total Tokens"
          value={breakdown.totalTokens.toLocaleString()}
          subtitle="All categories"
        />
        <SummaryCard
          title="Avg Cost/Token"
          value={`$${(breakdown.totalCostUsd / breakdown.totalTokens || 0).toFixed(6)}`}
          subtitle="Per token"
        />
      </div>

      {/* Overall breakdown */}
      <OverallBreakdown breakdown={breakdown} />

      {/* Detailed breakdowns */}
      <div className="space-y-4">
        <h2 className="text-lg font-semibold">Detailed Breakdown</h2>
        <div className="grid gap-4 md:grid-cols-2">
          <CategorySection title="Tools" category={breakdown.tools} color={CATEGORY_COLORS.tools} />
          <CategorySection title="Shell Commands" category={breakdown.shell} color={CATEGORY_COLORS.shell} />
          <CategorySection title="Model Output" category={breakdown.model} color={CATEGORY_COLORS.model} />
          <CategorySection title="System Overhead" category={breakdown.system} color={CATEGORY_COLORS.system} />
          <CategorySection title="User Input" category={breakdown.user} color={CATEGORY_COLORS.user} />
          <CategorySection title="Images & Attachments" category={breakdown.images} color={CATEGORY_COLORS.images} />
        </div>
      </div>

      {/* Footer note */}
      <div className="rounded-lg border border-border bg-muted/30 p-4 text-xs text-muted-foreground">
        <p>
          This is a simplified cost breakdown that estimates token usage by category. Token counts
          are approximated from text length and may not match exact LLM tokenization. Costs are
          computed using the session&apos;s model pricing.
        </p>
      </div>
    </div>
  );
}
