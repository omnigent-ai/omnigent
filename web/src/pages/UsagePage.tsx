import { useMemo, useState } from "react";
import { AlertTriangleIcon, CalendarIcon, Loader2Icon, TriangleAlertIcon } from "lucide-react";
import { useOmnigentAnalytics } from "@/lib/analytics";
import { PageScroll } from "@/components/PageScroll";
import { CostTimelineChart } from "@/components/usage/CostTimelineChart";
import { UsageBreakdownCharts } from "@/components/usage/UsageBreakdownCharts";
import { UsageSessionTable } from "@/components/usage/UsageSessionTable";
import { UsageForecastCard } from "@/components/usage/UsageForecastCard";
import { CostAlertSettings } from "@/components/usage/CostAlertSettings";
import { useUsageReport } from "@/hooks/useUsageReport";
import { formatSessionCostUsd } from "@/lib/formatCost";
import { cn } from "@/lib/utils";

type RangeKey = "7d" | "30d" | "90d" | "all" | "custom";

const RANGE_PRESETS: { key: RangeKey; label: string }[] = [
  { key: "7d", label: "7 days" },
  { key: "30d", label: "30 days" },
  { key: "90d", label: "90 days" },
  { key: "all", label: "All time" },
  { key: "custom", label: "Custom" },
];

function daysAgoIso(days: number): string {
  const d = new Date();
  d.setUTCDate(d.getUTCDate() - days + 1);
  return d.toISOString().slice(0, 10);
}

function todayIso(): string {
  return new Date().toISOString().slice(0, 10);
}

function rangeToWindow(
  key: RangeKey,
  customStart: string,
  customEnd: string,
): { since: string | null; until: string | null } {
  switch (key) {
    case "7d":
      return { since: daysAgoIso(7), until: null };
    case "30d":
      return { since: daysAgoIso(30), until: null };
    case "90d":
      return { since: daysAgoIso(90), until: null };
    case "all":
      return { since: null, until: null };
    case "custom":
      return { since: customStart || null, until: customEnd || null };
  }
}

export function UsagePage() {
  const { trackClick } = useOmnigentAnalytics();
  const [rangeKey, setRangeKey] = useState<RangeKey>("30d");
  const [customStart, setCustomStart] = useState(() => daysAgoIso(30));
  const [customEnd, setCustomEnd] = useState(todayIso);

  const today = todayIso();
  const { since, until } = rangeToWindow(rangeKey, customStart, customEnd);

  const { data, isLoading, isError, refetch } = useUsageReport({ since, until });

  const totalCost = useMemo(() => {
    if (!data) return 0;
    const fromDaily = data.dailyCosts.reduce((sum, d) => sum + d.costUsd, 0);
    if (fromDaily > 0) return fromDaily;
    return data.sessions.reduce((sum, s) => sum + s.costUsd, 0);
  }, [data]);

  return (
    <PageScroll>
      <div className="mx-auto w-full max-w-4xl px-4 py-8">
        <div className="mb-6 flex flex-wrap items-center justify-between gap-3">
          <h1 className="text-lg font-semibold">Usage</h1>
          <div className="flex items-center gap-1.5">
            {RANGE_PRESETS.map(({ key, label }) => (
              <button
                type="button"
                key={key}
                className={cn(
                  "rounded-md px-2.5 py-1 text-xs font-medium transition-colors",
                  rangeKey === key
                    ? "bg-primary text-primary-foreground"
                    : "bg-muted text-muted-foreground hover:bg-muted/80",
                )}
                onClick={() => {
                  trackClick(`usage.range_${key}`, "button");
                  setRangeKey(key);
                }}
              >
                {label}
              </button>
            ))}
          </div>
        </div>

        {rangeKey === "custom" && (
          <div className="mb-6 flex items-center gap-2 text-sm">
            <CalendarIcon className="h-4 w-4 text-muted-foreground" />
            <input
              type="date"
              value={customStart}
              max={customEnd < today ? customEnd : today}
              onChange={(e) => {
                const v = e.target.value;
                setCustomStart(v);
                if (v > customEnd) setCustomEnd(v);
              }}
              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            />
            <span className="text-muted-foreground">to</span>
            <input
              type="date"
              value={customEnd}
              min={customStart}
              max={today}
              onChange={(e) => {
                const v = e.target.value;
                setCustomEnd(v);
                if (v < customStart) setCustomStart(v);
              }}
              className="rounded-md border border-border bg-background px-2 py-1 text-sm"
            />
          </div>
        )}

        {isLoading && (
          <div className="flex h-64 items-center justify-center">
            <Loader2Icon className="h-5 w-5 animate-spin text-muted-foreground" />
          </div>
        )}

        {isError && (
          <div className="flex h-64 flex-col items-center justify-center gap-2 text-sm text-muted-foreground">
            <TriangleAlertIcon className="h-5 w-5" />
            <p>Failed to load usage data</p>
            <button
              type="button"
              className="text-primary underline"
              onClick={() => {
                trackClick("usage.retry", "button");
                void refetch();
              }}
            >
              Retry
            </button>
          </div>
        )}

        {data && (
          <div className="flex flex-col gap-8">
            {data.alertTriggered && (
              <div className="flex items-center gap-2 rounded-lg border border-destructive/30 bg-destructive/5 px-4 py-3 text-sm text-destructive">
                <AlertTriangleIcon className="h-4 w-4 shrink-0" />
                <p>A cost alert threshold has been exceeded</p>
              </div>
            )}

            <div className="grid gap-4 sm:grid-cols-2">
              <div className="rounded-lg border border-border bg-card p-4">
                <p className="text-xs text-muted-foreground">Total cost (selected range)</p>
                <p className="mt-1 text-2xl font-semibold tabular-nums">
                  {formatSessionCostUsd(totalCost)}
                </p>
                <p className="mt-0.5 text-xs text-muted-foreground">
                  {data.sessions.length} session{data.sessions.length !== 1 && "s"}
                </p>
              </div>

              {data.forecast && <UsageForecastCard forecast={data.forecast} />}
            </div>

            <section>
              <h2 className="mb-3 text-sm font-medium text-muted-foreground">Daily cost</h2>
              <CostTimelineChart
                dailyCosts={data.dailyCosts}
                forecastCosts={data.forecast?.projectedDaily}
              />
            </section>

            <UsageBreakdownCharts sessions={data.sessions} projects={data.projects} />

            <section>
              <CostAlertSettings alerts={data.alerts} alertTriggered={data.alertTriggered} />
            </section>

            <section>
              <h2 className="mb-3 text-sm font-medium text-muted-foreground">Sessions</h2>
              <UsageSessionTable sessions={data.sessions} />
            </section>
          </div>
        )}
      </div>
    </PageScroll>
  );
}
